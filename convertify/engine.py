"""Convertify: a YouTube link in, an MP3 out.

Built from the same parts as Midify, and deliberately separate from it. It shares the piece that
needs constant attention, the YouTube downloader, which keeps itself current as YouTube changes,
and it shares nothing else: no AI models, no transcription, no 750 MB of machine learning.

Runs on your own machine over your own connection, like Midify. A website cannot do this: a
browser is not allowed to fetch YouTube's media, so a hosted version would mean one server doing
every download, which YouTube blocks quickly and which is a different thing legally.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "app"))          # the shared parts live with Midify

# Point the shared parts at Convertify's own folder before they are imported. They fall back to a
# "state" folder beside their own code, which inside an app bundle means writing into the bundle:
# that breaks its signature and macOS then calls the app damaged.
_SUPPORT = Path(os.environ.get("CONVERTIFY_SUPPORT",
                               Path.home() / "Library" / "Application Support" / "Convertify"))
os.environ.setdefault("CLAVINOVA_STATE", str(_SUPPORT / "state"))
Path(os.environ["CLAVINOVA_STATE"]).mkdir(parents=True, exist_ok=True)

import downloader  # noqa: E402
import platform_bits  # noqa: E402
import sources  # noqa: E402
import usb  # noqa: E402
from fastapi import FastAPI, HTTPException  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

PORT = int(os.environ.get("CONVERTIFY_PORT", "8766"))
SAVE_TO = Path(os.environ.get("CONVERTIFY_SAVE", Path.home() / "Music" / "Convertify"))
WORK = Path(os.environ.get("CONVERTIFY_WORK", Path.home() / "Library" / "Application Support" /
                           "Convertify" / "work"))
STATE = Path(os.environ.get("CONVERTIFY_STATE", Path.home() / "Library" / "Application Support" /
                            "Convertify" / "state"))
FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
QUALITIES = {"320": "320k", "256": "256k", "192": "192k", "128": "128k"}
app = FastAPI()


@dataclass
class Job:
    link: str
    quality: str = "320"
    title: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    status: str = "queued"            # queued, downloading, converting, done, error, cancelled
    stage: str = "Waiting"
    progress: float = 0.0
    error: str = None
    file: str = None
    seconds: float = None
    cancelled: bool = False

    def public(self):
        return {k: getattr(self, k) for k in
                ("id", "link", "quality", "title", "status", "stage", "progress", "error", "file",
                 "seconds")}


JOBS = {}
ORDER = []
LOCK = threading.Lock()


def safe_name(title):
    """A file name that reads well and cannot wander out of the folder."""
    clean = re.sub(r"[^\w\s().,'-]", " ", title or "Audio", flags=re.UNICODE)
    clean = re.sub(r"\s+", " ", clean).strip(" .") or "Audio"
    return clean[:120]


def unique(folder, stem, suffix=".mp3"):
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / f"{stem}{suffix}"
    n = 2
    while out.exists():
        out = folder / f"{stem} {n}{suffix}"
        n += 1
    return out


def to_mp3(source, dest, quality, title, artist, on_progress=None, seconds=None):
    """Turn whatever was downloaded into an MP3, with its name and artist written into the file."""
    cmd = [FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y", "-i", str(source),
           "-vn", "-c:a", "libmp3lame", "-b:a", QUALITIES.get(quality, "320k"),
           "-metadata", f"title={title}", "-metadata", f"artist={artist}",
           "-id3v2_version", "3", str(dest)]
    run = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if run.returncode != 0 or not dest.exists():
        raise RuntimeError((run.stderr or "ffmpeg could not make the MP3")[-300:])
    return dest


def work_on(job):
    folder = WORK / job.id
    folder.mkdir(parents=True, exist_ok=True)
    try:
        job.status, job.stage = "downloading", "Finding the video"
        info = sources.video_info(job.link)
        job.title = info["title"]
        job.seconds = info.get("duration")
        job.stage = "Downloading"

        def progress(frac):
            job.progress = round(min(0.75, 0.75 * frac), 3)

        src = sources.download_youtube_audio(sources.youtube_id(job.link), folder,
                                             on_progress=progress, cancel=lambda: job.cancelled)
        if job.cancelled:
            raise KeyboardInterrupt
        job.status, job.stage, job.progress = "converting", "Making the MP3", 0.8
        dest = unique(SAVE_TO, safe_name(info["title"]))
        to_mp3(Path(src), dest, job.quality, info["title"], info.get("channel") or "", seconds=job.seconds)
        job.file, job.status, job.stage, job.progress = dest.name, "done", "Done", 1.0
        # What was asked for: show it, already picked out, in the folder it was saved to.
        platform_bits.reveal(dest)
    except KeyboardInterrupt:
        job.status, job.stage = "cancelled", "Cancelled"
    except ValueError as e:
        job.status, job.error = "error", str(e)
    except Exception as e:                                 # noqa: BLE001
        raw = str(e)
        if not job.cancelled and downloader.youtube_moved(raw):
            changed, said = downloader.update_because_it_failed(raw)
            if changed:
                job.stage = "Updating the downloader and trying again"
                try:
                    return work_on(job)
                except Exception:                          # noqa: BLE001
                    pass
        job.status = "error"
        job.error = sources.friendly_download_error(raw)
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def worker():
    while True:
        job = QUEUE.get()
        if job.cancelled:
            job.status, job.stage = "cancelled", "Cancelled"
            continue
        work_on(job)


import queue as _queue  # noqa: E402

QUEUE = _queue.Queue()


class Ask(BaseModel):
    link: str
    quality: str = "320"


class Which(BaseModel):
    file: str


@app.middleware("http")
async def only_this_mac(request, call_next):
    host = (request.headers.get("host") or "").rsplit(":", 1)[0]
    if host not in ("127.0.0.1", "localhost"):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    if request.method not in ("GET", "HEAD") and request.headers.get("x-convertify") != "1":
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return await call_next(request)


@app.get("/")
def page():
    return FileResponse(HERE / "page.html")


@app.get("/api/status")
def status():
    drives = []
    try:
        drives = usb.list_drives()
    except Exception:                                      # noqa: BLE001
        pass
    return {"save_to": str(SAVE_TO), "downloader": downloader.installed_version(),
            "qualities": list(QUALITIES), "drives": drives}


@app.get("/api/info")
def info(url: str):
    try:
        return sources.video_info(url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:                                 # noqa: BLE001
        raise HTTPException(400, sources.friendly_download_error(str(e)))


@app.post("/api/convert")
def convert(ask: Ask):
    if ask.quality not in QUALITIES:
        raise HTTPException(400, "Pick one of the usual qualities.")
    if not sources.youtube_id(ask.link):
        raise HTTPException(400, "That does not look like a YouTube link. Copy the address from "
                                 "the browser, or from Share then Copy link.")
    job = Job(link=ask.link.strip(), quality=ask.quality)
    with LOCK:
        JOBS[job.id] = job
        ORDER.append(job.id)
        while len(ORDER) > 40:
            old = ORDER[0]
            if JOBS.get(old) and JOBS[old].status in ("queued", "downloading", "converting"):
                break
            ORDER.pop(0)
            JOBS.pop(old, None)
    QUEUE.put(job)
    return job.public()


@app.get("/api/jobs")
def jobs():
    with LOCK:
        return [JOBS[i].public() for i in ORDER if i in JOBS][::-1]


@app.post("/api/jobs/{job_id}/cancel")
def cancel(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "No such conversion.")
    job.cancelled = True
    if job.status == "queued":
        job.status, job.stage = "cancelled", "Cancelled"
    elif job.status in ("downloading", "converting"):
        job.stage = "Stopping"
    return job.public()


@app.post("/api/reveal")
def reveal(which: Which):
    found = SAVE_TO / which.file
    if found.parent.resolve() != SAVE_TO.resolve() or not found.is_file():
        raise HTTPException(404, "That file is not in your Convertify folder.")
    platform_bits.reveal(found)
    return {"ok": True}


@app.post("/api/open-folder")
def open_folder():
    SAVE_TO.mkdir(parents=True, exist_ok=True)
    platform_bits.open_folder(SAVE_TO)
    return {"ok": True}


def keep_downloader_current():
    time.sleep(60)
    while True:
        try:
            busy = lambda: any(j.status in ("queued", "downloading", "converting") for j in JOBS.values())  # noqa: E731
            downloader.routine_check(busy=busy)
        except Exception:                                  # noqa: BLE001
            pass
        time.sleep(6 * 60 * 60)


def start_background():
    for folder in (SAVE_TO, WORK, STATE):
        folder.mkdir(parents=True, exist_ok=True)
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=keep_downloader_current, daemon=True).start()


if __name__ == "__main__":
    import uvicorn
    start_background()
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
