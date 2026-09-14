"""Updater tests. Everything destructive happens to disposable copies in a temp folder.

    .venv/bin/python mac/build_app.py        # the tests copy the built app
    .venv/bin/python tests/test_updater.py

Needs the internet: it reads the real latest release from GitHub and downloads it. Every
swap step waits for its script to finish completely before checking, so no step can leak
into the next one.
"""
import os
import plistlib
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / "app"))
import updater as U  # noqa: E402

WORK = Path(tempfile.mkdtemp(prefix="updater-tests-"))
U.STATE = WORK / "state"
U.CACHE = U.STATE / "update.json"
U.DOWNLOADS = U.STATE / "updates"
U.LOG = WORK / "test.log"
results = []


def check(label, ok, detail=""):
    ok = bool(ok)
    results.append(ok)
    print(f"  {'PASS' if ok else 'FAIL'}  {label}{('   ' + detail) if detail else ''}")


def expect_refusal(label, fn, must_contain):
    try:
        fn()
        check(label, False, "it did NOT refuse")
    except U.UpdateError as e:
        check(label, must_contain.lower() in str(e).lower(), f'said: "{e}"')


def stand_in(seconds, reap=True):
    """A process playing the running app. reap=False leaves a zombie behind when it exits."""
    p = subprocess.Popen(["sleep", str(seconds)])
    if reap:
        threading.Thread(target=p.wait, daemon=True).start()
    return p


def wait_for_scripts(limit=90):
    script = str(U.DOWNLOADS / "swap.sh")
    start = time.time()
    while time.time() - start < limit:
        if subprocess.run(["pgrep", "-f", script], capture_output=True).returncode != 0:
            return round(time.time() - start, 1)
        time.sleep(0.5)
    return None


def log_mark():
    return len(U.LOG.read_text()) if U.LOG.exists() else 0


def log_since(mark):
    return (U.LOG.read_text() if U.LOG.exists() else "")[mark:]


print("1. Versions")
check("1.10.0 is newer than 1.9.0", U.parse_version("1.10.0") > U.parse_version("1.9.0"))
check("v1.3.0 reads the same as 1.3.0", U.parse_version("v1.3.0") == (1, 3, 0))
check("1.3.0 is not newer than 1.3.0", not (U.parse_version("1.3.0") > U.parse_version("1.3.0")))
check("nonsense is not a version", U.parse_version("latest") is None)
check("running from the code has no version (updates off)", U.current_version() is None)

print("2. Checking GitHub, gently")
calls = {"n": 0}
real_urlopen = urllib.request.urlopen


def counting_urlopen(*a, **k):
    if "api.github.com" in str(getattr(a[0], "full_url", a[0])):
        calls["n"] += 1
    return real_urlopen(*a, **k)


urllib.request.urlopen = counting_urlopen
U.current_version = lambda: "1.2.9"
u = U.Updater()
s = u.check()
latest = s["latest"]
check("finds a newer release for an older app", s["state"] == "available" and latest, f"latest {latest}")
n = calls["n"]
u.check()
check("a second automatic check does not ask GitHub again", calls["n"] == n)
u.check(manual=True)
u.check(manual=True)
check("two manual checks within a minute ask GitHub at most once", calls["n"] <= n + 1)
U.current_version = lambda: latest
check("the newest version reports up to date", U.Updater().check()["state"] == "up_to_date")
U.current_version = lambda: "1.2.9"
release = u._release

print("3. Downloads must match GitHub's fingerprint")
dmg = u.download(release)
check("the real download matches its fingerprint", dmg.exists() and dmg.stat().st_size == release["size"])
dmg.unlink()
expect_refusal("a download with the wrong fingerprint is refused",
               lambda: u.download(dict(release, digest="sha256:" + "0" * 64)), "fingerprint")
check("nothing is left behind after a refused download", not any(U.DOWNLOADS.glob("*")))

apps = WORK / "Applications"
apps.mkdir()
fake = apps / "Clavinova MIDI Maker.app"


def make_old_copy():
    shutil.rmtree(fake, ignore_errors=True)
    for leftover in apps.glob(".*"):
        shutil.rmtree(leftover, ignore_errors=True)
    subprocess.run(["ditto", str(REPO / "mac/build/Midify.app"), str(fake)], check=True)
    p = fake / "Contents/Info.plist"
    info = plistlib.loads(p.read_bytes())
    info["CFBundleShortVersionString"] = info["CFBundleVersion"] = "1.2.9"
    p.write_bytes(plistlib.dumps(info))
    subprocess.run(["codesign", "--force", "--deep", "--sign", "-", str(fake)], check=True, capture_output=True)


def version_of(app):
    return plistlib.loads((Path(app) / "Contents/Info.plist").read_bytes()).get("CFBundleShortVersionString")


def signed(app):
    return subprocess.run(["codesign", "--verify", str(app)], capture_output=True).returncode == 0


print("4. Refusals that must leave the app untouched")
make_old_copy()
check("disposable copy is version 1.2.9, signed", version_of(fake) == "1.2.9" and signed(fake))
expect_refusal("a release without a fingerprint is refused",
               lambda: u.prepare(dict(release, digest=None), target=fake), "fingerprint")
orig_id = U.BUNDLE_ID
U.BUNDLE_ID = "com.someone.else"
expect_refusal("a download holding a different app is refused", lambda: u.prepare(release, target=fake), "different app")
U.BUNDLE_ID = orig_id
expect_refusal("a version that does not match the release is refused",
               lambda: u.prepare(dict(release, tag="v9.9.9"), target=fake), "version")
os.chmod(apps, 0o555)
expect_refusal("a folder the app cannot write to is refused", lambda: u.prepare(release, target=fake), "cannot replace")
os.chmod(apps, 0o755)
check("after all refusals the app is untouched and nothing hidden was left",
      version_of(fake) == "1.2.9" and signed(fake) and not list(apps.glob(".*")))

print("5. The real swap")
make_old_copy()
staged = u.prepare(release, target=fake)
check("the new version is staged beside the app, hidden", staged.name.startswith(".") and version_of(staged) == latest)
check("the live app is still the old version while staged", version_of(fake) == "1.2.9")
mark = log_mark()
app_proc = stand_in(2)
u.hand_over(staged, target=fake, pid=app_proc.pid, relaunch=False)
took = wait_for_scripts()
check("the swap finished soon after the app quit", took is not None and took < 15, f"script done after {took}s")
check("it is now the new version", version_of(fake) == latest, f"now {version_of(fake)}")
check("its signature is valid", signed(fake))
check("no hidden staged or backup copies remain", not list(apps.glob(".*")))
check("the log says it updated", f"updated to {latest}" in log_since(mark))

print("6. An app that exits but lingers as a zombie still counts as quit")
make_old_copy()
staged = u.prepare(release, target=fake)
zombie = stand_in(1, reap=False)
u.hand_over(staged, target=fake, pid=zombie.pid, relaunch=False)
took = wait_for_scripts()
check("the swap did not wait the full minute", took is not None and took < 15, f"script done after {took}s")
check("it installed the new version", version_of(fake) == latest)
zombie.wait()

print("7. Sabotage: the new copy disappears before the swap")
make_old_copy()
staged = u.prepare(release, target=fake)
mark = log_mark()
app_proc = stand_in(3)
u.hand_over(staged, target=fake, pid=app_proc.pid, relaunch=False)
shutil.rmtree(staged)
took = wait_for_scripts()
check("the script finished", took is not None, f"after {took}s")
check("the old app is back, intact and signed", version_of(fake) == "1.2.9" and signed(fake))
check("the log explains it restored the old app", "restoring the old one" in log_since(mark))

print("8. Sabotage: the new copy is corrupted before the swap")
make_old_copy()
staged = u.prepare(release, target=fake)
mark = log_mark()
app_proc = stand_in(3)
u.hand_over(staged, target=fake, pid=app_proc.pid, relaunch=False)
(staged / "Contents/Resources/app/desktop.py").write_text("tampered")     # breaks its signature
took = wait_for_scripts()
check("the script finished", took is not None, f"after {took}s")
check("the old app is back, intact and signed", version_of(fake) == "1.2.9" and signed(fake))
check("the log says the signature check failed", "failed its signature check" in log_since(mark))
check("no hidden copies remain", not list(apps.glob(".*")))

print("9. An app that never quits (waits a minute, by design)")
make_old_copy()
staged = u.prepare(release, target=fake)
mark = log_mark()
stubborn = stand_in(300)
u.hand_over(staged, target=fake, pid=stubborn.pid, relaunch=False)
took = wait_for_scripts(limit=90)
check("the script gave up after about a minute", took is not None and 55 <= took <= 75, f"after {took}s")
check("nothing was changed", version_of(fake) == "1.2.9" and signed(fake))
check("its staged copy was cleaned up", not staged.exists())
check("the log explains why", "did not quit" in log_since(mark))
stubborn.kill()

print("10. Leftovers from an interrupted update")
(apps / f".{fake.name}.update").mkdir()
(apps / f".{fake.name}.old").mkdir()
U.tidy_leftovers(target=fake)
check("tidied away while the app is intact", not list(apps.glob(".*")))

shutil.rmtree(WORK, ignore_errors=True)
print(f"\n{sum(results)} of {len(results)} checks passed.")
sys.exit(0 if all(results) else 1)
