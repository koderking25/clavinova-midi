"""Build a Windows Midify that runs from a flash drive.

    .venv/bin/python tools/make_portable_windows.py /Volumes/YOURDRIVE

**This has never been run on Windows.** It is built on a Mac, which cannot execute a Windows
binary, so nothing here has been proven to work. What is checked: every package has a Windows
build, the files land where they belong, and the downloads are the right kind of file. Whether it
actually starts on a PC is unknown until somebody tries it. Treat the first run as a test.

Everything is downloaded to the drive, never to the Mac, because filling someone's startup disk to
build something for another computer would be a poor trade.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import tarfile
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
PY_RELEASES = "https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest"
PY_WANTED = "cpython-3.11"
PY_PLATFORM = "x86_64-pc-windows-msvc-install_only.tar.gz"
FFMPEG = ("https://github.com/BtbN/FFmpeg-Builds/releases/download/latest/"
          "ffmpeg-master-latest-win64-gpl.zip")

LAUNCHER = r'''@echo off
rem Midify, running from this drive. Nothing is installed on the PC.
setlocal
set "HERE=%~dp0"
set "CLAVINOVA_STATE=%HERE%state"
set "CLAVINOVA_WORK=%HERE%working"
set "CLAVINOVA_LIBRARY=%HERE%Songs"
set "HF_HOME=%HERE%models"
set "HF_HUB_OFFLINE=1"
set "HF_HUB_DISABLE_SYMLINKS=1"
set "PYTHONPYCACHEPREFIX=%HERE%compiled"
set "PYTHONPATH=%HERE%app;%HERE%python\Lib\site-packages"
set "PATH=%HERE%tools;%PATH%"
if not exist "%CLAVINOVA_STATE%" mkdir "%CLAVINOVA_STATE%"
if not exist "%CLAVINOVA_WORK%" mkdir "%CLAVINOVA_WORK%"
if not exist "%CLAVINOVA_LIBRARY%" mkdir "%CLAVINOVA_LIBRARY%"
"%HERE%python\python.exe" "%HERE%app\start_windows.py"
if errorlevel 1 (
  echo.
  echo Midify stopped with an error. Please send the lines above to whoever built this.
  pause
)
'''

READ_ME = """Midify for Windows, on this drive
=================================

Double-click "Midify.bat". Midify runs from this drive: nothing is installed on the PC, and your
songs are saved in the Songs folder here, where your piano can read them.

THIS HAS NEVER BEEN RUN. It was built on a Mac, which cannot test Windows software. If it does not
start, the window will show an error: please send that text to whoever gave you this drive, and it
can be fixed.

Needs 64-bit Windows 10 or later.
"""


def say(m):
    print(m, flush=True)


def fetch(url, dest, label):
    say(f"   downloading {label}")
    with urllib.request.urlopen(url, timeout=120) as response, open(dest, "wb") as out:
        total = 0
        while True:
            chunk = response.read(1 << 20)
            if not chunk:
                break
            out.write(chunk)
            total += len(chunk)
    say(f"   {total / 1e6:.0f} MB")
    return dest


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("drive")
    args = ap.parse_args()
    drive = Path(args.drive)
    if not drive.is_dir():
        say(f"{drive} is not plugged in.")
        return 1
    root = drive / "Midify Windows"
    root.mkdir(parents=True, exist_ok=True)
    scratch = drive / ".midify-build"
    scratch.mkdir(exist_ok=True)
    # Everything heavy stays on the drive, so a small startup disk is never filled.
    os.environ["UV_CACHE_DIR"] = str(scratch / "uv-cache")
    os.environ["TMPDIR"] = str(scratch / "tmp")
    (scratch / "tmp").mkdir(exist_ok=True)

    say("1. Python for Windows")
    python_dir = root / "python"
    if not (python_dir / "python.exe").exists():
        with urllib.request.urlopen(PY_RELEASES, timeout=60) as r:
            release = json.load(r)
        asset = next((a for a in release["assets"]
                      if a["name"].startswith(PY_WANTED) and a["name"].endswith(PY_PLATFORM)), None)
        if not asset:
            say("   could not find a Windows Python to download.")
            return 1
        tarball = fetch(asset["browser_download_url"], scratch / asset["name"], asset["name"])
        with tarfile.open(tarball) as tf:
            tf.extractall(scratch / "pyout")
        inner = next((scratch / "pyout").glob("python*"))
        shutil.copytree(inner, python_dir, dirs_exist_ok=True)
        tarball.unlink(missing_ok=True)
    if not (python_dir / "python.exe").exists():
        say("   the download did not contain python.exe.")
        return 1
    say(f"   {python_dir} ready")

    say("2. The packages, built for Windows")
    site = python_dir / "Lib" / "site-packages"
    site.mkdir(parents=True, exist_ok=True)
    needs = scratch / "win-requirements.txt"
    needs.write_text("\n".join(line for line in (HERE / "requirements.lock").read_text().splitlines()
                               if not line.lower().startswith("pyobjc")))
    run = subprocess.run(["uv", "pip", "install", "--python-platform", "windows", "--python-version", "3.11",
                          "--target", str(site), "--no-deps", "--link-mode", "copy", "-r", str(needs)],
                         capture_output=True, text=True)
    if run.returncode != 0:
        say("   the packages would not install:\n" + (run.stderr or "")[-700:])
        return 1
    say(f"   {sum(1 for _ in site.iterdir())} packages in place")

    say("3. ffmpeg for Windows")
    tools = root / "tools"
    tools.mkdir(exist_ok=True)
    if not (tools / "ffmpeg.exe").exists():
        zipped = fetch(FFMPEG, scratch / "ffmpeg.zip", "ffmpeg (GPL build from BtbN)")
        with zipfile.ZipFile(zipped) as zf:
            for member in zf.namelist():
                if member.endswith(("/bin/ffmpeg.exe", "/bin/ffprobe.exe")):
                    with zf.open(member) as src, open(tools / Path(member).name, "wb") as out:
                        shutil.copyfileobj(src, out)
        zipped.unlink(missing_ok=True)
    say(f"   {', '.join(p.name for p in tools.iterdir())}")

    say("4. The AI models")
    sys.path.insert(0, str(HERE / "tools"))
    from make_portable import copy_models
    copied = copy_models(Path(os.environ.get("HF_HOME_SOURCE", Path.home() / ".cache" / "huggingface")),
                         root / "models")
    say(f"   {copied} files")

    say("5. Midify itself")
    app_dir = root / "app"
    app_dir.mkdir(exist_ok=True)
    for item in (HERE / "app").iterdir():
        if item.name == "__pycache__":
            continue
        if item.is_dir():
            shutil.copytree(item, app_dir / item.name, dirs_exist_ok=True)
        else:
            shutil.copy2(item, app_dir / item.name)
    (root / "Midify.bat").write_text(LAUNCHER)
    (root / "Read Me.txt").write_text(READ_ME)
    shutil.rmtree(scratch, ignore_errors=True)

    say("\nBuilt, and never tested. Plug this drive into a PC, open the 'Midify Windows' folder and")
    say("double-click Midify.bat. If it stops with an error, send that text back.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
