"""Make a real song with Storage saver on, and measure what it costs the Mac.

    .venv/bin/python tests/test_storage_song.py

The claim being tested is "nothing grows on the computer". This makes an actual song with every
folder pointed at a fake flash drive (a disk image) and measures every place on the Mac that could
grow. It also pulls the drive out in the middle of a song, to see what Midify says about it.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLAY = Path(tempfile.mkdtemp(prefix="storage-song-"))
os.environ["CLAVINOVA_STATE"] = str(PLAY / "state")
os.environ["CLAVINOVA_TEST_DISK_IMAGES"] = "1"
sys.path.insert(0, str(ROOT / "app"))

import pretty_midi  # noqa: E402

import pipeline  # noqa: E402
import storage  # noqa: E402

GM = "/System/Library/Components/CoreAudio.component/Contents/Resources/gs_instruments.dls"
passed, failed = [], []


def check(name, ok, detail=""):
    (passed if ok else failed).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{(': ' + detail) if detail else ''}")


def attach(img):
    out = subprocess.run(["hdiutil", "attach", str(img), "-nobrowse"], check=True,
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        if "/Volumes/" in line:
            return Path(line.split("\t")[-1].strip())
    raise RuntimeError("could not mount the test image")


def detach(mount):
    subprocess.run(["hdiutil", "detach", str(mount), "-force"], capture_output=True)


def a_piano_clip(path, seconds=12):
    pm = pretty_midi.PrettyMIDI(initial_tempo=96)
    inst = pretty_midi.Instrument(program=0)
    tune = [60, 62, 64, 65, 67, 65, 64, 62]
    t = 0.0
    while t < seconds:
        p = tune[int(t * 2) % len(tune)]
        inst.notes.append(pretty_midi.Note(92, p, t, t + 0.45))
        inst.notes.append(pretty_midi.Note(70, p - 12, t, t + 0.45))
        t += 0.5
    pm.instruments.append(inst)
    mid = path.with_suffix(".mid")
    pm.write(str(mid))
    subprocess.run(["fluidsynth", "-ni", "-q", "-g", "0.8", "-r", "44100", "-F", str(path), GM, str(mid)],
                   check=True, capture_output=True)
    return path


def mac_sizes():
    """Every place on the Mac a song could leave something behind."""
    support = Path.home() / "Library" / "Application Support" / "Clavinova MIDI Maker"
    return {"your songs folder": storage.folder_size(Path.home() / "Music" / "Midify"),
            "the model cache": storage.folder_size(Path.home() / ".cache" / "huggingface"),
            "the app's working folder": storage.folder_size(support / "work"),
            "the compiled-code cache": storage.folder_size(support / "pycache"),
            "the repository's work folder": storage.folder_size(ROOT / "work")}


def main():
    img_base = PLAY / "drive"
    subprocess.run(["hdiutil", "create", "-size", "2g", "-type", "SPARSE", "-fs", "MS-DOS FAT32",
                    "-volname", "SONGDRV", "-layout", "MBRSPUD", str(img_base)], check=True, capture_output=True)
    img = Path(str(img_base) + ".sparseimage")
    mount = attach(img)
    # Copy the real model across (read only), so the song can actually be made from the drive.
    storage.HOME_LIBRARY = PLAY / "fake-songs"
    storage.HOME_LIBRARY.mkdir(parents=True)
    print(f"Fake drive at {mount}, test folders under {PLAY}\n")
    try:
        print("Turning Storage saver on")
        said = storage.switch("saver", mount, say=lambda m: None)
        check("Storage saver is on", storage.mode() == "saver", said[:90])
        storage.apply_to_process()
        where = storage.places()
        print(f"  the model was copied to the drive: {storage.folder_size(where['models']) / 1e6:.0f} MB")

        print("\nMaking a real song with everything on the drive")
        clip = a_piano_clip(PLAY / "clip.wav")
        before = mac_sizes()
        job = pipeline.Job(title="Storage Test", mode="piano", source="upload", upload_path=str(clip),
                           duration_hint=12, split_hands=True)
        started = time.time()
        result = pipeline.run_in_child(job, where["work"], where["library"])
        took = time.time() - started
        after = mac_sizes()

        made = where["library"] / (result or {}).get("file", "")
        check("the song was made", bool(result) and made.is_file(), f"{made.name} in {took:.0f}s")
        check("it was written to the drive, not the Mac", str(made).startswith(str(mount)), str(made))
        if made.is_file():
            import mido
            notes = sum(1 for t in mido.MidiFile(made).tracks for m in t if m.type == "note_on" and m.velocity > 0)
            check("it has notes in it", notes > 10, f"{notes} notes")

        print("\n  What the Mac gained while that song was made:")
        worst = 0
        for place, size in after.items():
            grew = size - before[place]
            worst = max(worst, grew)
            print(f"    {place:30s} {'+' if grew >= 0 else ''}{grew / 1e6:.1f} MB")
        check("nothing meaningful grew on the Mac", worst < 2_000_000, f"largest gain {worst / 1e6:.1f} MB")

        print("\nPulling the drive out in the middle of a song")
        longer = a_piano_clip(PLAY / "clip2.wav", seconds=40)
        job2 = pipeline.Job(title="Yank Test", mode="piano", source="upload", upload_path=str(longer),
                            duration_hint=40, split_hands=True)
        threading.Timer(6.0, lambda: detach(mount)).start()
        failure = None
        try:
            r2 = pipeline.run_in_child(job2, where["work"], where["library"])
            outcome = f"it finished anyway: {bool(r2)}"
        except Exception as e:                     # noqa: BLE001
            failure = e
            outcome = f"{type(e).__name__}: {e}"
        print(f"    what happened: {outcome[:160]}")
        if failure is not None:
            import health
            plain = health.describe_failure(failure, doing="making a song")
            check("it explains the lost drive in plain English", bool(plain) and len(plain) > 20, plain)
            check("the explanation has no programmer words in it",
                  not any(w in plain.lower() for w in ("traceback", "errno", "exception", "none type",
                                                       "childfailed", "filenotfound")), plain)
            check("it says the drive was unplugged, and what to do",
                  "unplugged" in plain.lower() and "plug it back in" in plain.lower(), plain)
        else:
            check("losing the drive mid-song did not crash the app", True,
                  "the song finished before the drive went away")
        check("Midify now knows the drive is gone", storage.remembered_drive() is None)
        check("and says so in plain English", "not plugged in" in (storage.blocked_reason() or ""),
              storage.blocked_reason() or "")
    finally:
        for v in ("/Volumes/SONGDRV",):
            if Path(v).exists():
                detach(Path(v))
        shutil.rmtree(PLAY, ignore_errors=True)
    print()
    print(f"{len(passed)} passed, {len(failed)} failed")
    for f in failed:
        print(f"  FAILED: {f}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
