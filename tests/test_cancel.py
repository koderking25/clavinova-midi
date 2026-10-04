"""Stopping a song, and what comes back when the app reopens.

    .venv/bin/python tests/test_cancel.py

From a real report: a song was made with the Mac nearly full, Cancel appeared to do nothing, and
quitting the app brought the song back and made a second copy of it.
"""
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLAY = Path(tempfile.mkdtemp(prefix="cancel-test-"))
os.environ.update(CLAVINOVA_STATE=str(PLAY / "state"), CLAVINOVA_WORK=str(PLAY / "work"),
                  CLAVINOVA_LIBRARY=str(PLAY / "library"), CLAVINOVA_TEST_DISK_IMAGES="1")
sys.path.insert(0, str(ROOT / "app"))

import pipeline  # noqa: E402
import server  # noqa: E402

passed, failed = [], []


def check(name, ok, detail=""):
    (passed if ok else failed).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{(': ' + detail) if detail else ''}")


def fresh():
    with server.LOCK:
        server.JOBS.clear()
        server.ORDER.clear()
    while not server.Q.empty():
        server.Q.get_nowait()


def a_job(title, status, created=None):
    job = pipeline.Job(title=title, mode="piano", source="youtube", video_id="aaaaaaaaaaa")
    job.status = status
    job.created = created or time.time()
    with server.LOCK:
        server.JOBS[job.id] = job
        server.ORDER.append(job.id)
    return job


def saved():
    try:
        return json.loads((PLAY / "state" / "queue.json").read_text())
    except Exception:
        return []


def main():
    (PLAY / "library").mkdir(parents=True, exist_ok=True)

    print("Cancelling a song that is part way through")
    fresh()
    job = a_job("Angels We Have Heard on High", "running")
    reply = server.cancel_job(job.id)
    check("the window is told something happened at once", reply["stage"] == "Stopping", reply["stage"])
    check("the song is marked as stopped", job.cancelled is True)
    rows = saved()
    check("that is written down immediately", any(r.get("cancelled") for r in rows),
          f"{len(rows)} row(s) saved")

    print("\nQuitting and reopening after a cancel")
    fresh()
    server.restore_queue()
    check("the cancelled song does not come back", len(server.JOBS) == 0,
          f"{len(server.JOBS)} job(s) restored")

    print("\nQuitting while a song is genuinely being made")
    fresh()
    job = a_job("Half Finished Song", "running")
    server.save_queue()
    fresh()
    server.restore_queue()
    carried = list(server.JOBS.values())
    check("it does come back, so nothing is lost", len(carried) == 1, f"{len(carried)} restored")
    if carried:
        check("and it says why it is there",
              "carried on" in (carried[0].stage or ""), carried[0].stage)

    print("\nQuitting after the song had already finished its file")
    fresh()
    job = a_job("Already Done Song", "running", created=time.time() - 60)
    (PLAY / "library" / "Already Done Song.mid").write_bytes(b"MThd" + b"\x00" * 200)
    server.save_queue()
    fresh()
    server.restore_queue()
    check("it is not made a second time", len(server.JOBS) == 0,
          f"{len(server.JOBS)} job(s) restored; this is the duplicate that appeared as 'Song 2.mid'")

    print("\nAn older song of the same name must not be mistaken for this one")
    fresh()
    old_file = PLAY / "library" / "Older Song.mid"
    old_file.write_bytes(b"MThd" + b"\x00" * 200)
    os.utime(old_file, (time.time() - 9999, time.time() - 9999))
    a_job("Older Song", "running")
    server.save_queue()
    fresh()
    server.restore_queue()
    check("the song still gets made", len(server.JOBS) == 1,
          f"{len(server.JOBS)} restored")

    print()
    print(f"{len(passed)} passed, {len(failed)} failed")
    for f in failed:
        print(f"  FAILED: {f}")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        shutil.rmtree(PLAY, ignore_errors=True)
    sys.exit(code)
