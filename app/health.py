"""What is wrong with Midify right now, in plain English, and how to put it right.

Every check answers three questions: is it working, what does it mean for you if it is not,
and can Midify fix it by itself. Safe repairs (making a folder, clearing leftovers, dropping a
corrupt cache) run on their own at startup. Slow or heavy ones (downloading the AI models,
rebuilding the Python setup, installing audio tools) are offered as a button instead, because
they take minutes or install software.

Everything that goes wrong, and everything repaired, is written to problems.log as a sentence.
"""
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
STATE = Path(os.environ.get("CLAVINOVA_STATE", HERE.parent / "state"))
WORK = Path(os.environ.get("CLAVINOVA_WORK", HERE.parent / "work"))
LIBRARY = Path(os.environ.get("CLAVINOVA_LIBRARY", Path.home() / "Music" / "Midify"))
SUPPORT = Path(os.environ.get("CLAVINOVA_SUPPORT", Path.home() / "Library" / "Application Support" / "Clavinova MIDI Maker"))
PROBLEM_LOG = STATE / "problems.log"
# Where the models actually are, which is not always the home folder: Storage saver puts them on
# a flash drive, and a portable copy carries them with it. Asking the home folder told someone
# their model was missing while it sat right there on the drive.
MODEL_CACHE = (Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface"))
               / "hub" / "models--adefossez--HTDemucs")

SONG_NEEDS_GB = 0.6                      # a song's working space, the pipeline refuses below this


def note(what, fixed=None):
    """Write one plain sentence to the problem log. `fixed` says what Midify did about it."""
    line = f"{time.strftime('%Y-%m-%d %H:%M')}  {what}"
    if fixed:
        line += f"  {fixed}"
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        with open(PROBLEM_LOG, "a") as f:
            f.write(line + "\n")
        _trim_log()
    except OSError:
        pass
    return line


def _trim_log(keep=500):
    try:
        lines = PROBLEM_LOG.read_text().splitlines()
        if len(lines) > keep:
            PROBLEM_LOG.write_text("\n".join(lines[-keep:]) + "\n")
    except OSError:
        pass


def recent_problems(limit=20):
    try:
        return PROBLEM_LOG.read_text().splitlines()[-limit:][::-1]
    except OSError:
        return []


def _tool(name):
    return shutil.which(name) is not None


def _free_gb(path):
    try:
        return shutil.disk_usage(path).free / 1e9
    except OSError:
        return 0.0


# ---------------------------------------------------------------- checks
# Each returns (ok, plain, fix) where fix is None, "auto" (already repaired) or the name of a
# repair the user can press. "blocking" means no songs can be made until it is sorted.

def check_python_setup():
    need = ("torch", "demucs", "transkun", "basic_pitch", "soundfile", "librosa", "mido")
    missing = [m for m in need if not _importable(m)]
    if not missing:
        return True, "The app's Python setup is complete.", None, False
    return (False,
            f"Part of the app's Python setup is missing ({', '.join(missing)}). Midify cannot make "
            "songs until it is rebuilt. Rebuilding takes about ten minutes and keeps your songs.",
            "rebuild_python", True)


def _importable(name):
    import importlib.util
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


def check_audio_tools():
    missing = [t for t in ("ffmpeg",) if not _tool(t)]
    if missing:
        return (False,
                "ffmpeg is missing, and Midify needs it to read audio files. It comes from Homebrew.",
                "install_tools", True)
    if not _tool("fluidsynth"):
        return (False,
                "fluidsynth is missing, so songs still get made but you cannot preview them in the app.",
                "install_tools", False)
    return True, "The audio tools are installed.", None, False


def check_models():
    if not MODEL_CACHE.is_dir():
        return (False,
                "The instrument separation model is missing. Midify can still do Solo piano recordings, "
                "but not Piano version or Full band until it is downloaded again (about 80 MB).",
                "download_models", False)
    return True, "The AI models are in place.", None, False


def check_songs_folder(repair=True):
    if LIBRARY.is_dir() and os.access(LIBRARY, os.W_OK):
        return True, "Your songs folder is there.", None, False
    if repair:
        try:
            LIBRARY.mkdir(parents=True, exist_ok=True)
            (LIBRARY / ".clavinova").mkdir(exist_ok=True)
            if os.access(LIBRARY, os.W_OK):
                note(f"Your songs folder ({LIBRARY}) was missing.", "Midify made it again. Songs you had made are not affected unless the folder was deleted.")
                return True, "Your songs folder was missing, so Midify made it again.", "auto", False
        except OSError:
            pass
    return (False, f"Midify cannot write to your songs folder ({LIBRARY}). Check it is not locked or on a disconnected disk.",
            None, True)


def check_song_details(repair=True):
    """Details files are small notes beside each song. A damaged one must not break the list."""
    meta = LIBRARY / ".clavinova"
    if not meta.is_dir():
        return True, "Song details are fine.", None, False
    damaged = []
    for f in meta.glob("*.json"):
        try:
            json.loads(f.read_text())
        except (ValueError, OSError):
            damaged.append(f)
    if not damaged:
        return True, "Song details are fine.", None, False
    if repair:
        spoiled = meta / "damaged"
        try:
            spoiled.mkdir(exist_ok=True)
            for f in damaged:
                f.rename(spoiled / f.name)
            note(f"{len(damaged)} song detail file(s) were damaged and could not be read.",
                 "Midify set them aside; those songs still play, they just lost their tempo and part list.")
            return True, f"{len(damaged)} damaged song detail file(s) were set aside. The songs themselves are fine.", "auto", False
        except OSError:
            pass
    return False, f"{len(damaged)} song detail file(s) are damaged.", None, False


def check_disk():
    free = _free_gb(LIBRARY if LIBRARY.exists() else Path.home())
    if free >= 2:
        return True, f"{free:.1f} GB free on this Mac.", None, False
    if free >= SONG_NEEDS_GB:
        return (False, f"Only {free:.1f} GB free. Short songs will work; long ones may run out of room. "
                "Emptying the Trash and restarting usually frees several GB.", "free_space", False)
    return (False, f"Only {free:.1f} GB free, which is not enough to make a song. Emptying the Trash and "
            "restarting usually frees several GB.", "free_space", True)


def check_temp_files(repair=True):
    leftovers = [p for p in (WORK.glob("*") if WORK.is_dir() else []) if p.name != "uploads"]
    if not leftovers:
        return True, "No leftover working files.", None, False
    if repair:
        freed = 0
        for p in leftovers:
            try:
                freed += sum(f.stat().st_size for f in p.rglob("*") if f.is_file()) if p.is_dir() else p.stat().st_size
                shutil.rmtree(p, ignore_errors=True) if p.is_dir() else p.unlink(missing_ok=True)
            except OSError:
                pass
        note(f"{len(leftovers)} leftover working folder(s) from a song that was interrupted.",
             f"Midify deleted them and freed {freed / 1e6:.0f} MB.")
        return True, f"Cleared {len(leftovers)} leftover working folder(s), freeing {freed / 1e6:.0f} MB.", "auto", False
    return False, f"{len(leftovers)} leftover working folder(s) are taking up space.", None, False


def check_update_memory(repair=True):
    cache = STATE / "update.json"
    if not cache.exists():
        return True, "Update checks are fine.", None, False
    try:
        data = json.loads(cache.read_text())
        url = ((data.get("release") or {}).get("url") or "")
        bad = bool(url) and not url.startswith("https://github.com/")
    except (ValueError, OSError):
        bad = True
    if not bad:
        return True, "Update checks are fine.", None, False
    if repair:
        try:
            cache.unlink()
            note("The saved update check was unreadable or pointed somewhere other than GitHub.",
                 "Midify threw it away and will ask GitHub again.")
            return True, "The saved update check was wrong, so Midify threw it away.", "auto", False
        except OSError:
            pass
    return False, "The saved update check is wrong and could not be cleared.", None, False


def check_downloader():
    import shutil as _s
    if not (_s.which("deno") or _s.which("node")):
        return (False, "Deno is missing, so downloading from YouTube will fail. Dropping in your own audio "
                "files still works.", "install_tools", False)
    try:
        import downloader
        state = downloader.state()
        checked = state.get("last_checked")
        when = ""
        if checked:
            days = (time.time() - checked) / 86400
            when = " checked today." if days < 1 else f" last checked {int(days)} days ago."
        return True, f"The song downloader is ready (version {state['version']}).{when}", None, False
    except Exception:                                    # noqa: BLE001
        return True, "The song downloader is ready.", None, False


def check_storage():
    """Storage saver keeps everything on a flash drive, so it has to be plugged in to make songs."""
    try:
        import storage
    except Exception:                                   # noqa: BLE001
        return True, "Storage settings could not be read, so Midify is using your Mac.", None, False
    ok, plain, fix = storage.ready()
    return ok, plain, fix, not ok


CHECKS = [
    ("storage", "Where songs are kept", check_storage),
    ("python_setup", "The app's Python setup", check_python_setup),
    ("audio_tools", "Audio tools", check_audio_tools),
    ("models", "AI models", check_models),
    ("songs_folder", "Your songs folder", check_songs_folder),
    ("song_details", "Song details", check_song_details),
    ("disk", "Free space", check_disk),
    ("temp_files", "Leftover working files", check_temp_files),
    ("update_memory", "Update checks", check_update_memory),
    ("downloader", "Song downloader", check_downloader),
]


def report(repair=True):
    """Run every check. Safe repairs happen while checking; anything left is reported."""
    items = []
    for key, title, fn in CHECKS:
        try:
            ok, plain, fix, blocking = fn(repair) if fn.__code__.co_argcount else fn()
        except Exception as e:  # noqa: BLE001  a broken check must never break the app
            ok, plain, fix, blocking = False, f"Midify could not check {title.lower()} ({type(e).__name__}).", None, False
        items.append({"key": key, "title": title, "ok": bool(ok), "plain": plain,
                      "fix": fix, "blocking": bool(blocking) and not ok, "repaired": fix == "auto"})
    return {"checked_at": time.time(),
            "ok": all(i["ok"] for i in items),
            "blocking": [i for i in items if i["blocking"]],
            "items": items,
            "problems": recent_problems()}


# ---------------------------------------------------------------- repairs the user can press
class Repairs:
    """Slow repairs run one at a time in the background, and report in plain English."""

    def __init__(self):
        self.state = {"running": None, "message": "", "finished": None}

    def status(self):
        return dict(self.state)

    def start(self, what, on_done=None):
        import threading
        if self.state["running"]:
            raise ValueError(f"Midify is already {self.state['message'][:1].lower()}{self.state['message'][1:]}")
        job = {"download_models": self._download_models, "free_space": self._free_space,
               "rebuild_python": self._setup_terminal, "install_tools": self._setup_terminal,
               "storage_standard": self._storage_standard}.get(what)
        if not job:
            raise ValueError("There is nothing to fix there.")
        self.state = {"running": what, "message": {
            "download_models": "Downloading the AI models. This takes a minute or two.",
            "free_space": "Freeing space by removing song previews.",
            "rebuild_python": "Rebuilding the app's Python setup in a Terminal window. It takes about ten minutes.",
            "install_tools": "Installing the missing audio tools in a Terminal window.",
            "storage_standard": "Switching back to keeping songs on your Mac.",
        }[what], "finished": None}
        threading.Thread(target=self._run, args=(what, job, on_done), daemon=True).start()
        return self.status()

    def _run(self, what, job, on_done):
        try:
            done = job()
            self.state = {"running": None, "message": "", "finished": done}
            note(f"You asked Midify to fix: {what.replace('_', ' ')}.", done)
        except Exception as e:  # noqa: BLE001
            msg = f"That repair did not work ({type(e).__name__}: {e})."
            self.state = {"running": None, "message": "", "finished": msg}
            note(f"A repair failed: {what.replace('_', ' ')}.", msg)
        if on_done:
            on_done()

    @staticmethod
    def _download_models():
        import pipeline
        ok, err = pipeline.check_models_in_child(timeout=1800)
        if not ok:
            raise RuntimeError(err or "the models did not load")
        return "The AI models are downloaded and working again."

    @staticmethod
    def _storage_standard():
        import storage
        return storage.switch("standard") + " Reopen Midify to use it."

    @staticmethod
    def _free_space():
        meta = LIBRARY / ".clavinova"
        before = _free_gb(LIBRARY)
        removed = 0
        for mp3 in meta.glob("*.mp3") if meta.is_dir() else []:
            try:
                mp3.unlink()
                removed += 1
            except OSError:
                pass
        shutil.rmtree(STATE / "updates", ignore_errors=True)
        freed = max(0.0, _free_gb(LIBRARY) - before)
        return (f"Removed {removed} song preview(s) and freed about {freed:.1f} GB. Previews are made "
                "again whenever you play one.")

    @staticmethod
    def _setup_terminal():
        """Run the app's own setup script in a Terminal window, where the user can watch it."""
        script = HERE.parent / "setup-app.sh"                  # Contents/Resources/setup-app.sh
        if not script.exists():
            raise RuntimeError("this copy of Midify has no setup script, so run setup.sh from the code instead")
        subprocess.run(["osascript", "-e", f'tell application "Terminal" to do script "bash \'{script}\'"'],
                       capture_output=True, timeout=30)
        subprocess.run(["osascript", "-e", 'tell application "Terminal" to activate'], capture_output=True, timeout=30)
        return ("A Terminal window is doing the work. When it says it is done, quit Midify with Cmd+Q and "
                "open it again.")


REPAIRS = Repairs()


def describe_failure(exc, doing="making a song"):
    """Turn any exception into one sentence for the problem log."""
    import pipeline
    if isinstance(exc, getattr(pipeline, "UserError", ())):
        return str(exc)
    name = type(exc).__name__
    text = str(exc)
    # The most likely failure with Storage saver on: the drive was pulled out mid-song. Say that,
    # rather than a class name nobody can act on (tests/test_storage_song.py).
    if "/Volumes/" in text and ("No such file" in text or "Errno 2" in text
                                or "Input/output error" in text or "Errno 5" in text):
        drive = text.split("/Volumes/", 1)[1].split("/", 1)[0] or "the flash drive"
        return (f"The flash drive ({drive}) was unplugged while Midify was {doing}, so the song could "
                "not be finished. Plug it back in and make it again. Songs already on the drive are fine.")
    plain = {
        "MemoryError": "Your Mac ran out of memory.",
        "FileNotFoundError": "A file Midify expected was not there.",
        "PermissionError": "macOS would not let Midify read or write a file.",
        "TimeoutError": "Something took too long and was given up on.",
    }.get(name)
    if plain is None and "space" in str(exc).lower():
        plain = "Your Mac ran out of space."
    return f"{plain or 'Something went wrong'} while {doing} ({name})."
