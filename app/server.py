"""Clavinova MIDI Maker: a local web app at http://127.0.0.1:8765

Only this Mac can reach it. One song is converted at a time (8 GB of memory
fits one comfortably); the rest wait in line.
"""
import json
import os
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

# Homebrew tools (ffmpeg, fluidsynth, deno) must be findable even when started from Finder.
for _p in ("/usr/local/bin", "/opt/homebrew/bin"):
    if _p not in os.environ.get("PATH", "").split(":"):
        os.environ["PATH"] = _p + ":" + os.environ.get("PATH", "")

APP = Path(__file__).resolve().parent
sys.path.insert(0, str(APP))

# Once the separation model is downloaded, never ask Hugging Face again, so dropped-in files
# convert with no internet at all.
if (Path.home() / ".cache" / "huggingface" / "hub" / "models--adefossez--HTDemucs").is_dir():
    os.environ.setdefault("HF_HUB_OFFLINE", "1")

import mido  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile  # noqa: E402
from fastapi.responses import FileResponse, JSONResponse  # noqa: E402
from pydantic import BaseModel  # noqa: E402

import pipeline  # noqa: E402
import sources  # noqa: E402
import usb  # noqa: E402

ROOT = APP.parent
WORK = ROOT / "work"
UPLOADS = WORK / "uploads"
STATE = ROOT / "state"
LIB = Path(os.environ.get("CLAVINOVA_LIBRARY", Path.home() / "Music" / "Clavinova MIDI"))
META = LIB / ".clavinova"
PORT = int(os.environ.get("CLAVINOVA_PORT", "8765"))
MAX_UPLOAD = 400 * 1024 * 1024
ERROR_LOG = STATE / "errors.log"

for _d in (WORK, UPLOADS, STATE, LIB, META):
    _d.mkdir(parents=True, exist_ok=True)


def clean_leftovers():
    """Remove what a crash or force-quit left behind. Called only at start-up, never on import:
    each song's process imports this file too, and must not delete the upload it is about to read."""
    for d in WORK.iterdir():
        if d.is_dir() and d.name != "uploads":
            shutil.rmtree(d, ignore_errors=True)
    for f in UPLOADS.iterdir():
        f.unlink(missing_ok=True)
    for f in META.glob("*.partial"):
        f.unlink(missing_ok=True)

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
JOBS = {}
ORDER = []
Q = queue.Queue()
LOCK = threading.Lock()


@app.middleware("http")
async def only_this_mac(request: Request, call_next):
    host = (request.headers.get("host") or "").rsplit(":", 1)[0]
    if host not in ("127.0.0.1", "localhost"):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    # Web pages from other sites cannot add this header, so they cannot trigger actions here.
    if request.method not in ("GET", "HEAD") and request.headers.get("x-clavinova") != "1":
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return await call_next(request)


def log_error(job, exc):
    try:
        with open(ERROR_LOG, "a") as f:
            f.write(f"\n=== {time.ctime()} job {job.id} '{job.title}' mode {job.mode}\n")
            f.write("".join(traceback.format_exception(type(exc), exc, exc.__traceback__)))
            f.write(getattr(exc, "details", "") or "")      # the song process's own traceback
    except OSError:
        pass


def worker():
    while True:
        job = Q.get()
        if job.cancelled:
            job.status, job.stage = "cancelled", "Cancelled"
            continue
        job.status = "running"
        try:
            job.stage = "Getting ready"
            pipeline.MODELS_CHECKED.wait(timeout=900)     # never run alongside the start-up model check
            job.result = pipeline.run_in_child(job, WORK, LIB)
            job.status, job.stage, job.progress, job.eta = "done", "Done", 1.0, 0
        except pipeline.Cancelled:
            job.status, job.stage = "cancelled", "Cancelled"
        except pipeline.UserError as e:
            job.status, job.error = "error", str(e)
        except MemoryError as e:
            log_error(job, e)
            job.status, job.error = "error", "Your Mac ran out of memory. Close other apps, or try a shorter song."
        except Exception as e:  # noqa: BLE001
            log_error(job, e)
            job.status = "error"
            job.error = (f"Something went wrong ({type(e).__name__}). Try again, or try the other "
                         f"mode. Details were saved to {ERROR_LOG}.")
        finally:
            if job.upload_path:
                Path(job.upload_path).unlink(missing_ok=True)


def add_job(job):
    with LOCK:
        JOBS[job.id] = job
        ORDER.append(job.id)
        while len(ORDER) > 50:
            old = ORDER.pop(0)
            if JOBS.get(old) and JOBS[old].status in ("done", "error", "cancelled"):
                JOBS.pop(old, None)
    Q.put(job)
    return job.public()


def _ytdlp_version():
    try:
        import yt_dlp
        return yt_dlp.version.__version__
    except Exception:  # noqa: BLE001
        return "?"


@app.get("/")
def index():
    return FileResponse(APP / "static" / "index.html", headers={"Cache-Control": "no-store"})


@app.get("/api/status")
def status():
    busy = any(j.status == "running" for j in JOBS.values())
    return {
        "models_ready": pipeline.MODELS.ready,
        "models_state": pipeline.MODELS.state,
        "models_error": pipeline.MODELS.error,
        "busy": busy,
        "queued": sum(1 for j in JOBS.values() if j.status == "queued"),
        "disk_free": shutil.disk_usage(LIB).free,
        "ytdlp": _ytdlp_version(),
        "modes": pipeline.MODES,
        "melody_programs": {str(k): v for k, v in pipeline.MELODY_PROGRAMS.items()},
        "library": str(LIB),
    }


def _query(q):
    q = re.sub(r"\s+", " ", q or "").strip()[:120]
    if len(q) < 2:
        raise HTTPException(400, "Type at least two letters.")
    return q


# Two endpoints so the page can show YouTube results right away, even if BitMidi is slow.
@app.get("/api/search/youtube")
def search_youtube(q: str):
    try:
        return {"results": sources.search_youtube(_query(q))}
    except HTTPException:
        raise
    except sources.SlowDown as e:
        return {"results": [], "error": str(e)}
    except Exception:  # noqa: BLE001
        return {"results": [], "error": "Could not search YouTube right now. Check your internet connection, or drop in a file."}


@app.get("/api/search/bitmidi")
def search_bitmidi(q: str):
    try:
        return {"results": sources.search_bitmidi(_query(q))}
    except HTTPException:
        raise
    except Exception:  # noqa: BLE001
        return {"results": [], "error": "BitMidi did not answer."}


class YTJob(BaseModel):
    video_id: str
    title: str
    duration: float | None = None
    mode: str = "arrange"
    melody_program: int = 73
    split_hands: bool = True


def _check_options(mode, melody_program):
    if mode not in pipeline.MODES:
        raise HTTPException(400, "Unknown mode.")
    if melody_program not in pipeline.MELODY_PROGRAMS:
        raise HTTPException(400, "Unknown melody instrument.")


@app.post("/api/jobs")
def create_job(req: YTJob):
    if not re.fullmatch(r"[A-Za-z0-9_-]{11}", req.video_id):
        raise HTTPException(400, "That does not look like a YouTube video.")
    _check_options(req.mode, req.melody_program)
    if req.duration and req.duration > sources.MAX_SONG_SECONDS:
        raise HTTPException(400, "That video is longer than 15 minutes. Pick a shorter one.")
    job = pipeline.Job(title=req.title.strip()[:150] or "Song", mode=req.mode, source="youtube",
                       video_id=req.video_id, duration_hint=req.duration,
                       melody_program=req.melody_program, split_hands=req.split_hands)
    return add_job(job)


def _probe_seconds(path):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                           capture_output=True, text=True, timeout=30)
        return float(r.stdout.strip())
    except Exception:  # noqa: BLE001
        return None


@app.post("/api/upload")
async def upload(file: UploadFile = File(...), mode: str = Form("arrange"), melody_program: int = Form(73),
                 split_hands: bool = Form(True)):
    _check_options(mode, melody_program)
    name = Path(file.filename or "song").name
    stem, ext = Path(name).stem, Path(name).suffix.lower()
    if ext in (".mid", ".midi", ".kar"):
        data = await file.read(10 * 1024 * 1024 + 1)
        if len(data) > 10 * 1024 * 1024:
            raise HTTPException(400, "That MIDI file is too big.")
        tmp = UPLOADS / f"import-{time.time_ns()}.mid"
        tmp.write_bytes(data)
        try:
            m = mido.MidiFile(tmp)
            notes = sum(1 for t in m.tracks for msg in t if msg.type == "note_on" and msg.velocity > 0)
        except Exception:  # noqa: BLE001
            tmp.unlink(missing_ok=True)
            raise HTTPException(400, "That .mid file is damaged and would not play on the Clavinova.")
        if notes == 0:
            tmp.unlink(missing_ok=True)
            raise HTTPException(400, "That MIDI file has no notes in it.")
        dest = pipeline.unique_path(LIB, usb.safe_filename(stem))
        shutil.move(tmp, dest)
        (META / (dest.stem + ".json")).write_text(json.dumps({
            "file": dest.name, "title": stem, "mode": "imported", "notes": notes,
            "seconds": round(m.length, 1), "created": time.time(), "preview": False}))
        threading.Thread(target=_render_import_preview, args=(dest,), daemon=True).start()
        return {"imported": dest.name}
    if shutil.disk_usage(UPLOADS).free < 800 * 1024 * 1024:
        raise HTTPException(400, "Your Mac is almost out of space. Free up at least 1 GB first.")
    safe_ext = ext if re.fullmatch(r"\.[a-z0-9]{1,5}", ext) else ""
    dest = UPLOADS / f"{time.time_ns()}{safe_ext}"
    size = 0
    with open(dest, "wb") as f:
        while chunk := await file.read(1024 * 1024):
            size += len(chunk)
            if size > MAX_UPLOAD:
                f.close()
                dest.unlink(missing_ok=True)
                raise HTTPException(400, "That file is bigger than 400 MB. Try a shorter or smaller file.")
            f.write(chunk)
    if size == 0:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, "That file is empty.")
    job = pipeline.Job(title=stem[:150] or "Song", mode=mode, source="upload", upload_path=str(dest),
                       duration_hint=_probe_seconds(dest), melody_program=melody_program, split_hands=split_hands)
    return add_job(job)


def _render_import_preview(dest):
    mp3 = META / (dest.stem + ".mp3")
    tmpdir = WORK / f"preview-{dest.stem}-{time.time_ns()}"
    tmpdir.mkdir(parents=True, exist_ok=True)
    try:
        if pipeline.render_preview(dest, mp3, tmpdir):
            meta_file = META / (dest.stem + ".json")
            meta = json.loads(meta_file.read_text())
            meta["preview"] = True
            meta_file.write_text(json.dumps(meta))
    except Exception:  # noqa: BLE001
        pass
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


@app.get("/api/jobs")
def list_jobs():
    with LOCK:
        return [JOBS[i].public() for i in reversed(ORDER) if i in JOBS][:20]


@app.get("/api/jobs/{job_id}")
def get_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "No such job.")
    return job.public()


@app.post("/api/jobs/{job_id}/cancel")
def cancel_job(job_id: str):
    job = JOBS.get(job_id)
    if not job:
        raise HTTPException(404, "No such job.")
    job.cancelled = True
    if job.status == "queued":
        job.status, job.stage = "cancelled", "Cancelled"
    return job.public()


def _lib_file(name):
    if not name or "/" in name or "\\" in name or name.startswith("."):
        raise HTTPException(400, "Bad file name.")
    p = LIB / name
    if p.parent.resolve() != LIB.resolve() or not p.is_file() or p.suffix.lower() != ".mid":
        raise HTTPException(404, "That song is not in your library any more.")
    return p


@app.get("/api/library")
def library():
    items = []
    for p in sorted(LIB.glob("*.mid"), key=lambda p: p.stat().st_mtime, reverse=True):
        if p.name.startswith("."):
            continue
        meta = {}
        mf = META / (p.stem + ".json")
        if mf.exists():
            try:
                meta = json.loads(mf.read_text())
            except ValueError:
                meta = {}
        items.append({"file": p.name, "size": p.stat().st_size, "modified": p.stat().st_mtime,
                      "title": meta.get("title") or p.stem, "mode": meta.get("mode"), "bpm": meta.get("bpm"),
                      "seconds": meta.get("seconds"), "parts": meta.get("parts") or [],
                      "preview": (META / (p.stem + ".mp3")).exists()})
    return items


@app.get("/api/library/{name}/midi")
def library_midi(name: str):
    p = _lib_file(name)
    return FileResponse(p, media_type="audio/midi", filename=p.name)


@app.get("/api/library/{name}/preview")
def library_preview(name: str):
    p = _lib_file(name)
    mp3 = META / (p.stem + ".mp3")
    if not mp3.exists():
        raise HTTPException(404, "No preview for this song.")
    return FileResponse(mp3, media_type="audio/mpeg")


@app.post("/api/library/{name}/reveal")
def library_reveal(name: str):
    subprocess.run(["open", "-R", str(_lib_file(name))], timeout=10)
    return {"ok": True}


@app.post("/api/open-library")
def open_library():
    subprocess.run(["open", str(LIB)], timeout=10)
    return {"ok": True}


@app.post("/api/library/{name}/trash")
def library_trash(name: str):
    """Moves the song to the Mac's Trash (recoverable), never deletes outright."""
    p = _lib_file(name)
    trash = Path.home() / ".Trash"
    dest = trash / p.name
    n = 2
    while dest.exists():
        dest = trash / f"{p.stem} {n}{p.suffix}"
        n += 1
    shutil.move(str(p), str(dest))
    for ext in (".json", ".mp3"):
        (META / (p.stem + ext)).unlink(missing_ok=True)
    return {"ok": True}


class DriveReq(BaseModel):
    drive: str
    name: str | None = None
    dry_run: bool = True


def _drive_call(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except (ValueError, OSError) as e:
        raise HTTPException(400, str(e))


@app.get("/api/drives")
def drives():
    return usb.list_drives()


@app.post("/api/drives/copy")
def drives_copy(req: DriveReq):
    src = _lib_file(req.name or "")
    return _drive_call(usb.copy_to_drive, src, req.drive)


@app.post("/api/drives/tidy")
def drives_tidy(req: DriveReq):
    return _drive_call(usb.tidy_drive, req.drive, dry_run=req.dry_run)


@app.post("/api/drives/eject")
def drives_eject(req: DriveReq):
    _drive_call(usb.eject, req.drive)
    return {"ok": True}


@app.post("/api/update-downloader")
def update_downloader():
    if any(j.status in ("running", "queued") for j in JOBS.values()):
        raise HTTPException(400, "Wait until the current song is finished, then update.")
    uv = shutil.which("uv")
    cmd = ([uv, "pip", "install", "--python", sys.executable, "-U", "yt-dlp[default]"] if uv
           else [sys.executable, "-m", "pip", "install", "-U", "yt-dlp[default]"])
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        raise HTTPException(500, "The update failed. Check your internet connection and try again.")
    # Restart ourselves so the new downloader is the one in use.
    threading.Timer(1.0, lambda: os.execv(sys.executable, [sys.executable, str(APP / "server.py")])).start()
    return {"ok": True, "restarting": True}


def main():
    clean_leftovers()
    threading.Thread(target=pipeline.check_models_in_child, daemon=True).start()
    threading.Thread(target=worker, daemon=True).start()
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
