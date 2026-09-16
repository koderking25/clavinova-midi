#!/bin/bash
# One-time setup for Midify on a Mac with Apple Silicon (M1 or newer).
# Safe to run again: it skips anything already installed.
set -euo pipefail
cd "$(dirname "$0")"

say() { printf '\n==> %s\n' "$1"; }
fail() { printf '\nSetup stopped: %s\n' "$1" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || fail "this app runs on macOS."
[ "$(uname -m)" = "arm64" ] || fail "this app needs a Mac with Apple Silicon (M1 or newer)."

free_kb=$(df -k . | awk 'NR==2 {print $4}')
if [ ! -x .venv/bin/python ] && [ "$free_kb" -lt 2500000 ]; then
  fail "you need about 2.5 GB free (you have $((free_kb / 1024)) MB). Free up some space and run this again."
fi

export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
export UV_NO_CACHE=1                      # no second copy of every package: disk space matters

command -v brew >/dev/null || fail "Homebrew is needed. Install it from https://brew.sh, then run this again."

say "Installing tools with Homebrew: ffmpeg (audio), fluid-synth (previews), deno (YouTube), uv (Python)"
for tool in ffmpeg fluid-synth deno uv; do
  brew list --versions "$tool" >/dev/null 2>&1 || brew install "$tool"
done

say "Installing Python 3.11"
uv python install 3.11

if [ ! -x .venv/bin/python ]; then
  if [ -e .venv ] || [ -L .venv ]; then
    say "Clearing a broken Python environment"     # a dangling link or half-built folder blocks uv
    rm -rf .venv
  fi
  say "Creating the app's Python environment"
  uv venv --python 3.11 .venv
fi

say "Installing the exact package versions this app was tested with"
uv pip install --python .venv/bin/python --no-deps -r requirements.lock

say "Downloading the AI models (about 80 MB, one time only) and checking they load"
.venv/bin/python - <<'EOF'
import os, sys, warnings
warnings.filterwarnings("ignore")
sys.path.insert(0, "app")
import pipeline
pipeline.MODELS.warm_up()
if not pipeline.MODELS.ready:
    sys.exit(f"The AI models did not load: {pipeline.MODELS.error}")
print("All three AI models loaded.")
EOF

if [ "${SKIP_SELFTEST:-0}" != "1" ]; then
  say "Checking that everything works (about 3 minutes)"
  .venv/bin/python tests/selftest.py 2>/dev/null || fail "the self-test found a problem. The lines above say which check failed."
fi

chmod +x "Midify.command"
say "Done. Double-click 'Midify.command' to start the app."
