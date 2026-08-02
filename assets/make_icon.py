"""Generate assets/mapgen.ico: an MG monogram, no third-party libraries.

Renders at 4x then box-downsamples, which is cheaper to write than a real
anti-aliased rasteriser and gives clean enough edges at every icon size.
"""
import math
import struct
import sys
import zlib
from pathlib import Path

BG = (0x1E, 0x2A, 0x38, 0xFF)
FG = (0xE8, 0xE4, 0xDC, 0xFF)
SIZES = [16, 24, 32, 48, 64, 128, 256]
SS = 4  # supersample factor


def dist_to_segment(px, py, ax, ay, bx, by):
    dx, dy = bx - ax, by - ay
    if dx == 0 and dy == 0:
        return math.hypot(px - ax, py - ay)
    t = ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)
    t = max(0.0, min(1.0, t))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def letter_segments(n):
    """Segments for M and G laid out in an n x n box."""
    segs = []
    top = n * 0.34
    bot = n * 0.66

    # M on the left. The middle vertex drops most of the way to the
    # baseline, which is what stops it reading as a rounded blob.
    mx0, mx1 = n * 0.13, n * 0.43
    mid_x = (mx0 + mx1) / 2
    apex_y = top + (bot - top) * 0.80
    segs.append((mx0, bot, mx0, top))
    segs.append((mx0, top, mid_x, apex_y))
    segs.append((mid_x, apex_y, mx1, top))
    segs.append((mx1, top, mx1, bot))

    # G on the right: a bowl open at the upper right, plus the crossbar.
    cx, cy = n * 0.705, (top + bot) / 2
    r = (bot - top) / 2
    start, end = math.radians(60), math.radians(355)
    steps = 96
    prev = None
    for i in range(steps + 1):
        a = start + (end - start) * i / steps
        # y is negated because screen coordinates run downward.
        p = (cx + r * math.cos(a), cy - r * math.sin(a))
        if prev is not None:
            segs.append((prev[0], prev[1], p[0], p[1]))
        prev = p
    # Crossbar running left from the lower terminal into the bowl.
    segs.append((cx + r * 0.99, cy, cx + r * 0.10, cy))
    return segs


def render(n):
    big = n * SS
    segs = letter_segments(big)
    stroke = big * 0.052
    radius = big * 0.18  # rounded corner radius of the tile

    rows = []
    for y in range(big):
        row = []
        for x in range(big):
            px, py = x + 0.5, y + 0.5
            # Rounded-rectangle mask for the background tile.
            qx = max(radius - px, px - (big - radius), 0.0)
            qy = max(radius - py, py - (big - radius), 0.0)
            inside = math.hypot(qx, qy) <= radius
            if not inside:
                row.append((0, 0, 0, 0))
                continue
            d = min(dist_to_segment(px, py, *s) for s in segs)
            row.append(FG if d <= stroke else BG)
        rows.append(row)

    # Box-downsample back to n x n.
    out = []
    for y in range(n):
        row = []
        for x in range(n):
            r = g = b = a = 0
            for dy in range(SS):
                for dx in range(SS):
                    pr, pg, pb, pa = rows[y * SS + dy][x * SS + dx]
                    r += pr * pa
                    g += pg * pa
                    b += pb * pa
                    a += pa
            if a == 0:
                row.append((0, 0, 0, 0))
            else:
                row.append((r // a, g // a, b // a, a // (SS * SS)))
        out.append(row)
    return out


def to_png(pixels):
    n = len(pixels)
    raw = b"".join(
        b"\x00" + b"".join(struct.pack("BBBB", *px) for px in row) for row in pixels
    )

    def chunk(tag, data):
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", n, n, 8, 6, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def main():
    target = Path(sys.argv[1])
    target.parent.mkdir(parents=True, exist_ok=True)

    images = []
    for n in SIZES:
        png = to_png(render(n))
        images.append((n, png))
        print(f"  rendered {n}x{n}: {len(png)} bytes")

    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for n, png in images:
        entries += struct.pack(
            "<BBBBHHII", 0 if n >= 256 else n, 0 if n >= 256 else n,
            0, 0, 1, 32, len(png), offset
        )
        blobs += png
        offset += len(png)

    target.write_bytes(header + entries + blobs)
    print(f"wrote {target} ({target.stat().st_size} bytes, {len(images)} sizes)")


if __name__ == "__main__":
    main()
