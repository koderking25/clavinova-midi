"""Where does the time and the space go? A measuring tool, not a pass/fail test.

    .venv/bin/python tests/profile_songs.py PIANO_RECORDING BAND_RECORDING [SECONDS]

Runs a clip of each recording through the real pipeline in every mode it would use, times
every stage, reports what each finished song costs on disk, and lists every installed
Python package with its size and whether the app ever loaded it.
"""
import importlib.metadata as md
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections import defaultdict
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
WORK = Path(tempfile.mkdtemp(prefix="profile-"))
os.environ["CLAVINOVA_STATE"] = str(WORK / "state")
os.environ["CLAVINOVA_WORK"] = str(WORK / "work")
os.environ["CLAVINOVA_LIBRARY"] = str(WORK / "library")
sys.path.insert(0, str(REPO / "app"))

import drums  # noqa: E402
import midi_export as mx  # noqa: E402
import pipeline  # noqa: E402

TIMES = defaultdict(float)


def timed(module, name, label):
    fn = getattr(module, name)

    def wrapper(*a, **k):
        t = time.time()
        try:
            return fn(*a, **k)
        finally:
            TIMES[label] += time.time() - t
    setattr(module, name, wrapper)


for mod, name, label in [
    (pipeline, "decode_audio", "read the audio"), (pipeline, "separate", "separate instruments"),
    (pipeline, "run_transkun", "piano notes (transkun)"), (pipeline, "run_basic_pitch", "melody/bass (basic pitch)"),
    (drums, "transcribe_drums", "drums"), (pipeline, "detect_tempo", "tempo"),
    (mx, "write_smf", "write MIDI"), (mx, "verify_smf", "check MIDI"), (pipeline, "render_preview", "preview audio"),
]:
    timed(mod, name, label)


def clip(src, seconds):
    out = WORK / (Path(src).stem[:20] + ".wav")
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-ss", "20", "-t", str(seconds), "-i", src,
                    "-ac", "2", "-ar", "44100", str(out)], check=True)
    return out


def run(path, mode, seconds):
    TIMES.clear()
    job = pipeline.Job(title=f"{mode} test", mode=mode, source="upload", upload_path=str(path),
                       duration_hint=seconds)
    lib = WORK / "library"
    t = time.time()
    result = pipeline.process(job, WORK / "work", lib, lambda j: None)
    total = time.time() - t
    print(f"\n{mode} mode, {seconds} s of audio: {total:.1f} s total ({total / seconds:.2f} s per second of audio)")
    for label, secs in sorted(TIMES.items(), key=lambda kv: -kv[1]):
        print(f"  {secs:6.1f} s  {100 * secs / total:4.0f}%  {label}")
    stem = Path(result["file"]).stem
    mid = lib / result["file"]
    meta = lib / ".clavinova"
    print(f"  on disk: MIDI {mid.stat().st_size / 1e3:.0f} KB, preview "
          f"{(meta / (stem + '.mp3')).stat().st_size / 1e6:.2f} MB, details "
          f"{(meta / (stem + '.json')).stat().st_size / 1e3:.1f} KB")


def package_report():
    import desktop  # noqa: F401  the window code, not run
    import server  # noqa: F401
    import sources  # noqa: F401
    import updater  # noqa: F401
    import usb  # noqa: F401
    loaded = {m.split(".")[0] for m in list(sys.modules)}
    owners = md.packages_distributions()
    used = {d for top in loaded for d in owners.get(top, [])}
    rows = []
    for dist in md.distributions():
        name = dist.metadata["Name"]
        size = 0
        for f in dist.files or []:
            try:
                size += (dist.locate_file(f)).stat().st_size
            except OSError:
                pass
        rows.append((size, name, name in used))
    rows.sort(reverse=True)
    total = sum(r[0] for r in rows)
    unused = sum(r[0] for r in rows if not r[2])
    print(f"\nPython packages: {total / 1e6:.0f} MB installed, {unused / 1e6:.0f} MB of it never loaded by the app")
    for size, name, is_used in rows[:40]:
        print(f"  {size / 1e6:7.1f} MB  {'used    ' if is_used else 'NOT USED'}  {name}")


if __name__ == "__main__":
    piano, band = sys.argv[1], sys.argv[2]
    seconds = int(sys.argv[3]) if len(sys.argv) > 3 else 120
    pc, bc = clip(piano, seconds), clip(band, seconds)
    run(pc, "piano", seconds)
    run(bc, "arrange", seconds)
    run(bc, "band", seconds)
    package_report()

shutil.rmtree(WORK, ignore_errors=True)                  # rendered audio is large: never leave it behind
