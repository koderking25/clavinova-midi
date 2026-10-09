"""How soon the app knows an update exists.

    .venv/bin/python tests/test_update_timing.py

Before this, the app asked GitHub once when it opened and then not again for six hours. With the
app left open, a release published in the morning was still unknown that evening. Nothing here
reaches GitHub: the request is stood in for.
"""
import json
import os
import shutil
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLAY = Path(tempfile.mkdtemp(prefix="update-timing-"))
os.environ["CLAVINOVA_STATE"] = str(PLAY / "state")
# Its own cache file and a made up address, so nothing here can touch the real one.
os.environ["CLAVINOVA_UPDATE_API"] = "https://example.invalid/release.json"
sys.path.insert(0, str(ROOT / "app"))
import updater  # noqa: E402

passed, failed = [], []
asked = []


def check(name, ok, detail=""):
    (passed if ok else failed).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{(': ' + detail) if detail else ''}")


class Answer:
    """A stand-in for GitHub's reply."""

    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()
        self.headers = {"ETag": '"abc"'}

    def read(self):
        return self.payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def pretend_github(version="99.0.0"):
    def fake(request, timeout=None):
        asked.append(time.time())
        return Answer({"tag_name": f"v{version}", "name": version, "body": "",
                       "html_url": "https://github.com/koderking25/clavinova-midi/releases",
                       "assets": [{"name": "Midify.dmg", "size": 400000,
                                   "browser_download_url":
                                       "https://github.com/koderking25/clavinova-midi/releases/download/v99/Midify.dmg",
                                   "digest": "sha256:" + "0" * 64}]})
    urllib.request.urlopen = fake


def main():
    # Running from the code has no version, and updates are off without one. Stand in for that, so
    # the timing can be tested at all.
    updater.current_version = lambda: "1.0.0"
    pretend_github()
    print("How often it looks on its own\n")
    check("it looks every half hour, not every six hours", updater.CHECK_EVERY == 30 * 60,
          f"{updater.CHECK_EVERY // 60} minutes")
    check("coming back to the window can prompt a look sooner", updater.FOCUS_GAP <= 5 * 60,
          f"{updater.FOCUS_GAP // 60} minutes")

    print("\nLooking while the app is open")
    asked.clear()
    updater.UPDATER.check()
    check("the first look asks GitHub", len(asked) == 1, f"{len(asked)} requests")
    updater.UPDATER.check()
    check("asking again straight away uses what it already knows", len(asked) == 1,
          f"{len(asked)} requests")

    cache_file = Path(os.environ["CLAVINOVA_STATE"]) / "update-test.json"
    saved = json.loads(cache_file.read_text())
    saved["checked_at"] = time.time() - (updater.CHECK_EVERY + 60)
    cache_file.write_text(json.dumps(saved))
    updater.UPDATER.check()
    check("half an hour later it asks again", len(asked) == 2, f"{len(asked)} requests")

    print("\nComing back to the window")
    asked.clear()
    saved = json.loads(cache_file.read_text())
    saved["checked_at"] = time.time() - (updater.FOCUS_GAP + 30)
    cache_file.write_text(json.dumps(saved))
    started = updater.check_soon("window")
    time.sleep(1.5)
    check("it looks when you come back after a while", started and len(asked) == 1,
          f"{len(asked)} requests")
    asked.clear()
    again = updater.check_soon("window")
    time.sleep(0.5)
    check("but not every time you switch apps", not again and not asked,
          f"{len(asked)} requests")

    print("\nWhen it knows, it says so")
    snap = updater.UPDATER.snapshot()
    check("the window is told a newer version exists", snap.get("latest") == "99.0.0", str(snap.get("latest")))
    check("and when it last looked", snap.get("checked_at") is not None)

    print("\nA look that fails must not stop the next one")
    asked.clear()

    def broken(request, timeout=None):
        asked.append(time.time())
        raise urllib.error.URLError("nodename nor servname provided")

    urllib.request.urlopen = broken
    saved = json.loads(cache_file.read_text())
    saved["checked_at"] = time.time() - (updater.CHECK_EVERY + 60)
    cache_file.write_text(json.dumps(saved))
    updater.UPDATER.check()
    check("a failed look is survived", len(asked) == 1)
    pretend_github()
    saved = json.loads(cache_file.read_text())
    saved["checked_at"] = time.time() - (updater.CHECK_EVERY + 60)
    cache_file.write_text(json.dumps(saved))
    updater.UPDATER.check()
    check("and the next one still happens", len(asked) == 2, f"{len(asked)} requests")

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
