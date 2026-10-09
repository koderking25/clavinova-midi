"""Keeping the YouTube downloader current, without anyone having to think about it.

YouTube changes how it serves video every few weeks, and yt-dlp puts out a release to match,
often within days. A copy that is a month old starts refusing downloads with errors that look
like the song is at fault when it is not.

So two things happen here. Once a day, quietly, Midify looks for a newer downloader and installs
it. And when a download fails in a way that says YouTube moved something, it updates there and
then and tries the song once more, instead of handing back an error nobody can act on.

Neither ever interrupts a song: an update waits until nothing is being made.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

STATE = Path(os.environ.get("CLAVINOVA_STATE", Path(__file__).resolve().parent.parent / "state"))
RECORD = STATE / "downloader.json"
PYPI = "https://pypi.org/pypi/yt-dlp/json"

CHECK_EVERY = 24 * 60 * 60          # a day between routine checks
AFTER_FAILURE_GAP = 60 * 60         # at most one emergency update an hour, whatever goes wrong
TIMEOUT = 300

# What a download failure looks like when YouTube has changed something, rather than the song
# being private or removed. These are worth updating the downloader for; nothing else is.
YOUTUBE_MOVED = ("403", "forbidden", "nsig", "signature", "js runtime", "jsinterp",
                 "player response", "unable to extract", "challenge", "precondition check failed",
                 "sign in to confirm you", "format is not available")


def _read():
    try:
        return json.loads(RECORD.read_text())
    except Exception:                                      # noqa: BLE001
        return {}


def _write(data):
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        tmp = RECORD.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(RECORD)
    except Exception:                                      # noqa: BLE001
        pass


def installed_version():
    try:
        import yt_dlp
        return yt_dlp.version.__version__
    except Exception:                                      # noqa: BLE001
        return "unknown"


def youtube_moved(message):
    """Does this failure mean YouTube changed something, as opposed to the song being unavailable?"""
    low = (message or "").lower()
    # Checked first, and deliberately includes the age one: "sign in to confirm your age" would
    # otherwise match the bot check below and update the downloader over a video that is simply
    # age-restricted.
    if any(sure in low for sure in ("private video", "has been removed", "video unavailable",
                                    "age-restricted", "not available in your country",
                                    "confirm your age", "members-only", "is live")):
        return False
    return any(sign in low for sign in YOUTUBE_MOVED)


def same_version(a, b):
    """Is this the same release, written two ways?

    The installed copy says 2026.08.19 and PyPI says 2026.8.19. Comparing the text would call
    those different every day and reinstall the same thing for ever."""
    def parts(v):
        out = []
        for piece in (v or "").replace("-", ".").split("."):
            out.append(int(piece) if piece.isdigit() else piece)
        return out
    return bool(a) and bool(b) and parts(a) == parts(b)


def newest_version(timeout=15):
    """What the newest downloader is called, or None if the question cannot be answered."""
    try:
        with urllib.request.urlopen(PYPI, timeout=timeout) as answer:
            return json.load(answer)["info"]["version"]
    except Exception:                                      # noqa: BLE001
        return None


def install_newest(reason="routine"):
    """Install the newest downloader. Returns (changed, what to say about it)."""
    before = installed_version()
    uv = shutil.which("uv")
    cmd = ([uv, "pip", "install", "--python", sys.executable, "-U", "yt-dlp[default]"] if uv
           else [sys.executable, "-m", "pip", "install", "-U", "yt-dlp[default]"])
    try:
        run = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        return False, "The downloader update took too long and was left alone."
    record = _read()
    record.update({"last_try": time.time(), "reason": reason})
    if run.returncode != 0:
        record["last_error"] = (run.stderr or "")[-300:]
        _write(record)
        return False, "The downloader could not be updated just now. Midify will try again later."
    after = _version_after_install()
    record.update({"last_success": time.time(), "version": after})
    record.pop("last_error", None)
    _write(record)
    if after and not same_version(after, before):
        return True, f"Updated the song downloader to {after}."
    return False, "The song downloader was already the newest one."


def _version_after_install():
    """Ask a fresh process, because this one already has the old downloader loaded."""
    try:
        out = subprocess.run([sys.executable, "-c",
                              "import yt_dlp;print(yt_dlp.version.__version__)"],
                             capture_output=True, text=True, timeout=60)
        return out.stdout.strip() or None
    except Exception:                                      # noqa: BLE001
        return None


def due_for_routine_check(now=None):
    now = now or time.time()
    return (now - _read().get("last_try", 0)) >= CHECK_EVERY


def allowed_after_failure(now=None):
    now = now or time.time()
    return (now - _read().get("last_try", 0)) >= AFTER_FAILURE_GAP


def routine_check(busy=lambda: False, note=None):
    """Once a day: if there is a newer downloader, install it. Never while a song is being made."""
    if not due_for_routine_check() or busy():
        return False, None
    newest = newest_version()
    if not newest:
        record = _read()
        record["last_try"] = time.time()                   # no internet; ask again tomorrow
        _write(record)
        return False, None
    if same_version(newest, installed_version()):
        record = _read()
        record.update({"last_try": time.time(), "version": newest})
        _write(record)
        return False, None
    changed, said = install_newest("newer version available")
    if changed and note:
        note(said)
    return changed, said


def update_because_it_failed(message, busy=lambda: False, note=None):
    """A download failed in a way that says YouTube moved something. Update now, if allowed."""
    if not youtube_moved(message) or busy() or not allowed_after_failure():
        return False, None
    changed, said = install_newest("a download failed")
    if note:
        note(said if changed else f"A download failed and the downloader was already current. {message[:90]}")
    return changed, said


def state():
    """For the health card."""
    record = _read()
    return {"version": installed_version(), "last_checked": record.get("last_try"),
            "last_updated": record.get("last_success"), "last_error": record.get("last_error")}
