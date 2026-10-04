"""Midify on Windows: start the engine, then open it in the browser.

The Mac app puts the page in its own window using PyObjC, which only exists on macOS. On Windows
the same engine runs and the default browser shows it, which needs nothing installed.

**Never run.** There is no Windows machine here and an Apple Silicon Mac cannot execute Windows
binaries, so this is written from documented behaviour and checked only for syntax and imports.
Expect to fix something the first time it runs on a real PC.
"""
import os
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

PORT = int(os.environ.get("CLAVINOVA_PORT", "8765"))
URL = f"http://127.0.0.1:{PORT}"


def already_running(port):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.3)
        return s.connect_ex(("127.0.0.1", port)) == 0


def wait_for_engine(port, seconds=90):
    until = time.time() + seconds
    while time.time() < until:
        if already_running(port):
            return True
        time.sleep(0.4)
    return False


def main():
    if already_running(PORT):
        print(f"Midify is already open. Showing it at {URL}")
        webbrowser.open(URL)
        return 0

    import server
    threading.Thread(target=server.start_background, daemon=True).start()

    def open_when_ready():
        if wait_for_engine(PORT):
            webbrowser.open(URL)
        else:
            print(f"The engine did not start within 90 seconds. Try opening {URL} yourself.")

    threading.Thread(target=open_when_ready, daemon=True).start()
    print(f"Midify is starting. It will open in your browser at {URL}")
    print("Closing this window stops Midify.")

    import uvicorn
    uvicorn.run(server.app, host="127.0.0.1", port=PORT, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
