"""The handful of things Midify asks the operating system to do.

Everything here has two answers: the Mac one, which is what this app has always done and is
covered by the tests, and the Windows one, which is new.

**The Windows half has never been run.** There is no Windows machine here and no way to execute a
Windows binary on an Apple Silicon Mac, so every Windows branch below is written from the
documented behaviour of those commands and nothing more. Treat it as a first draft until somebody
runs it on a real PC. The Mac branches are unchanged and still take exactly the path they always
did.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

WINDOWS = sys.platform == "win32"
MAC = sys.platform == "darwin"


def _powershell(script, timeout=20):
    """Ask Windows a question and read the answer as JSON."""
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                             capture_output=True, text=True, timeout=timeout)
        if out.returncode != 0 or not out.stdout.strip():
            return None
        data = json.loads(out.stdout)
        return data if isinstance(data, list) else [data]
    except Exception:                                      # noqa: BLE001
        return None


def removable_drives():
    """Every drive someone might put songs on: (path, name, filesystem, free bytes, total bytes)."""
    if WINDOWS:
        rows = _powershell(
            "Get-Volume | Where-Object { $_.DriveLetter -ne $null } | "
            "Select-Object DriveLetter,FileSystemLabel,FileSystem,SizeRemaining,Size,DriveType | ConvertTo-Json")
        drives = []
        for row in rows or []:
            if str(row.get("DriveType")) not in ("Removable", "2"):
                continue
            letter = row.get("DriveLetter")
            if not letter:
                continue
            drives.append({
                "path": f"{letter}:\\",
                "name": row.get("FileSystemLabel") or f"{letter}:",
                "filesystem": (row.get("FileSystem") or "").upper(),
                "free_bytes": int(row.get("SizeRemaining") or 0),
                "total_bytes": int(row.get("Size") or 0),
            })
        return drives
    return []                                              # on a Mac, usb.py walks /Volumes itself


def volume_id(path):
    """Something that identifies a drive even when it is plugged in somewhere else."""
    if WINDOWS:
        letter = str(path)[:1]
        rows = _powershell(f"Get-Volume -DriveLetter {letter} | Select-Object UniqueId | ConvertTo-Json")
        return (rows[0].get("UniqueId") or "") if rows else ""
    return ""                                              # the Mac asks diskutil, in usb.py


def eject(path):
    """Let go of a drive so it is safe to unplug. Returns (ok, what to tell the person)."""
    if WINDOWS:
        letter = str(path)[:1]
        script = (f"$sh = New-Object -comObject Shell.Application; "
                  f"$sh.Namespace(17).ParseName('{letter}:').InvokeVerb('Eject')")
        try:
            run = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                                 capture_output=True, text=True, timeout=60)
            if run.returncode == 0:
                return True, "You can unplug the drive now."
            return False, "Windows would not let go of the drive. Close anything using it and try again."
        except Exception as e:                             # noqa: BLE001
            return False, f"The drive could not be ejected ({type(e).__name__})."
    return False, "Ejecting is handled elsewhere on this Mac."


def reveal(path):
    """Show a file to the person, in Finder or in Explorer."""
    path = Path(path)
    try:
        if WINDOWS:
            subprocess.run(["explorer", "/select,", str(path)], timeout=15)
        else:
            subprocess.run(["open", "-R", str(path)], timeout=10)
        return True
    except Exception:                                      # noqa: BLE001
        return False


def open_folder(path):
    try:
        if WINDOWS:
            os.startfile(str(path))                        # noqa: S606 - the Windows way to open a folder
        else:
            subprocess.run(["open", str(path)], timeout=10)
        return True
    except Exception:                                      # noqa: BLE001
        return False


def open_in_browser(url):
    import webbrowser
    try:
        webbrowser.open(url)
        return True
    except Exception:                                      # noqa: BLE001
        return False


def hidden_file_cleanup_needed():
    """Macs leave ._ files on drives that pianos list as broken songs. Windows does not."""
    return MAC
