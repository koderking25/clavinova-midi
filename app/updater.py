"""Keep the Mac app up to date from the GitHub releases page.

The one rule: a failed update never leaves you without a working app.

  check    Ask GitHub for the latest release, at most every 6 hours on its own. Repeat
           checks send the last answer's ETag, and GitHub does not count those against its
           limit of 60 an hour. If GitHub says slow down, the app waits an hour.
  download The disk image goes to Application Support, and must match the size and the
           SHA-256 fingerprint GitHub records for that file, or it is thrown away.
  inspect  The app inside is opened read only: it must be this app (bundle id), exactly the
           version the release says, carry an intact signature, and hold its own code.
  stage    It is copied next to the running app, into a hidden folder on the same disk.
  swap     A small script waits for the app to quit, moves the old app aside, moves the new
           one in, checks its signature, and only then deletes the old one. Any step that
           fails puts the old app back. Then it reopens the app.

Nothing is installed while running from the code (no .app around this file).
"""
import hashlib
import json
import os
import plistlib
import shutil
import subprocess
import tempfile
import threading
import socket
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = "koderking25/clavinova-midi"
ASSET = "Midify.dmg"                 # releases also carry the old name, for apps from before the rename
LEGACY_ASSET = "Clavinova-MIDI-Maker.dmg"
BUNDLE_ID = "com.koderking25.clavinova-midi-maker"
# Only a test sets CLAVINOVA_UPDATE_API, to install a local build end to end. A normal launch
# never has it, so the app only ever looks at this repository's real releases.
TEST_API = os.environ.get("CLAVINOVA_UPDATE_API")
API = TEST_API or f"https://api.github.com/repos/{REPO}/releases/latest"
CHECK_EVERY = 6 * 3600
MANUAL_GAP = 60
BACKOFF = 3600
MAX_DOWNLOAD = 50 * 1024 * 1024

HERE = Path(__file__).resolve().parent
STATE = Path(os.environ.get("CLAVINOVA_STATE", HERE.parent / "state"))
# A test release never touches the app's own memory of updates or its downloads. It once did, and
# left the real app remembering a test file that no longer existed, so every update "failed".
CACHE = STATE / ("update-test.json" if TEST_API else "update.json")
DOWNLOADS = STATE / ("updates-test" if TEST_API else "updates")
LOG = Path.home() / "Library" / "Logs" / "Midify.log"

SWAP_SCRIPT = r"""#!/bin/bash
PID="$1"; CURRENT="$2"; STAGED="$3"; BACKUP="$4"; LOG="$5"; RELAUNCH="$6"
log() { echo "$(date '+%Y-%m-%d %H:%M:%S') updater: $*" >> "$LOG"; }
# Running means present and not a zombie. A process that has exited but not been collected
# by its parent still answers "kill -0", which once made this wait a full minute for nothing.
alive() { local s; s=$(ps -o stat= -p "$1" 2>/dev/null | tr -d ' '); [ -n "$s" ] && [ "${s#Z}" = "$s" ]; }
for _ in $(seq 1 120); do alive "$PID" || break; sleep 0.5; done
if alive "$PID"; then
  log "the app did not quit within a minute, so the update was not installed"; rm -rf "$STAGED"; exit 1
fi
rm -rf "$BACKUP"
if ! mv "$CURRENT" "$BACKUP"; then
  log "could not move the old app aside; nothing was changed"; rm -rf "$STAGED"; exit 1
fi
if ! mv "$STAGED" "$CURRENT"; then
  log "could not put the new app in place; restoring the old one"; mv "$BACKUP" "$CURRENT"; exit 1
fi
if ! codesign --verify "$CURRENT" 2>/dev/null; then
  log "the new app failed its signature check; restoring the old one"; rm -rf "$CURRENT"; mv "$BACKUP" "$CURRENT"; exit 1
fi
rm -rf "$BACKUP"
log "updated to $(plutil -extract CFBundleShortVersionString raw "$CURRENT/Contents/Info.plist" 2>/dev/null)"
# -n launches the copy just installed, rather than switching to some other window of an app
# with the same identity. After a real swap nothing else is running, so it costs nothing.
[ "$RELAUNCH" = "1" ] && open -n "$CURRENT"
exit 0
"""


class UpdateError(Exception):
    """A reason to show the user, in plain words."""


class DownloadFailed(UpdateError):
    """The update file could not be fetched. `network` says whether the connection was the problem,
    so "check the internet" is only ever said when it is true. `detail` goes to the log."""

    def __init__(self, message, network=False, detail=""):
        super().__init__(message)
        self.network, self.detail = network, detail


def trusted_url(url):
    """Updates only ever come from GitHub over HTTPS (a test release excepted)."""
    if TEST_API:
        return True
    u = urllib.parse.urlparse(url or "")
    host = (u.hostname or "").lower()
    return u.scheme == "https" and (host == "github.com" or host.endswith(".githubusercontent.com"))


def _is_network(exc):
    reason = getattr(exc, "reason", exc)
    return isinstance(reason, (socket.gaierror, socket.timeout, TimeoutError, ConnectionError)) or \
        isinstance(exc, (socket.timeout, TimeoutError, ConnectionError))


def parse_version(text):
    text = (text or "").strip().lstrip("vV")
    parts = []
    for piece in text.split("."):
        digits = "".join(ch for ch in piece if ch.isdigit())
        if not digits:
            return None
        parts.append(int(digits))
    return tuple(parts) if parts else None


def bundle_path():
    """The .app this code is running inside, or None when running from the code."""
    b = HERE.parents[2] if len(HERE.parents) >= 3 else None       # app -> Resources -> Contents -> X.app
    if b and b.suffix == ".app" and (b / "Contents" / "Info.plist").exists():
        return b
    return None


def read_plist(app):
    with open(Path(app) / "Contents" / "Info.plist", "rb") as f:
        return plistlib.load(f)


def current_version():
    b = bundle_path()
    if not b:
        return None
    try:
        return read_plist(b).get("CFBundleShortVersionString")
    except Exception:  # noqa: BLE001
        return None


def _log(msg):
    try:
        with open(LOG, "a") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") + f" updater: {msg}\n")
    except OSError:
        pass


class Updater:
    def __init__(self):
        self.lock = threading.Lock()
        self.quit_hook = None                 # set by the window; without it nothing installs
        self.status = {"state": "idle", "current": current_version(), "latest": None, "notes_url": None,
                       "progress": 0.0, "error": None, "checked_at": None}
        self._release = None
        self._last_manual = 0.0

    # ---------- state ----------
    def snapshot(self):
        with self.lock:
            s = dict(self.status)
        s["can_install"] = bool(bundle_path()) and self.quit_hook is not None
        return s

    def _set(self, **kw):
        with self.lock:
            self.status.update(kw)

    def _load_cache(self):
        try:
            return json.loads(CACHE.read_text())
        except Exception:  # noqa: BLE001
            return {}

    def _save_cache(self, data):
        try:
            STATE.mkdir(parents=True, exist_ok=True)
            tmp = CACHE.with_suffix(".tmp")
            tmp.write_text(json.dumps(data))
            os.replace(tmp, CACHE)
        except OSError:
            pass

    # ---------- check ----------
    def check(self, manual=False):
        cur = current_version()
        self._set(current=cur)
        if not cur:
            self._set(state="unsupported", error=None)
            return self.snapshot()
        if self.status["state"] in ("downloading", "installing"):
            return self.snapshot()
        now = time.time()
        cache = self._load_cache()
        cached = cache.get("release") or {}
        if cached and not trusted_url(cached.get("url")):
            cache = {k: v for k, v in cache.items() if k == "backoff_until"}   # never trust it: ask GitHub again
        if now < cache.get("backoff_until", 0):
            self._apply_release(cache.get("release"), cur, cache.get("checked_at"))
            if manual:
                mins = int((cache["backoff_until"] - now) / 60) + 1
                self._set(error=f"GitHub asked for a pause. Try again in about {mins} minutes.")
            return self.snapshot()
        if manual:
            if now - self._last_manual < MANUAL_GAP:
                self._apply_release(cache.get("release"), cur, cache.get("checked_at"))
                return self.snapshot()
            self._last_manual = now
        elif now - cache.get("checked_at", 0) < CHECK_EVERY:
            self._apply_release(cache.get("release"), cur, cache.get("checked_at"))
            return self.snapshot()

        self._set(state="checking", error=None)
        headers = {"Accept": "application/vnd.github+json", "User-Agent": f"Midify/{cur}"}
        if cache.get("etag") and cache.get("release"):
            headers["If-None-Match"] = cache["etag"]
        try:
            with urllib.request.urlopen(urllib.request.Request(API, headers=headers), timeout=15) as r:
                data = json.loads(r.read().decode("utf-8"))
                etag = r.headers.get("ETag")
            release = self._pick(data)
            cache.update(release=release, etag=etag, checked_at=now)
        except urllib.error.HTTPError as e:
            if e.code == 304:                                  # nothing new since the last check
                release = cache.get("release")
                cache["checked_at"] = now
            elif e.code in (403, 429):
                cache["backoff_until"] = now + BACKOFF
                self._save_cache(cache)
                self._apply_release(cache.get("release"), cur, cache.get("checked_at"))
                self._set(error="GitHub asked for a pause. The app will check again later." if manual else None)
                return self.snapshot()
            else:
                self._set(state="error" if manual else "idle",
                          error="Could not reach GitHub to check for updates." if manual else None)
                return self.snapshot()
        except Exception:  # noqa: BLE001
            self._set(state="error" if manual else "idle",
                      error="Could not check for updates. Is the internet connected?" if manual else None)
            return self.snapshot()
        self._save_cache(cache)
        self._apply_release(release, cur, now)
        return self.snapshot()

    @staticmethod
    def _pick(data):
        if data.get("draft") or data.get("prerelease"):
            return None
        assets = data.get("assets") or []
        # Midify's file first; the old name too, so a release from before the rename still counts.
        asset = next((a for name in (ASSET, LEGACY_ASSET) for a in assets if a.get("name") == name), None)
        if not asset or not parse_version(data.get("tag_name")) or not trusted_url(asset.get("browser_download_url")):
            return None
        return {"tag": data["tag_name"], "url": asset.get("browser_download_url"), "size": asset.get("size"),
                "digest": asset.get("digest"), "notes_url": data.get("html_url")}

    def _apply_release(self, release, cur, checked_at):
        self._release = release
        latest = release["tag"].lstrip("vV") if release else None
        newer = bool(release and parse_version(latest) and parse_version(cur)
                     and parse_version(latest) > parse_version(cur))
        self._set(state="available" if newer else "up_to_date", latest=latest,
                  notes_url=release.get("notes_url") if release else None, checked_at=checked_at)

    # ---------- install ----------
    def start_install(self):
        with self.lock:
            if self.status["state"] in ("downloading", "installing"):
                return
            # "error" is allowed too, so "Try again" works after a failed download.
            cur, rel = parse_version(current_version()), self._release
            newer = bool(rel and cur and parse_version(rel["tag"]) and parse_version(rel["tag"]) > cur)
            if self.status["state"] not in ("available", "error") or not newer:
                raise UpdateError("There is no update to install.")
            if not bundle_path() or self.quit_hook is None:
                raise UpdateError("Updates install from the Mac app itself.")
            self.status.update(state="downloading", progress=0.0, error=None)
        threading.Thread(target=self._install, daemon=True).start()

    def refresh_release(self):
        """Ask GitHub again right now, ignoring what was remembered. Used when a download fails,
        in case the remembered release is out of date."""
        cache = self._load_cache()
        cache.pop("etag", None)
        cache["checked_at"] = 0
        self._save_cache(cache)
        self._last_manual = 0
        state = self.status["state"]
        self._set(state="idle")
        self.check(manual=True)
        fresh = self._release
        self._set(state=state)
        return fresh

    def prepare_with_retry(self, on_progress=None, target=None):
        """Download and stage. If the download fails, ask GitHub again and try once more, so a stale
        or expired download address never ends the update."""
        try:
            return self.prepare(self._release, on_progress=on_progress, target=target)
        except DownloadFailed as first:
            _log(f"download failed ({first.detail or first}); asking GitHub again and retrying once")
            fresh = self.refresh_release()
            cur = parse_version(current_version())
            if not fresh or not cur or not parse_version(fresh["tag"]) or parse_version(fresh["tag"]) <= cur:
                raise first
            return self.prepare(fresh, on_progress=on_progress, target=target)

    def _install(self):
        try:
            staged = self.prepare_with_retry(on_progress=lambda p: self._set(progress=round(p, 3)))
            self._set(state="installing", progress=1.0)
            self.hand_over(staged)
        except UpdateError as e:
            _log(f"update failed: {e}")
            self._set(state="error", error=str(e))
        except Exception as e:  # noqa: BLE001
            _log(f"update failed unexpectedly: {type(e).__name__}: {e}")
            self._set(state="error", error="The update did not install. Your app is unchanged. Try again later.")

    def prepare(self, release, on_progress=None, target=None):
        """Download, verify, inspect and stage. Returns the staged app path. Touches nothing live."""
        target = Path(target) if target else bundle_path()
        if not target:
            raise UpdateError("Updates install from the Mac app itself.")
        if not (os.access(target.parent, os.W_OK) and os.access(target, os.W_OK)):
            raise UpdateError(f"The app cannot replace itself in {target.parent}. Download the new version "
                              "from the website and drag it onto Applications instead.")
        digest = (release.get("digest") or "")
        if not digest.startswith("sha256:") or not release.get("size"):
            raise UpdateError("GitHub did not give a fingerprint for the download, so it was not installed.")
        want_version = release["tag"].lstrip("vV")
        free = shutil.disk_usage(target.parent).free
        if free < max(200 * 1024 * 1024, 4 * int(release["size"])):
            raise UpdateError("Your Mac is too full to install the update. Free up some space and try again.")

        dmg = self.download(release, on_progress)
        mount = tempfile.mkdtemp(prefix="midify-update-")
        attached = False
        try:
            r = subprocess.run(["hdiutil", "attach", str(dmg), "-readonly", "-nobrowse", "-noautoopen",
                                "-mountpoint", mount], capture_output=True, text=True, timeout=120)
            if r.returncode != 0:
                raise UpdateError("The downloaded update would not open. Try again later.")
            attached = True
            new_app = Path(mount) / target.name
            if not new_app.is_dir():
                new_app = next(Path(mount).glob("*.app"), None)
            if not new_app:
                raise UpdateError("The download did not contain the app.")
            self.inspect(new_app, want_version)
            staged = target.parent / f".{target.name}.update"
            shutil.rmtree(staged, ignore_errors=True)
            r = subprocess.run(["ditto", str(new_app), str(staged)], capture_output=True, text=True, timeout=300)
            if r.returncode != 0:
                shutil.rmtree(staged, ignore_errors=True)
                raise UpdateError("Could not prepare the update. Your app is unchanged.")
            try:
                self.inspect(staged, want_version)
            except UpdateError:
                shutil.rmtree(staged, ignore_errors=True)
                raise
            return staged
        finally:
            if attached:
                subprocess.run(["hdiutil", "detach", mount, "-quiet"], capture_output=True, timeout=60)
            try:
                os.rmdir(mount)
            except OSError:
                pass
            try:
                dmg.unlink()
            except OSError:
                pass

    def download(self, release, on_progress=None):
        DOWNLOADS.mkdir(parents=True, exist_ok=True)
        part = DOWNLOADS / f"{release['tag']}.dmg.part"
        size, want = int(release["size"]), release["digest"].split(":", 1)[1].lower()
        if size > MAX_DOWNLOAD:
            raise UpdateError("The update is unexpectedly large, so it was not downloaded.")
        if not trusted_url(release.get("url")):
            raise DownloadFailed("The update did not come from GitHub, so it was not downloaded.",
                                 detail=f"untrusted address {release.get('url')}")
        req = urllib.request.Request(release["url"], headers={"User-Agent": "Midify"})
        h, got = hashlib.sha256(), 0
        try:
            with urllib.request.urlopen(req, timeout=30) as r, open(part, "wb") as f:
                while True:
                    chunk = r.read(64 * 1024)
                    if not chunk:
                        break
                    got += len(chunk)
                    if got > size:
                        raise UpdateError("The download was bigger than GitHub said it would be, so it was thrown away.")
                    h.update(chunk)
                    f.write(chunk)
                    if on_progress:
                        on_progress(got / size)
        except UpdateError:
            part.unlink(missing_ok=True)
            raise
        except urllib.error.HTTPError as e:
            part.unlink(missing_ok=True)
            raise DownloadFailed("GitHub would not hand over the update file. Try again in a few minutes.",
                                 detail=f"HTTP {e.code} for {release['url']}")
        except Exception as e:  # noqa: BLE001
            part.unlink(missing_ok=True)
            if _is_network(e):
                raise DownloadFailed("Could not reach GitHub to download the update. Check the internet "
                                     "connection and try again.", network=True, detail=f"{type(e).__name__}: {e}")
            raise DownloadFailed("The update could not be downloaded. Try again in a few minutes.",
                                 detail=f"{type(e).__name__}: {e} for {release['url']}")
        if got != size or h.hexdigest() != want:
            part.unlink(missing_ok=True)
            raise UpdateError("The download did not match GitHub's fingerprint for it, so it was thrown away. "
                              "Your app is unchanged.")
        final = part.with_suffix("")
        os.replace(part, final)
        return final

    @staticmethod
    def inspect(app, want_version):
        try:
            info = read_plist(app)
        except Exception:  # noqa: BLE001
            raise UpdateError("The update is not a valid app, so it was not installed.")
        if info.get("CFBundleIdentifier") != BUNDLE_ID:
            raise UpdateError("The download contained a different app, so it was not installed.")
        if info.get("CFBundleShortVersionString") != want_version:
            raise UpdateError("The download's version did not match the release, so it was not installed.")
        for need in ("Contents/MacOS/launch", "Contents/Resources/app/desktop.py", "Contents/Resources/app/server.py"):
            if not (Path(app) / need).exists():
                raise UpdateError("The update is missing part of the app, so it was not installed.")
        # The program macOS actually starts must exist and be runnable, or the new app would not open.
        exe = Path(app) / "Contents" / "MacOS" / str(info.get("CFBundleExecutable") or "")
        if not info.get("CFBundleExecutable") or not exe.is_file() or not os.access(exe, os.X_OK):
            raise UpdateError("The update's app would not start, so it was not installed.")
        r = subprocess.run(["codesign", "--verify", str(app)], capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            raise UpdateError("The update's signature check failed, so it was not installed.")

    def hand_over(self, staged, target=None, pid=None, relaunch=True):
        """Start the swap script, then quit so it can run."""
        target = Path(target) if target else bundle_path()
        DOWNLOADS.mkdir(parents=True, exist_ok=True)
        script = DOWNLOADS / "swap.sh"
        script.write_text(SWAP_SCRIPT)
        script.chmod(0o755)
        backup = target.parent / f".{target.name}.old"
        subprocess.Popen(["/bin/bash", str(script), str(pid or os.getpid()), str(target), str(staged),
                          str(backup), str(LOG), "1" if relaunch else "0"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True)
        _log(f"installing {staged.name} over {target}")
        if self.quit_hook:
            self.quit_hook()


def tidy_leftovers(target=None):
    """After a finished or interrupted update, remove the hidden staged and backup copies,
    but only while the real app is intact."""
    target = Path(target) if target else bundle_path()
    if not target or not (target / "Contents" / "Info.plist").exists():
        return
    for leftover in (target.parent / f".{target.name}.update", target.parent / f".{target.name}.old"):
        if leftover.exists():
            shutil.rmtree(leftover, ignore_errors=True)
    shutil.rmtree(DOWNLOADS, ignore_errors=True)


UPDATER = Updater()


def startup():
    tidy_leftovers()
    UPDATER.check()
