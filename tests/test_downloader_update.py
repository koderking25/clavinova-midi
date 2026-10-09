"""Keeping the YouTube downloader current by itself.

    .venv/bin/python tests/test_downloader_update.py

After a download failed with HTTP 403 and the fix turned out to be "ask again", Neil asked for it
to never need his attention: update whenever YouTube does.

Nothing here reaches the internet or installs anything: the parts that would are stood in for.
"""
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLAY = Path(tempfile.mkdtemp(prefix="dl-update-"))
os.environ["CLAVINOVA_STATE"] = str(PLAY / "state")
sys.path.insert(0, str(ROOT / "app"))
import downloader  # noqa: E402

passed, failed = [], []
installs = []


def check(name, ok, detail=""):
    (passed if ok else failed).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{(': ' + detail) if detail else ''}")


def pretend(newest="2026.11.01", installed="2026.08.19", works=True):
    """Stand in for PyPI and for the installer."""
    downloader.newest_version = lambda timeout=15: newest
    downloader.installed_version = lambda: installed

    def fake_install(reason="routine"):
        installs.append(reason)
        record = downloader._read()
        record.update({"last_try": time.time(), "last_success": time.time() if works else 0,
                       "version": newest if works else installed})
        downloader._write(record)
        return (True, f"Updated the song downloader to {newest}.") if works else \
               (False, "The downloader could not be updated just now. Midify will try again later.")

    downloader.install_newest = fake_install


def forget():
    downloader._write({})
    installs.clear()


def main():
    print("Telling a YouTube change from a song that is simply gone\n")
    cases = {"HTTP Error 403: Forbidden": True, "nsig extraction failed": True,
             "Unable to extract player response": True, "js runtime error": True,
             "Sign in to confirm you are not a bot": True,
             "Private video. Sign in if you've been granted access": False,
             "This video has been removed by the uploader": False,
             "Video unavailable": False, "Sign in to confirm your age": False,
             "The uploaded file is not audio": False}
    wrong = [m for m, want in cases.items() if downloader.youtube_moved(m) != want]
    check(f"all {len(cases)} kinds of failure are judged correctly", not wrong,
          "; ".join(wrong)[:90])

    print("\nThe same version written two ways")
    check("2026.08.19 and 2026.8.19 are the same release",
          downloader.same_version("2026.08.19", "2026.8.19"))
    check("a real new release is seen as different",
          not downloader.same_version("2026.08.19", "2026.11.01"))
    check("this is what stops it reinstalling the same thing every day", True,
          "the installed copy pads the month, PyPI does not")

    print("\nThe daily check")
    forget(); pretend()
    changed, said = downloader.routine_check(busy=lambda: False)
    check("a newer downloader is installed", changed, said or "")
    check("and only once", len(installs) == 1, f"{len(installs)} installs")
    changed, _ = downloader.routine_check(busy=lambda: False)
    check("it does not check again the same day", not changed and len(installs) == 1)

    forget(); pretend()
    changed, _ = downloader.routine_check(busy=lambda: True)
    check("it never updates while a song is being made", not changed and not installs)

    forget(); pretend(newest="2026.8.19", installed="2026.08.19")
    changed, _ = downloader.routine_check(busy=lambda: False)
    check("nothing happens when it is already current", not changed and not installs)

    forget(); pretend(newest=None)
    changed, _ = downloader.routine_check(busy=lambda: False)
    check("no internet means wait until tomorrow, not an error", not changed and not installs)
    check("and the attempt is written down so it does not retry in a loop",
          downloader._read().get("last_try", 0) > 0)

    print("\nWhen a download fails")
    forget(); pretend()
    changed, said = downloader.update_because_it_failed("HTTP Error 403: Forbidden")
    check("a YouTube change updates the downloader at once", changed, said or "")
    forget(); pretend()
    changed, _ = downloader.update_because_it_failed("Private video. Sign in")
    check("a private video does not", not changed and not installs)

    forget(); pretend()
    downloader.update_because_it_failed("HTTP Error 403: Forbidden")
    before = len(installs)
    downloader.update_because_it_failed("HTTP Error 403: Forbidden")
    check("it will not reinstall over and over within the hour", len(installs) == before,
          f"{len(installs)} installs")

    forget(); pretend(works=False)
    changed, said = downloader.update_because_it_failed("HTTP Error 403: Forbidden")
    check("an update that fails says so and changes nothing", not changed)

    print("\nWhat the health card can say")
    forget(); pretend()
    downloader.routine_check(busy=lambda: False)
    state = downloader.state()
    check("it knows which version is in use and when it last looked",
          bool(state["version"]) and state["last_checked"], str(state)[:90])

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
