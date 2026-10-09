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

    print("\nMaking the MP3 itself")
    tone = PLAY / "tone.wav"
    subprocess.run(["ffmpeg", "-nostdin", "-loglevel", "error", "-y", "-f", "lavfi",
                    "-i", "sine=frequency=440:duration=3", str(tone)], check=True, capture_output=True)
    out = engine.SAVE_TO / "Tone.mp3"
    engine.to_mp3(tone, out, "192", "A Tone", "Convertify")
    check("an MP3 comes out", out.is_file() and out.stat().st_size > 5000, f"{out.stat().st_size} bytes")
    probe = subprocess.run(["ffprobe", "-v", "error", "-show_entries",
                            "format=bit_rate:format_tags=title,artist", "-of", "default=nw=1", str(out)],
                           capture_output=True, text=True).stdout
    check("at the quality asked for", "19" in probe.split("bit_rate=")[1][:3], probe.split("\n")[0])
    check("with the name written into the file", "title=A Tone" in probe)
    check("and who it is by", "artist=Convertify" in probe)

    print("\nA whole conversion, with the download stood in for")
    engine.sources.video_info = lambda link: {"id": "x" * 11, "title": "Pretend Song",
                                              "duration": 3, "channel": "Nobody", "too_long": False}

    def fake_download(video_id, folder, on_progress=None, cancel=None, seconds=None):
        on_progress and on_progress(1.0)
        target = Path(folder) / "source.wav"
        shutil.copyfile(tone, target)
        return target

    engine.sources.download_youtube_audio = fake_download
    revealed.clear()
    job = engine.Job(link="https://youtu.be/hYH80WGPet8", quality="320")
    engine.work_on(job)
    check("it finishes", job.status == "done", f"{job.status}: {job.error or ''}")
    check("the file is named after the video", job.file == "Pretend Song.mp3", str(job.file))
    check("it is really there", (engine.SAVE_TO / (job.file or "x")).is_file())
    check("and it was shown to you without being asked", revealed == ["Pretend Song.mp3"], str(revealed))
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
