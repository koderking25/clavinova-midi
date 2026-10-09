"""Draw Convertify's icon in code, and build the .icns.

    python mac/make_convertify_icon.py

Original artwork, and a drawing of what the app does: a run of sound bars where the middle one
keeps going down and turns into an arrow. Green, to match the app, and the same rounded tile shape
as Midify so the two sit together in a Dock and look related.

The canvas and the PNG writer come from mac/make_icon.py: one place that knows how to write a
PNG without an image library on this Mac.
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from make_icon import Canvas  # noqa: E402

SIZE = 1024
TOP_GREEN = (46, 190, 126)
BOTTOM_GREEN = (10, 95, 60)
PALE = (240, 253, 245)


def plate(c, m, radius):
    """The rounded tile, shaded from a lighter green at the top to a deeper one at the bottom."""
    x0, y0, x1, y1 = m, m, SIZE - m, SIZE - m
    for y in range(int(y0), int(y1)):
        through = (y - y0) / (y1 - y0)
        rgb = tuple(int(TOP_GREEN[k] + (BOTTOM_GREEN[k] - TOP_GREEN[k]) * through) for k in range(3))
        for x in range(int(x0), int(x1)):
            dx = max(x0 + radius - x, x - (x1 - 1 - radius), 0)
            dy = max(y0 + radius - y, y - (y1 - 1 - radius), 0)
            d = (dx * dx + dy * dy) ** 0.5
            if d <= radius - 1:
                c.set(x, y, rgb)
            elif d < radius:
                c.set(x, y, rgb, int(255 * (radius - d)))


def triangle(c, apex, left, right, rgb):
    """A filled triangle, softened at the edges by sampling each pixel in nine places."""
    xs = [apex[0], left[0], right[0]]
    ys = [apex[1], left[1], right[1]]
    def inside(px, py):
        def side(ax, ay, bx, by):
            return (bx - ax) * (py - ay) - (by - ay) * (px - ax)
        d1 = side(xs[0], ys[0], xs[1], ys[1])
        d2 = side(xs[1], ys[1], xs[2], ys[2])
        d3 = side(xs[2], ys[2], xs[0], ys[0])
        has_neg = min(d1, d2, d3) < 0
        has_pos = max(d1, d2, d3) > 0
        return not (has_neg and has_pos)
    for y in range(int(min(ys)) - 1, int(max(ys)) + 2):
        for x in range(int(min(xs)) - 1, int(max(xs)) + 2):
            hits = sum(inside(x + dx, y + dy)
                       for dx in (0.17, 0.5, 0.83) for dy in (0.17, 0.5, 0.83))
            if hits:
                c.set(x, y, rgb, int(255 * hits / 9))


def draw():
    c = Canvas(SIZE)
    m = SIZE * 0.08
    plate(c, m, SIZE * 0.22)

    bar_w = SIZE * 0.072
    gap = SIZE * 0.038
    first = SIZE / 2 - (bar_w * 5 + gap * 4) / 2
    middle_y = SIZE * 0.375
    # Four bars that look like sound, and a fifth in the middle that becomes the arrow. The side
    # bars stop well above the arrowhead: when they reached down beside it the whole thing read as
    # a fork rather than an arrow.
    heights = (SIZE * 0.072, SIZE * 0.125, None, SIZE * 0.118, SIZE * 0.068)
    for i, half in enumerate(heights):
        x = first + i * (bar_w + gap)
        if half is None:
            continue
        c.rounded_rect(x, middle_y - half, x + bar_w, middle_y + half, bar_w / 2, PALE)

    shaft_x = first + 2 * (bar_w + gap)
    c.rounded_rect(shaft_x, SIZE * 0.215, shaft_x + bar_w, SIZE * 0.565, bar_w / 2, PALE)
    centre = shaft_x + bar_w / 2
    triangle(c, (centre, SIZE * 0.80), (centre - SIZE * 0.125, SIZE * 0.555),
             (centre + SIZE * 0.125, SIZE * 0.555), PALE)
    return c


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    out = os.path.join(here, "convertify.icns")
    with tempfile.TemporaryDirectory() as tmp:
        base = os.path.join(tmp, "icon.png")
        draw().png(base)
        preview = os.path.join(here, "convertify-icon.png")
        subprocess.run(["cp", base, preview], check=True)
        iconset = os.path.join(tmp, "convertify.iconset")
        os.makedirs(iconset)
        for size in (16, 32, 64, 128, 256, 512, 1024):
            for name, pixels in ((f"icon_{size}x{size}.png", size),
                                 (f"icon_{size // 2}x{size // 2}@2x.png", size)):
                if size == 16 and "@2x" in name:
                    continue
                subprocess.run(["sips", "-z", str(pixels), str(pixels), base, "--out",
                                os.path.join(iconset, name)], capture_output=True, check=True)
        subprocess.run(["iconutil", "-c", "icns", iconset, "-o", out], check=True)
    print(f"  wrote {out}")
    print(f"  and a look at it: {preview}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
