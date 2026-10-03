"""Two ways to use space: Standard, or Storage saver with everything on the flash drive.

Standard keeps songs in ~/Music/Midify and works in a folder on the Mac, tidying up after itself.

Storage saver puts everything that grows onto the flash drive: the scratch space a song needs while
it is being made (about 600 MB for a five minute song), the finished songs, their previews and
details, the separation model (161 MB), and the compiled-code cache. The Mac side then stops
growing at all.

What cannot move, honestly: the app's Python environment, about 900 MB. The piano model's weights
(56 MB) ship inside it, and it needs symlinks and executable files that a FAT32 flash drive cannot
hold. Storage saver is about everything that grows, not the fixed install.

The drive is remembered by its volume UUID, not its name, so another drive called the same thing is
never mistaken for yours.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import usb

STATE = Path(os.environ.get("CLAVINOVA_STATE", Path(__file__).resolve().parent.parent / "state"))
SETTINGS = STATE / "storage.json"
FOLDER = "Midify"                       # what we make on the drive
MODES = ("standard", "saver")

# Space needed on the drive before Storage saver is allowed: the model (161 MB), room for one song
# being made, and a little headroom. Measured, not guessed: see tests/test_storage.py.
NEED_GB = 1.6
COMFORTABLE_GB = 4.0

# An explicitly set folder wins, exactly as the engine has always treated these. Overriding it
# broke test isolation once and wrote test songs into a real songs folder.
HOME_LIBRARY = Path(os.environ.get("CLAVINOVA_LIBRARY", Path.home() / "Music" / FOLDER))
HOME_WORK = Path(os.environ.get("CLAVINOVA_WORK", Path(__file__).resolve().parent.parent / "work"))
HOME_MODELS = Path.home() / ".cache" / "huggingface"


def _read():
    try:
        data = json.loads(SETTINGS.read_text())
        if data.get("mode") in MODES:
            return data
    except Exception:
        pass
    return {"mode": "standard"}


def _write(data):
    STATE.mkdir(parents=True, exist_ok=True)
    tmp = SETTINGS.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(SETTINGS)


def mode():
    return _read()["mode"]


def volume_id(path):
    """A drive's own identity, so a renamed or re-plugged drive is still recognised."""
    info = usb._diskutil_info(path) or {}
    return info.get("VolumeUUID") or info.get("DiskUUID") or ""


def remembered_drive():
    """Where the chosen drive is now, or None if it is not plugged in."""
    data = _read()
    want_id, want_name = data.get("drive_id", ""), data.get("drive_name", "")
    for drive in usb.list_drives():
        path = Path(drive["path"])
        if want_id and volume_id(path) == want_id:
            return path
        if not want_id and want_name and path.name == want_name:
            return path
    return None


def free_gb(path):
    try:
        usage = shutil.disk_usage(path)
        return usage.free / 1e9
    except Exception:
        return 0.0


def places(for_mode=None, drive=None):
    """Where everything lives right now. Falls back to the Mac when the drive is away, so the app
    always opens; making songs is blocked separately with a clear message."""
    chosen = for_mode or mode()
    if chosen == "saver":
        root = drive or remembered_drive()
        if root:
            base = Path(root) / FOLDER
            return {"where": "drive", "drive": Path(root), "library": base / "Songs",
                    "work": base / "Working", "models": base / "Models", "pycache": base / "Compiled"}
    return {"where": "mac", "drive": None, "library": HOME_LIBRARY, "work": HOME_WORK,
            "models": HOME_MODELS, "pycache": Path.home() / "Library" / "Application Support" / "Clavinova MIDI Maker" / "pycache"}


def env(for_mode=None, drive=None):
    """What to hand a song's own process, so it reads and writes in the right place."""
    p = places(for_mode, drive)
    out = {"CLAVINOVA_LIBRARY": str(p["library"]), "CLAVINOVA_WORK": str(p["work"]),
           "HF_HOME": str(p["models"]), "PYTHONPYCACHEPREFIX": str(p["pycache"]),
           "TMPDIR": str(p["work"] / "tmp")}
    if p["where"] == "drive":
        # A FAT32 drive cannot hold symlinks, which is how the model cache normally saves space.
        out["HF_HUB_DISABLE_SYMLINKS"] = "1"
        out["HF_HUB_OFFLINE"] = "1"
    return out


def apply_to_process(for_mode=None, drive=None):
    """Point this process at the right folders too, and make sure they exist."""
    settings = env(for_mode, drive)
    os.environ.update(settings)
    p = places(for_mode, drive)
    for key in ("library", "work", "models", "pycache"):
        try:
            p[key].mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
    (p["work"] / "tmp").mkdir(parents=True, exist_ok=True)
    return settings


def can_use(drive):
    """Is this drive usable for Storage saver? Returns (ok, plain English)."""
    drive = Path(drive)
    if not drive.is_dir():
        return False, "That drive is not plugged in."
    free = free_gb(drive)
    if free < NEED_GB:
        return False, (f"That drive has {free:.1f} GB free. Storage saver needs about "
                       f"{NEED_GB:.1f} GB: the AI model is 161 MB and a five minute song needs about "
                       "600 MB of room while it is being made.")
    probe = drive / ".midify-write-test"
    try:
        probe.write_bytes(b"x" * 1024)
        probe.unlink()
    except Exception:
        return False, "That drive is read only, so Midify cannot work on it."
    note = ""
    if free < COMFORTABLE_GB:
        note = (f" It has {free:.1f} GB free, which is enough for one song at a time but will fill up "
                "as you save songs.")
    return True, "That drive is ready for Storage saver." + note


def ready():
    """For the health check. (ok, plain English, what would fix it)"""
    if mode() != "saver":
        return True, "Songs and working files are kept on your Mac.", None
    data = _read()
    drive = remembered_drive()
    if not drive:
        name = data.get("drive_name") or "your flash drive"
        return False, (f"Storage saver is on, so Midify keeps everything on {name}, but that drive is not "
                       "plugged in. Plug it in to make songs, or switch back to Standard."), "storage_standard"
    ok, plain = can_use(drive)
    if not ok:
        return False, plain, "storage_standard"
    free = free_gb(drive)
    if free < COMFORTABLE_GB:
        return True, (f"Storage saver is on. {drive.name} has {free:.1f} GB free, enough to keep going for "
                      "now."), None
    return True, f"Storage saver is on. Everything is kept on {drive.name} ({free:.1f} GB free).", None


def blocked_reason():
    """Why a song cannot be made right now, in plain English, or None."""
    if mode() != "saver":
        return None
    drive = remembered_drive()
    if not drive:
        name = _read().get("drive_name") or "your flash drive"
        return (f"Storage saver keeps everything on {name}, and it is not plugged in. Plug it in, or "
                "switch to Standard in Storage settings, and try again.")
    ok, plain = can_use(drive)
    return None if ok else plain


def folder_size(path):
    path = Path(path)
    if not path.exists():
        return 0
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file() and not p.is_symlink():
                total += p.stat().st_size
        except Exception:
            continue
    return total


def usage():
    """How much Midify is keeping, and where."""
    p = places()
    support = Path.home() / "Library" / "Application Support" / "Clavinova MIDI Maker"
    out = {"mode": mode(), "where": p["where"], "drive": str(p["drive"]) if p["drive"] else None,
           "fixed_install_bytes": folder_size(support / "venv"),
           "parts": {}}
    for label, path in (("songs", p["library"]), ("working files", p["work"]),
                        ("AI model", p["models"]), ("compiled code", p["pycache"])):
        out["parts"][label] = {"bytes": folder_size(path), "path": str(path),
                               "on_drive": p["where"] == "drive"}
    out["growing_total_bytes"] = sum(v["bytes"] for v in out["parts"].values())
    if p["drive"]:
        out["drive_free_bytes"] = int(free_gb(p["drive"]) * 1e9)
        out["drive_name"] = p["drive"].name
    out["mac_free_bytes"] = int(free_gb(Path.home()) * 1e9)
    return out


def _copy_tree(src, dest, say=None):
    """Copy a folder, following symlinks (a FAT32 drive cannot store them), and check it arrived."""
    src, dest = Path(src), Path(dest)
    if not src.exists():
        return 0
    dest.mkdir(parents=True, exist_ok=True)
    copied = 0
    files = [p for p in src.rglob("*") if p.is_file()]
    for i, item in enumerate(files):
        target = dest / item.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.stat().st_size == item.stat().st_size:
            copied += 1
            continue
        shutil.copyfile(item, target, follow_symlinks=True)       # resolves links into real files
        if target.stat().st_size != item.resolve().stat().st_size:
            raise OSError(f"{item.name} did not copy across completely")
        copied += 1
        if say and i % 25 == 0:
            say(f"Copying across: {i + 1} of {len(files)} files")
    return copied


def _copy_models(src, dest, say=None):
    """Copy the AI models to the drive at half the size.

    The cache keeps every file twice: the real file under blobs, and a link to it under snapshots.
    Copying the lot with links followed means two full copies, 161 MB instead of 81. Only the
    snapshot side is copied, with the real contents in place, which is what loads the model
    (checked on a FAT32 drive: it loads in 2.6s).
    """
    src, dest = Path(src), Path(dest)
    hub = src / "hub"
    if not hub.is_dir():
        return _copy_tree(src, dest, say)
    copied = 0
    for model in sorted(hub.glob("models--*")):
        for part in ("refs", "snapshots"):
            if (model / part).is_dir():
                copied += _copy_tree(model / part, dest / "hub" / model.name / part, say)
    return copied


def switch(to, drive=None, say=None, move_existing=True):
    """Change mode. Copies what you already have to its new home, checks it arrived, and only then
    removes the old copy. Returns a plain English sentence about what happened."""
    if to not in MODES:
        raise ValueError("unknown storage mode")
    say = say or (lambda _m: None)
    if to == "saver":
        if drive is None:
            drive = remembered_drive()
        if drive is None:
            raise ValueError("Storage saver needs a flash drive. Plug one in and choose it.")
        drive = Path(drive)
        ok, plain = can_use(drive)
        if not ok:
            raise ValueError(plain)
        target = places("saver", drive)
        moved = []
        if move_existing:
            for label, src, dest, how in (("your songs", HOME_LIBRARY, target["library"], _copy_tree),
                                          ("the AI model", HOME_MODELS, target["models"], _copy_models)):
                if folder_size(src) == 0:
                    continue
                need = folder_size(src) / 1e9
                if free_gb(drive) < need + 0.3:
                    raise ValueError(f"{drive.name} does not have room for {label} "
                                     f"({need:.1f} GB needed, {free_gb(drive):.1f} GB free).")
                say(f"Copying {label} to {drive.name}")
                how(src, dest, say)
                moved.append(label)
        _write({"mode": "saver", "drive_id": volume_id(drive), "drive_name": drive.name,
                "drive_path": str(drive)})
        apply_to_process("saver", drive)
        what = " and ".join(moved) if moved else "nothing to copy yet"
        return (f"Storage saver is on. Everything new goes to {drive.name} ({what} copied across). "
                "The copies on your Mac are still there until you choose to remove them.")
    # back to the Mac
    before = remembered_drive()
    target = places("standard")
    moved = []
    if move_existing and before:
        on_drive = places("saver", before)
        for label, src, dest in (("your songs", on_drive["library"], target["library"]),):
            if folder_size(src) == 0:
                continue
            need = folder_size(src) / 1e9
            if free_gb(Path.home()) < need + 1.0:
                raise ValueError(f"Your Mac does not have room for {label} ({need:.1f} GB needed).")
            say(f"Copying {label} back to your Mac")
            _copy_tree(src, dest, say)
            moved.append(label)
    _write({"mode": "standard"})
    apply_to_process("standard")
    what = f" ({' and '.join(moved)} copied back)" if moved else ""
    return f"Standard storage is on. Songs and working files are kept on your Mac{what}."


def free_mac_copy(say=None):
    """After switching to Storage saver: remove the Mac copies, but only what is safely on the drive."""
    if mode() != "saver":
        raise ValueError("This only applies when Storage saver is on.")
    drive = remembered_drive()
    if not drive:
        raise ValueError("Plug the drive in first, so Midify can check your songs are safely on it.")
    on_drive = places("saver", drive)
    freed = 0
    for label, mac_copy, drive_copy in (("songs", HOME_LIBRARY, on_drive["library"]),
                                        ("the AI model", HOME_MODELS, on_drive["models"])):
        if folder_size(mac_copy) == 0:
            continue
        missing = []
        for item in mac_copy.rglob("*"):
            if item.is_file() and not item.is_symlink():
                twin = drive_copy / item.relative_to(mac_copy)
                if not twin.exists() or twin.stat().st_size != item.stat().st_size:
                    missing.append(item.name)
        if missing:
            raise ValueError(f"Not removing the Mac copy of {label}: {len(missing)} file(s) are not on the "
                             f"drive yet, starting with {missing[0]}.")
        size = folder_size(mac_copy)
        if say:
            say(f"Removing the Mac copy of {label}")
        shutil.rmtree(mac_copy, ignore_errors=True)
        freed += size
    return f"Freed {freed / 1e9:.1f} GB on your Mac. Everything is on {drive.name}." if freed else \
           "There was nothing left to remove on your Mac."
