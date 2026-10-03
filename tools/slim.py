"""Remove parts of the Python environment that Midify never uses.

    .venv/bin/python tools/slim.py            # remove them
    .venv/bin/python tools/slim.py --check    # just say what would go

Saves about 156 MB of 908 with no change to how anything sounds: tests/selftest.py scores the same
notes before and after (piano 1.000, band bass 0.975).

Only things proven unnecessary are listed here. Two obvious-looking candidates were tried and both
broke the app, which is why they are named below rather than removed:
  torch/bin      holds torch_shm_manager, which torch needs to pass audio between processes, and
                 every song is made in its own process.
  torch/testing  torch imports it on the way up: without it, "import torch" fails outright.
"""
import argparse
import shutil
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent

# (path inside site-packages, why it is safe to go)
UNUSED = [
    ("torch/include", "C++ headers for building against torch, which nothing here does"),
    ("PyObjCTest", "the PyObjC project's own test suite"),
]


def find_site():
    """The packages folder of the environment this is running in, and only that one.

    Not Path(sys.executable).resolve(): a virtual environment's python is a link to a shared
    interpreter, so resolving it walks out of the environment and lands on the Python other
    projects use. Trimming that would damage them."""
    import sysconfig
    site = Path(sysconfig.get_paths()["purelib"])
    if not site.is_dir():
        return None
    # Only ever trim an environment made for this app: it must have its own pyvenv.cfg.
    root = site.parent.parent.parent
    if not (root / "pyvenv.cfg").is_file():
        print(f"{site} is not a private environment for this app, so nothing was touched.")
        return None
    return site


def megabytes(path):
    total = 0
    for item in Path(path).rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
        except OSError:
            continue
    return total / 1e6


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="say what would go, and remove nothing")
    args = ap.parse_args()
    site = find_site()
    if not site:
        print("Could not find the Python environment, so nothing was changed.")
        return 0

    before = megabytes(site)
    freed = 0.0
    for rel, why in UNUSED:
        target = site / rel
        if not target.exists():
            continue
        size = megabytes(target)
        print(f"  {rel}: {size:.0f} MB, {why}")
        freed += size
        if not args.check:
            shutil.rmtree(target, ignore_errors=True)

    pyc = [p for p in site.rglob("*.pyc")]
    pyc_mb = sum(p.stat().st_size for p in pyc if p.is_file()) / 1e6
    if pyc:
        print(f"  compiled copies inside the environment: {pyc_mb:.0f} MB in {len(pyc)} files, "
              "kept again outside it while the app runs")
        freed += pyc_mb
        if not args.check:
            for p in pyc:
                try:
                    p.unlink()
                except OSError:
                    pass
            for d in sorted(site.rglob("__pycache__"), key=lambda d: -len(d.parts)):
                try:
                    d.rmdir()
                except OSError:
                    pass

    if args.check:
        print(f"\nWould free about {freed:.0f} MB of {before:.0f} MB.")
    else:
        print(f"\nFreed about {freed:.0f} MB. The environment is now {megabytes(site):.0f} MB.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
