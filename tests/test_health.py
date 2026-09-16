"""Break things on purpose, then check Midify notices, says so plainly, and puts them right.

    .venv/bin/python tests/test_health.py

Everything happens in a temporary folder: your songs, settings and app are never touched.
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

WORK = Path(tempfile.mkdtemp(prefix="health-tests-"))
os.environ.update(CLAVINOVA_STATE=str(WORK / "state"), CLAVINOVA_WORK=str(WORK / "work"),
                  CLAVINOVA_LIBRARY=str(WORK / "songs"), CLAVINOVA_SUPPORT=str(WORK / "support"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "app"))
import health as H  # noqa: E402

results = []


def check(label, ok, detail=""):
    results.append(bool(ok))
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('   ' + detail) if detail else ''}")


def item(report, key):
    return next(i for i in report["items"] if i["key"] == key)


def plain_enough(text):
    """No programmer words, a real sentence, and it says something."""
    bad = ("Traceback", "Exception", "errno", "None", "os error", "stack")
    return (isinstance(text, str) and len(text) > 20 and text.strip().endswith((".", "!"))
            and not any(b.lower() in text.lower() for b in bad))


def fresh_songs():
    shutil.rmtree(H.LIBRARY, ignore_errors=True)
    H.LIBRARY.mkdir(parents=True)
    (H.LIBRARY / ".clavinova").mkdir()


print("1. Every message is in plain English")
fresh_songs()
report = H.report(repair=True)
check("every check returns a readable sentence",
      all(plain_enough(i["plain"]) for i in report["items"]),
      "; ".join(i["plain"][:40] for i in report["items"] if not plain_enough(i["plain"])) or "all fine")

print("2. The songs folder is missing")
shutil.rmtree(H.LIBRARY, ignore_errors=True)
check("without repair it is reported, not hidden", item(H.report(repair=False), "songs_folder")["ok"] is False)
r = item(H.report(repair=True), "songs_folder")
check("with repair the folder is made again", H.LIBRARY.is_dir() and r["ok"] and r["repaired"], r["plain"])

print("3. A song's details file is damaged")
fresh_songs()
(H.LIBRARY / "Song.mid").write_bytes(b"MThd")
(H.LIBRARY / ".clavinova" / "Song.json").write_text("{this is not json")
check("without repair it is reported", item(H.report(repair=False), "song_details")["ok"] is False)
r = item(H.report(repair=True), "song_details")
check("the damaged file is set aside", (H.LIBRARY / ".clavinova" / "damaged" / "Song.json").exists() and r["ok"])
check("the song itself is untouched", (H.LIBRARY / "Song.mid").exists(), r["plain"])

print("4. Leftover working files from an interrupted song")
(H.WORK / "half-done-song").mkdir(parents=True, exist_ok=True)
(H.WORK / "half-done-song" / "audio.wav").write_bytes(b"x" * 2_000_000)
check("without repair they are reported", item(H.report(repair=False), "temp_files")["ok"] is False)
r = item(H.report(repair=True), "temp_files")
check("they are cleared", not (H.WORK / "half-done-song").exists() and r["ok"], r["plain"])

print("5. The saved update check is wrong")
H.STATE.mkdir(parents=True, exist_ok=True)
(H.STATE / "update.json").write_text(json.dumps({"release": {"url": "file:///tmp/gone/Midify.dmg"}}))
check("without repair it is reported", item(H.report(repair=False), "update_memory")["ok"] is False)
r = item(H.report(repair=True), "update_memory")
check("it is thrown away", not (H.STATE / "update.json").exists() and r["ok"], r["plain"])
(H.STATE / "update.json").write_text("not json at all")
check("an unreadable one is thrown away too", item(H.report(repair=True), "update_memory")["ok"])

print("6. Running out of space")
real_free = H._free_gb
H._free_gb = lambda p: 0.3
r = item(H.report(repair=False), "disk")
check("too little space stops songs and says so", not r["ok"] and r["blocking"] and "not enough" in r["plain"], r["plain"])
H._free_gb = lambda p: 1.2
r = item(H.report(repair=False), "disk")
check("a bit tight is a warning, not a blocker", not r["ok"] and not r["blocking"], r["plain"])
H._free_gb = lambda p: 9.0
check("plenty of space is fine", item(H.report(repair=False), "disk")["ok"])
H._free_gb = real_free

print("7. Missing audio tools")
real_tool = H._tool
H._tool = lambda name: name != "ffmpeg"
r = item(H.report(repair=False), "audio_tools")
check("no ffmpeg stops songs, and offers to install", not r["ok"] and r["blocking"] and r["fix"] == "install_tools", r["plain"])
H._tool = lambda name: name != "fluidsynth"
r = item(H.report(repair=False), "audio_tools")
check("no fluidsynth only costs previews", not r["ok"] and not r["blocking"] and "preview" in r["plain"], r["plain"])
H._tool = real_tool

print("8. The AI models are gone")
real_cache = H.MODEL_CACHE
H.MODEL_CACHE = WORK / "no-models-here"
r = item(H.report(repair=False), "models")
check("noticed, with a button to fetch them", not r["ok"] and r["fix"] == "download_models", r["plain"])
check("solo piano still works, so it is not a blocker", not r["blocking"])
H.MODEL_CACHE = real_cache

print("9. The Python setup is broken (what happened for real)")
real_import = H._importable
H._importable = lambda name: name != "torch"
r = item(H.report(repair=False), "python_setup")
check("noticed, blocking, and offers to rebuild", not r["ok"] and r["blocking"] and r["fix"] == "rebuild_python", r["plain"])
check("it promises the songs are kept", "songs" in r["plain"].lower())
H._importable = real_import

print("10. A broken check must not break the app")
broken = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
H.CHECKS.append(("exploding", "A check that fails", broken))
try:
    r = H.report(repair=False)
    check("the report still comes back", isinstance(r.get("items"), list) and len(r["items"]) == len(H.CHECKS))
    check("and says so plainly", plain_enough(item(r, "exploding")["plain"]), item(r, "exploding")["plain"])
finally:
    H.CHECKS.pop()

print("11. Failures are written down in plain English")
before = len(H.recent_problems(500))
H.note("A song could not be made because the file was not audio.", "Nothing else was affected.")
lines = H.recent_problems(5)
check("the newest line is first and reads plainly", lines and "not audio" in lines[0] and plain_enough(lines[0][17:]), lines[0] if lines else "")
check("the log grew by one", len(H.recent_problems(500)) == before + 1)
import pipeline  # noqa: E402
check("running out of memory is said in words", "memory" in H.describe_failure(MemoryError()).lower())
check("a full disk is said in words", "space" in H.describe_failure(OSError("No space left on device")).lower())
check("a plain user error is passed through as is",
      H.describe_failure(pipeline.UserError("That audio is silent.")) == "That audio is silent.")
check("anything unknown still reads plainly", plain_enough(H.describe_failure(ValueError("x"), doing="sending to the flash drive")),
      H.describe_failure(ValueError("x"), doing="sending to the flash drive"))

print("12. Repairs refuse sensibly")
try:
    H.REPAIRS.start("something that does not exist")
    check("an unknown repair is refused", False)
except ValueError as e:
    check("an unknown repair is refused", plain_enough(str(e)) or "nothing to fix" in str(e).lower(), str(e))

shutil.rmtree(WORK, ignore_errors=True)
print(f"\n{sum(results)} of {len(results)} checks passed.")
sys.exit(0 if all(results) else 1)
