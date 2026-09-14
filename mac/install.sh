#!/bin/bash
# Install Midify on a Mac, without fighting macOS about unsigned downloads.
#
#   curl -fsSL https://raw.githubusercontent.com/koderking25/clavinova-midi/main/mac/install.sh | bash
#
# It puts the code in ~/clavinova-midi, builds the app, and moves it to /Applications.
# The AI models are set up the first time you open the app.
set -euo pipefail

REPO="https://github.com/koderking25/clavinova-midi.git"
DEST="${CLAVINOVA_DIR:-$HOME/clavinova-midi}"
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"

say() { printf '\n==> %s\n' "$1"; }
fail() { printf '\nStopped: %s\n' "$1" >&2; exit 1; }

[ "$(uname -s)" = "Darwin" ] || fail "this app runs on macOS."
[ "$(uname -m)" = "arm64" ] || fail "this app needs a Mac with Apple Silicon (M1 or newer)."
command -v git >/dev/null || fail "git is needed. Install the Xcode command line tools with: xcode-select --install"
command -v brew >/dev/null || fail "Homebrew is needed for the audio tools. Install it from https://brew.sh, then run this again."

if [ -d "$DEST/.git" ]; then
  say "Updating $DEST"
  git -C "$DEST" pull --ff-only
else
  say "Downloading into $DEST"
  git clone --depth 1 "$REPO" "$DEST"
fi

say "Setting up the app environment (about ten minutes the first time)"
cd "$DEST"
SKIP_SELFTEST=1 ./setup.sh

say "Building the app"
.venv/bin/python mac/build_app.py

say "Putting it in your Applications folder"
rm -rf "/Applications/Midify.app" "/Applications/Clavinova MIDI Maker.app"   # the old name, from before the rename
cp -R "mac/build/Midify.app" /Applications/

say "Done. Open Midify from your Applications folder or Spotlight."
