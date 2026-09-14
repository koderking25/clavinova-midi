"""Readable score: does the beat grid put notes in the right place in the bar?

    .venv/bin/python tests/test_score_grid.py

Made-up performances with known rubato are rendered to real audio, so every true beat and bar line
is known. The app finds the beats, the bar length (3 or 4) and where bars start, from that audio and
from the notes it transcribes. We measure where the performed notes land in the written bars, against
today's single fixed tempo, and every file must pass the app's own timing self-check.

TUNING pieces were used while choosing the method's two thresholds. HELD OUT pieces were not, and
they decide whether it ships. The slow transcription is cached in /tmp/score-grid-cache.
"""
import math
import pickle
import random
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import numpy as np
import pretty_midi

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "app"))
import midi_export as mx  # noqa: E402
import pipeline  # noqa: E402

DLS = "/System/Library/Components/CoreAudio.component/Contents/Resources/gs_instruments.dls"
WORK = Path(tempfile.mkdtemp(prefix="score-grid-"))
CACHE = Path("/tmp/score-grid-cache")
CACHE.mkdir(exist_ok=True)


def beat_times(n_beats, base_bpm, per_bar, rubato=0.0, ritard=0.0, pickup=0, seed=0):
    rng = random.Random(seed)
    phrase = 8 * per_bar
    t, out = 1.0, []
    for i in range(n_beats + 1):
        out.append(t)
        pos = (i - pickup) % phrase
        bpm = base_bpm * (1 + rubato * math.sin(2 * math.pi * (i - pickup) / phrase + 0.7))
        if ritard and pos >= phrase - per_bar:
            bpm *= 1 - ritard * (pos - (phrase - per_bar) + 1) / per_bar
        if rubato:
            bpm *= 1 + rng.uniform(-0.02, 0.02)
        t += 60.0 / bpm
    return out


def score_notes(style, per_bar, n_bars, pickup, seed):
    """(position in beats from the first downbeat, length in beats, pitch, velocity)."""
    rng = random.Random(seed)
    chords = [(45, [57, 60, 64]), (41, [53, 57, 60]), (48, [55, 60, 64]), (43, [55, 59, 62])]
    out = []
    if pickup:
        out.append((-float(pickup), pickup * 0.9, 67, 70))
    for bar in range(n_bars):
        root, triad = chords[bar % 4]
        p0 = bar * per_bar
        if style == "block":                               # bass on 1, chords on 1 and 3, busy melody
            out.append((p0, per_bar - 0.2, root - 12, rng.randint(80, 100)))
            for beat in (0, 2):
                out += [(p0 + beat, 1.8, p, rng.randint(55, 75)) for p in triad]
            pos = 0.0
            while pos < per_bar:
                d = rng.choice([0.5, 0.5, 1.0])
                out.append((p0 + pos, d * 0.9, rng.choice(triad) + 12, rng.randint(60, 90)))
                pos += d
        elif style == "waltz":                             # bass on 1, chords on 2 and 3
            out.append((p0, 0.9, root - 12, rng.randint(80, 100)))
            for beat in (1, 2):
                out += [(p0 + beat, 0.8, p, rng.randint(45, 62)) for p in triad]
            pos = 0.0
            while pos < per_bar:
                d = rng.choice([1.0, 1.0, 2.0, 0.5]) if pos + 2 <= per_bar else 1.0
                out.append((p0 + pos, d * 0.9, rng.choice(triad) + 12, rng.randint(60, 90)))
                pos += d
        elif style == "arpeggio":                          # flowing broken chords, slow melody
            broken = [root - 12, triad[0], triad[1], triad[2], triad[1], triad[0], triad[2], triad[1]]
            for k in range(per_bar * 2):
                out.append((p0 + k * 0.5, 0.45, broken[k % 8], rng.randint(40, 62) + (20 if k == 0 else 0)))
            pos = 0.0
            while pos < per_bar:
                d = rng.choice([1.0, 2.0])
                d = min(d, per_bar - pos)
                out.append((p0 + pos, d * 0.95, rng.choice(triad) + 12, rng.randint(65, 90)))
                pos += d
        elif style == "syncopated":                        # bass on 1 and the "and" of 2, off-beat chords
            out.append((p0, 1.4, root - 12, rng.randint(85, 100)))
            out.append((p0 + 1.5, 1.0, root - 12, rng.randint(70, 85)))
            for beat in (0.5, 1.5, 2.5, 3.5):
                out += [(p0 + beat, 0.4, p, rng.randint(50, 68)) for p in triad]
            for pos in (0.0, 0.75, 1.5, 2.0, 3.0, 3.5):
                out.append((p0 + pos, 0.4, rng.choice(triad) + 12, rng.randint(60, 90)))
    return out


def performance(notes, beats, pickup, seed):
    def at(pos):
        i = int(math.floor(pos)) + pickup
        i = max(0, min(i, len(beats) - 2))
        return beats[i] + (pos + pickup - i) * (beats[i + 1] - beats[i])
    perf = []
    for pos, dur, pitch, vel in notes:
        start = at(pos) + random.Random(seed * 1000 + int(pos * 8)).uniform(-0.012, 0.012)
        perf.append((pos, start, at(pos + dur), pitch, vel))
    return perf


def render(perf, name):
    pm = pretty_midi.PrettyMIDI()
    inst = pretty_midi.Instrument(program=0)
    for _, s, e, p, v in perf:
        inst.notes.append(pretty_midi.Note(velocity=v, pitch=p, start=s, end=max(e, s + 0.05)))
    pm.instruments.append(inst)
    mid, wav = WORK / f"{name}.mid", WORK / f"{name}.wav"
    pm.write(str(mid))
    subprocess.run(["fluidsynth", "-ni", "-q", "-R", "0", "-C", "0", "-g", "0.8", "-r", "44100", "-F", str(wav), DLS, str(mid)],
                   check=True, capture_output=True)
    return wav


def analysed(key, perf):
    """What the app sees from the audio: its notes, tempo and raw beats. Cached, because transcribing is slow."""
    f = CACHE / f"{key}.pkl"
    if f.exists():
        return pickle.loads(f.read_bytes())
    wav = render(perf, key)
    x = pipeline.decode_audio(wav, WORK / f"{key}_dec.wav")
    notes, _ = pipeline.run_transkun(x)
    bpm = pipeline.detect_tempo(x)
    beats = pipeline.detect_beats(x, bpm)
    data = dict(notes=[(n.start, n.end, n.pitch, n.velocity) for n in notes], bpm=bpm,
                beats=None if beats is None else list(beats), seconds=len(x) / pipeline.SR)
    f.write_bytes(pickle.dumps(data))
    return data


def measure(perf, grid, per_bar, lead_in=None):
    true_pos = np.array([p[0] for p in perf])
    ticks = np.array([grid.tick(p[1]) for p in perf], dtype=float)
    written = ticks / mx.PPQ if lead_in is None else (ticks - lead_in) / mx.PPQ
    diff = written - true_pos
    offset = np.median(diff)
    placed = np.mean(np.abs(diff - offset) <= 0.125)
    bars = (lead_in is None and grid.per_bar == per_bar and abs(offset - round(offset)) <= 0.125
            and round(offset) % per_bar == 0)
    return placed, bars


CASES = [
    # (label, group, style, per_bar, base_bpm, rubato, ritard, pickup)
    # "tuning": looked at while designing the method (the first five held-out pieces became tuning
    # pieces once their failures shaped the bass rules). "held out": first seen at the final test.
    ("steady 96 BPM", "tuning", "block", 4, 96, 0.0, 0.0, 0),
    ("gentle rubato", "tuning", "block", 4, 84, 0.08, 0.0, 0),
    ("strong rubato, slowing at phrase ends", "tuning", "block", 4, 80, 0.18, 0.30, 0),
    ("steady 104 BPM, starts on a pickup", "tuning", "block", 4, 104, 0.0, 0.0, 1),
    ("waltz, steady 132 BPM", "tuning", "waltz", 3, 132, 0.0, 0.0, 0),
    ("waltz with rubato", "tuning", "waltz", 3, 120, 0.10, 0.20, 0),
    ("arpeggio ballad, gentle rubato", "tuning", "arpeggio", 4, 72, 0.08, 0.15, 0),
    ("arpeggio ballad, pickup of two beats", "tuning", "arpeggio", 4, 76, 0.0, 0.0, 2),
    ("syncopated pop, steady 100 BPM", "tuning", "syncopated", 4, 100, 0.0, 0.0, 0),
    ("broken-chord waltz, gentle rubato", "held out", "arpeggio", 3, 108, 0.07, 0.10, 0),
    ("slow block ballad 66 BPM, rubato, pickup of 3", "held out", "block", 4, 66, 0.10, 0.20, 3),
    ("syncopated pop, gentle rubato 92 BPM", "held out", "syncopated", 4, 92, 0.06, 0.0, 0),
    ("waltz 96 BPM with a pickup", "held out", "waltz", 3, 96, 0.06, 0.10, 1),
    ("brisk arpeggios, steady 116 BPM", "held out", "arpeggio", 4, 116, 0.0, 0.0, 0),
]

if __name__ == "__main__":
    rows = []
    for n, (label, group, style, per_bar, bpm0, rub, rit, pickup) in enumerate(CASES):
        if "--tuning" in sys.argv and group != "tuning":
            continue                                   # held-out pieces stay unseen while tuning
        n_bars = 28 if per_bar == 4 else 36
        beats = beat_times(n_bars * per_bar + pickup, bpm0, per_bar, rub, rit, pickup, seed=n)
        perf = performance(score_notes(style, per_bar, n_bars, pickup, seed=n), beats, pickup, seed=n)
        key = f"{n}-{style}-{per_bar}-{bpm0}-{rub}-{rit}-{pickup}"
        a = analysed(key, perf)
        notes = [mx.Note(*t) for t in a["notes"]]
        fixed_placed, _ = measure(perf, mx.TimeGrid(a["bpm"]), per_bar, lead_in=mx.LEAD_IN_TICKS)
        plan = None
        if a["beats"] is not None:
            b, meter, downbeat = pipeline.choose_meter(pipeline.align_beats(a["beats"], notes), notes)
            b = pipeline.align_beats(b, notes)
            grid = mx.TimeGrid(a["bpm"], b, downbeat, meter)
            cons = pipeline.bass_consistency(grid, notes)
            if (pipeline.usable_beats(b, a["seconds"]) and pipeline.grid_fit(grid, notes) >= pipeline.GRID_MIN_FIT
                    and (cons is None or cons >= pipeline.BASS_MIN_CONSISTENCY)):
                plan = (b, meter, downbeat)
        parts = [mx.Part("Piano", 0, 0, [mx.Note(s, e, p, v) for _, s, e, p, v in perf])]
        path = WORK / f"{key}.mid"
        if plan is None:
            grid = mx.TimeGrid(a["bpm"])
            placed, bars = fixed_placed, False
            mx.write_smf(parts, path, a["bpm"], label)
            problems = mx.verify_smf(path, parts, a["bpm"])
            used = "kept today's fixed tempo"
            ok = not problems                              # falling back safely is acceptable, not a win
        else:
            b, meter, downbeat = plan
            grid = mx.TimeGrid(a["bpm"], b, downbeat, meter)
            placed, bars = measure(perf, grid, per_bar)
            mx.write_smf(parts, path, a["bpm"], label, b, downbeat, meter)
            problems = mx.verify_smf(path, parts, a["bpm"], b, downbeat, meter)
            used = f"beat grid, {meter}/4, fit {pipeline.grid_fit(grid, notes):.2f}"
            ok = placed >= 0.90 and bars and not problems   # a grid that is used must be right
        rows.append((label, group, ok, plan is not None))
        print(f"{group:8s}  {label:46s} today {100 * fixed_placed:5.1f}%  ->  {100 * placed:5.1f}% placed, "
              f"bars {'right' if bars else ('n/a' if plan is None else 'WRONG')}, self-check {'ok' if not problems else 'FAILED'}"
              f"  [{used}]  {'PASS' if ok else 'FAIL'}")
    for group in ("tuning", "held out"):
        sel = [r for r in rows if r[1] == group]
        if not sel:
            continue
        print(f"{group}: {sum(r[2] for r in sel)} of {len(sel)} pass ({sum(r[3] for r in sel)} used the beat grid, "
              f"{sum(1 for r in sel if not r[3])} safely kept the fixed tempo)")
    shutil.rmtree(WORK, ignore_errors=True)            # rendered audio is large: never leave it behind
    sys.exit(0 if all(r[2] for r in rows) else 1)
