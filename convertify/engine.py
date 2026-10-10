"""Convertify: a YouTube link in, an MP3 out.

Built from the same parts as Midify, and deliberately separate from it. It shares the piece that
needs constant attention, the YouTube downloader, which keeps itself current as YouTube changes,
and it shares nothing else: no AI models, no transcription, no 750 MB of machine learning.

Runs on your own machine over your own connection, like Midify. A website cannot do this: a
browser is not allowed to fetch YouTube's media, so a hosted version would mean one server doing
every download, which YouTube blocks quickly and which is a different thing legally.
"""
import functools
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
# Downloads by default, because that is where a downloaded thing belongs and where people look
# for it. Pick any folder instead and Convertify remembers it.
SAVE_TO = Path(os.environ.get("CONVERTIFY_SAVE", Path.home() / "Downloads"))
WORK = Path(os.environ.get("CONVERTIFY_WORK", Path.home() / "Library" / "Application Support" /
                           "Convertify" / "work"))
STATE = Path(os.environ.get("CONVERTIFY_STATE", Path.home() / "Library" / "Application Support" /
                            "Convertify" / "state"))
FFMPEG = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
QUALITIES = {"320": "320k", "256": "256k", "192": "192k", "128": "128k"}


@functools.lru_cache(maxsize=1)
def aac_encoder():
    """Apple's AAC encoder when this Mac has it, ffmpeg's own otherwise.

    Measured on five minutes of stereo: ffmpeg's built-in encoder took 4.42s and Apple's took
    3.01s for the same request, and Apple's is the better encoder besides. Asked once, because
    asking ffmpeg what it can do costs about a tenth of a second and the answer never changes."""
    try:
        out = subprocess.run([FFMPEG, "-hide_banner", "-encoders"],
                             capture_output=True, text=True, timeout=20).stdout
        if re.search(r"^\s*\S+\s+aac_at\b", out, re.M):
            return "aac_at"
    except Exception:                                      # noqa: BLE001
        pass
    return "aac"

# What it can turn a video into. "keep" is the fast one: YouTube already sends m4a, so there is
# nothing to re-encode, only to copy, which takes about a second whatever the length.
FORMATS = {
    "m4a":  {"ext": ".m4a",  "label": "M4A, straight from YouTube", "fast": True},
    "mp3":  {"ext": ".mp3",  "label": "MP3, plays anywhere", "fast": False},
    "wav":  {"ext": ".wav",  "label": "WAV, uncompressed", "fast": False},
    "flac": {"ext": ".flac", "label": "FLAC, lossless and smaller", "fast": False},
    "aac":  {"ext": ".aac",  "label": "AAC", "fast": False},
    "midi": {"ext": ".mid",  "label": "MIDI, played by Midify", "fast": False},
}
# The most of a recording it will take in one go. The source may be up to four hours long; this
# is how much of it comes out the other end. It matches the number the page will accept.
MOST_SECONDS = 60 * 60
MIDIFY_APP = Path("/Applications/Midify.app")
MIDIFY_VENV = Path.home() / "Library" / "Application Support" / "Clavinova MIDI Maker" / "venv"
app = FastAPI()


@dataclass
class Job:
    link: str
    quality: str = "320"
    kind: str = "mp3"                 # which of FORMATS
    limit_seconds: float = None       # only the first part of a long one
    save_to: str = None               # where it goes, if not the usual place
    to_drive: str = None              # a flash drive to copy it onto as well
    title: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:10])
    status: str = "queued"            # queued, downloading, converting, done, error, cancelled
    stage: str = "Waiting"
    progress: float = 0.0
    error: str = None
    file: str = None
    seconds: float = None
    cancelled: bool = False
    drive_status: str = None

    def public(self):
        out = {k: getattr(self, k) for k in
               ("id", "link", "quality", "kind", "title", "status", "stage", "progress", "error",
                "file", "seconds", "limit_seconds")}
        out["drive_status"] = self.drive_status
        out["folder"] = self.save_to or str(SAVE_TO)
        return out


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


def convert_to(source, dest, kind, quality, title, artist, limit_seconds=None):
    """Turn what was downloaded into the format asked for, with its name and artist written in.

    The quick one is m4a: YouTube already sends m4a, so there is nothing to re-encode and the
    stream is copied as it is, which takes about a second however long the song is."""
    spec = FORMATS[kind]
    cut = ["-t", str(float(limit_seconds))] if limit_seconds else []
    fade = []
    if limit_seconds and float(limit_seconds) > 8:
        # Three seconds of fade, so a shortened track does not stop mid-note.
        fade = ["-af", f"afade=t=out:st={float(limit_seconds) - 3:.2f}:d=3"]
    tags = ["-metadata", f"title={title}", "-metadata", f"artist={artist}"]

    if kind == "m4a" and Path(source).suffix.lower() == ".m4a" and not fade:
        codec = ["-c", "copy", "-movflags", "+faststart"]
    elif kind == "m4a":
        codec = ["-c:a", aac_encoder(), "-b:a", QUALITIES.get(quality, "320k")]
    elif kind == "mp3":
        codec = ["-c:a", "libmp3lame", "-b:a", QUALITIES.get(quality, "320k"), "-id3v2_version", "3"]
    elif kind == "wav":
        codec, tags = ["-c:a", "pcm_s16le"], []
    elif kind == "flac":
        codec = ["-c:a", "flac"]
    elif kind == "aac":
        codec = ["-c:a", aac_encoder(), "-b:a", QUALITIES.get(quality, "320k")]
    else:
        raise ValueError(f"Convertify cannot make a {kind} file.")

    def attempt(with_codec):
        cmd = ([FFMPEG, "-nostdin", "-hide_banner", "-loglevel", "error", "-y",
                "-i", str(source), "-vn"] + cut + fade + with_codec + tags + [str(dest)])
        return subprocess.run(cmd, capture_output=True, text=True, timeout=1800)

    run = attempt(codec)
    chose_the_quick_one = (len(codec) >= 2 and codec[0] == "-c:a"
                           and codec[1] != "aac" and codec[1].startswith("aac"))
    if (run.returncode != 0 or not dest.exists()) and chose_the_quick_one:
        # Apple's encoder is the quick one, but it is pickier about what it will take. If it
        # will not do this particular file, ffmpeg's own always will: slower beats refused.
        dest.unlink(missing_ok=True)
        run = attempt(["-c:a", "aac"] + codec[2:])
    if run.returncode != 0 or not dest.exists():
        raise RuntimeError((run.stderr or "ffmpeg could not make that file")[-300:])
    return dest


def midify_python():
    """Midify's own Python, if Midify is installed. MIDI needs its transcription, not ffmpeg."""
    python = MIDIFY_VENV / "bin" / "python"
    code = MIDIFY_APP / "Contents" / "Resources" / "app"
    if not code.is_dir():
        code = Path.home() / "clavinova-midi" / "app"          # a copy built from the source
    return (python, code) if python.exists() and code.is_dir() else (None, None)


def to_midi(source, dest, limit_seconds=None, mode="piano"):
    """Hand the audio to Midify, which is the family member that hears notes.

    Convertify does not carry the models: that is 750 MB of machine learning, and the whole point
    of it being a separate app is that it does not need them."""
    python, code = midify_python()
    if not python:
        raise ValueError("MIDI needs Midify, which does the listening. Install Midify, then try "
                         "again: it is the other app in this family.")
    script = (
        "import sys, json\n"
        f"sys.path.insert(0, {str(code)!r})\n"
        "import pipeline\n"
        "job = pipeline.Job(title='Convertify', mode=%r, source='upload', upload_path=%r,\n"
        "                   limit_seconds=%s, split_hands=True)\n"
        "res = pipeline.process(job, %r, %r, lambda j: None)\n"
        "print(json.dumps(res))\n"
    ) % (mode, str(source), repr(float(limit_seconds)) if limit_seconds else "None",
         str(Path(source).parent), str(Path(dest).parent))
    run = subprocess.run([str(python), "-c", script], capture_output=True, text=True, timeout=3600)
    if run.returncode != 0:
        raise RuntimeError((run.stderr or "Midify could not make a MIDI of that")[-300:])
    made = json.loads(run.stdout.strip().splitlines()[-1])
    produced = Path(dest).parent / made["file"]
    if produced != dest:
        produced.replace(dest)
    return dest


# One video, several formats: the audio only has to come down once.
# Converting the same thing to MP3 and then to MIDI used to fetch it twice. The download is the
# slow part, so the second format now starts at the encoding step.
KEEP_FOR = 15 * 60            # a quarter of an hour, which covers "oh, and as a FLAC too"
KEEP_MOST = 3                 # at most three videos held back
KEEP_BYTES = 400 * 1024 * 1024
KEPT = {}                     # video id -> {"path", "when", "seconds"}
KEEP_LOCK = threading.Lock()


def kept_folder():
    return WORK / "kept"


def tidy_kept():
    """Drop what is stale, what is surplus, and anything whose file has gone."""
    with KEEP_LOCK:
        now = time.time()
        for vid, e in list(KEPT.items()):
            if now - e["when"] > KEEP_FOR or not Path(e["path"]).exists():
                KEPT.pop(vid, None)
                shutil.rmtree(Path(e["path"]).parent, ignore_errors=True)
        order = sorted(KEPT.items(), key=lambda kv: kv[1]["when"], reverse=True)
        total = 0
        for i, (vid, e) in enumerate(order):
            try:
                total += Path(e["path"]).stat().st_size
            except OSError:
                total += 0
            if i >= KEEP_MOST or total > KEEP_BYTES:
                KEPT.pop(vid, None)
                shutil.rmtree(Path(e["path"]).parent, ignore_errors=True)


def kept_source(video_id, limit_seconds):
    """The audio for this video if it is still here and may be used for this request.

    A long recording is only allowed through when just part of it is being taken. A copy fetched
    under that allowance must not become a way to convert the whole of something too long, so it
    is only handed back when the new request would have been allowed to fetch it anyway."""
    tidy_kept()
    with KEEP_LOCK:
        e = KEPT.get(video_id)
        if not e:
            return None
        if not limit_seconds and e["seconds"] and e["seconds"] > sources.MAX_SONG_SECONDS:
            return None
        p = Path(e["path"])
        return p if p.exists() else None


def keep_source(video_id, src, seconds):
    """Hold on to the downloaded audio in case another format is asked for next.

    The file is moved rather than copied. The folder it was downloaded into is thrown away at the
    end of the job anyway, and a four hour recording is a few hundred megabytes: copying it would
    cost a second or two and twice the disk for no reason. Returns where it ended up, or the
    original path if it could not be kept, which is a disappointment and not a failure."""
    try:
        home = kept_folder() / video_id
        home.mkdir(parents=True, exist_ok=True)
        held = home / Path(src).name
        if held.resolve() != Path(src).resolve():
            shutil.move(str(src), str(held))
        with KEEP_LOCK:
            KEPT[video_id] = {"path": str(held), "when": time.time(), "seconds": seconds}
        tidy_kept()
        return held if held.exists() else Path(src)
    except Exception:                                      # noqa: BLE001
        return Path(src)                                   # holding it is a nicety, never a need


def work_on(job):
    folder = WORK / job.id
    folder.mkdir(parents=True, exist_ok=True)
    where = Path(job.save_to) if job.save_to else SAVE_TO
    try:
        job.status, job.stage = "downloading", "Finding the video"
        info = sources.video_info(job.link)
        job.title = info["title"]
        job.seconds = info.get("duration")
        job.stage = "Downloading"

        def progress(frac):
            job.progress = round(min(0.7, 0.7 * frac), 3)

        vid = sources.youtube_id(job.link)
        already = kept_source(vid, job.limit_seconds)
        if already:
            job.stage = "Already have the audio, straight to it"
            job.progress = 0.7
            src = already
        else:
            src = sources.download_youtube_audio(vid, folder, on_progress=progress,
                                                 cancel=lambda: job.cancelled,
                                                 seconds=job.limit_seconds)
            src = keep_source(vid, src, job.seconds)
        if job.cancelled:
            raise KeyboardInterrupt
        spec = FORMATS[job.kind]
        job.status = "converting"
        job.stage = "Copying it across" if spec["fast"] else f"Making the {job.kind.upper()}"
        if job.kind == "midi":
            job.stage = "Asking Midify to listen to it"
        job.progress = 0.75
        where.mkdir(parents=True, exist_ok=True)
        dest = unique(where, safe_name(info["title"]), spec["ext"])
        if job.kind == "midi":
            to_midi(Path(src), dest, job.limit_seconds)
        else:
            convert_to(Path(src), dest, job.kind, job.quality, info["title"],
                       info.get("channel") or "", job.limit_seconds)

        if job.to_drive:
            job.stage, job.progress = "Copying to the flash drive", 0.95
            try:
                sent = usb.copy_to_drive(dest, job.to_drive)
                job.drive_status = f"Copied to {Path(job.to_drive).name}"
            except Exception as e:                         # noqa: BLE001
                job.drive_status = f"Could not copy to the drive: {e}"

        job.file, job.status, job.stage, job.progress = dest.name, "done", "Done", 1.0
        job.save_to = str(where)
        note_in_history(job, dest)
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
    kind: str = "mp3"
    limit_seconds: float | None = None
    save_to: str | None = None
    to_drive: str | None = None


class Which(BaseModel):
    file: str
    folder: str | None = None


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
    # No caching. The page changes with every update, and a browser holding on to the old one
    # means installing an update and still looking at the previous app. Midify has sent this
    # from the start; this one did not, and a stale page turned up the first time it mattered.
    return FileResponse(HERE / "page.html", headers={"Cache-Control": "no-store"})


@app.get("/api/status")
def status():
    drives = []
    try:
        drives = usb.list_drives()
    except Exception:                                      # noqa: BLE001
        pass
    return {"save_to": remembered() or str(SAVE_TO), "downloader": downloader.installed_version(),
            "qualities": list(QUALITIES), "drives": drives,
            "formats": {k: v["label"] for k, v in FORMATS.items()},
            "fast": [k for k, v in FORMATS.items() if v["fast"]],
            "midi_ready": bool(midify_python()[0]),
            "version": os.environ.get("CONVERTIFY_VERSION", "1.0.0"),
            "update": _update_snapshot()}


@app.get("/api/info")
def info(url: str):
    try:
        return sources.video_info(url)
    except ValueError as e:
        raise HTTPException(400, str(e))
    except Exception as e:                                 # noqa: BLE001
        raise HTTPException(400, sources.friendly_download_error(str(e)))


@app.get("/api/search")
def search(q: str):
    """The same search Midify has, because it is the nicest way to find a song."""
    if sources.looks_like_link(q) or sources.youtube_id(q):
        try:
            return {"results": [sources.video_info(q)], "error": None, "pasted": True}
        except ValueError as e:
            raise HTTPException(400, str(e))
        except Exception as e:                             # noqa: BLE001
            raise HTTPException(400, sources.friendly_download_error(str(e)))
    try:
        return {"results": sources.search_youtube(q), "error": None, "pasted": False}
    except sources.SlowDown:
        raise HTTPException(429, "YouTube is asking Convertify to slow down. Try again in a minute.")
    except Exception as e:                                 # noqa: BLE001
        raise HTTPException(400, sources.friendly_download_error(str(e)))


@app.post("/api/convert")
def convert(ask: Ask):
    if ask.quality not in QUALITIES:
        raise HTTPException(400, "Pick one of the usual qualities.")
    if ask.kind not in FORMATS:
        raise HTTPException(400, "Convertify cannot make that kind of file.")
    if ask.kind == "midi" and not midify_python()[0]:
        raise HTTPException(400, "MIDI needs Midify, the other app in this family: it does the "
                                 "listening. Install Midify and this appears straight away.")
    if ask.limit_seconds and ask.limit_seconds < 30:
        raise HTTPException(400, "Convert at least 30 seconds of it.")
    if ask.limit_seconds and ask.limit_seconds > MOST_SECONDS:
        raise HTTPException(400, f"The longest piece it will take is "
                                 f"{MOST_SECONDS // 60} minutes.")
    if ask.save_to:
        folder = Path(ask.save_to).expanduser()
        if not folder.is_dir():
            raise HTTPException(400, "That folder is not there any more. Pick another one.")
    if not sources.youtube_id(ask.link):
        raise HTTPException(400, "That does not look like a YouTube link. Copy the address from "
                                 "the browser, or from Share then Copy link.")
    if ask.save_to:
        remember(ask.save_to)
    job = Job(link=ask.link.strip(), quality=ask.quality, kind=ask.kind,
              limit_seconds=ask.limit_seconds, save_to=ask.save_to or remembered(),
              to_drive=ask.to_drive)
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
    folder = Path(which.folder or remembered() or SAVE_TO)
    found = folder / Path(which.file).name
    if found.parent.resolve() != folder.resolve() or not found.is_file():
        raise HTTPException(404, "That file is not there any more.")
    platform_bits.reveal(found)
    return {"ok": True}


@app.post("/api/choose-folder")
def choose_folder():
    """The real macOS folder chooser, so picking a place feels like picking a place."""
    script = ('set chosen to choose folder with prompt "Where should Convertify put your files?"\n'
              'return POSIX path of chosen')
    run = subprocess.run(["osascript", "-e", script], capture_output=True, text=True, timeout=300)
    picked = (run.stdout or "").strip().rstrip("/")
    if run.returncode != 0 or not picked:
        return {"ok": False, "folder": None}               # they changed their mind, which is fine
    remember(picked)
    return {"ok": True, "folder": picked}


# ---------- history: everything it has ever made ----------
# Midify keeps every song it writes and lets you find it again. This is the same idea, kept as
# a written record rather than by looking in a folder, because a converted file can be saved
# anywhere, moved afterwards, or sit next to a thousand unrelated downloads.
HISTORY_MOST = 300
HISTORY_LOCK = threading.Lock()


def history_file():
    return STATE / "history.json"


def read_history():
    try:
        rows = json.loads(history_file().read_text())
        return rows if isinstance(rows, list) else []
    except Exception:                                      # noqa: BLE001
        return []


def write_history(rows):
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        tmp = history_file().with_suffix(".json.part")
        tmp.write_text(json.dumps(rows[:HISTORY_MOST]))
        tmp.replace(history_file())                        # all of it or none of it
    except Exception:                                      # noqa: BLE001
        pass


def note_in_history(job, dest):
    """Write down one finished conversion. Newest first."""
    with HISTORY_LOCK:
        rows = read_history()
        rows.insert(0, {
            "id": job.id,
            "title": job.title or dest.stem,
            "kind": job.kind,
            "file": dest.name,
            "folder": str(dest.parent),
            "link": job.link,
            "when": time.time(),
            "size": dest.stat().st_size if dest.exists() else 0,
            "seconds": job.seconds,
            "limit_seconds": job.limit_seconds,
        })
        write_history(rows)


def remember(folder):
    try:
        STATE.mkdir(parents=True, exist_ok=True)
        (STATE / "folder.txt").write_text(str(folder))
    except Exception:                                      # noqa: BLE001
        pass


def remembered():
    try:
        saved = (STATE / "folder.txt").read_text().strip()
        return saved if saved and Path(saved).is_dir() else None
    except Exception:                                      # noqa: BLE001
        return None


@app.get("/api/history")
def history():
    """Everything it has made, newest first, and whether each one is still where it was put."""
    rows = read_history()
    out = []
    for r in rows:
        here = Path(r.get("folder", "")) / r.get("file", "")
        out.append({**r, "still_there": here.is_file()})
    return out


class Forget(BaseModel):
    id: str | None = None
    all: bool = False


@app.post("/api/history/forget")
def forget(ask: Forget):
    """Take something out of the list. The file itself is yours and is never touched."""
    with HISTORY_LOCK:
        rows = read_history()
        rows = [] if ask.all else [r for r in rows if r.get("id") != ask.id]
        write_history(rows)
    return {"ok": True, "left": len(rows)}


@app.post("/api/open-folder")
def open_folder():
    folder = Path(remembered() or SAVE_TO)
    folder.mkdir(parents=True, exist_ok=True)
    platform_bits.open_folder(folder)
    return {"ok": True}


def _update_snapshot():
    """Convertify updates itself the way Midify does, using the same machinery."""
    try:
        import updater
        return updater.UPDATER.snapshot()
    except Exception:                                      # noqa: BLE001
        return None


@app.get("/api/update")
def update_state():
    """The current situation, asked for every few seconds by the window, exactly as Midify does.
    Painting the banner once at startup left it sitting there long after it stopped being true."""
    return _update_snapshot() or {"state": "unknown"}


@app.post("/api/update/check")
def update_check():
    import updater
    return updater.UPDATER.check(manual=True)


@app.post("/api/update/install")
def update_install():
    import updater
    snap = updater.UPDATER.snapshot()
    if not snap.get("can_install"):
        raise HTTPException(400, "This copy cannot update itself. Open Convertify from your "
                                 "Applications folder, or download the new one from the releases "
                                 "page.")
    if snap.get("state") != "available":
        raise HTTPException(400, "Convertify is already the newest one. ✅")
    return updater.UPDATER.install()


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
    # Audio held back for a second format belongs to the run that fetched it. A new run knows
    # nothing about it, so it would sit on the disk forever: clear it out on the way in.
    shutil.rmtree(kept_folder(), ignore_errors=True)
    for leftover in WORK.iterdir() if WORK.is_dir() else []:
        if leftover.is_dir():
            shutil.rmtree(leftover, ignore_errors=True)
    threading.Thread(target=worker, daemon=True).start()
    threading.Thread(target=keep_downloader_current, daemon=True).start()


if __name__ == "__main__":
    import uvicorn
    start_background()
    uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
