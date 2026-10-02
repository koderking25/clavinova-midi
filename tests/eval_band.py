"""Measure Full band, part by part, against songs where every note is known.

    .venv/bin/python tests/eval_band.py            # score the pipeline as it is
    .venv/bin/python tests/eval_band.py --variants # also score alternatives for each part

Songs are generated here (original, seeded, so every run is the same), played through the Mac's
General MIDI sounds, separated and transcribed exactly as the app does, then each part is scored
against the notes it was made from. Audio is written to a temporary folder and deleted after.
"""
import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
import pretty_midi
import soundfile as sf

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
sys.path.insert(0, str(ROOT / "tests"))
import drums as drumkit  # noqa: E402
import midi_export as mx  # noqa: E402
import pipeline  # noqa: E402
import postproc  # noqa: E402
from score_notes import score  # noqa: E402

GM = "/System/Library/Components/CoreAudio.component/Contents/Resources/gs_instruments.dls"
SECONDS = 30

# Four songs with different textures, keys, tempos and "other" instruments, so no single trick wins.
SONGS = [
    dict(name="pop band (electric piano)", bpm=100, other=4, root=57, feel="straight"),
    dict(name="rock band (steel guitar)", bpm=124, other=25, root=52, feel="straight"),
    dict(name="ballad (strings)", bpm=72, other=48, root=53, feel="slow"),
    dict(name="funk (clean guitar)", bpm=108, other=27, root=45, feel="syncopated"),
]


def make_song(spec, seed):
    """Melody, chords, bass and drums as four separate tracks, so each can be scored on its own."""
    rng = np.random.default_rng(seed)
    beat = 60 / spec["bpm"]
    pm = pretty_midi.PrettyMIDI(initial_tempo=spec["bpm"])
    melody = pretty_midi.Instrument(program=52, name="Melody")      # sung line, "voice oohs"
    chords = pretty_midi.Instrument(program=spec["other"], name="Chords")
    bass = pretty_midi.Instrument(program=33, name="Bass")
    drums = pretty_midi.Instrument(program=0, is_drum=True, name="Drums")
    root = spec["root"]
    progression = [0, -4, -7, -5] if spec["feel"] != "slow" else [0, 5, -2, -4]
    scale = [0, 2, 4, 5, 7, 9, 11]
    t = 0.0
    bar = 0
    while t < SECONDS:
        step = root + progression[bar % 4]
        triad = [step, step + 4, step + 7]
        for i in range(4):                                            # chords on every beat or held
            start = t + i * beat
            if spec["feel"] == "slow" and i % 2:
                continue
            length = beat * (1.8 if spec["feel"] == "slow" else 0.9)
            for note in triad:
                chords.notes.append(pretty_midi.Note(int(rng.integers(62, 84)), note + 12, start, start + length))
        for i in range(4):                                            # bass
            if spec["feel"] == "syncopated" and i == 2:
                continue
            start = t + i * beat + (beat / 2 if spec["feel"] == "syncopated" and i == 3 else 0)
            bass.notes.append(pretty_midi.Note(int(rng.integers(80, 105)), step - 12, start, start + beat * 0.8))
        for i in range(4):                                            # melody
            if rng.random() < 0.25:
                continue
            start = t + i * beat
            pitch = step + 24 + int(rng.choice(scale))
            melody.notes.append(pretty_midi.Note(int(rng.integers(75, 105)), pitch, start, start + beat * 0.85))
        for i in range(8):                                            # drums
            start = t + i * beat / 2
            drums.notes.append(pretty_midi.Note(int(rng.integers(55, 80)), 42, start, start + 0.08))
            if i in (0, 4) or (spec["feel"] == "syncopated" and i == 3):
                drums.notes.append(pretty_midi.Note(105, 36, start, start + 0.08))
            if i in (2, 6):
                drums.notes.append(pretty_midi.Note(100, 38, start, start + 0.08))
        t += 4 * beat
        bar += 1
    for inst in (melody, chords, bass, drums):
        inst.notes = [n for n in inst.notes if n.start < SECONDS]
    pm.instruments += [melody, chords, bass, drums]
    return pm


def render(pm, folder, name):
    midi, wav = folder / f"{name}.mid", folder / f"{name}.wav"
    pm.write(str(midi))
    subprocess.run(["fluidsynth", "-ni", "-q", "-g", "0.7", "-r", str(pipeline.SR), "-F", str(wav), GM, str(midi)],
                   check=True, capture_output=True)
    audio, sr = sf.read(wav, dtype="float32", always_2d=True)
    return audio, midi, wav


def only(pm, names):
    out = pretty_midi.PrettyMIDI()
    for inst in pm.instruments:
        if inst.name in names:
            copy = pretty_midi.Instrument(program=inst.program, is_drum=inst.is_drum, name=inst.name)
            copy.notes = list(inst.notes)
            out.instruments.append(copy)
    return out


def as_midi(notes, program=0):
    pm = pretty_midi.PrettyMIDI()
    inst = pretty_midi.Instrument(program=program)
    inst.notes = [pretty_midi.Note(int(min(127, max(1, n.velocity))), int(n.pitch), float(n.start), float(max(n.end, n.start + 0.02)))
                  for n in notes]
    pm.instruments.append(inst)
    return pm


def drum_scores(hits, truth):
    import mir_eval
    out = {}
    ref_drums = [i for i in truth.instruments if i.is_drum][0]
    for label, ref_pitches, est_pitch in (("kick", {36}, 36), ("snare", {38}, 38), ("hats", {42, 46}, 42)):
        ref = np.array(sorted({round(n.start, 3) for n in ref_drums.notes if n.pitch in ref_pitches}))
        est = np.array(sorted(t for t, p, _ in hits if p == est_pitch))
        out[label] = mir_eval.onset.f_measure(ref, est, window=0.05)[0] if len(ref) and len(est) else 0.0
    return out


class Silent:
    def set(self, f): pass


class NotCancelled:
    cancelled = False


def evaluate(parts_fn, label, songs=SONGS):
    """parts_fn(stems, mix_rms, work) -> dict with melody/chords/bass/drums, so alternatives can be swapped in."""
    folder = Path(tempfile.mkdtemp(prefix="eval-band-"))
    totals = {}
    try:
        for i, spec in enumerate(songs):
            truth = make_song(spec, seed=11 + i)
            audio, midi, wav = render(truth, folder, f"song{i}")
            x = pipeline.decode_audio(wav, folder / f"dec{i}.wav")
            mix_rms = pipeline._rms(x.mean(axis=1))
            stems = pipeline.separate(x, Silent(), NotCancelled())
            got = parts_fn(stems, mix_rms, folder)
            row = {
                "melody": score(only(truth, {"Melody"}), as_midi(got["melody"]))["onset_f1"],
                "chords": score(only(truth, {"Chords"}), as_midi(got["chords"]))["onset_f1"],
                "bass": score(only(truth, {"Bass"}), as_midi(got["bass"]))["onset_f1"],
            }
            row.update(drum_scores(got["drums"], truth))
            totals[spec["name"]] = row
            for f in (midi, wav, folder / f"dec{i}.wav"):
                f.unlink(missing_ok=True)
            del stems, x
    finally:
        shutil.rmtree(folder, ignore_errors=True)
    keys = ["melody", "chords", "bass", "kick", "snare", "hats"]
    print(f"\n{label}")
    print(f"  {'song':28s}" + "".join(f"{k:>9s}" for k in keys))
    for name, row in totals.items():
        print(f"  {name:28s}" + "".join(f"{row[k]:9.3f}" for k in keys))
    avg = {k: float(np.mean([r[k] for r in totals.values()])) for k in keys}
    print(f"  {'average':28s}" + "".join(f"{avg[k]:9.3f}" for k in keys))
    return avg


def current_parts(stems, mix_rms, work):
    """Exactly what the app does today."""
    present = {k: pipeline._rms(v.mean(axis=1)) >= pipeline.STEM_GATE * mix_rms for k, v in stems.items()}
    melody = postproc.mono(pipeline.run_basic_pitch(stems["vocals"], work / "v.wav", 80, 1400)) if present["vocals"] else []
    chords = []
    if present["other"]:
        chords, _ = pipeline.run_transkun(stems["other"])
        chords = postproc.ghost_octave(chords, dv=6, maxdur=0.06)
    bass = postproc.rel_vel(postproc.mono(pipeline.run_basic_pitch(stems["bass"], work / "b.wav", 30, 400)), 0.7) if present["bass"] else []
    hits = drumkit.transcribe_drums(stems["drums"].mean(axis=1), pipeline.SR, mix_rms)
    return {"melody": melody, "chords": chords, "bass": bass, "drums": hits}


def diagnose(songs=SONGS):
    """Where does each instrument end up, and are its notes found anywhere at all?

    Scoring each part against its own track punishes the app for putting a note in a different
    part, which matters far less than losing it. This prints both.
    """
    folder = Path(tempfile.mkdtemp(prefix="diag-band-"))
    try:
        for i, spec in enumerate(songs):
            truth = make_song(spec, seed=11 + i)
            audio, midi, wav = render(truth, folder, f"song{i}")
            x = pipeline.decode_audio(wav, folder / f"dec{i}.wav")
            mix_rms = pipeline._rms(x.mean(axis=1))
            stems = pipeline.separate(x, Silent(), NotCancelled())
            shares = {k: pipeline._rms(v.mean(axis=1)) / max(mix_rms, 1e-9) for k, v in stems.items()}
            got = current_parts(stems, mix_rms, folder)
            everything = as_midi(list(got["melody"]) + list(got["chords"]) + list(got["bass"]))
            all_pitched = only(truth, {"Melody", "Chords", "Bass"})
            print(f"\n  {spec['name']}")
            print("    stem loudness: " + ", ".join(f"{k} {v:.2f}" for k, v in shares.items()))
            print(f"    melody notes found in the melody part : {score(only(truth, {'Melody'}), as_midi(got['melody']))['onset_f1']:.3f}")
            print(f"    melody notes found in the chords part : {score(only(truth, {'Melody'}), as_midi(got['chords']))['onset_f1']:.3f}")
            print(f"    melody notes found ANYWHERE           : {score(only(truth, {'Melody'}), everything)['onset_f1']:.3f}")
            print(f"    chords notes found ANYWHERE           : {score(only(truth, {'Chords'}), everything)['onset_f1']:.3f}")
            print(f"    every pitched note, found anywhere    : {score(all_pitched, everything)['onset_f1']:.3f}"
                  f"   (notes: {sum(len(inst.notes) for inst in all_pitched.instruments)} wanted, "
                  f"{sum(len(inst.notes) for inst in everything.instruments)} found)")
            for f in (midi, wav, folder / f"dec{i}.wav"):
                f.unlink(missing_ok=True)
            del stems, x
    finally:
        shutil.rmtree(folder, ignore_errors=True)



# ---------------------------------------------------------------- alternatives being measured
def merge_notes(*lists, window=0.05):
    """One list from several listeners: the same note heard twice is kept once, the louder one."""
    notes = sorted((n for lst in lists for n in lst), key=lambda n: (n.start, n.pitch))
    kept = []
    for n in notes:
        twin = next((k for k in reversed(kept)
                     if k.pitch == n.pitch and abs(k.start - n.start) <= window), None)
        if twin is None:
            kept.append(n)
        elif n.velocity > twin.velocity:
            twin.velocity = n.velocity
            twin.end = max(twin.end, n.end)
    return kept


def melody_line(stem, min_note=0.08):
    """Follow the single strongest line by pitch, the way an ear follows a tune.

    pYIN gives a pitch for every moment and says when there is no clear pitch at all; steady
    stretches of the same semitone become notes. It works on whatever stem carries the tune,
    so a voice filed under the wrong stem is still found.
    """
    import librosa
    y = stem.mean(axis=1) if stem.ndim > 1 else stem
    y = librosa.resample(y, orig_sr=pipeline.SR, target_sr=22050)
    if float(np.sqrt(np.mean(np.square(y)))) < 1e-4:
        return []
    f0, voiced, _ = librosa.pyin(y, sr=22050, fmin=110.0, fmax=1400.0, frame_length=2048, hop_length=256)
    times = librosa.frames_to_time(np.arange(len(f0)), sr=22050, hop_length=256)
    rms = librosa.feature.rms(y=y, frame_length=2048, hop_length=256)[0][: len(f0)]
    loud = rms / max(float(rms.max()), 1e-9)
    midi = np.where(np.isfinite(f0), librosa.hz_to_midi(np.nan_to_num(f0, nan=0.0) + 1e-9), np.nan)
    notes, run_pitch, run_start, run_loud = [], None, None, []
    for i, (t, p, v) in enumerate(zip(times, midi, voiced)):
        semitone = int(round(p)) if np.isfinite(p) and v else None
        if semitone != run_pitch:
            if run_pitch is not None and t - run_start >= min_note:
                notes.append(mx.Note(run_start, t, run_pitch, int(np.clip(40 + 80 * float(np.mean(run_loud)), 30, 127))))
            run_pitch, run_start, run_loud = semitone, t, []
        if semitone is not None:
            run_loud.append(loud[i])
    if run_pitch is not None and times[-1] - run_start >= min_note:
        notes.append(mx.Note(run_start, times[-1], run_pitch, int(np.clip(40 + 80 * float(np.mean(run_loud)), 30, 127))))
    return notes


def parts_basic_pitch(stems, mix_rms, work):
    """The 'other' stem through Basic Pitch, which is built for any instrument, not just piano."""
    present = {k: pipeline._rms(v.mean(axis=1)) >= pipeline.STEM_GATE * mix_rms for k, v in stems.items()}
    melody = postproc.mono(pipeline.run_basic_pitch(stems["vocals"], work / "v.wav", 80, 1400)) if present["vocals"] else []
    chords = pipeline.run_basic_pitch(stems["other"], work / "o.wav", 55, 2500) if present["other"] else []
    bass = postproc.rel_vel(postproc.mono(pipeline.run_basic_pitch(stems["bass"], work / "b.wav", 30, 400)), 0.7) if present["bass"] else []
    hits = drumkit.transcribe_drums(stems["drums"].mean(axis=1), pipeline.SR, mix_rms)
    return {"melody": melody, "chords": chords, "bass": bass, "drums": hits}


def parts_merged(stems, mix_rms, work):
    """Two listeners on the 'other' stem, merged: the piano model and Basic Pitch."""
    present = {k: pipeline._rms(v.mean(axis=1)) >= pipeline.STEM_GATE * mix_rms for k, v in stems.items()}
    melody = postproc.mono(pipeline.run_basic_pitch(stems["vocals"], work / "v.wav", 80, 1400)) if present["vocals"] else []
    chords = []
    if present["other"]:
        piano, _ = pipeline.run_transkun(stems["other"])
        chords = merge_notes(postproc.ghost_octave(piano, dv=6, maxdur=0.06),
                             pipeline.run_basic_pitch(stems["other"], work / "o.wav", 55, 2500))
    bass = postproc.rel_vel(postproc.mono(pipeline.run_basic_pitch(stems["bass"], work / "b.wav", 30, 400)), 0.7) if present["bass"] else []
    hits = drumkit.transcribe_drums(stems["drums"].mean(axis=1), pipeline.SR, mix_rms)
    return {"melody": melody, "chords": chords, "bass": bass, "drums": hits}


def parts_merged_plus_melody(stems, mix_rms, work):
    """Merged chords, plus a melody followed by pitch on whichever stem is carrying the tune."""
    got = parts_merged(stems, mix_rms, work)
    vocals_loud = pipeline._rms(stems["vocals"].mean(axis=1)) >= pipeline.STEM_GATE * mix_rms
    lead_stem = stems["vocals"] if vocals_loud else stems["other"]
    got["melody"] = merge_notes(got["melody"], melody_line(lead_stem))
    return got


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--variants", action="store_true")
    ap.add_argument("--diagnose", action="store_true")
    args = ap.parse_args()
    started = time.time()
    if args.diagnose:
        diagnose()
    elif args.variants:
        now = evaluate(current_parts, "1. as it is today: piano model on the other stem")
        bp = evaluate(parts_basic_pitch, "2. Basic Pitch on the other stem")
        merged = evaluate(parts_merged, "3. both listeners merged")
        full = evaluate(parts_merged_plus_melody, "4. both merged, plus a melody followed by pitch")
        print("\n  average of everything pitched (melody, chords, bass):")
        for label, avg in (("today", now), ("basic pitch", bp), ("merged", merged), ("merged + melody", full)):
            print(f"    {label:16s}{np.mean([avg['melody'], avg['chords'], avg['bass']]):.3f}"
                  f"   melody {avg['melody']:.3f}  chords {avg['chords']:.3f}  bass {avg['bass']:.3f}")
    else:
        evaluate(current_parts, "Full band as it is today")
    print(f"\n({time.time() - started:.0f}s)")
