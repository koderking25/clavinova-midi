"""What the readable score does on real recordings. Not pass/fail: real songs have no answer key.

    .venv/bin/python tests/real_grid_check.py

A minute of each recording is transcribed as solo piano. For each it reports whether the beat grid
was used, the bar length and BPM it chose, and the two confidence checks, and it writes and
self-checks the MIDI file. A few meters are known for certain and are marked.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "app"))
import midi_export as mx  # noqa: E402
import pipeline  # noqa: E402

D = Path.home() / "Downloads"
SONGS = [
    ("Andrea Vanzo - Valzer d'Inverno.mp3", "3/4 (it is a waltz)"),
    ("Yiruma - River Flows in You 4.mp3", "4/4"),
    ("Debussy - Clair de Lune 4.mp3", "9/8, which this cannot write: falling back is right"),
    ("Ludovico Einaudi - Experience 4.mp3", None),
    ("Mia & Sebastian’s Theme.mp3", None),
    ("Gibran Alcocer - Idea 10 (Sheet Music).mp3", None),
    ("Tony Ann – ICARUS [Virtuosic Piano Solo].mp3", None),
]
WORK = Path(tempfile.mkdtemp(prefix="real-grid-"))

for name, known in SONGS:
    src = D / name
    if not src.exists():
        print(f"missing: {name}")
        continue
    clip = WORK / "clip.wav"
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-ss", "30", "-t", "60", "-i", str(src),
                    "-ac", "2", "-ar", "44100", str(clip)], check=True)
    x = pipeline.decode_audio(clip, WORK / "dec.wav")
    notes, pedal = pipeline.run_transkun(x)
    bpm = pipeline.detect_tempo(x)
    raw = pipeline.detect_beats(x, bpm)
    plan = pipeline.plan_score_grid(x, notes, bpm)
    detail = ""
    if raw is not None:
        b, per_bar, down = pipeline.choose_meter(pipeline.align_beats(raw, notes), notes)
        b = pipeline.align_beats(b, notes)
        g = mx.TimeGrid(bpm, b, down, per_bar)
        cons = pipeline.bass_consistency(g, notes)
        detail = (f"candidate {per_bar}/4, fit {pipeline.grid_fit(g, notes):.2f}, bass consistency "
                  f"{'n/a' if cons is None else f'{cons:.2f}'}")
    else:
        detail = "beats not trustworthy"
    parts = [mx.Part("Piano", 0, 0, notes, pedal)]
    out = WORK / "song.mid"
    if plan:
        b, per_bar, down = plan
        grid_bpm = mx.TimeGrid(bpm, b, down, per_bar).first_bpm()
        mx.write_smf(parts, out, grid_bpm, name, b, down, per_bar)
        problems = mx.verify_smf(out, parts, grid_bpm, b, down, per_bar)
        used = f"BEAT GRID {per_bar}/4 at about {grid_bpm:.0f} BPM"
    else:
        mx.write_smf(parts, out, bpm, name)
        problems = mx.verify_smf(out, parts, bpm)
        used = f"fixed tempo {bpm:.0f} BPM"
    print(f"{name[:44]:44s} {used:34s} [{detail}]  self-check {'ok' if not problems else problems}"
          + (f"\n{'':44s} known meter: {known}" if known else ""))

shutil.rmtree(WORK, ignore_errors=True)                  # rendered audio is large: never leave it behind
