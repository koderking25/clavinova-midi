"""Draw the app icon in code (no image libraries on this Mac) and build the .icns.

Original artwork: a warm rounded square with a run of piano keys across the lower
half and a single note dot above them.

    python mac/make_icon.py
"""
import os
import struct
import subprocess
import sys
import tempfile
import zlib

SIZE = 1024
BG = (24, 20, 18)
PLATE = (180, 71, 47)
PLATE_DARK = (150, 55, 36)
WHITE_KEY = (247, 242, 233)
BLACK_KEY = (28, 24, 21)
DOT = (247, 242, 233)


class Canvas:
    def __init__(self, size):
        self.size = size
        self.px = bytearray(size * size * 4)

    def set(self, x, y, rgb, alpha=255):
        if 0 <= x < self.size and 0 <= y < self.size:
            i = (y * self.size + x) * 4
            if alpha == 255:
                self.px[i:i + 4] = bytes((*rgb, 255))
            else:                                   # simple blend, for smooth edges
                a = alpha / 255
                for k in range(3):
                    old = self.px[i + k]
                    self.px[i + k] = int(rgb[k] * a + old * (1 - a))
                self.px[i + 3] = max(self.px[i + 3], alpha)

    def rounded_rect(self, x0, y0, x1, y1, radius, rgb):
        for y in range(int(y0), int(y1)):
            for x in range(int(x0), int(x1)):
                dx = max(x0 + radius - x, x - (x1 - 1 - radius), 0)
                dy = max(y0 + radius - y, y - (y1 - 1 - radius), 0)
                d = (dx * dx + dy * dy) ** 0.5
                if d <= radius - 1:
                    self.set(x, y, rgb)
                elif d < radius:                    # feather the corner by one pixel
                    self.set(x, y, rgb, int(255 * (radius - d)))

    def circle(self, cx, cy, r, rgb):
        for y in range(int(cy - r - 1), int(cy + r + 2)):
            for x in range(int(cx - r - 1), int(cx + r + 2)):
                d = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
                if d <= r - 1:
                    self.set(x, y, rgb)
                elif d < r:
                    self.set(x, y, rgb, int(255 * (r - d)))

    def png(self, path):
        raw = bytearray()
        for y in range(self.size):
            raw.append(0)                            # filter: none
            raw += self.px[y * self.size * 4:(y + 1) * self.size * 4]

        def chunk(tag, data):
            return (struct.pack(">I", len(data)) + tag + data
                    + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF))

        head = struct.pack(">IIBBBBB", self.size, self.size, 8, 6, 0, 0, 0)
        with open(path, "wb") as f:
            f.write(b"\x89PNG\r\n\x1a\n")
            f.write(chunk(b"IHDR", head))
            f.write(chunk(b"IDAT", zlib.compress(bytes(raw), 9)))
            f.write(chunk(b"IEND", b""))


def draw():
    c = Canvas(SIZE)
    m = SIZE * 0.08                                   # margin macOS leaves around icons
    c.rounded_rect(m, m, SIZE - m, SIZE - m, SIZE * 0.20, PLATE)
    # a darker band behind the keys, so the keys sit on something
    c.rounded_rect(m, SIZE * 0.52, SIZE - m, SIZE - m, SIZE * 0.20, PLATE_DARK)

    left, right = m + SIZE * 0.06, SIZE - m - SIZE * 0.06
    top, bottom = SIZE * 0.56, SIZE - m - SIZE * 0.06
    keys = 7
    width = (right - left) / keys
    for i in range(keys):
        x = left + i * width
        c.rounded_rect(x + width * 0.06, top, x + width * 0.94, bottom, width * 0.12, WHITE_KEY)
    # black keys sit between the white ones, skipping the two usual gaps
    for i in (0, 1, 3, 4, 5):
        x = left + (i + 1) * width
        c.rounded_rect(x - width * 0.22, top, x + width * 0.22, top + (bottom - top) * 0.58,
                       width * 0.1, BLACK_KEY)
    # one note above the keyboard
    c.circle(SIZE * 0.40, SIZE * 0.34, SIZE * 0.075, DOT)
    c.rounded_rect(SIZE * 0.455, SIZE * 0.17, SIZE * 0.487, SIZE * 0.35, SIZE * 0.016, DOT)
    c.rounded_rect(SIZE * 0.455, SIZE * 0.17, SIZE * 0.60, SIZE * 0.205, SIZE * 0.016, DOT)
    return c


def main():
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "icon.icns")
    with tempfile.TemporaryDirectory() as tmp:
        base = os.path.join(tmp, "icon.png")
        draw().png(base)
        iconset = os.path.join(tmp, "icon.iconset")
        os.makedirs(iconset)
        for size in (16, 32, 64, 128, 256, 512, 1024):
            for name, px in ((f"icon_{size}x{size}.png", size), (f"icon_{size // 2}x{size // 2}@2x.png", size)):
                if size == 16 and "@2x" in name:
                    continue
                subprocess.run(["sips", "-z", str(px), str(px), base, "--out", os.path.join(iconset, name)],
                               check=True, capture_output=True)
        subprocess.run(["iconutil", "-c", "icns", iconset, "-o", out], check=True)
    print(f"wrote {out} ({os.path.getsize(out) / 1e3:.0f} KB)")


if __name__ == "__main__":
    sys.exit(main())
