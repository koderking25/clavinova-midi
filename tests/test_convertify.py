"""Convertify: a YouTube link in, an MP3 out.

    .venv/bin/python tests/test_convertify.py

Nothing here touches YouTube. The download is stood in for, so what is tested is Convertify's own
behaviour: naming, where files go, what it refuses, and that the finished file is shown to you.
"""
import os
import shutil
import time
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

    print("\nA search result stands in for the lookup")
    sources = engine.sources
    vid = "abcdefghijk"
    sources._cache.pop(("ytinfo", vid), None)
    sources.remember_video({"id": vid, "title": "Found By Searching", "duration": 123,
                            "channel": "Someone", "thumb": "https://i.ytimg.com/vi/x/mqdefault.jpg"})
    before = dict(sources._cache)
    got = sources.video_info(vid)        # must not touch the network at all
    check("picking a result does not look the same video up again",
          got["title"] == "Found By Searching" and got["duration"] == 123, str(got)[:70])
    check("and nothing else was fetched to answer it", len(sources._cache) == len(before))
    sources.remember_video({"id": "nodurationxx", "title": "No Length", "duration": None})
    check("a result with no length is not trusted as the answer",
          ("ytinfo", "nodurationxx") not in sources._cache)

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
    check("the job's own working folder is cleared away",
          not (engine.WORK / job.id).exists())

    print("\nThe encoders")
    check("it picks Apple's AAC encoder when this Mac has it",
          engine.aac_encoder() in ("aac_at", "aac"), engine.aac_encoder())
    was = engine.aac_encoder
    try:
        engine.aac_encoder = lambda: "aac_at_pretend_this_is_broken"
        out = engine.SAVE_TO / "fallen-back.aac"
        engine.convert_to(tone, out, "aac", "192", "T", "A")
        check("and falls back to the one that always works if it is refused",
              out.exists() and out.stat().st_size > 0)
    finally:
        engine.aac_encoder = was

    print("\nHistory")
    rows = engine.history()
    check("the conversion that just finished is written down", len(rows) == 1, f"{len(rows)} rows")
    one = rows[0] if rows else {}
    check("with what it was", one.get("kind") == "flac", str(one.get("kind")))
    check("what it is called", one.get("title") == "Pretend Song", str(one.get("title")))
    check("where it went", one.get("folder") == str(engine.SAVE_TO), str(one.get("folder"))[:40])
    check("and that the file is really there", one.get("still_there") is True)
    check("it remembers the link, so it can be made again", "youtu" in (one.get("link") or ""))

    moved = engine.SAVE_TO / one["file"]
    kept_name = moved.name
    moved.rename(engine.SAVE_TO / "somewhere else.flac")
    check("a file you moved is shown as moved, not pretended to be there",
          engine.history()[0]["still_there"] is False)
    (engine.SAVE_TO / "somewhere else.flac").rename(engine.SAVE_TO / kept_name)

    engine.forget(engine.Forget(id=one["id"]))
    check("forgetting takes it off the list", len(engine.history()) == 0)
    check("but never touches the file itself", (engine.SAVE_TO / kept_name).is_file())

    # it has to survive the app being closed and opened
    job2 = engine.Job(link="https://youtu.be/hYH80WGPet8", kind="mp3", save_to=str(engine.SAVE_TO))
    job2.title = "Written Down"
    engine.note_in_history(job2, engine.SAVE_TO / kept_name)
    check("what is written down is on disk, not just in memory",
          (engine.STATE / "history.json").is_file())
    check("and reads back after a restart",
          engine.read_history()[0]["title"] == "Written Down")

    for n in range(engine.HISTORY_MOST + 20):
        j = engine.Job(link="https://youtu.be/hYH80WGPet8", kind="mp3", save_to=str(engine.SAVE_TO))
        j.title = f"Song {n}"
        engine.note_in_history(j, engine.SAVE_TO / kept_name)
    check("the list does not grow without end",
          len(engine.read_history()) == engine.HISTORY_MOST, str(len(engine.read_history())))
    engine.forget(engine.Forget(all=True))
    check("and it can all be cleared at once", len(engine.history()) == 0)

    print("\nThe same video, a second format")
    # The download is the slow part. Asking for the same video again should not pay for it twice.
    fetches = []

    def counted_download(video_id, folder, on_progress=None, cancel=None, seconds=None):
        fetches.append(video_id)
        return fake_download(video_id, folder, on_progress, cancel, seconds)

    engine.sources.download_youtube_audio = counted_download
    again = engine.Job(link="https://youtu.be/hYH80WGPet8", kind="wav", limit_seconds=5,
                       save_to=str(engine.SAVE_TO))
    engine.work_on(again)
    check("it finishes", again.status == "done", f"{again.status}: {again.error or ''}")
    check("in the second format", (again.file or "").endswith(".wav"), str(again.file))
    check("and it did not download it again", fetches == [], f"{len(fetches)} downloads")

    third = engine.Job(link="https://youtu.be/hYH80WGPet8", kind="mp3", save_to=str(engine.SAVE_TO))
    third.seconds = 20
    engine.work_on(third)
    check("a third format is still free", fetches == [], f"{len(fetches)} downloads")

    # A long recording is only allowed through when part of it is being taken. The held copy must
    # not become a side door to converting the whole of something too long.
    vid = engine.sources.youtube_id("https://youtu.be/hYH80WGPet8")
    with engine.KEEP_LOCK:
        engine.KEPT[vid]["seconds"] = engine.sources.MAX_SONG_SECONDS + 600
    check("a long one held back for a trim is not reused whole",
          engine.kept_source(vid, None) is None)
    check("but it is still there for another trim",
          engine.kept_source(vid, 300) is not None)

    # Stale copies go, so the disk does not fill up with videos nobody asked for again.
    with engine.KEEP_LOCK:
        engine.KEPT[vid]["when"] = time.time() - engine.KEEP_FOR - 5
    engine.tidy_kept()
    check("and it is thrown out once it is stale", engine.kept_source(vid, 300) is None)
    check("with its file removed too", not (engine.WORK / "kept" / vid).exists())

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
