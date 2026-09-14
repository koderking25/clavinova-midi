"""Batch to flash drive, tested on a fake FAT32 drive (a small disk image), never a real one.

    .venv/bin/python tests/test_batch.py

Refuses to run if a real flash drive is plugged in, so it can never write to one.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
TMP = Path(tempfile.mkdtemp(prefix="batch-test-"))
os.environ.update(CLAVINOVA_LIBRARY=str(TMP / "library"), CLAVINOVA_STATE=str(TMP / "state"),
                  CLAVINOVA_WORK=str(TMP / "work"), CLAVINOVA_TEST_DISK_IMAGES="1")
sys.path.insert(0, str(REPO / "app"))
import pipeline  # noqa: E402
import server  # noqa: E402
import usb  # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('   ' + detail) if detail else ''}")


real = [d for d in usb.list_drives()]
if real:
    sys.exit(f"A flash drive is plugged in ({', '.join(d['name'] for d in real)}). Unplug it: this test never writes to a real drive.")

IMG = TMP / "fake-drive.dmg"
subprocess.run(["hdiutil", "create", "-size", "40m", "-fs", "MS-DOS FAT32", "-volname", "CLAVTEST", "-layout", "MBRSPUD", str(IMG)],
               check=True, capture_output=True)


def attach():
    out = subprocess.run(["hdiutil", "attach", str(IMG), "-nobrowse"], check=True, capture_output=True, text=True).stdout
    return next(line.split("\t")[-1].strip() for line in out.splitlines() if "/Volumes/" in line)


def detach(mount):
    subprocess.run(["hdiutil", "detach", mount, "-force"], capture_output=True)


def finished_job(title, drive_path, folder="My Batch"):
    """A job as it looks once its song is made: a real MIDI file in the library."""
    src = next((Path.home() / "Music" / "Clavinova MIDI").glob("*.mid"), None) or (REPO / "tests/fixtures/piano2h.mid")
    dest = server.LIB / usb.safe_filename(title)
    shutil.copyfile(src, dest)
    job = pipeline.Job(title=title, mode="piano", source="upload", status="done",
                       result={"file": dest.name, "title": title}, send_to_drive=True, drive_path=drive_path,
                       drive_folder=folder)
    with server.LOCK:
        server.JOBS[job.id] = job
        server.ORDER.append(job.id)
    return job


def run_sender_once():
    """One pass of the background sender, without its sleep."""
    with server.LOCK:
        waiting = [server.JOBS[i] for i in server.ORDER if i in server.JOBS and server.JOBS[i].drive_status == "waiting"]
    with server.SEND_LOCK:
        for job in waiting:
            if not server.try_send(job):
                break
    server.save_queue()


print("1. Songs finishing while no drive is plugged in")
mount = attach()
drive_path = mount
detach(mount)
jobs = [finished_job(t, drive_path) for t in ("Clair de Lune", "Debussy - Arabesque No. 1 4", "River Flows in You")]
for j in jobs:
    server.try_send(j)
check("each one waits for the drive", all(j.drive_status == "waiting" for j in jobs), str([j.drive_status for j in jobs]))

print("2. The drive is plugged in")
mount = attach()
run_sender_once()
folder = Path(mount) / "My Batch"
names = sorted(p.name for p in folder.glob("*.mid")) if folder.exists() else []
check("all three copied, numbered in the order they were added", names == ["01 Clair de Lune.mid",
      "02 Debussy - Arabesque No 1 4.mid", "03 River Flows in You.mid"], str(names))
check("each job says where its song went", [j.drive_file for j in jobs] == [f"My Batch/{n}" for n in names], str([j.drive_file for j in jobs]))
check("the copies are identical to the songs", all((folder / n).read_bytes() == (server.LIB / j.result["file"]).read_bytes()
                                                for n, j in zip(names, jobs)))
check("no hidden Mac files on the drive", not [p.name for p in Path(mount).rglob("._*")], str([p.name for p in Path(mount).rglob("._*")]))

print("3. More songs later continue the numbering")
later = finished_job("Experience", drive_path)
with server.SEND_LOCK:
    server.try_send(later)
check("the next song is number 04", later.drive_file == "My Batch/04 Experience.mid", str(later.drive_file))

print("4. The drive is pulled out partway through a batch")
a = finished_job("Idea 10", drive_path)
b = finished_job("Valzer d'Inverno", drive_path)
with server.SEND_LOCK:
    server.try_send(a)
detach(mount)
with server.SEND_LOCK:
    server.try_send(b)
check("the song made before unplugging was copied", a.drive_status == "sent", str(a.drive_status))
check("the song made after unplugging waits", b.drive_status == "waiting", str(b.drive_status))

print("5. The app quits and reopens while that song is still waiting")
queued_upload = TMP / "work" / "uploads" / "123.mp3"
queued_upload.write_bytes(b"audio")
stray_upload = TMP / "work" / "uploads" / "999.mp3"
stray_upload.write_bytes(b"left over from a crash")
queued = pipeline.Job(title="Not made yet", mode="piano", source="upload", upload_path=str(queued_upload),
                      send_to_drive=True, drive_path=drive_path, drive_folder="My Batch")
running = pipeline.Job(title="Being made when the app quit", mode="piano", source="youtube", video_id="abcdefghijk",
                       status="running", send_to_drive=True, drive_folder="My Batch")
with server.LOCK:
    for j in (queued, running):
        server.JOBS[j.id] = j
        server.ORDER.append(j.id)
server.save_queue()
saved = json.loads(server.QUEUE_FILE.read_text())
check("the saved queue holds the waiting, queued and running songs", {s["title"] for s in saved} ==
      {"Valzer d'Inverno", "Not made yet", "Being made when the app quit"}, str(sorted(s["title"] for s in saved)))
with server.LOCK:                                         # a fresh app: nothing in memory
    server.JOBS.clear()
    server.ORDER.clear()
while not server.Q.empty():
    server.Q.get_nowait()
server.clean_leftovers()
check("start-up cleaning keeps the queued song's recording", queued_upload.exists())
check("and still removes a stray one", not stray_upload.exists())
server.restore_queue()
restored = {j.title: j for j in server.JOBS.values()}
check("all three are back", set(restored) == {"Valzer d'Inverno", "Not made yet", "Being made when the app quit"}, str(sorted(restored)))
check("the song that was being made starts again", restored["Being made when the app quit"].status == "queued")
check("two songs are in line to be made", server.Q.qsize() == 2, f"queue size {server.Q.qsize()}")
mount = attach()
run_sender_once()
check("the waiting song is copied once the drive is back, as 06",
      restored["Valzer d'Inverno"].drive_file == "My Batch/06 Valzer d Inverno.mid", str(restored["Valzer d'Inverno"].drive_file))

detach(mount)
shutil.rmtree(TMP, ignore_errors=True)
print(f"\n{sum(results)} of {len(results)} checks passed.")
sys.exit(0 if all(results) else 1)
