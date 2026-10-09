"""Midify: the engine behind the Mac app, a local web app at http://127.0.0.1:8765

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

import downloader  # noqa: E402
import health  # noqa: E402
import platform_bits  # noqa: E402
import pipeline  # noqa: E402
import sources  # noqa: E402
import updater  # noqa: E402
import usb  # noqa: E402

ROOT = APP.parent
# Inside a Mac .app the code folder is read only, so the launcher points these somewhere
# writable. Left alone they behave exactly as before.
import storage  # noqa: E402

# Storage saver keeps everything on the flash drive. This sets the folder names below, so it has to
# happen before they are read. With the drive unplugged it falls back to the Mac, and making a song
# is blocked separately with a clear message (app/storage.py).
STORAGE_AT_START = storage.apply_to_process()

WORK = Path(os.environ.get("CLAVINOVA_WORK", ROOT / "work"))
UPLOADS = WORK / "uploads"
STATE = Path(os.environ.get("CLAVINOVA_STATE", ROOT / "state"))
LIB = Path(os.environ.get("CLAVINOVA_LIBRARY", Path.home() / "Music" / "Midify"))
# The app used to be called Clavinova MIDI Maker. Move its songs folder over once, keeping every song
# and its details. Only when nothing is in the way, so nothing can be overwritten.
_OLD_LIB = Path.home() / "Music" / "Clavinova MIDI"


def migrate_library(old=_OLD_LIB, new=LIB):
    """Move songs from the old folder into the new one. Renames it when the new one does not exist
    yet; otherwise merges, moving only what the new folder does not already have, so an older app
    that kept saving into the old folder never strands a song. Never overwrites anything."""
    if not old.is_dir():
        return
    if not new.exists():
        try:
            old.rename(new)
        except OSError:
            pass
        return
    for src_dir, dst_dir in ((old / ".clavinova", new / ".clavinova"), (old, new)):
        if not src_dir.is_dir():
            continue
        dst_dir.mkdir(parents=True, exist_ok=True)
        for item in src_dir.iterdir():
            if item.name == ".clavinova":
                continue
            target = dst_dir / item.name
            if not target.exists():
                try:
                    item.rename(target)
                except OSError:
                    pass
    for leftover in (old / ".clavinova", old):
        try:
            (leftover / ".DS_Store").unlink(missing_ok=True)
            leftover.rmdir()                            # only if now empty
        except OSError:
            pass


if "CLAVINOVA_LIBRARY" not in os.environ:
    migrate_library()
META = LIB / ".clavinova"
PORT = int(os.environ.get("CLAVINOVA_PORT", "8765"))
MAX_UPLOAD = 400 * 1024 * 1024
ERROR_LOG = STATE / "errors.log"

for _d in (WORK, UPLOADS, STATE, LIB, META):
    _d.mkdir(parents=True, exist_ok=True)


QUEUE_FILE = STATE / "queue.json"
SAVED_FIELDS = ("id", "title", "mode", "source", "video_id", "upload_path", "duration_hint", "melody_program",
                "split_hands", "created", "status", "result", "send_to_drive", "drive_path", "drive_folder",
                "drive_status", "drive_file", "cancelled", "limit_seconds")


def _saved_queue():
    try:
        return json.loads(QUEUE_FILE.read_text())
    except Exception:  # noqa: BLE001
        return []


def clean_leftovers():
    """Remove what a crash or force-quit left behind. Called only at start-up, never on import:
    each song's process imports this file too, and must not delete the upload it is about to read.
    Recordings still waiting in the saved queue are kept, so an unfinished batch can carry on."""
    keep = {str(Path(j["upload_path"]).resolve()) for j in _saved_queue() if j.get("upload_path")}
    for d in WORK.iterdir():
        if d.is_dir() and d.name != "uploads":
            shutil.rmtree(d, ignore_errors=True)
    for f in UPLOADS.iterdir():
        if str(f.resolve()) not in keep:
            f.unlink(missing_ok=True)
    for f in META.glob("*.partial"):
        f.unlink(missing_ok=True)

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
JOBS = {}
ORDER = []
Q = queue.Queue()
LOCK = threading.Lock()
SEND_LOCK = threading.Lock()


def save_queue():
    """Songs not yet made, and made songs still waiting for the flash drive, survive quitting the app."""
    with LOCK:
        keep = [JOBS[i] for i in ORDER if i in JOBS and
                (JOBS[i].status in ("queued", "running") or JOBS[i].drive_status == "waiting")]
        data = [{k: getattr(j, k) for k in SAVED_FIELDS} for j in keep]
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        tmp = QUEUE_FILE.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        os.replace(tmp, QUEUE_FILE)
    except OSError:
        pass


def _already_made(job):
    """Did this song finish before the app closed? Then restarting it only makes a second copy.

    Matched by name and by the file being newer than the job itself, so an older song of the same
    name never counts."""
    try:
        stem = usb.safe_filename(job.title).removesuffix(".mid")
        for made in LIB.glob("*.mid"):
            if made.stem.rstrip(" 0123456789") == stem.rstrip(" 0123456789") \
                    and made.stat().st_mtime > (job.created or 0):
                return True
    except Exception:                                    # noqa: BLE001
        pass
    return False


def restore_queue():
    """Put back what was saved when the app last closed. A song that was being made starts again."""
    restored = 0
    for fields in _saved_queue():
        try:
            job = pipeline.Job(**{k: v for k, v in fields.items() if k in SAVED_FIELDS})
        except TypeError:
            continue
        if job.source == "upload" and job.status != "done" and not (job.upload_path and Path(job.upload_path).exists()):
            continue
        if fields.get("cancelled"):
            continue                                     # you stopped it; it does not come back
        if job.status == "running" and _already_made(job):
            continue                                     # it finished before the app closed
        if job.status in ("queued", "running"):
            job.status, job.stage, job.progress = "queued", "Waiting (carried on from last time)", 0.0
            with LOCK:
                JOBS[job.id] = job
                ORDER.append(job.id)
            Q.put(job)
            restored += 1
        elif job.drive_status == "waiting" and job.result:
            with LOCK:
                JOBS[job.id] = job
                ORDER.append(job.id)
            restored += 1
    return restored


def _next_number(folder):
    best = 0
    try:
        for f in Path(folder).glob("*.mid"):
            m = re.match(r"(\d{1,3}) ", f.name)
            if m:
                best = max(best, int(m.group(1)))
    except OSError:
        pass
    return best + 1


def try_send(job):
    """Copy a finished song to the flash drive. False means no usable drive yet (try again later)."""
    usable = [d for d in usb.list_drives() if d["writable"]]
    drive = next((d for d in usable if d["path"] == job.drive_path), None)
    if drive is None and len(usable) == 1:
        drive = usable[0]
    if drive is None:
        job.drive_status = "waiting"
        return False
    src = LIB / (job.result or {}).get("file", "")
    if not job.result or not src.is_file():
        job.drive_status = "The song was removed before it could be copied to the flash drive."
        return True
    folder_name = usb.safe_filename(job.drive_folder, ext="") if job.drive_folder else ""
    folder = Path(drive["path"]) / folder_name if folder_name else Path(drive["path"])
    title = job.result.get("title") or src.stem
    try:
        r = usb.copy_to_drive(src, drive["path"], filename=f"{_next_number(folder):02d} {title}.mid",
                              subfolder=job.drive_folder or "")
    except (ValueError, OSError) as e:
        if "not connected" in str(e):
            job.drive_status = "waiting"
            return False
        job.drive_status = str(e)
        return True
    job.drive_status = "sent"
    job.drive_file = f"{folder_name}/{r['name']}" if folder_name else r["name"]
    return True


def drive_sender():
    """Every few seconds, copy finished songs that are waiting for the flash drive, in order."""
    while True:
        time.sleep(4)
        with LOCK:
            waiting = [JOBS[i] for i in ORDER if i in JOBS and JOBS[i].drive_status == "waiting"]
        if not waiting:
            continue
        changed = False
        with SEND_LOCK:
            for job in waiting:
                if not try_send(job):
                    break                                # no drive yet: keep the order, try later
                changed = True
        if changed:
            save_queue()


def keep_downloader_current():
    """Once a day, quietly, make sure the YouTube downloader is the newest one.

    YouTube changes how it serves video every few weeks and yt-dlp follows within days. Waiting for
    someone to notice downloads failing and press a button is the wrong way round."""
    time.sleep(90)                                   # let the app finish opening first
    while True:
        try:
            busy = lambda: any(j.status in ("running", "queued") for j in JOBS.values())  # noqa: E731
            downloader.routine_check(busy=busy, note=health.note)
        except Exception:                            # noqa: BLE001
            pass                                     # never let housekeeping break the app
        time.sleep(6 * 60 * 60)


def start_background():
    """Everything the engine runs besides the web server. Shared by the app window and server.py."""
    import updater
    clean_leftovers()
    restore_queue()
    threading.Thread(target=pipeline.check_models_in_child, daemon=True).start()
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=drive_sender, daemon=True).start()
    threading.Thread(target=updater.startup, daemon=True).start()
    threading.Thread(target=check_health_at_startup, daemon=True).start()
    threading.Thread(target=keep_downloader_current, daemon=True).start()
    threading.Thread(target=updater.watch_for_updates, daemon=True).start()


@app.middleware("http")
async def only_this_mac(request: Request, call_next):
    host = (request.headers.get("host") or "").rsplit(":", 1)[0]
    if host not in ("127.0.0.1", "localhost"):
        return JSONResponse({"error": "forbidden"}, status_code=403)
    # Web pages from other sites cannot add this header, so they cannot trigger actions here.
    if request.method not in ("GET", "HEAD") and request.headers.get("x-clavinova") != "1":
        return JSONResponse({"error": "forbidden"}, status_code=403)
    return await call_next(request)


HEALTH = {"last": None}


def check_health_at_startup():
    """Look Midify over, put right what can be put right quietly, and remember the rest."""
    HEALTH["last"] = health.report(repair=True)
    for item in HEALTH["last"]["items"]:
        if item["repaired"]:
            print(f"health: {item['plain']}", flush=True)
    for item in HEALTH["last"]["blocking"]:
        print(f"health: {item['plain']}", flush=True)
        health.note(item["plain"])


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
        save_queue()
        try:
            job.stage = "Getting ready"
            pipeline.MODELS_CHECKED.wait(timeout=900)     # never run alongside the start-up model check
            job.result = pipeline.run_in_child(job, WORK, LIB)
            job.status, job.stage, job.progress, job.eta = "done", "Done", 1.0, 0
            prune_previews()
            if job.send_to_drive:
                with SEND_LOCK:
                    try_send(job)
        except pipeline.Cancelled:
            job.status, job.stage = "cancelled", "Cancelled"
        except pipeline.UserError as e:
            # A download that failed because YouTube moved something is worth one more go with an
            # up to date downloader, rather than handing back an error nobody can act on.
            raw = getattr(e, "raw", str(e))
            if not job.downloader_retried and downloader.youtube_moved(raw):
                job.downloader_retried = True
                job.stage = "Updating the song downloader"
                changed, said = downloader.update_because_it_failed(raw, note=health.note)
                if changed:
                    job.status, job.error, job.progress = "queued", None, 0.0
                    job.stage = "Trying again with the new downloader"
                    Q.put(job)
                    save_queue()
                    continue
            job.status, job.error = "error", str(e)
            health.note(f'"{job.title}" could not be made. {e}')
        except MemoryError as e:
            log_error(job, e)
            job.status, job.error = "error", "Your Mac ran out of memory. Close other apps, or try a shorter song."
            health.note(f'"{job.title}" ran out of memory while being made.',
                        "That song stopped; nothing else was affected.")
        except Exception as e:  # noqa: BLE001
            log_error(job, e)
            job.status = "error"
            job.error = (f"Something went wrong ({type(e).__name__}). Try again, or try the other "
                         f"mode. Details were saved to {ERROR_LOG}.")
            health.note(f'"{job.title}": ' + health.describe_failure(e),
                        f"The technical details are in {ERROR_LOG.name}. Nothing else was affected.")
        finally:
            if job.upload_path:
                Path(job.upload_path).unlink(missing_ok=True)
            save_queue()


def add_job(job):
    with LOCK:
        JOBS[job.id] = job
        ORDER.append(job.id)
        while len(ORDER) > 50:
            old = ORDER[0]
            if JOBS.get(old) and (JOBS[old].status in ("queued", "running") or JOBS[old].drive_status == "waiting"):
                break                                    # never forget work that is still to do
            ORDER.pop(0)
            JOBS.pop(old, None)
    Q.put(job)
    save_queue()
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
        "version": updater.current_version(),
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
    video_id: str = ""
    link: str | None = None              # a pasted address works as well as a search result
    title: str = ""
    duration: float | None = None
    limit_seconds: float | None = None   # make only the first part of a long recording
    mode: str = "arrange"
    melody_program: int = 73
    split_hands: bool = True
    send_to_drive: bool = False
    drive_path: str | None = None
    drive_folder: str | None = None


def _clean_folder(name):
    name = (name or "").strip()
    return usb.safe_filename(name, ext="") if name else None


def _check_limit(seconds):
    """How much of a recording to make. None means all of it."""
    if seconds in (None, "", 0):
        return None
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        raise HTTPException(400, "That length does not look like a number.")
    if seconds < 30:
        raise HTTPException(400, "Make at least 30 seconds of it.")
    return min(seconds, float(sources.MAX_SONG_SECONDS))


def _check_options(mode, melody_program):
    if mode not in pipeline.MODES:
        raise HTTPException(400, "Unknown mode.")
    if melody_program not in pipeline.MELODY_PROGRAMS:
        raise HTTPException(400, "Unknown melody instrument.")
    stopped = storage.blocked_reason()
    if stopped:
        raise HTTPException(400, stopped)


@app.post("/api/jobs")
def create_job(req: YTJob):
    video_id = req.video_id if re.fullmatch(r"[A-Za-z0-9_-]{11}", req.video_id or "") \
        else sources.youtube_id(req.link or req.video_id or "")
    if not video_id:
        raise HTTPException(400, "That does not look like a YouTube video. Paste the address from "
                                 "the browser, or search for the song by name.")
    _check_options(req.mode, req.melody_program)
    limit = _check_limit(req.limit_seconds)
    if req.duration and req.duration > sources.MAX_SONG_SECONDS and not limit:
        longest = int(sources.MAX_SONG_SECONDS // 60)
        raise HTTPException(400, f"That recording is {int(req.duration // 60)} minutes long, and "
                                 f"Midify makes up to {longest}. Choose how much of it to make, "
                                 "and it will stop there.")
    job = pipeline.Job(title=(req.title or "").strip()[:150] or "Song", mode=req.mode, source="youtube",
                       video_id=video_id, duration_hint=req.duration, limit_seconds=limit,
                       melody_program=req.melody_program, split_hands=req.split_hands,
                       send_to_drive=req.send_to_drive, drive_path=req.drive_path, drive_folder=_clean_folder(req.drive_folder))
    return add_job(job)


@app.get("/api/youtube/link")
def youtube_link(url: str):
    """What is at this address, so a pasted link can be shown before anything is made."""
    try:
        return sources.video_info(url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except sources.SlowDown:
        raise HTTPException(429, "YouTube is asking Midify to slow down. Try again in a minute.")
    except Exception as e:                                   # noqa: BLE001
        raise HTTPException(400, sources.friendly_download_error(str(e)))


def _probe_seconds(path):
    try:
        r = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                           capture_output=True, text=True, timeout=30)
        return float(r.stdout.strip())
    except Exception:  # noqa: BLE001
        return None


@app.post("/api/upload")
async def upload(file: UploadFile = File(...), mode: str = Form("arrange"), melody_program: int = Form(73),
                 split_hands: bool = Form(True), send_to_drive: bool = Form(False), drive_path: str = Form(""),
                 drive_folder: str = Form(""), limit_seconds: float = Form(0)):
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
            raise HTTPException(400, "That .mid file is damaged and would not play on your piano.")
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
                       duration_hint=_probe_seconds(dest), limit_seconds=_check_limit(limit_seconds),
                       melody_program=melody_program, split_hands=split_hands,
                       send_to_drive=send_to_drive, drive_path=drive_path or None, drive_folder=_clean_folder(drive_folder))
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
    elif job.status == "running":
        # The song's own process takes a moment to notice, and inside a long step it can take a few
        # seconds. Say so, rather than leaving the button looking like it did nothing.
        job.stage, job.eta = "Stopping", 0
    save_queue()            # quitting straight after cancelling must not bring the song back
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
                      "preview": True,                  # made on demand if it is not there yet
                      "auto": bool(meta.get("auto")), "score_grid": meta.get("score_grid")})
    return items


@app.get("/api/library/{name}/midi")
def library_midi(name: str):
    p = _lib_file(name)
    return FileResponse(p, media_type="audio/midi", filename=p.name)


PREVIEW_LOCK = threading.Lock()


def prune_previews(keep=None):
    """Keep only the most recently made previews. Any song's preview is remade when Preview is pressed."""
    keep = pipeline.PREVIEWS_KEPT if keep is None else keep
    previews = sorted(META.glob("*.mp3"), key=lambda f: f.stat().st_mtime, reverse=True)
    for old in previews[keep:]:
        old.unlink(missing_ok=True)


@app.get("/api/library/{name}/preview")
def library_preview(name: str):
    p = _lib_file(name)
    mp3 = META / (p.stem + ".mp3")
    if not mp3.exists():
        with PREVIEW_LOCK:                              # one at a time; a second request just waits for it
            if not mp3.exists():
                tmpdir = WORK / f"preview-{p.stem}-{time.time_ns()}"
                tmpdir.mkdir(parents=True, exist_ok=True)
                try:
                    made = pipeline.render_preview(p, mp3, tmpdir)
                finally:
                    shutil.rmtree(tmpdir, ignore_errors=True)
                if not made:
                    raise HTTPException(500, "The preview could not be made.")
                prune_previews()
    return FileResponse(mp3, media_type="audio/mpeg")


class PracticeIn(BaseModel):
    kind: str
    amount: int = 0


@app.post("/api/library/{name}/practice")
def make_practice(name: str, body: PracticeIn):
    """A practice version of a song: slower, in another key, with a count-in, or easier to play.

    Works on any .mid in the songs folder, including hand-made ones dropped in from BitMidi."""
    import practice
    src = LIB / name
    if src.parent.resolve() != LIB.resolve() or not src.is_file() or src.suffix.lower() != ".mid":
        raise HTTPException(404, "That song is not in your songs folder.")
    # Names stay plain: brackets and percent signs are stripped for a piano's small screen, which
    # would leave "Song (slow 70". A dash survives and reads properly on the instrument.
    label = {"slow": f"slow {body.amount}", "transpose": f"{'up' if body.amount > 0 else 'down'} {abs(body.amount)}",
             "count_in": "count-in", "simplify": "easier"}.get(body.kind)
    if label is None:
        raise HTTPException(400, "Unknown practice version.")
    # Keep the label whole: names are trimmed for a piano's small screen, and losing the end turns
    # "slow 70" into the misleading "slow 7". The song's own name gives way instead.
    room = max(8, usb.NAME_LIMIT - len(label) - 3)
    dest = pipeline.unique_path(LIB, usb.safe_filename(f"{src.stem[:room].strip()} - {label}"))
    try:
        if body.kind == "slow":
            says = practice.slower(src, dest, body.amount or 70)
        elif body.kind == "transpose":
            says = practice.transpose(src, dest, body.amount)
        elif body.kind == "count_in":
            says = practice.count_in(src, dest, body.amount or 4)
        else:
            says = practice.simplify(src, dest)
    except ValueError as e:
        dest.unlink(missing_ok=True)
        raise HTTPException(400, str(e))
    except Exception as e:                                  # noqa: BLE001
        dest.unlink(missing_ok=True)
        health.note(health.describe_failure(e, doing="making a practice version"))
        raise HTTPException(400, "Midify could not make that practice version. The log says what happened.")
    try:                                                    # carry the song's details over, so the list reads well
        mf = META / (src.stem + ".json")
        if mf.exists():
            meta = json.loads(mf.read_text())
            meta["title"] = f"{meta.get('title') or src.stem} - {label}"
            (META / (dest.stem + ".json")).write_text(json.dumps(meta))
    except Exception:                                       # noqa: BLE001
        pass
    return {"ok": True, "file": dest.name, "says": says}


@app.post("/api/library/{name}/reveal")
def library_reveal(name: str):
    platform_bits.reveal(_lib_file(name))
    return {"ok": True}


@app.get("/api/update")
def update_status():
    return updater.UPDATER.snapshot()


@app.post("/api/update/check")
def update_check():
    return updater.UPDATER.check(manual=True)


@app.post("/api/update/install")
def update_install():
    if any(j.status in ("running", "queued") for j in JOBS.values()):
        raise HTTPException(400, "Finish or cancel the song being made first, then update.")
    try:
        updater.UPDATER.start_install()
    except updater.UpdateError as e:
        raise HTTPException(400, str(e))
    return updater.UPDATER.snapshot()


@app.get("/api/health")
def health_report():
    return {**(HEALTH["last"] or health.report(repair=False)), "repair": health.REPAIRS.status()}


@app.post("/api/health/check")
def health_check():
    HEALTH["last"] = health.report(repair=True)        # the safe repairs happen while checking
    return {**HEALTH["last"], "repair": health.REPAIRS.status()}


class FixIn(BaseModel):
    what: str


@app.post("/api/health/fix")
def health_fix(body: FixIn):
    try:
        state = health.REPAIRS.start(body.what, on_done=lambda: HEALTH.update(last=health.report(repair=True)))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"repair": state}


class StorageIn(BaseModel):
    mode: str
    drive: str = None


@app.get("/api/storage")
def storage_state():
    """Where everything is kept, how much it is, and which drives could hold it."""
    ok, plain, _fix = storage.ready()
    return {"usage": storage.usage(), "ready": ok, "says": plain,
            "blocked": storage.blocked_reason(),
            "drives": [d for d in usb.list_drives()],
            "needs_gb": storage.NEED_GB,
            "fixed_install_note": ("The app's Python environment stays on your Mac. The piano model "
                                   "lives inside it, and a flash drive cannot hold the links and "
                                   "programs it needs.")}


@app.post("/api/storage/mode")
def storage_mode(body: StorageIn):
    """Switch between Standard and Storage saver, copying what you already have to its new home."""
    try:
        said = storage.switch(body.mode, body.drive)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except OSError as e:
        health.note(f"Could not move your songs: {e}")
        raise HTTPException(400, f"Midify could not finish moving your files: {e}")
    health.note(said)
    return {"ok": True, "says": said + " Reopen Midify to start using it.", "reopen": True}


@app.post("/api/storage/free-mac")
def storage_free_mac():
    """Remove the Mac copies, once they are safely on the drive."""
    try:
        return {"ok": True, "says": storage.free_mac_copy()}
    except ValueError as e:
        raise HTTPException(400, str(e))


@app.post("/api/storage/reopen")
def storage_reopen():
    """Quit and reopen, so the new storage choice takes effect."""
    import shlex
    bundle = updater.bundle_path()
    if not bundle or updater.UPDATER.quit_hook is None:
        return {"ok": False, "says": "Quit Midify and open it again to start using the new location."}
    subprocess.Popen(["/bin/bash", "-c", f"sleep 2; open -n {shlex.quote(str(bundle))}"],
                     start_new_session=True)
    threading.Timer(0.3, updater.UPDATER.quit_hook).start()
    return {"ok": True, "says": "Reopening Midify."}


@app.post("/api/health/open-log")
def health_open_log():
    health.PROBLEM_LOG.parent.mkdir(parents=True, exist_ok=True)
    if not health.PROBLEM_LOG.exists():
        health.note("You opened the problem log. Nothing had gone wrong yet.")
    platform_bits.reveal(health.PROBLEM_LOG)
    return {"ok": True}


@app.post("/api/open-library")
def open_library():
    platform_bits.open_folder(LIB)
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
    start_background()
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
