"""Build the folder and zip to upload to Cloudflare.

    .venv/bin/python web/package_site.py

Cloudflare's dashboard takes "a zip file or single folder of assets" dragged onto it.
The catch that broke the first attempt: whatever you upload becomes the site root. Upload
the repository and the page ends up at /web/ while "/" is a 404. So this builds a folder
whose root IS the site, and a zip whose root is the same, and then checks exactly that.

It leaves out the workbench pages, the test fixtures and the Python, which have no
business on a public site.
"""
import os
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "dist")
SITE = os.path.join(OUT, "midify-site")
ZIP = os.path.join(OUT, "midify-site.zip")
CLOUDFLARE_FILE_LIMIT = 25 * 1024 * 1024          # 25 MiB per file, free or paid

WANTED_FILES = ["index.html", "_headers"]
WANTED_DIRS = ["js", "models"]


def main():
    if os.path.exists(OUT):
        shutil.rmtree(OUT)
    os.makedirs(SITE)

    for name in WANTED_FILES:
        src = os.path.join(HERE, name)
        if not os.path.exists(src):
            sys.exit(f"missing {name}. The site cannot go up without it.")
        shutil.copy(src, SITE)
    for name in WANTED_DIRS:
        src = os.path.join(HERE, name)
        if not os.path.isdir(src):
            sys.exit(f"missing {name}/. Run web/export_models.py first to build the model files.")
        shutil.copytree(src, os.path.join(SITE, name),
                        ignore=shutil.ignore_patterns(".DS_Store", "__pycache__"))

    files = [os.path.join(dp, f) for dp, _, fs in os.walk(SITE) for f in fs]
    total = sum(os.path.getsize(f) for f in files)
    biggest = max(files, key=os.path.getsize)
    too_big = [f for f in files if os.path.getsize(f) > CLOUDFLARE_FILE_LIMIT]

    with zipfile.ZipFile(ZIP, "w", zipfile.ZIP_DEFLATED) as z:
        for f in sorted(files):
            z.write(f, os.path.relpath(f, SITE))       # relative to the site, so its root is the site

    with zipfile.ZipFile(ZIP) as z:
        names = z.namelist()
    problems = []
    if "index.html" not in names:
        problems.append("index.html is not at the root of the zip, so the site would land one level down")
    if not any(n.startswith("models/") for n in names):
        problems.append("no model files in the zip")
    if too_big:
        problems.append(f"{len(too_big)} file(s) over Cloudflare's 25 MiB limit: "
                        + ", ".join(os.path.basename(f) for f in too_big))

    print(f"folder : {SITE}")
    print(f"zip    : {ZIP} ({os.path.getsize(ZIP) / 1e6:.1f} MB)")
    print(f"{len(files)} files, {total / 1e6:.1f} MB unpacked; largest "
          f"{os.path.basename(biggest)} at {os.path.getsize(biggest) / 1e6:.1f} MB")
    print(f"at the root of the zip: {', '.join(sorted(n for n in names if '/' not in n))}")
    if problems:
        sys.exit("PROBLEMS: " + "; ".join(problems))
    print("\nReady. In the Cloudflare dashboard, drag this zip (or the folder) onto the upload box.")
    print("Whatever you drop becomes the site root, which is why the zip contains index.html itself")
    print("rather than a folder containing it.")


if __name__ == "__main__":
    main()
