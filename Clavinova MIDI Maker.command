#!/bin/bash
# Double-click to start. Keep the window open while you use the app; close it to stop.
cd "$(dirname "$0")" || exit 1
export PATH="/opt/homebrew/bin:/usr/local/bin:$PATH"
PORT=8765
URL="http://127.0.0.1:$PORT"

if curl -s -m 2 "$URL/api/status" >/dev/null 2>&1; then
  echo "Clavinova MIDI Maker is already running. Opening it..."
  open "$URL"
  exit 0
fi

if [ ! -x .venv/bin/python ]; then
  echo "First time on this Mac: setting up the app (about 10 minutes, one time only)..."
  if ! ./setup.sh; then
    read -r -p "Setup did not finish. Scroll up for the reason, then press Return to close."
    exit 1
  fi
fi

echo "Starting Clavinova MIDI Maker..."
echo "Keep this window open while you use it. Close it to stop the app."
(
  for _ in $(seq 1 90); do
    sleep 1
    if curl -s -m 1 "$URL/api/status" >/dev/null 2>&1; then open "$URL"; exit 0; fi
  done
  echo "The app did not start. Scroll up in this window for the error."
) &
exec .venv/bin/python app/server.py
