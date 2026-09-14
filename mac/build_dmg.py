"""Build the disk image people download: open it, drag the app onto Applications.

    .venv/bin/python mac/build_app.py      # first, if the app is not built
    .venv/bin/python mac/build_dmg.py

A browser cannot download a .app on its own (an app is a folder), so Mac apps ship as
disk images. This one opens to a plain window with the app on the left, the Applications
folder on the right and an arrow between them.

The window's background is drawn in code, because there are no image libraries here,
and the layout is written straight into the image with dmgbuild, because scripting
Finder is not allowed on this Mac.

This does not stop macOS warning that it "could not verify" the app. Only Apple's paid
notarisation does that. See the README.
"""
import os
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from make_icon import Canvas  # noqa: E402

NAME = "Midify"
APP = os.path.join(HERE, "build", f"{NAME}.app")
DMG = os.path.join(HERE, "build", "Midify.dmg")

WINDOW = (640, 400)                     # points
APP_AT = (170, 190)                     # icon centres, in points from the top left
APPS_AT = (470, 190)
ICON_SIZE = 128

BG = (246, 241, 234)
ARROW = (200, 104, 76)


def draw_background(scale):
    w, h = WINDOW[0] * scale, WINDOW[1] * scale
    c = Canvas(w)                       # Canvas is square; draw on it and crop rows below
    c.px[:] = bytes((*BG, 255)) * (w * w)        # solid fill: a zero radius rounded_rect paints nothing
    y = APP_AT[1] * scale
    x0 = (APP_AT[0] + ICON_SIZE * 0.62) * scale
    x1 = (APPS_AT[0] - ICON_SIZE * 0.62) * scale
    head = 26 * scale
    thick = 7 * scale
    c.rounded_rect(x0, y - thick / 2, x1 - head * 0.6, y + thick / 2, thick / 2, ARROW)
    for i in range(int(head)):          # the arrowhead, narrowing to a point
        half = (head - i) * 0.85
        x = int(x1 - head + i)
        for yy in range(int(y - half), int(y + half) + 1):
            c.set(x, yy, ARROW)
    return c, w, h


def write_png(canvas, w, h, path):
    """Write the top h rows of the square canvas as a w by h PNG.

    Cropping with sips cropped from the centre, which moved the arrow 120 points above
    the icons. Writing only the rows wanted cannot drift.
    """
    import struct
    import zlib
    raw = bytearray()
    for y in range(h):
        raw.append(0)                                 # PNG filter: none
        raw += canvas.px[y * canvas.size * 4:(y * canvas.size + w) * 4]

    def chunk(tag, data):
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

    with open(path, "wb") as f:
        f.write(b"\x89PNG\r\n\x1a\n")
        f.write(chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)))
        f.write(chunk(b"IDAT", zlib.compress(bytes(raw), 9)))
        f.write(chunk(b"IEND", b""))


def verify(dmg):
    """Mount it read only and check that what people will see is really inside."""
    problems = []
    mount = tempfile.mkdtemp(prefix="midify-dmg-")
    r = subprocess.run(["hdiutil", "attach", dmg, "-readonly", "-nobrowse", "-noautoopen",
                        "-mountpoint", mount], capture_output=True, text=True)
    if r.returncode != 0:
        return [f"the disk image does not mount: {r.stderr.strip()}"]
    try:
        app = os.path.join(mount, f"{NAME}.app")
        apps = os.path.join(mount, "Applications")
        if not os.path.isdir(app):
            problems.append("the app is not inside the disk image")
        if not os.path.islink(apps) or os.readlink(apps) != "/Applications":
            problems.append("the Applications shortcut is missing or points somewhere else")
        if not os.path.exists(os.path.join(mount, ".DS_Store")):
            problems.append("the window layout is missing")
        # dmgbuild keeps the picture as .background.tiff at the top of the image; other tools
        # use a .background folder. Accept either, but it has to really be there.
        bg_dir = os.path.join(mount, ".background")
        has_bg = any(n.startswith(".background.") for n in os.listdir(mount)) or (
            os.path.isdir(bg_dir) and any(f.startswith("background") for f in os.listdir(bg_dir)))
        if not has_bg:
            problems.append("the background picture is missing")
        if os.path.isdir(app):
            s = subprocess.run(["codesign", "--verify", app], capture_output=True, text=True)
            if s.returncode != 0:
                problems.append("the app's signature did not survive being put in the disk image")
            if not os.path.exists(os.path.join(app, "Contents", "Resources", "app", "desktop.py")):
                problems.append("the app inside is an old build")
    finally:
        subprocess.run(["hdiutil", "detach", mount, "-quiet"], capture_output=True)
        try:
            os.rmdir(mount)
        except OSError:
            pass
    return problems


def main():
    import dmgbuild

    if not os.path.isdir(APP):
        sys.exit("Build the app first: .venv/bin/python mac/build_app.py")
    with tempfile.TemporaryDirectory() as tmp:
        for scale, name in ((1, "background.png"), (2, "background@2x.png")):
            canvas, w, h = draw_background(scale)
            write_png(canvas, w, h, os.path.join(tmp, name))
        if os.path.exists(DMG):
            os.remove(DMG)
        settings = {
            "format": "UDZO",
            "filesystem": "HFS+",
            "files": [APP],
            "symlinks": {"Applications": "/Applications"},
            "icon": os.path.join(HERE, "icon.icns"),
            "background": os.path.join(tmp, "background.png"),     # picks up the @2x file too
            "window_rect": ((200, 140), WINDOW),
            "icon_size": ICON_SIZE,
            "text_size": 13,
            "icon_locations": {f"{NAME}.app": APP_AT, "Applications": APPS_AT},
            "show_status_bar": False,
            "show_tab_view": False,
            "show_toolbar": False,
            "show_pathbar": False,
            "show_sidebar": False,
            "default_view": "icon-view",
        }
        dmgbuild.build_dmg(DMG, NAME, settings=settings)

    problems = verify(DMG)
    print(f"built {DMG} ({os.path.getsize(DMG) / 1e6:.1f} MB)")
    if problems:
        sys.exit("PROBLEMS: " + "; ".join(problems))
    print("checked by mounting it: the app, the Applications shortcut, the window layout and the")
    print("background are all inside, and the app's signature survived.")


if __name__ == "__main__":
    main()
