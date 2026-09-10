"""Find USB flash drives, copy MIDI files onto them cleanly, tidy and eject."""
import os
import plistlib
import re
import shutil
import subprocess
import unicodedata
from pathlib import Path

VOLUMES = Path("/Volumes")
NAME_LIMIT = 40          # keeps names readable on the Clavinova's song list


def safe_filename(title, ext=".mid"):
    s = unicodedata.normalize("NFKD", title or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^A-Za-z0-9 _()\-]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" .-_")
    return (s[:NAME_LIMIT].rstrip(" .-_") or "Song") + ext


def _diskutil_info(path):
    try:
        out = subprocess.run(["diskutil", "info", "-plist", str(path)], capture_output=True, timeout=15)
        return plistlib.loads(out.stdout) if out.returncode == 0 and out.stdout else {}
    except Exception:  # noqa: BLE001
        return {}


def list_drives():
    drives = []
    if not VOLUMES.exists():
        return drives
    for vol in sorted(VOLUMES.iterdir()):
        try:
            if not vol.is_dir() or vol.is_symlink() or not os.path.ismount(vol):
                continue
        except OSError:
            continue
        info = _diskutil_info(vol)
        if not info:
            continue
        removable = info.get("Ejectable") or info.get("RemovableMedia") or info.get("RemovableMediaOrExternalDevice")
        if info.get("Internal", True) and not removable:
            continue
        if info.get("MountPoint") in ("/", "/System/Volumes/Data"):
            continue
        is_image = info.get("BusProtocol") == "Disk Image" or info.get("VirtualOrPhysical") == "Virtual"
        if is_image and os.environ.get("CLAVINOVA_TEST_DISK_IMAGES") != "1":   # tests mount FAT32 images
            continue
        fs_type = (info.get("FilesystemType") or "").lower()
        fs_name = info.get("FilesystemName") or fs_type or "unknown"
        try:
            st = os.statvfs(vol)
            free = st.f_bavail * st.f_frsize
            total = st.f_blocks * st.f_frsize
        except OSError:
            free = total = 0
        if fs_type == "msdos":
            status, note = "good", f"{fs_name}: the Clavinova can read this."
        elif fs_type == "exfat":
            status, note = "warn", ("This drive is formatted exFAT. The CVP-503 is from 2007 and very likely "
                                    "cannot read exFAT. Reformat it as MS-DOS (FAT32) in Disk Utility, "
                                    "or on the Clavinova itself.")
        else:
            status, note = "bad", (f"This drive is formatted {fs_name}, which the Clavinova cannot read. "
                                   "Reformat it as MS-DOS (FAT32) in Disk Utility.")
        drives.append({
            "name": info.get("VolumeName") or vol.name,
            "path": str(vol),
            "filesystem": fs_name,
            "status": status,
            "note": note,
            "writable": bool(info.get("WritableVolume", True)) and os.access(vol, os.W_OK),
            "free_bytes": free,
            "total_bytes": total,
        })
    return drives


def resolve_drive(path):
    """Only accept a path that is one of the currently listed external drives."""
    for d in list_drives():
        if os.path.realpath(d["path"]) == os.path.realpath(path):
            return d
    return None


def _remove_appledouble(folder, name):
    ad = Path(folder) / f"._{name}"
    try:
        if ad.is_file() and ad.stat().st_size < 64 * 1024:
            ad.unlink()
    except OSError:
        pass


def copy_to_drive(src, drive_path, filename=None, subfolder=""):
    d = resolve_drive(drive_path)
    if not d:
        raise ValueError("That flash drive is not connected any more.")
    if not d["writable"]:
        raise ValueError("That flash drive is locked or read-only.")
    src = Path(src)
    size = src.stat().st_size
    if d["free_bytes"] and d["free_bytes"] < size + 64 * 1024:
        raise ValueError("That flash drive is full.")
    folder = Path(d["path"])
    if subfolder:
        folder = folder / safe_filename(subfolder, ext="")
        folder.mkdir(exist_ok=True)
    name = safe_filename(Path(filename or src.name).stem)
    dest = folder / name
    n = 2
    while dest.exists():
        if dest.stat().st_size == size and dest.read_bytes() == src.read_bytes():
            return {"path": str(dest), "name": dest.name, "already_there": True}
        dest = folder / (Path(name).stem[: NAME_LIMIT - 4] + f" {n}.mid")
        n += 1
    tmp = folder / (".partial-" + dest.name)
    try:
        shutil.copyfile(src, tmp)             # data only: no Mac metadata, so no ._ file
        with open(tmp, "rb+") as f:
            os.fsync(f.fileno())
        os.replace(tmp, dest)
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        _remove_appledouble(folder, tmp.name)
        raise IOError("The copy did not finish. Was the drive unplugged? Plug it back in and try again.")
    _remove_appledouble(folder, tmp.name)
    _remove_appledouble(folder, dest.name)
    if dest.read_bytes() != src.read_bytes():
        raise IOError("The copy on the flash drive does not match. Try again or try another drive.")
    return {"path": str(dest), "name": dest.name, "already_there": False}


def mac_clutter(drive_path):
    """Hidden files macOS leaves on drives. The Clavinova may list them as broken songs."""
    d = resolve_drive(drive_path)
    if not d:
        raise ValueError("That flash drive is not connected any more.")
    root = Path(d["path"])
    found = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [x for x in dirnames if x not in (".Spotlight-V100", ".fseventsd", ".Trashes", ".TemporaryItems")]
        for f in filenames:
            p = Path(dirpath) / f
            try:
                small = p.stat().st_size <= 64 * 1024
            except OSError:
                continue
            if (f.startswith("._") and small) or f == ".DS_Store":
                found.append(p)
        if len(found) > 5000:
            break
    return found


def tidy_drive(drive_path, dry_run=True):
    files = mac_clutter(drive_path)
    removed = 0
    if not dry_run:
        for p in files:
            try:
                p.unlink()
                removed += 1
            except OSError:
                pass
    return {"found": len(files), "removed": removed, "examples": [str(p.name) for p in files[:8]]}


def eject(drive_path):
    d = resolve_drive(drive_path)
    if not d:
        raise ValueError("That flash drive is not connected any more.")
    r = subprocess.run(["diskutil", "eject", d["path"]], capture_output=True, text=True, timeout=60)
    if r.returncode != 0:
        raise ValueError("Could not eject. Close anything using the drive (like a Finder window) and try again.")
    return True
