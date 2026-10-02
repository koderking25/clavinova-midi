"""Standard and Storage saver, tested on a fake flash drive (a disk image), never a real one.

    .venv/bin/python tests/test_storage.py

Every folder this test touches is inside a temporary directory or the test image. It asserts that
before it writes anything, because an earlier version of this project moved real songs by mistake.
"""
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLAY = Path(tempfile.mkdtemp(prefix="storage-test-"))
os.environ["CLAVINOVA_STATE"] = str(PLAY / "state")
# usb.list_drives() ignores disk images unless a test says so, which keeps real drives safe.
os.environ["CLAVINOVA_TEST_DISK_IMAGES"] = "1"
sys.path.insert(0, str(ROOT / "app"))

import storage  # noqa: E402

IMG = PLAY / "drive.sparseimage"
SMALL = PLAY / "tiny.sparseimage"
passed, failed = [], []


def check(name, ok, detail=""):
    (passed if ok else failed).append(name)
    print(f"  {'PASS' if ok else 'FAIL'}  {name}{(': ' + detail) if detail else ''}")


def make_image(path, size, volname):
    subprocess.run(["hdiutil", "create", "-size", size, "-type", "SPARSE", "-fs", "MS-DOS FAT32",
                    "-volname", volname, "-layout", "MBRSPUD", str(path).replace(".sparseimage", "")],
                   check=True, capture_output=True)
    return Path(str(path))


def attach(path, readonly=False):
    cmd = ["hdiutil", "attach", str(path), "-nobrowse"]
    if readonly:
        cmd.append("-readonly")
    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    for line in out.splitlines():
        if "/Volumes/" in line:
            return Path(line.split("\t")[-1].strip())
    raise RuntimeError("could not mount the test image")


def detach(mount):
    subprocess.run(["hdiutil", "detach", str(mount), "-force"], capture_output=True)


def main():
    # Never touch the real songs folder, the real model cache or the real work folder.
    storage.HOME_LIBRARY = PLAY / "fake-music" / "Midify"
    storage.HOME_MODELS = PLAY / "fake-models"
    storage.HOME_WORK = PLAY / "fake-work"
    for guard in (storage.HOME_LIBRARY, storage.HOME_MODELS, storage.HOME_WORK, storage.STATE):
        assert str(guard).startswith(str(PLAY)), f"test would touch {guard}, refusing"
    print(f"Everything this test touches is under {PLAY}\n")

    storage.HOME_LIBRARY.mkdir(parents=True)
    (storage.HOME_LIBRARY / "Fake Song.mid").write_bytes(b"MThd" + b"\x00" * 2000)
    (storage.HOME_LIBRARY / "Another Song.mid").write_bytes(b"MThd" + b"\x00" * 3000)
    storage.HOME_MODELS.mkdir(parents=True)
    (storage.HOME_MODELS / "pretend-model.bin").write_bytes(b"\x00" * 400_000)

    print("A drive that is too small")
    make_image(SMALL, "120m", "TINYDRV")
    small_mount = attach(SMALL)
    ok, plain = storage.can_use(small_mount)
    check("a small drive is refused", not ok)
    check("and it says how much is needed, in plain English",
          "GB free" in plain and "600 MB" in plain, plain)
    detach(small_mount)

    print("\nA drive that cannot be written to")
    make_image(IMG, "2g", "MIDIFYTEST")
    ro_mount = attach(IMG, readonly=True)
    ok, plain = storage.can_use(ro_mount)
    check("a read-only drive is refused", not ok)
    check("and it says so, rather than blaming space", "read only" in plain, plain)
    detach(ro_mount)

    print("\nTurning Storage saver on")
    mount = attach(IMG)
    ok, plain = storage.can_use(mount)
    check("a suitable drive is accepted", ok, plain)
    said = storage.switch("saver", mount, say=lambda m: None)
    check("it says what happened in plain English", "Storage saver is on" in said, said)
    check("mode is remembered", storage.mode() == "saver")
    p = storage.places()
    check("songs now live on the drive", str(p["library"]).startswith(str(mount)), str(p["library"]))
    check("working files now live on the drive", str(p["work"]).startswith(str(mount)))
    check("the model now lives on the drive", str(p["models"]).startswith(str(mount)))
    check("the songs were copied across",
          (p["library"] / "Fake Song.mid").exists() and (p["library"] / "Another Song.mid").exists())
    check("the model was copied across", (p["models"] / "pretend-model.bin").stat().st_size == 400_000)
    check("the Mac copies are left alone until you ask",
          (storage.HOME_LIBRARY / "Fake Song.mid").exists())
    e = storage.env()
    check("a song's process is told to use the drive",
          e["CLAVINOVA_WORK"].startswith(str(mount)) and e["HF_HOME"].startswith(str(mount)))
    check("symlinks are turned off for the model cache (FAT32 cannot store them)",
          e.get("HF_HUB_DISABLE_SYMLINKS") == "1")

    print("\nThe drive is recognised by itself, not by its name")
    uuid = storage.volume_id(mount)
    check("the drive has an identity", bool(uuid), uuid)
    check("it is found while plugged in", storage.remembered_drive() == mount)

    print("\nWhen the drive is not plugged in")
    detach(mount)
    check("Midify knows it is gone", storage.remembered_drive() is None)
    why = storage.blocked_reason()
    check("making a song is blocked", why is not None)
    check("and the reason is plain English, naming the drive",
          why and "MIDIFYTEST" in why and "plug it in" in why.lower(), why)
    ok, plain, fix = storage.ready()
    check("the health check flags it", not ok and fix == "storage_standard", plain)
    check("the app still opens, falling back to the Mac", storage.places()["where"] == "mac")

    print("\nPlugging it back in")
    mount = attach(IMG)
    check("it is recognised again", storage.remembered_drive() == mount)
    check("making a song is allowed again", storage.blocked_reason() is None)

    print("\nFreeing up the Mac copy")
    (storage.HOME_LIBRARY / "Not On Drive.mid").write_bytes(b"MThd" + b"\x00" * 100)
    try:
        storage.free_mac_copy()
        check("it refuses while a song is not safely on the drive", False, "it went ahead anyway")
    except ValueError as err:
        check("it refuses while a song is not safely on the drive", True, str(err))
    (storage.HOME_LIBRARY / "Not On Drive.mid").unlink()
    said = storage.free_mac_copy()
    check("then it frees the space and says how much", "Freed" in said, said)
    check("the songs are still on the drive",
          (storage.places()["library"] / "Fake Song.mid").exists())
    check("the Mac copy is gone", not storage.HOME_LIBRARY.exists())

    print("\nSwitching back to Standard")
    said = storage.switch("standard", say=lambda m: None)
    check("it copies the songs back to the Mac",
          (storage.HOME_LIBRARY / "Fake Song.mid").exists(), said)
    check("mode is remembered", storage.mode() == "standard")
    check("nothing is blocked in Standard", storage.blocked_reason() is None)

    print("\nA songs folder set on purpose is respected")
    # This went wrong once: Storage saver overrode CLAVINOVA_LIBRARY, so a test wrote its fixture
    # songs into a real songs folder. Never again.
    import importlib
    os.environ["CLAVINOVA_LIBRARY"] = str(PLAY / "explicit-library")
    importlib.reload(storage)
    check("it uses the folder it was told to use",
          str(storage.places()["library"]) == str(PLAY / "explicit-library"),
          str(storage.places()["library"]))
    check("and a song's process is told the same folder",
          storage.env()["CLAVINOVA_LIBRARY"] == str(PLAY / "explicit-library"))
    del os.environ["CLAVINOVA_LIBRARY"]
    importlib.reload(storage)
    check("with nothing set, it falls back to the usual Music folder",
          str(storage.places()["library"]) == str(Path.home() / "Music" / "Midify"),
          str(storage.places()["library"]))

    detach(mount)
    print()
    print(f"{len(passed)} passed, {len(failed)} failed")
    for f in failed:
        print(f"  FAILED: {f}")
    return 1 if failed else 0


if __name__ == "__main__":
    try:
        code = main()
    finally:
        for m in Path("/Volumes").glob("*"):
            if m.name in ("MIDIFYTEST", "TINYDRV"):
                detach(m)
        shutil.rmtree(PLAY, ignore_errors=True)
    sys.exit(code)
