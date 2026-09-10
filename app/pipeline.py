"""The conversion pipeline: a recording in, a Clavinova-ready MIDI file out.

Modes
  piano    Solo piano recording. transkun on the whole recording.
  arrange  Piano version of any song. Drums removed with demucs, then transkun.
  band     Separate instruments. demucs stems: vocals become the melody,
           "other" becomes chords, bass and drums get their own parts.

Every setting here was chosen by scoring against songs with known notes
(see tests/). Every file is read back and checked before it is kept.
"""
import gc
import json
import os
import shutil
import subprocess
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import soundfile as sf

import drums as drumkit
import midi_export as mx
import postproc
import sources
import usb

SR = 44100
MAX_SECONDS = sources.MAX_SONG_SECONDS
FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
FLUIDSYNTH = shutil.which("fluidsynth") or "/opt/homebrew/bin/fluidsynth"
APPLE_GM = "/System/Library/Components/CoreAudio.component/Contents/Resources/gs_instruments.dls"
FALLBACK_SF = "/opt/homebrew/share/fluid-synth/sf2/VintageDreamsWaves-v2.sf2"

MODES = {
    "arrange": "Piano version of any song",
    "piano": "Solo piano recording",
    "band": "Full band (separate instruments)",
}
MELODY_PROGRAMS = {0: "Piano", 73: "Flute", 65: "Alto Sax", 40: "Violin", 52: "Choir", 11: "Vibraphone", 80: "Synth Lead"}
HAND_SPLIT = 60                   # middle C: below goes to the left-hand part
STEM_GATE = 0.05                  # a stem this much quieter than the song is treated as absent
# Piano version: the piano model ignores singing, so the sung melody is transcribed separately and
# added to the right hand. On real songs this lifted the pitch match from 0.65 to 0.79 (Someone You
# Loved) and 0.53 to 0.65 (Wonderwall), and left songs without vocals unchanged. Also feeding the
# vocals to the piano model made Wonderwall worse (0.54), so they stay out of it.
KEEP_VOCALS_IN_PIANO = False

# Seconds of work per second of audio on an M1, measured; used for progress and ETA.
SPEED = {"demucs": 0.55, "transkun": 0.5, "basic_pitch": 0.06, "drums": 0.05}


class Cancelled(Exception):
    pass


class UserError(Exception):
    """A problem to show the user in plain words."""


@dataclass
class Job:
    title: str
    mode: str
    source: str                           # "youtube" or "upload"
    video_id: str = None
    upload_path: str = None
    duration_hint: float = None
    melody_program: int = 73
    split_hands: bool = True
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    status: str = "queued"                # queued, running, done, error, cancelled
    stage: str = "Waiting"
    progress: float = 0.0
    eta: float = None
    error: str = None
    result: dict = None
    cancelled: bool = False
    created: float = field(default_factory=time.time)

    def public(self):
        return {k: getattr(self, k) for k in ("id", "title", "mode", "source", "status", "stage", "progress",
                                               "eta", "error", "result", "created", "melody_program", "split_hands")}


class Models:
    """Loaded once and kept, so only the first song pays the load time."""

    def __init__(self):
        self.lock = threading.Lock()
        self._tk = self._dm = self._bp = None
        self.ready = False
        self.error = None

    def transkun(self):
        with self.lock:
            if self._tk is None:
                import moduleconf
                import torch
                import transkun
                d = os.path.dirname(transkun.__file__)
                cm = moduleconf.parseFromFile(os.path.join(d, "pretrained", "2.0.conf"))
                model = cm["Model"].module.TransKun(conf=cm["Model"].config)
                ck = torch.load(os.path.join(d, "pretrained", "2.0.pt"), map_location="cpu", weights_only=False)
                res = model.load_state_dict(ck["best_state_dict"] if "best_state_dict" in ck else ck["state_dict"], strict=False)
                if res.missing_keys:
                    raise RuntimeError(f"piano model is incomplete ({len(res.missing_keys)} missing weights)")
                self._tk = model.eval()
            return self._tk

    def demucs(self):
        with self.lock:
            if self._dm is None:
                from demucs.pretrained import get_model
                self._dm = get_model("htdemucs").eval()
            return self._dm

    def basic_pitch(self):
        with self.lock:
            if self._bp is None:
                from basic_pitch import ICASSP_2022_MODEL_PATH
                from basic_pitch.inference import Model
                self._bp = Model(ICASSP_2022_MODEL_PATH)
            return self._bp

    def warm_up(self):
        try:
            self.transkun()
            self.demucs()
            self.basic_pitch()
            self.ready = True
        except Exception as e:  # noqa: BLE001
            self.error = f"{type(e).__name__}: {e}"


MODELS = Models()


class Progress:
    """Weighted stages. Stages without their own progress creep forward on a timer."""

    def __init__(self, job, stages, on_update):
        self.job, self.on_update = job, on_update
        self.stages = stages                                  # list of (key, label, expected_seconds)
        self.total = sum(s[2] for s in stages) or 1.0
        self.done_weight = 0.0
        self.cur = None
        self.frac = 0.0
        self.t0 = time.time()
        self.stage_t0 = time.time()
        self._stop = threading.Event()
        self._tick = threading.Thread(target=self._ticker, daemon=True)
        self._tick.start()

    def _ticker(self):
        while not self._stop.wait(0.5):
            if self.cur and self.cur[3]:                      # timer-driven stage
                est = max(self.cur[2], 0.1)
                self.frac = max(self.frac, min(0.95, (time.time() - self.stage_t0) / est))
            self._push()

    def _push(self):
        if not self.cur:
            return
        p = (self.done_weight + self.frac * self.cur[2]) / self.total
        # Re-planning with the real song length can shift the weights; the bar must never go backwards.
        p = max(self.job.progress, min(0.99, p))
        self.job.progress = round(p, 3)
        elapsed = time.time() - self.t0
        remaining_est = self.total * (1 - p)
        speed = (elapsed / (p * self.total)) if p > 0.05 else 1.0
        self.job.eta = round(max(0.0, remaining_est * min(3.0, max(0.5, speed))))
        self.on_update(self.job)

    def start(self, key, timer=True):
        if self.job.cancelled:
            raise Cancelled()
        if self.cur:
            self.done_weight += self.cur[2]
        s = next(s for s in self.stages if s[0] == key)
        self.cur = (s[0], s[1], s[2], timer)
        self.frac = 0.0
        self.stage_t0 = time.time()
        self.job.stage = s[1]
        self._push()

    def set(self, frac):
        self.frac = max(self.frac, min(1.0, frac))
        self._push()

    def stop(self):
        self._stop.set()


def _rms(x):
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64)))) if x.size else 0.0


def decode_audio(src, dst):
    cmd = [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(src),
           "-vn", "-sn", "-dn", "-ac", "2", "-ar", str(SR), "-t", str(MAX_SECONDS), "-c:a", "pcm_f32le", str(dst)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        raise UserError("Reading that file took too long. Is it a normal audio file?")
    if r.returncode != 0 or not Path(dst).exists() or Path(dst).stat().st_size < 4096:
        raise UserError("That file could not be read as audio. Try an MP3, M4A, WAV or FLAC file.")
    x, sr = sf.read(dst, dtype="float32", always_2d=True)
    x = np.nan_to_num(x)
    if len(x) < 2 * SR:
        raise UserError("That audio is shorter than 2 seconds.")
    peak, rms = float(np.abs(x).max()), _rms(x)
    if peak < 1e-4 or rms < 1e-5:
        raise UserError("That audio is silent.")
    gain = min(0.95 / peak, 0.1 / rms)                      # about -20 dBFS, never clipping
    return np.ascontiguousarray(x * gain, dtype=np.float32)


def _grid_fit(t, bpm, div=4, tol=0.035, win=8.0):
    """Share of onsets within tol of a 16th-note grid, best phase per 8 s window (so small tempo drift is fine)."""
    step = 60.0 / bpm / div
    ph = np.linspace(0, step, 48, endpoint=False)
    on = tot = 0
    for w in np.arange(t[0], t[-1] + 1e-9, win):
        tt = t[(t >= w) & (t < w + win)]
        if len(tt) < 3:
            continue
        hit = np.abs(((tt[None, :] - ph[:, None] + step / 2) % step) - step / 2) < tol
        on += hit.sum(1).max()
        tot += len(tt)
    return on / max(tot, 1)


def _grid_excess(t, bpm, rng):
    """How much better the real onsets fit this tempo's grid than random onsets would."""
    rnd = np.mean([_grid_fit(np.sort(rng.uniform(t[0], t[-1], len(t))), bpm) for _ in range(3)])
    return _grid_fit(t, bpm) - rnd


def refine_tempo(y, sr, bpm0, margin=0.08):
    """The beat tracker can lock onto 4/3 or 3/2 of the real tempo (Wonderwall: 117.5 instead of 87).
    Try the related tempos, each fine-tuned by up to 3%, and keep the one whose grid explains the
    note onsets best. Only leave the tracker's own tempo when another is clearly better.
    Checked against 8 songs with known tempos (tests: 80, 80, 99, 85, 87, 100, 100, 124)."""
    import librosa
    t = librosa.onset.onset_detect(y=y, sr=sr, hop_length=256, units="time")
    if len(t) < 16:
        return bpm0
    rng = np.random.default_rng(0)
    rows = []
    for ratio in (1.0, 3 / 4, 4 / 3, 2 / 3, 3 / 2):
        c = bpm0 * ratio
        if not 55 <= c <= 190:
            continue
        rows.append(max(((_grid_excess(t, b, rng), b, ratio) for b in c * (1 + np.linspace(-0.03, 0.03, 25))),
                        key=lambda r: r[0]))
    own = next(r for r in rows if r[2] == 1.0)
    best = max(rows, key=lambda r: r[0])
    return float(best[1] if best[0] >= own[0] + margin else own[1])


def detect_tempo(x):
    """Tempo only sets the BPM the Clavinova shows (and its bar lines); note timing is exact either way."""
    import librosa
    try:
        y = librosa.resample(x.mean(axis=1), orig_sr=SR, target_sr=22050)
        tempo, _ = librosa.beat.beat_track(y=y, sr=22050)
        bpm = float(np.atleast_1d(tempo)[0])
    except Exception:  # noqa: BLE001
        return 120.0
    if not np.isfinite(bpm) or bpm <= 0:
        return 120.0
    while bpm < 70:
        bpm *= 2
    while bpm > 170:
        bpm /= 2
    try:
        span = 120 * 22050                                  # a 2-minute stretch from the middle is plenty
        start = max(0, len(y) // 2 - span // 2)
        bpm = refine_tempo(y[start:start + span], 22050, bpm)
    except Exception:  # noqa: BLE001
        pass
    return round(bpm, 1)


def run_transkun(x):
    import torch
    from transkun.Data import writeMidi
    model = MODELS.transkun()
    with torch.inference_mode():
        notes = model.transcribe(torch.from_numpy(np.ascontiguousarray(x)), discardSecondHalf=False)
    pm = writeMidi(notes)
    ins = pm.instruments[0] if pm.instruments else None
    if ins is None:
        return [], []
    ns = [mx.Note(n.start, n.end, n.pitch, n.velocity) for n in ins.notes if 21 <= n.pitch <= 108]
    pedal = sorted((c.time, c.value) for c in ins.control_changes if c.number == 64)
    return ns, pedal


def run_basic_pitch(stem, path, fmin, fmax):
    sf.write(path, stem, SR, subtype="FLOAT")
    from basic_pitch.inference import predict
    _, pm, _ = predict(str(path), MODELS.basic_pitch(), minimum_frequency=fmin, maximum_frequency=fmax)
    return [mx.Note(n.start, n.end, n.pitch, n.velocity) for i in pm.instruments for n in i.notes]


def separate(x, prog, job):
    import torch
    from demucs.apply import apply_model
    model = MODELS.demucs()
    wav = torch.from_numpy(x.T.copy())
    ref = wav.mean(0)
    mu, sd = ref.mean(), ref.std() + 1e-8
    length = wav.shape[1]
    inner = getattr(model, "models", None)
    seg_s = getattr(model, "segment", None) or (getattr(inner[0], "segment", None) if inner else None) or 7.8
    seg = float(seg_s) * SR

    def cb(d):
        # demucs reports each chunk with its start sample; there is no total, so we use our own length.
        if job.cancelled:
            raise Cancelled()
        off = d.get("segment_offset")
        if off is not None and d.get("state") == "end":
            prog.set((off + seg) / max(1, length))

    with torch.inference_mode():
        out = apply_model(model, ((wav - mu) / sd)[None], device="cpu", split=True, overlap=0.25,
                          shifts=0, progress=False, callback=cb)[0]
    out = out * sd + mu
    return {name: np.ascontiguousarray(out[i].numpy().T) for i, name in enumerate(model.sources)}


def render_preview(midi_path, mp3_path, workdir):
    """Play the MIDI through the Mac's own General MIDI sounds so it can be heard in the browser."""
    bank = APPLE_GM if Path(APPLE_GM).exists() else FALLBACK_SF
    wav = Path(workdir) / "preview.wav"
    try:
        subprocess.run([FLUIDSYNTH, "-ni", "-q", "-g", "0.7", "-r", str(SR), "-F", str(wav), bank, str(midi_path)],
                       capture_output=True, timeout=600, stdin=subprocess.DEVNULL)
        if not wav.exists() or wav.stat().st_size < 10000:
            return False
        subprocess.run([FFMPEG, "-nostdin", "-loglevel", "error", "-y", "-i", str(wav), "-af", "loudnorm=I=-16:TP=-1.5",
                        "-b:a", "128k", str(mp3_path)], capture_output=True, timeout=600)
        return Path(mp3_path).exists() and Path(mp3_path).stat().st_size > 1000
    except Exception:  # noqa: BLE001
        return False


def piano_parts(notes, pedal, split_hands):
    if not split_hands:
        return [mx.Part("Piano", 0, 0, notes, pedal)]
    right = [n for n in notes if n.pitch >= HAND_SPLIT]
    left = [n for n in notes if n.pitch < HAND_SPLIT]
    return [mx.Part("Right hand", 0, 0, right, pedal), mx.Part("Left hand", 1, 0, left, pedal)]


def plan_stages(job, seconds):
    s = []
    if job.source == "youtube":
        s.append(("download", "Downloading the song", 8 + seconds * 0.02))
    s.append(("decode", "Reading the audio", 2 + seconds * 0.01))
    if job.mode in ("arrange", "band"):
        s.append(("separate", "Separating the instruments", seconds * SPEED["demucs"]))
    if job.mode == "arrange":
        s.append(("melody", "Finding the sung melody", seconds * SPEED["basic_pitch"]))
    if job.mode == "band":
        s += [("melody", "Finding the melody", seconds * SPEED["basic_pitch"]),
              ("chords", "Finding the chords", seconds * SPEED["transkun"]),
              ("bass", "Finding the bass line", seconds * SPEED["basic_pitch"]),
              ("drums", "Finding the drums", seconds * SPEED["drums"])]
    else:
        s.append(("notes", "Finding every piano note", seconds * SPEED["transkun"]))
    s += [("tempo", "Finding the tempo", 2 + seconds * 0.02),
          ("write", "Writing and checking the MIDI file", 3 + seconds * 0.03)]
    return s


def unique_path(folder, name):
    p = Path(folder) / name
    n = 2
    while p.exists():
        p = Path(folder) / (Path(name).stem[: usb.NAME_LIMIT - 4] + f" {n}.mid")
        n += 1
    return p


def process(job, work_root, lib_dir, on_update):
    work = Path(work_root) / job.id
    work.mkdir(parents=True, exist_ok=True)
    lib_dir = Path(lib_dir)
    meta_dir = lib_dir / ".clavinova"
    meta_dir.mkdir(parents=True, exist_ok=True)
    free = shutil.disk_usage(work).free
    if free < 600 * 1024 * 1024:
        raise UserError(f"Your Mac is almost out of space ({free / 1e9:.1f} GB free). Free up at least 1 GB and try again.")

    seconds = float(job.duration_hint or 240)
    prog = Progress(job, plan_stages(job, seconds), on_update)
    try:
        if job.source == "youtube":
            prog.start("download", timer=False)
            try:
                src = sources.download_youtube_audio(job.video_id, work, on_progress=prog.set, cancel=lambda: job.cancelled)
            except Cancelled:
                raise
            except sources.SlowDown as e:
                raise UserError(str(e))
            except Exception as e:  # noqa: BLE001
                if job.cancelled:
                    raise Cancelled()
                raise UserError(sources.friendly_download_error(str(e)))
        else:
            src = Path(job.upload_path)

        prog.start("decode")
        x = decode_audio(src, work / "audio.wav")
        seconds = len(x) / SR
        prog.stages = plan_stages(job, seconds)                 # re-plan with the real length
        prog.total = sum(s[2] for s in prog.stages)
        mix_rms = _rms(x.mean(axis=1))

        parts = []
        if job.mode == "piano":
            prog.start("notes")
            notes, pedal = run_transkun(x)
            parts = piano_parts(notes, pedal, job.split_hands)
        elif job.mode == "arrange":
            prog.start("separate", timer=False)
            stems = separate(x, prog, job)
            prog.start("melody")
            melody = []
            if _rms(stems["vocals"].mean(axis=1)) >= STEM_GATE * mix_rms:
                melody = postproc.mono(run_basic_pitch(stems["vocals"], work / "vocals.wav", 80, 1400))
            accompaniment = stems["bass"] + stems["other"]
            if KEEP_VOCALS_IN_PIANO:
                accompaniment = accompaniment + stems["vocals"]
            del stems
            gc.collect()
            prog.start("notes")
            notes, pedal = run_transkun(accompaniment)
            del accompaniment
            parts = piano_parts(notes, pedal, job.split_hands)
            parts[0].notes = parts[0].notes + melody          # the sung melody belongs to the right hand
        else:
            prog.start("separate", timer=False)
            stems = separate(x, prog, job)
            present = {k: _rms(v.mean(axis=1)) >= STEM_GATE * mix_rms for k, v in stems.items()}

            prog.start("melody")
            melody = []
            if present["vocals"]:
                melody = postproc.mono(run_basic_pitch(stems["vocals"], work / "vocals.wav", 80, 1400))
            prog.start("chords")
            chords, pedal = ([], [])
            if present["other"]:
                chords, pedal = run_transkun(stems["other"])
                chords = postproc.ghost_octave(chords, dv=6, maxdur=0.06)
            prog.start("bass")
            bass = []
            if present["bass"]:
                bass = postproc.rel_vel(postproc.mono(run_basic_pitch(stems["bass"], work / "bass.wav", 30, 400)), 0.7)
            prog.start("drums")
            hits = drumkit.transcribe_drums(stems["drums"].mean(axis=1), SR, mix_rms)
            drum_notes = [mx.Note(t, t + 0.1, p, v) for t, p, v in hits]
            del stems
            gc.collect()
            parts = [mx.Part("Melody", 0, int(job.melody_program), melody),
                     mx.Part("Chords", 1, 0, chords, pedal),
                     mx.Part("Bass", 2, 33, bass),
                     mx.Part("Drums", 9, 0, drum_notes)]

        total_notes = sum(len(p.notes) for p in parts)
        if total_notes == 0:
            raise UserError("No notes were found in that recording. Is it silent, or only talking?")

        prog.start("tempo")
        bpm = detect_tempo(x)
        del x
        gc.collect()

        prog.start("write")
        parts = [p for p in parts if p.notes]
        out = unique_path(lib_dir, usb.safe_filename(job.title))
        tmp = work / "song.mid"
        mx.write_smf(parts, tmp, bpm, job.title)
        problems = mx.verify_smf(tmp, parts, bpm)
        if problems:
            raise RuntimeError("MIDI self-check failed: " + "; ".join(problems))
        shutil.copyfile(tmp, out)
        preview = meta_dir / (out.stem + ".mp3")
        has_preview = render_preview(out, preview, work)
        result = {
            "file": out.name,
            "title": job.title,
            "mode": job.mode,
            "bpm": bpm,
            "seconds": round(seconds, 1),
            "parts": [{"name": p.name, "notes": len(p.notes), "channel": p.channel + 1} for p in parts],
            "notes": total_notes,
            "preview": has_preview,
            "video_id": job.video_id,
            "created": time.time(),
        }
        (meta_dir / (out.stem + ".json")).write_text(json.dumps(result, indent=1))
        return result
    finally:
        prog.stop()
        shutil.rmtree(work, ignore_errors=True)
        gc.collect()
