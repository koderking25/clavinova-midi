"""Build Convertify.app.

Same family as Midify, built the same way, and independent of it: its own bundle, its own small
Python environment, its own releases. It needs no AI models and no torch, so where Midify needs
750 MB this needs about 90 MB, and ffmpeg travels inside the app so a Mac with nothing installed
can still use it.

    .venv/bin/python mac/build_convertify.py
"""
import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "mac" / "build"
NAME = "Convertify"
VERSION = "1.1.0"
BUNDLE_ID = "com.koderking25.convertify"
APP = OUT / f"{NAME}.app"
SHARED = ("sources.py", "downloader.py", "usb.py", "platform_bits.py")

LAUNCHER = r"""#!/bin/bash
# Opens Convertify. The first run builds a small Python environment; after that it starts at once.
set -u
BUNDLE="$(cd "$(dirname "$0")/.." && pwd)"
RES="$BUNDLE/Resources"
SUPPORT="${CONVERTIFY_SUPPORT:-$HOME/Library/Application Support/Convertify}"
LOG="$HOME/Library/Logs/Convertify.log"
mkdir -p "$SUPPORT" "$HOME/Library/Logs"

export PATH="$RES/tools/bin:/opt/homebrew/bin:/usr/local/bin:$HOME/.local/bin:$PATH"
export PYTHONPYCACHEPREFIX="$SUPPORT/pycache"       # never write compiled code inside a signed app
export CONVERTIFY_STATE="$SUPPORT/state"
export CLAVINOVA_STATE="$SUPPORT/state"            # the shared parts use this name

# Convertify updates itself with Midify's machinery, pointed at its own releases. Those are
# marked pre-release so they never become GitHub's "latest" and confuse Midify, which is why this
# reads the list of releases rather than the latest one.
export UPDATE_ASSET="Convertify.dmg"
export UPDATE_LEGACY_ASSET="Convertify.dmg"
export UPDATE_TAG_PREFIX="convertify-v"
export UPDATE_FROM_LIST=1
export UPDATE_ALLOW_PRERELEASE=1
export CONVERTIFY_VERSION="__VERSION__"
export CONVERTIFY_WORK="$SUPPORT/work"

say() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >>"$LOG"; }
alert() { osascript -e "display dialog \"$1\" buttons {\"OK\"} default button 1 with title \"Convertify\"" >/dev/null 2>&1; }

VENV="$SUPPORT/venv"
if [ ! -x "$VENV/bin/python" ]; then
  say "first run: building the environment"
  # In the background on purpose: a dialog waits for a click, and the setup should not.
  alert "Setting Convertify up. This takes about a minute, and only happens once." &
  if ! command -v uv >/dev/null 2>&1; then
    say "installing uv"
    curl -LsSf https://astral.sh/uv/install.sh | sh >>"$LOG" 2>&1
    export PATH="$HOME/.local/bin:$PATH"
  fi
  if ! command -v uv >/dev/null 2>&1; then
    alert "Convertify could not set itself up: it needs an internet connection the first time."
    exit 1
  fi
  rm -rf "$VENV"
  uv python install 3.11 >>"$LOG" 2>&1
  uv venv --python 3.11 "$VENV" >>"$LOG" 2>&1
  uv pip install --python "$VENV/bin/python" -r "$RES/requirements.txt" >>"$LOG" 2>&1 || {
    alert "Convertify could not finish setting up. The details are in ~/Library/Logs/Convertify.log"
    exit 1
  }
  say "environment ready"
fi

cd "$RES"
exec "$VENV/bin/python" "$RES/convertify/window.py" >>"$LOG" 2>&1
"""

REQUIREMENTS = """yt-dlp[default]
fastapi
uvicorn
pyobjc-framework-Cocoa
pyobjc-framework-WebKit
certifi
"""


def run(*cmd):
    return subprocess.run(cmd, capture_output=True, text=True)


def main():
    shutil.rmtree(APP, ignore_errors=True)
    macos = APP / "Contents" / "MacOS"
    res = APP / "Contents" / "Resources"
    macos.mkdir(parents=True)
    (res / "convertify").mkdir(parents=True)
    (res / "app").mkdir(parents=True)

    for name in ("engine.py", "window.py", "page.html"):
        shutil.copy2(ROOT / "convertify" / name, res / "convertify" / name)
    for name in SHARED:
        shutil.copy2(ROOT / "app" / name, res / "app" / name)
    # window.py looks for the shared parts beside it, as it does in the repo
    (res / "convertify" / "__init__.py").write_text("")
    (res / "requirements.txt").write_text(REQUIREMENTS)

    print("  bundling ffmpeg, so a Mac with nothing installed can still make an MP3")
    sys.path.insert(0, str(ROOT / "tools"))
    import bundle_tools
    placed = bundle_tools.bundle("ffmpeg", res / "tools", say=lambda m: print("  " + m.strip()))
    if not bundle_tools.check(placed, say=lambda m: print("  " + m.strip())):
        print("  ffmpeg would not travel properly; stopping rather than shipping something broken")
        return 1

    launcher = macos / NAME
    launcher.write_text(LAUNCHER.replace("__VERSION__", VERSION))
    launcher.chmod(0o755)

    plist = {
        "CFBundleName": NAME,
        "CFBundleDisplayName": NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleVersion": VERSION,
        "CFBundleShortVersionString": VERSION,
        "CFBundleExecutable": NAME,
        "CFBundlePackageType": "APPL",
        "LSMinimumSystemVersion": "12.0",
        "LSRequiresNativeExecution": True,            # never start under Rosetta
        "LSArchitecturePriority": ["arm64"],
        "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": "Convertify",
    }
    with open(APP / "Contents" / "Info.plist", "wb") as f:
        plistlib.dump(plist, f)

    run("codesign", "--force", "--deep", "--sign", "-", str(APP))
    checked = run("codesign", "--verify", "--verbose=1", str(APP))
    size = sum(f.stat().st_size for f in APP.rglob("*") if f.is_file()) / 1e6
    print(f"  built {APP} ({size:.0f} MB in the bundle, about 90 MB once set up)")
    print(f"  signature: {(checked.stderr or '').strip()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
