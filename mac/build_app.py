"""Build "Clavinova MIDI Maker.app" and a zip of it.

    .venv/bin/python mac/build_app.py

The app holds the code and the icon. The Python environment and the AI models are
set up once on first launch, into Application Support, because they come to about
2 GB and have no business inside a download.

The app is signed ad hoc (no Apple developer account), so a copy downloaded from
the web is quarantined by macOS. Opening it the first time needs a right click and
Open, or the one line install in mac/install.sh, which avoids that entirely.
"""
import os
import plistlib
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BUILD = os.path.join(HERE, "build")
NAME = "Clavinova MIDI Maker"
APP = os.path.join(BUILD, f"{NAME}.app")
VERSION = "1.2.0"

LAUNCHER = r"""#!/bin/bash
# Starts the app: sets up the environment on first run, then runs the local
# server and opens the browser. Quitting the app stops the server.
set -u
BUNDLE="$(cd "$(dirname "$0")/.." && pwd)"
RES="$BUNDLE/Resources"
SUPPORT="$HOME/Library/Application Support/Clavinova MIDI Maker"
LOG_DIR="$HOME/Library/Logs"
LOG="$LOG_DIR/Clavinova MIDI Maker.log"
URL="http://127.0.0.1:8765"

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
export CLAVINOVA_WORK="$SUPPORT/work"
export CLAVINOVA_STATE="$SUPPORT/state"
mkdir -p "$SUPPORT" "$CLAVINOVA_WORK" "$CLAVINOVA_STATE" "$LOG_DIR"

say() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >>"$LOG"; }
alert() { osascript -e "display dialog \"$1\" buttons {\"OK\"} default button 1 with title \"Clavinova MIDI Maker\"" >/dev/null 2>&1; }

# Already running? Just show it.
if curl -fsS -m 2 "$URL/api/status" >/dev/null 2>&1; then
  open "$URL"
  exit 0
fi

VENV="$SUPPORT/venv"
if [ ! -x "$VENV/bin/python" ]; then
  if ! command -v brew >/dev/null 2>&1; then
    alert "Clavinova MIDI Maker needs Homebrew for its audio tools.\n\nInstall it from https://brew.sh, then open this app again."
    exit 1
  fi
  osascript -e 'display dialog "First time setup: this downloads the AI models and takes about ten minutes. A Terminal window will show what it is doing." buttons {"Set up now", "Later"} default button 1 with title "Clavinova MIDI Maker"' >/dev/null 2>&1 || exit 0
  say "starting first run setup"
  osascript -e "tell application \"Terminal\" to do script \"bash '$RES/setup-app.sh'\"" >/dev/null 2>&1
  osascript -e 'tell application "Terminal" to activate' >/dev/null 2>&1
  for _ in $(seq 1 240); do            # up to 40 minutes, checked every 10 seconds
    [ -x "$VENV/bin/python" ] && break
    sleep 10
  done
  if [ ! -x "$VENV/bin/python" ]; then
    alert "Setup did not finish. The Terminal window shows why."
    exit 1
  fi
fi

say "opening the window"
# desktop.py is the app itself: a real Mac window with the engine running inside it.
# No browser, no tab. Quitting the window quits everything.
exec "$VENV/bin/python" "$RES/app/desktop.py" >>"$LOG" 2>&1
"""

SETUP = r"""#!/bin/bash
# One time setup for Clavinova MIDI Maker: a private Python environment and the AI models.
set -euo pipefail
RES="$(cd "$(dirname "$0")" && pwd)"
SUPPORT="$HOME/Library/Application Support/Clavinova MIDI Maker"
VENV="$SUPPORT/venv"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
export UV_NO_CACHE=1

say() { printf '\n==> %s\n' "$1"; }
fail() { printf '\nSetup stopped: %s\n' "$1" >&2; exit 1; }

[ "$(uname -m)" = "arm64" ] || fail "this app needs a Mac with Apple Silicon (M1 or newer)."
free_kb=$(df -k "$HOME" | awk 'NR==2 {print $4}')
[ "$free_kb" -gt 2500000 ] || fail "you need about 2.5 GB free (you have $((free_kb / 1024)) MB)."
command -v brew >/dev/null || fail "Homebrew is needed. Install it from https://brew.sh and run this again."

say "Installing the audio tools (ffmpeg, fluid-synth, deno, uv)"
for tool in ffmpeg fluid-synth deno uv; do
  brew list --versions "$tool" >/dev/null 2>&1 || brew install "$tool"
done

say "Building a private Python for the app"
mkdir -p "$SUPPORT"
uv python install 3.11
[ -x "$VENV/bin/python" ] || uv venv --python 3.11 "$VENV"
uv pip install --python "$VENV/bin/python" --no-deps -r "$RES/requirements.lock"

say "Downloading the AI models (about 80 MB, once)"
CLAVINOVA_STATE="$SUPPORT/state" "$VENV/bin/python" - <<'PY'
import os, sys, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, os.path.join(os.environ["RES"], "app"))
import pipeline
pipeline.MODELS.warm_up()
sys.exit(0 if pipeline.MODELS.ready else f"models did not load: {pipeline.MODELS.error}")
PY

say "Done. You can close this window and open Clavinova MIDI Maker."
"""


def run(*cmd, **kw):
    return subprocess.run(cmd, check=True, capture_output=True, text=True, **kw)


def main():
    if os.path.exists(BUILD):
        shutil.rmtree(BUILD)
    os.makedirs(os.path.join(APP, "Contents", "MacOS"))
    res = os.path.join(APP, "Contents", "Resources")
    os.makedirs(res)

    icon = os.path.join(HERE, "icon.icns")
    if not os.path.exists(icon):
        run(sys.executable, os.path.join(HERE, "make_icon.py"))
    shutil.copy(icon, os.path.join(res, "icon.icns"))

    shutil.copytree(os.path.join(ROOT, "app"), os.path.join(res, "app"),
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copytree(os.path.join(ROOT, "tests"), os.path.join(res, "tests"),
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy(os.path.join(ROOT, "requirements.lock"), res)

    setup_path = os.path.join(res, "setup-app.sh")
    # RES has to be exported: the small Python block inside the setup script reads it.
    with open(setup_path, "w") as f:
        f.write(SETUP.replace('RES="$(cd "$(dirname "$0")" && pwd)"',
                              'RES="$(cd "$(dirname "$0")" && pwd)"; export RES'))
    os.chmod(setup_path, 0o755)

    launch = os.path.join(APP, "Contents", "MacOS", "launch")
    with open(launch, "w") as f:
        f.write(LAUNCHER)
    os.chmod(launch, 0o755)

    plist = {
        "CFBundleName": NAME,
        "CFBundleDisplayName": NAME,
        "CFBundleIdentifier": "com.koderking25.clavinova-midi-maker",
        "CFBundleVersion": VERSION,
        "CFBundleShortVersionString": VERSION,
        "CFBundlePackageType": "APPL",
        "CFBundleExecutable": "launch",
        "CFBundleIconFile": "icon",
        "LSMinimumSystemVersion": "12.0",
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.music",
        "NSHumanReadableCopyright": "Made with Claude Code",
    }
    with open(os.path.join(APP, "Contents", "Info.plist"), "wb") as f:
        plistlib.dump(plist, f)

    run("codesign", "--force", "--deep", "--sign", "-", APP)      # ad hoc: stops "damaged" errors
    zip_path = os.path.join(BUILD, "Clavinova-MIDI-Maker.zip")
    run("ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", APP, zip_path)

    size = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(APP) for f in fs)
    print(f"built {APP}")
    print(f"  app {size / 1e6:.1f} MB, zip {os.path.getsize(zip_path) / 1e6:.1f} MB")
    print(f"  signature: {run('codesign', '--verify', '--verbose=1', APP).stderr.strip()}")


if __name__ == "__main__":
    main()
