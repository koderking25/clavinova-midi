"""Build a Midify that runs from a flash drive, on any Apple Silicon Mac.

    .venv/bin/python tools/make_portable.py /Volumes/YOURDRIVE

Everything goes on the drive: Python itself, the packages, the AI models, ffmpeg and fluidsynth
with the libraries they need, and the app. Nothing is left behind on the Mac it was built on, and
nothing is needed on the Mac it is plugged into. About 950 MB.

Built on your own Mac rather than downloaded, so macOS never flags it as a download and there is
no warning to click through and no Terminal command to run.
"""
import argparse
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(HERE / "tools"))
import bundle_tools  # noqa: E402

NEEDED_GB = 1.6
LAUNCHER = '''#!/bin/bash
# Midify, running from this drive. Nothing is installed on the Mac.
HERE="$(cd "$(dirname "$0")" && pwd)"
export CLAVINOVA_STATE="$HERE/state"
export CLAVINOVA_WORK="$HERE/working"
export CLAVINOVA_LIBRARY="$HERE/Songs"
export HF_HOME="$HERE/models"
export HF_HUB_OFFLINE=1
export HF_HUB_DISABLE_SYMLINKS=1
export PYTHONPYCACHEPREFIX="$HERE/compiled"
export PATH="$HERE/tools/bin:$PATH"
mkdir -p "$CLAVINOVA_STATE" "$CLAVINOVA_WORK" "$CLAVINOVA_LIBRARY"
exec "$HERE/python/bin/python3.11" "$HERE/app/desktop.py"
'''

READ_ME = '''Midify, on this drive
=====================

Double-click "Midify.command" in this folder to open it. Midify runs from this drive: nothing is installed on
the Mac, and nothing is left behind when you unplug it.

Your songs are saved in Midify/Songs on this drive, so your piano can read them straight from here.

Needs an Apple Silicon Mac (M1 or newer). It will not run on an Intel Mac.

If macOS says it cannot check the app for malicious software, the drive was copied from somewhere
else rather than built on your own Mac. Open Terminal and run:

    xattr -dr com.apple.quarantine "/Volumes/%(volume)s"
'''


def say(message):
    print(message, flush=True)


def folder_mb(path):
    return sum(f.stat().st_size for f in Path(path).rglob("*") if f.is_file() and not f.is_symlink()) / 1e6


def copy_models(src, dest):
    """Only the real model files, not the duplicate copies the cache keeps beside them."""
    hub = Path(src) / "hub"
    count = 0
    for model in sorted(hub.glob("models--*")) if hub.is_dir() else []:
        for part in ("refs", "snapshots"):
            if (model / part).is_dir():
                target = Path(dest) / "hub" / model.name / part
                target.mkdir(parents=True, exist_ok=True)
                for item in (model / part).rglob("*"):
                    if item.is_file():
                        out = target / item.relative_to(model / part)
                        out.parent.mkdir(parents=True, exist_ok=True)
                        if not out.exists():
                            shutil.copyfile(item, out, follow_symlinks=True)
                            count += 1
    return count


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("drive", help="where to build it, usually /Volumes/YOURDRIVE")
    ap.add_argument("--keep-going", action="store_true", help="carry on even if something is already there")
    args = ap.parse_args()

    drive = Path(args.drive)
    if not drive.is_dir():
        say(f"{drive} is not plugged in.")
        return 1
    free = shutil.disk_usage(drive).free / 1e9
    if free < NEEDED_GB:
        say(f"{drive.name} has {free:.1f} GB free. This needs about {NEEDED_GB} GB.")
        return 1
    root = drive / "Midify"
    if root.exists() and not args.keep_going:
        say(f"There is already a Midify folder on {drive.name}. Remove it first, or pass --keep-going.")
        return 1

    started = time.time()
    say(f"Building a portable Midify on {drive.name} ({free:.1f} GB free)")
    root.mkdir(parents=True, exist_ok=True)

    say("\n1. Python itself")
    base = Path(sys.base_prefix)
    target_python = root / "python"
    if not (target_python / "bin" / "python3.11").exists():
        try:
            shutil.copytree(base, target_python, symlinks=True, dirs_exist_ok=True)
        except (OSError, shutil.Error):
            # A FAT32 drive cannot store a link, so copy what each one points at instead.
            shutil.rmtree(target_python, ignore_errors=True)
            shutil.copytree(base, target_python, symlinks=False, dirs_exist_ok=True)
    # uv marks its own Python as managed and refuses to install into it. On the drive this is our
    # Python, not uv's, so the marker goes.
    for marker in target_python.rglob("EXTERNALLY-MANAGED"):
        marker.unlink()
    # Parts of Python nothing here uses. Each file costs 32 KB on a FAT32 drive however small it
    # is, so dropping thousands of them saves real room and real minutes of copying.
    for unused in ("test", "idlelib", "tkinter", "turtledemo", "lib2to3", "ensurepip"):
        shutil.rmtree(target_python / "lib" / "python3.11" / unused, ignore_errors=True)
    say(f"   {folder_mb(target_python):.0f} MB")

    say("\n2. The packages Midify needs")
    python_on_drive = target_python / "bin" / "python3.11"
    result = subprocess.run(["uv", "pip", "install", "--python", str(python_on_drive), "--no-deps",
                             "--link-mode", "copy", "-r", str(HERE / "requirements.lock")],
                            capture_output=True, text=True)
    if result.returncode != 0:
        say("   could not install the packages:\n" + (result.stderr or "")[-600:])
        return 1
    subprocess.run([str(python_on_drive), str(HERE / "tools" / "slim.py")], capture_output=True)
    say(f"   {folder_mb(target_python):.0f} MB with everything in")

    say("\n3. ffmpeg and fluidsynth, with the libraries they need")
    tools_root = root / "tools"
    for tool in ("ffmpeg", "fluidsynth"):
        placed = bundle_tools.bundle(tool, tools_root, say=lambda m: say("  " + m.strip()))
        if not bundle_tools.check(placed, say=lambda m: say("  " + m.strip())):
            say(f"   {tool} would not travel properly, so this drive would not work on another Mac.")
            return 1

    say("\n4. The AI models")
    copied = copy_models(Path(os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")), root / "models")
    say(f"   {copied} files, {folder_mb(root / 'models'):.0f} MB")

    say("\n5. Midify itself")
    app_dir = root / "app"
    app_dir.mkdir(exist_ok=True)
    for item in (HERE / "app").iterdir():
        if item.name == "__pycache__":
            continue
        if item.is_dir():
            shutil.copytree(item, app_dir / item.name, dirs_exist_ok=True)
        else:
            shutil.copy2(item, app_dir / item.name)
    # Inside the Midify folder, not at the top of the drive: a piano lists everything at the top
    # of a drive, and a .command and a .txt show up there as broken songs.
    launcher = root / "Midify.command"
    launcher.write_text(LAUNCHER)
    launcher.chmod(0o755)
    (root / "Read Me.txt").write_text(READ_ME % {"volume": drive.name})

    total = folder_mb(root) + folder_mb(launcher)
    say(f"\nDone in {(time.time() - started) / 60:.0f} minutes. {total:.0f} MB on {drive.name}.")
    say(f"Open {drive.name} in Finder, go into the Midify folder, and double-click Midify.command.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
