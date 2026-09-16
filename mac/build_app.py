"""Build "Midify.app" and a zip of it.

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
NAME = "Midify"
# Hidden support folder keeps its old name: it holds the private Python and AI models, and
# renaming it would force every existing install through the ten minute setup again.
SUPPORT_NAME = "Clavinova MIDI Maker"
APP = os.path.join(BUILD, f"{NAME}.app")
VERSION = "1.5.2"

LAUNCHER = r"""#!/bin/bash
# Starts the app: sets up the environment on first run, then opens the app's own
# window with the engine running inside it. Quitting the window stops everything.
set -u
BUNDLE="$(cd "$(dirname "$0")/.." && pwd)"
RES="$BUNDLE/Resources"
SUPPORT="${CLAVINOVA_SUPPORT:-$HOME/Library/Application Support/Clavinova MIDI Maker}"
LOG_DIR="$HOME/Library/Logs"
LOG="$LOG_DIR/Midify.log"

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
export CLAVINOVA_WORK="$SUPPORT/work"
export CLAVINOVA_STATE="$SUPPORT/state"
# Python caches compiled code next to the source by default, which here means inside the app.
# That added files to a signed app and broke its seal the first time it ran. Keep them out.
export PYTHONPYCACHEPREFIX="$SUPPORT/pycache"
mkdir -p "$SUPPORT" "$CLAVINOVA_WORK" "$CLAVINOVA_STATE" "$LOG_DIR"

say() { echo "$(date '+%Y-%m-%d %H:%M:%S') $*" >>"$LOG"; }
alert() { osascript -e "display dialog \"$1\" buttons {\"OK\"} default button 1 with title \"Midify\"" >/dev/null 2>&1; }

# No "already running" shortcut here. One used to open the page in a web browser, which is
# exactly what the app no longer does; desktop.py handles a second copy itself, in a window.

VENV="$SUPPORT/venv"
if [ ! -x "$VENV/bin/python" ]; then
  if ! command -v brew >/dev/null 2>&1; then
    alert "Midify needs Homebrew for its audio tools.\n\nInstall it from https://brew.sh, then open this app again."
    exit 1
  fi
  WHAT="First time setup: this downloads the AI models and takes about ten minutes."
  if [ -e "$VENV" ] || [ -L "$VENV" ]; then       # something is there but cannot run: repair, not first run
    WHAT="Midify needs to repair its setup: part of it is missing or was deleted. This takes about ten minutes."
  fi
  osascript -e "display dialog \"$WHAT A Terminal window will show what it is doing.\" buttons {\"Set up now\", \"Later\"} default button 1 with title \"Midify\"" >/dev/null 2>&1 || exit 0
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

# The app's real front door: a tiny native Apple Silicon program. A shell script as the main
# executable declares no architecture, and macOS once launched it through Rosetta as an Intel
# app, where it hung and every double click said "not responding". This program is arm64, so
# macOS knows exactly what it is. It sets up the same environment as the script and hands
# straight over to Python; only a first launch, with nothing installed yet, goes via the script.
NATIVE_LAUNCHER_C = r"""
#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <mach-o/dyld.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <time.h>
#include <unistd.h>

static void make_dirs(const char *path) {
    char tmp[PATH_MAX];
    snprintf(tmp, sizeof tmp, "%s", path);
    for (char *p = tmp + 1; *p; p++) {
        if (*p == '/') { *p = 0; mkdir(tmp, 0755); *p = '/'; }
    }
    mkdir(tmp, 0755);
}

static void parent(char *path) {
    char *slash = strrchr(path, '/');
    if (slash) *slash = 0;
}

int main(void) {
    char exe[PATH_MAX], real[PATH_MAX];
    uint32_t size = sizeof exe;
    if (_NSGetExecutablePath(exe, &size) != 0 || !realpath(exe, real)) return 1;
    char macos[PATH_MAX], contents[PATH_MAX];
    snprintf(macos, sizeof macos, "%s", real);
    parent(macos);                                         /* .../Contents/MacOS */
    snprintf(contents, sizeof contents, "%s", macos);
    parent(contents);                                      /* .../Contents */

    /* The app used to be called "Clavinova MIDI Maker", and an update installs into that same place.
       Rename it before anything runs from inside it, and only if nothing is already called Midify. */
    {
        char bundle[PATH_MAX], dir[PATH_MAX], renamed[PATH_MAX];
        snprintf(bundle, sizeof bundle, "%s", contents);
        parent(bundle);                                    /* .../Something.app */
        const char *base = strrchr(bundle, '/');
        if (base && strcmp(base + 1, "Clavinova MIDI Maker.app") == 0) {
            snprintf(dir, sizeof dir, "%s", bundle);
            parent(dir);
            snprintf(renamed, sizeof renamed, "%s/Midify.app", dir);
            if (access(renamed, F_OK) != 0 && rename(bundle, renamed) == 0) {
                snprintf(contents, sizeof contents, "%s/Contents", renamed);
                snprintf(macos, sizeof macos, "%s/Contents/MacOS", renamed);
            }
        }
    }

    const char *home = getenv("HOME");
    if (!home || !*home) return 1;
    char support[PATH_MAX], work[PATH_MAX], state[PATH_MAX], pycache[PATH_MAX];
    char logdir[PATH_MAX], logfile[PATH_MAX], python[PATH_MAX], desktop[PATH_MAX], script[PATH_MAX];
    snprintf(support, sizeof support, "%s/Library/Application Support/Clavinova MIDI Maker", home);
    snprintf(work, sizeof work, "%s/work", support);
    snprintf(state, sizeof state, "%s/state", support);
    snprintf(pycache, sizeof pycache, "%s/pycache", support);
    snprintf(logdir, sizeof logdir, "%s/Library/Logs", home);
    snprintf(logfile, sizeof logfile, "%s/Midify.log", logdir);
    snprintf(python, sizeof python, "%s/venv/bin/python", support);
    snprintf(desktop, sizeof desktop, "%s/Resources/app/desktop.py", contents);
    snprintf(script, sizeof script, "%s/launch", macos);

    const char *old_path = getenv("PATH");
    char path[8192];
    snprintf(path, sizeof path, "/opt/homebrew/bin:/usr/local/bin:%s",
             old_path && *old_path ? old_path : "/usr/bin:/bin:/usr/sbin:/sbin");
    setenv("PATH", path, 1);
    setenv("CLAVINOVA_WORK", work, 1);
    setenv("CLAVINOVA_STATE", state, 1);
    setenv("PYTHONPYCACHEPREFIX", pycache, 1);
    make_dirs(work);
    make_dirs(state);
    make_dirs(logdir);

    if (access(python, X_OK) != 0) {                        /* first launch: the script sets things up */
        execl("/bin/bash", "/bin/bash", script, (char *)NULL);
        return 127;
    }
    int fd = open(logfile, O_WRONLY | O_CREAT | O_APPEND, 0644);
    if (fd >= 0) { dup2(fd, STDOUT_FILENO); dup2(fd, STDERR_FILENO); close(fd); }
    char stamp[32];
    time_t now = time(NULL);
    strftime(stamp, sizeof stamp, "%Y-%m-%d %H:%M:%S", localtime(&now));
    dprintf(STDOUT_FILENO, "%s opening the window\n", stamp);
    execl(python, python, desktop, (char *)NULL);
    dprintf(STDERR_FILENO, "%s could not start Python (%s)\n", stamp, strerror(errno));
    return 127;
}
"""

SETUP = r"""#!/bin/bash
# One time setup for Midify: a private Python environment and the AI models.
set -euo pipefail
RES="$(cd "$(dirname "$0")" && pwd)"
SUPPORT="${CLAVINOVA_SUPPORT:-$HOME/Library/Application Support/Clavinova MIDI Maker}"
VENV="$SUPPORT/venv"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
export UV_NO_CACHE=1
export PYTHONPYCACHEPREFIX="$SUPPORT/pycache"          # never write compiled code inside the app

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
# Anything at the venv path that cannot run Python is in the way: a shortcut to a folder that has
# been deleted, or a half-built environment. uv stops with "File exists", so clear it first.
if [ ! -x "$VENV/bin/python" ] && { [ -e "$VENV" ] || [ -L "$VENV" ]; }; then
  say "Clearing a broken Python folder and building it again"
  rm -rf "$VENV"
fi
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

say "Done. You can close this window and open Midify."
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

    executable = "launch"                         # fallback: the script, if no compiler is available
    clang = shutil.which("clang")
    if clang:
        native = os.path.join(APP, "Contents", "MacOS", NAME)
        src = os.path.join(BUILD, "launcher.c")
        with open(src, "w") as f:
            f.write(NATIVE_LAUNCHER_C)
        run(clang, "-O2", "-arch", "arm64", "-mmacosx-version-min=12.0", "-Wall", "-Werror", "-o", native, src)
        os.remove(src)
        executable = NAME
    else:
        print("  no C compiler found: the app starts through its script (it still works)")

    plist = {
        "CFBundleName": NAME,
        "CFBundleDisplayName": NAME,
        "CFBundleIdentifier": "com.koderking25.clavinova-midi-maker",
        "CFBundleVersion": VERSION,
        "CFBundleShortVersionString": VERSION,
        "CFBundlePackageType": "APPL",
        "CFBundleExecutable": executable,
        "CFBundleIconFile": "icon",
        "LSMinimumSystemVersion": "12.0",
        # The executable is a shell script, which declares no architecture. Left to guess,
        # macOS once launched it through Rosetta as an Intel app, and it hung "not responding".
        # This app only runs on Apple Silicon, so say so and forbid Rosetta outright.
        "LSRequiresNativeExecution": True,
        "LSArchitecturePriority": ["arm64"],
        "NSHighResolutionCapable": True,
        "LSApplicationCategoryType": "public.app-category.music",
        "NSHumanReadableCopyright": "Made with Claude Code",
    }
    with open(os.path.join(APP, "Contents", "Info.plist"), "wb") as f:
        plistlib.dump(plist, f)

    run("codesign", "--force", "--deep", "--sign", "-", APP)      # ad hoc: stops "damaged" errors
    zip_path = os.path.join(BUILD, "Midify.zip")
    run("ditto", "-c", "-k", "--sequesterRsrc", "--keepParent", APP, zip_path)

    size = sum(os.path.getsize(os.path.join(dp, f)) for dp, _, fs in os.walk(APP) for f in fs)
    print(f"built {APP}")
    print(f"  app {size / 1e6:.1f} MB, zip {os.path.getsize(zip_path) / 1e6:.1f} MB")
    print(f"  signature: {run('codesign', '--verify', '--verbose=1', APP).stderr.strip()}")


if __name__ == "__main__":
    main()
