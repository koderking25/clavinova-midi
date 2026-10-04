"""Turn a built portable Midify into one file anyone can copy to a flash drive.

    .venv/bin/python tools/pack_portable.py /Volumes/BEN/Midify

Why one file: a portable Midify is about 14,000 small files, and a FAT32 flash drive takes roughly
a second to create each one. Copying the folder took an hour and a half on a real drive. The same
contents inside a disk image is a single file that copies at the drive's normal speed, about a
minute, and the files inside sit on a proper Mac filesystem with no 32 KB minimum each.

What lands on the drive:

    Midify-Portable.dmg      everything, read only
    Start Midify.command     double-click this
    Midify Data/             your songs and working files, written on the drive itself

The songs stay outside the image, on the part of the drive a piano can read.
"""
import argparse
import shutil
import subprocess
import sys
import time
from pathlib import Path

LAUNCHER = '''#!/bin/bash
# Midify, portable. Everything runs from the disk image beside this file.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
IMAGE="$HERE/Midify-Portable.dmg"
DATA="$HERE/Midify Data"
MOUNT="/Volumes/Midify Portable"

if [ ! -f "$IMAGE" ]; then
  echo "Midify-Portable.dmg is not next to this file."; read -r -p "Press return to close."; exit 1
fi
mkdir -p "$DATA/state" "$DATA/working" "$DATA/Songs"
if [ ! -d "$MOUNT" ]; then
  echo "Opening Midify..."
  hdiutil attach "$IMAGE" -nobrowse -quiet -mountpoint "$MOUNT"
fi
export CLAVINOVA_STATE="$DATA/state"
export CLAVINOVA_WORK="$DATA/working"
export CLAVINOVA_LIBRARY="$DATA/Songs"
export HF_HOME="$MOUNT/models"
export HF_HUB_OFFLINE=1
export HF_HUB_DISABLE_SYMLINKS=1
export PYTHONPYCACHEPREFIX="$DATA/compiled"
export PATH="$MOUNT/tools/bin:$PATH"

# Read the big files straight through before starting. A flash drive is slow at jumping about and
# quick at reading in order, and the first song has to load about 200 MB of them. Costs nothing
# when they are already in memory.
echo "Getting ready (first time after plugging in takes a minute)..."
find "$MOUNT/python/lib/python3.11/site-packages/torch" "$MOUNT/models" "$MOUNT/tools" \
     -type f -size +1M -exec cat {} + > /dev/null 2>&1 || true

"$MOUNT/python/bin/python3.11" "$MOUNT/app/desktop.py" || true
hdiutil detach "$MOUNT" -quiet 2>/dev/null || true
'''

READ_ME = """Midify, portable
================

1. Double-click "Start Midify.command".
2. Your songs are saved in "Midify Data/Songs" on this drive, where your piano can read them.

Nothing is installed on the Mac, and nothing is left behind when you unplug the drive.

Needs an Apple Silicon Mac (M1 or newer).

If macOS says it cannot check this for malicious software, it was downloaded rather than made on
your own Mac. Open Terminal and run, with this drive's name in place of DRIVE:

    xattr -dr com.apple.quarantine "/Volumes/DRIVE"
"""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("built", help="the Midify folder made by tools/make_portable.py")
    ap.add_argument("--out", help="where to put the finished files (default: beside the built folder)")
    args = ap.parse_args()
    built = Path(args.built)
    if not (built / "python" / "bin" / "python3.11").exists():
        print(f"{built} does not look like a built portable Midify.")
        return 1
    out = Path(args.out) if args.out else built.parent
    out.mkdir(parents=True, exist_ok=True)
    image = out / "Midify-Portable.dmg"
    if image.exists():
        image.unlink()

    print(f"Packing {built} into one file. Reading thousands of small files takes a while.")
    started = time.time()
    made = subprocess.run(["hdiutil", "create", "-srcfolder", str(built), "-volname", "Midify Portable",
                           "-format", "UDZO", "-imagekey", "zlib-level=6", "-quiet", str(image)],
                          capture_output=True, text=True)
    if made.returncode != 0:
        print("Could not make the disk image:\n" + (made.stderr or "")[-500:])
        return 1

    launcher = out / "Start Midify.command"
    launcher.write_text(LAUNCHER)
    launcher.chmod(0o755)
    (out / "Read Me.txt").write_text(READ_ME)

    size = image.stat().st_size / 1e6
    print(f"\nDone in {(time.time() - started) / 60:.0f} minutes.")
    print(f"  Midify-Portable.dmg   {size:.0f} MB")
    print(f"  Start Midify.command  double-click this one")
    print("\nCopy those two onto any flash drive. That is two files, not fourteen thousand.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
