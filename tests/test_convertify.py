"""Convertify: a YouTube link in, an MP3 out.

    .venv/bin/python tests/test_convertify.py

Nothing here touches YouTube. The download is stood in for, so what is tested is Convertify's own
behaviour: naming, where files go, what it refuses, and that the finished file is shown to you.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLAY = Path(tempfile.mkdtemp(prefix="convertify-"))
os.environ.update(CONVERTIFY_SAVE=str(PLAY / "out"), CONVERTIFY_WORK=str(PLAY / "work"),
                  CONVERTIFY_STATE=str(PLAY / "state"))
sys.path.insert(0, str(ROOT / "convertify"))
sys.path.insert(0, str(ROOT / "app"))

import engine  # noqa: E402

passed, failed = [], []
revealed = []


def check(name, ok, detail=""):
    (passed if ok else failed).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{(': ' + detail) if detail else ''}")


def main():
    for folder in (engine.SAVE_TO, engine.WORK):
        folder.mkdir(parents=True, exist_ok=True)

    print("Naming the file\n")
    cases = {
        "Stephan Moccio - Fracture": "Stephan Moccio - Fracture",
        "AC/DC: Thunder\\Struck?": "AC DC Thunder Struck",
        "  spaces   everywhere  ": "spaces everywhere",
        "": "Audio",
        "..........": "Audio",
    }
    wrong = {bad: engine.safe_name(bad) for bad, want in cases.items() if engine.safe_name(bad) != want}
    check("titles become sensible file names", not wrong, str(wrong)[:110])

    # The property that matters is not what the name looks like, it is where the file ends up.
    nasty = ["../../etc/passwd", "a/../b", "/etc/hosts", "..", "....//....//x", "~/Desktop/thing",
             "name\u0000with a null"]
    escaped = [t for t in nasty
               if not str((engine.SAVE_TO / f"{engine.safe_name(t)}.mp3").resolve())
               .startswith(str(engine.SAVE_TO.resolve()) + "/")]
    check(f"no title can put a file outside your folder ({len(nasty)} tried)", not escaped,
          "escaped: " + ", ".join(escaped) if escaped else "")

    print("\nNot writing over something you already have")
    first = engine.unique(engine.SAVE_TO, "Song")
    first.write_bytes(b"one")
    second = engine.unique(engine.SAVE_TO, "Song")
    second.write_bytes(b"two")
    check("a second copy gets its own name", second.name == "Song 2.mp3", second.name)
    check("and the first is untouched", first.read_bytes() == b"one")

    print("\nWhat it refuses")
    from fastapi import HTTPException
    for link, why in (("https://www.youtube.com/playlist?list=PLabc", "a playlist"),
                      ("not a link at all", "plain words"),
                      ("https://example.com/watch?v=hYH80WGPet8", "another site")):
        try:
            engine.convert(engine.Ask(link=link, quality="320"))
            check(f"{why} is refused", False, "it was accepted")
        except HTTPException as e:
            check(f"{why} is refused", True, str(e.detail)[:58])
    try:
        engine.convert(engine.Ask(link="https://youtu.be/hYH80WGPet8", quality="9999"))
        check("a silly quality is refused", False, "it was accepted")
    except HTTPException as e:
        check("a silly quality is refused", True, str(e.detail))

    print("\nShowing you the file")
    engine.platform_bits.reveal = lambda path: revealed.append(Path(path).name) or True
    made = engine.SAVE_TO / "Shown.mp3"
    made.write_bytes(b"x" * 100)
    engine.reveal(engine.Which(file="Shown.mp3"))
    check("the finished file is picked out in Finder", revealed == ["Shown.mp3"], str(revealed))
    try:
        engine.reveal(engine.Which(file="../../../etc/hosts"))
        check("it will not show a file outside your folder", False, "it tried")
    except HTTPException as e:
        check("it will not show a file outside your folder", True, str(e.detail))

    print("\nMaking each kind of file")
    tone = PLAY / "tone.m4a"
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=20", "-c:a", "aac", str(tone)],
                   check=True, capture_output=True)

    def length_of(path):
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "csv=p=0", str(path)], capture_output=True, text=True).stdout
        return float(out.strip() or 0)

    import time
    speeds = {}
    for kind in ("m4a", "mp3", "wav", "flac", "aac"):
        out = PLAY / f"made{engine.FORMATS[kind]['ext']}"
        t0 = time.time()
        engine.convert_to(tone, out, kind, "192", "A Tone", "Convertify")
        speeds[kind] = time.time() - t0
        check(f"it can make a {kind.upper()}", out.is_file() and out.stat().st_size > 2000,
              f"{out.stat().st_size / 1000:.0f} KB in {speeds[kind]:.2f}s")
    check("M4A is the quick one, because nothing is re-encoded",
          speeds["m4a"] < speeds["mp3"], f"{speeds['m4a']:.2f}s against {speeds['mp3']:.2f}s")
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format_tags=title,artist",
                            "-of", "default=nw=1", str(PLAY / "made.mp3")],
                           capture_output=True, text=True).stdout
    check("the name and artist are written into the file", "title=A Tone" in probe and "artist=Convertify" in probe)

    print("\nOnly the first part of a long one")
    short = PLAY / "short.mp3"
    engine.convert_to(tone, short, "mp3", "192", "A Tone", "Convertify", limit_seconds=8)
    check("it stops where you asked", abs(length_of(short) - 8) < 0.6, f"{length_of(short):.1f}s of 20")
    check("the full one is still full", abs(length_of(PLAY / "made.mp3") - 20) < 0.6)

    print("\nMIDI is Midify's job, not Convertify's")
    real = engine.midify_python
    engine.midify_python = lambda: (None, None)
    try:
        engine.convert(engine.Ask(link="https://youtu.be/hYH80WGPet8", kind="midi"))
        check("without Midify, MIDI is refused kindly", False, "it was accepted")
    except HTTPException as e:
        check("without Midify, MIDI is refused kindly", "Midify" in str(e.detail), str(e.detail)[:70])
    engine.midify_python = real

    print("\nWhere things land")
    engine.remember(str(PLAY / "chosen"))
    (PLAY / "chosen").mkdir(exist_ok=True)
    check("it remembers the folder you picked", engine.remembered() == str(PLAY / "chosen"))
    engine.remember("/nowhere/at/all")
    check("and forgets one that has gone", engine.remembered() is None)
    try:
        engine.convert(engine.Ask(link="https://youtu.be/hYH80WGPet8", save_to="/nowhere/at/all"))
        check("a folder that is not there is refused", False, "it was accepted")
    except HTTPException as e:
        check("a folder that is not there is refused", True, str(e.detail)[:60])

    print("\nA whole conversion, with the download stood in for")
    engine.sources.video_info = lambda link: {"id": "x" * 11, "title": "Pretend Song",
                                              "duration": 20, "channel": "Nobody", "too_long": False}

    def fake_download(video_id, folder, on_progress=None, cancel=None, seconds=None):
        on_progress and on_progress(1.0)
        target = Path(folder) / "source.m4a"
        shutil.copyfile(tone, target)
        return target

    engine.sources.download_youtube_audio = fake_download
    revealed.clear()
    job = engine.Job(link="https://youtu.be/hYH80WGPet8", kind="flac", limit_seconds=5,
                     save_to=str(engine.SAVE_TO))
    engine.work_on(job)
    check("it finishes", job.status == "done", f"{job.status}: {job.error or ''}")
    check("in the format asked for", (job.file or "").endswith(".flac"), str(job.file))
    check("cut to the length asked for",
          abs(length_of(engine.SAVE_TO / job.file) - 5) < 0.6,
          f"{length_of(engine.SAVE_TO / (job.file or 'x')):.1f}s")
    check("and shown to you without being asked", revealed == [job.file], str(revealed))
    check("nothing is left in the working folder",
          not any(engine.WORK.iterdir()) if engine.WORK.exists() else True)

    print()
    print(f"{len(passed)} passed, {len(failed)} failed")
    for f in failed:
        print(f"  FAILED: {f}")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(PLAY, ignore_errors=True)
    sys.exit(code)
