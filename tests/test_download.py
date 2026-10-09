"""Downloading a song when YouTube refuses the first attempt.

    .venv/bin/python tests/test_download.py

From a real failure on 9 October 2026: "The download failed. Try a different result." The cause was
HTTP 403 on the media address. The very same video downloaded fine moments later, so giving up
after one attempt threw away a song that was there all along.

Nothing here touches YouTube: the downloader is stood in for, so the failures are exactly the ones
being tested.
"""
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "app"))
import sources  # noqa: E402

PLAY = Path(tempfile.mkdtemp(prefix="download-test-"))
passed, failed = [], []


def check(name, ok, detail=""):
    (passed if ok else failed).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{(': ' + detail) if detail else ''}")


class Pretend:
    """Stands in for the downloader: fails the first `fail_times` attempts, then works."""

    attempts = 0
    fail_times = 0
    message = "ERROR: unable to download video data: HTTP Error 403: Forbidden"
    dest = None

    def __init__(self, opts):
        self.opts = opts

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def download(self, urls):
        Pretend.attempts += 1
        if Pretend.attempts <= Pretend.fail_times:
            raise RuntimeError(Pretend.message)
        (Pretend.dest / "source.m4a").write_bytes(b"\x00" * 4096)


def run(fail_times, message=None):
    folder = Path(tempfile.mkdtemp(dir=PLAY))
    Pretend.attempts, Pretend.fail_times, Pretend.dest = 0, fail_times, folder
    if message:
        Pretend.message = message
    real_class, real_sleep, real_wait = sources.yt_dlp.YoutubeDL, time.sleep, sources.PACER.wait
    sources.yt_dlp.YoutubeDL = Pretend
    time.sleep = lambda s: None                       # no waiting about in a test
    sources.PACER.wait = lambda service: None
    try:
        got = sources.download_youtube_audio("hYH80WGPet8", folder)
        return Pretend.attempts, got, None
    except Exception as e:                            # noqa: BLE001
        return Pretend.attempts, None, e
    finally:
        sources.yt_dlp.YoutubeDL = real_class
        time.sleep = real_sleep
        sources.PACER.wait = real_wait
        Pretend.message = "ERROR: unable to download video data: HTTP Error 403: Forbidden"


def main():
    print("YouTube refuses once, then works (what actually happened)\n")
    tries, got, err = run(fail_times=1)
    check("the song is downloaded anyway", got is not None and Path(got).exists(), str(err or "")[:70])
    check("it took two attempts, not one", tries == 2, f"{tries} attempts")

    print("\nRefused twice, then works")
    tries, got, err = run(fail_times=2)
    check("still gets there", got is not None, str(err or "")[:70])
    check("on the third attempt", tries == 3, f"{tries} attempts")

    print("\nRefused every time")
    tries, got, err = run(fail_times=99)
    check("it gives up rather than trying for ever", err is not None and tries == sources.DOWNLOAD_ATTEMPTS,
          f"{tries} attempts")
    says = sources.friendly_download_error(str(err))
    check("and says something you can act on", "temporary" in says and "Update downloader" in says, says[:95])

    print("\nSomething that will never work, however many times it is asked")
    tries, got, err = run(fail_times=99, message="ERROR: Private video. Sign in if you've been granted access")
    check("a private video is not retried", tries == 1, f"{tries} attempt")
    check("and says what is wrong", "private" in sources.friendly_download_error(str(err)).lower(),
          sources.friendly_download_error(str(err)))

    print("\nHalf a file is never left behind to be mistaken for the song")
    folder = Path(tempfile.mkdtemp(dir=PLAY))
    Pretend.attempts, Pretend.fail_times, Pretend.dest = 0, 1, folder
    (folder / "source.part").write_bytes(b"half a song")
    real_class, real_sleep, real_wait = sources.yt_dlp.YoutubeDL, time.sleep, sources.PACER.wait
    sources.yt_dlp.YoutubeDL = Pretend
    time.sleep = lambda s: None
    sources.PACER.wait = lambda service: None
    try:
        got = sources.download_youtube_audio("hYH80WGPet8", folder)
        leftover = (folder / "source.part").exists()
        check("the leftover piece is cleared before trying again", not leftover,
              "a half file was still sitting there" if leftover else "")
        check("and the real file is what comes back", Path(got).name == "source.m4a", Path(got).name)
    finally:
        sources.yt_dlp.YoutubeDL = real_class
        time.sleep = real_sleep
        sources.PACER.wait = real_wait

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
