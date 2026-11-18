#!/usr/bin/env python3
"""Create a small original reference portrait for I2V smoke tests.

The image is deliberately synthetic and license-free: it gives the model a
clear face, hair silhouette and color palette without shipping a copyrighted
character. It is not a quality benchmark on its own; it is a reproducible
identity/geometry probe.
"""

from __future__ import annotations

import math
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter


def make(path: Path, size: tuple[int, int] = (480, 864)) -> None:
    scale = 2
    w, h = size[0] * scale, size[1] * scale
    image = Image.new("RGB", (w, h), (18, 22, 34))
    draw = ImageDraw.Draw(image)

    # Night city gradient.
    for y in range(h):
        t = y / h
        color = (
            int(18 + 26 * t),
            int(22 + 12 * t),
            int(34 + 44 * t),
        )
        draw.line([(0, y), (w, y)], fill=color)
    # Window glow.
    for x, y, r in [(90, 180, 60), (760, 250, 90), (650, 700, 46), (120, 720, 70)]:
        draw.ellipse((x - r, y - r, x + r, y + r), fill=(40, 80, 96))
    image = image.filter(ImageFilter.GaussianBlur(18 * scale))
    draw = ImageDraw.Draw(image)

    cx = w // 2
    # Shoulders / coat.
    draw.rounded_rectangle(
        (cx - 150 * scale, int(h * 0.72), cx + 150 * scale, h + 40 * scale),
        radius=60 * scale,
        fill=(176, 52, 62),
    )
    draw.polygon(
        [(cx - 150 * scale, int(h * 0.78)), (cx, int(h * 0.70)), (cx + 150 * scale, int(h * 0.78)),
         (cx + 110 * scale, h), (cx - 110 * scale, h)],
        fill=(198, 66, 74),
    )
    # Neck.
    draw.rounded_rectangle(
        (cx - 32 * scale, int(h * 0.62), cx + 32 * scale, int(h * 0.78)),
        radius=24 * scale,
        fill=(238, 190, 168),
    )
    # Hair back.
    draw.ellipse(
        (cx - 108 * scale, int(h * 0.20), cx + 108 * scale, int(h * 0.66)),
        fill=(38, 30, 48),
    )
    draw.polygon(
        [(cx - 105 * scale, int(h * 0.36)), (cx - 130 * scale, int(h * 0.68)),
         (cx - 70 * scale, int(h * 0.64)), (cx - 78 * scale, int(h * 0.35))],
        fill=(46, 36, 58),
    )
    draw.polygon(
        [(cx + 105 * scale, int(h * 0.36)), (cx + 130 * scale, int(h * 0.68)),
         (cx + 70 * scale, int(h * 0.64)), (cx + 78 * scale, int(h * 0.35))],
        fill=(46, 36, 58),
    )
    # Face.
    draw.ellipse(
        (cx - 88 * scale, int(h * 0.25), cx + 88 * scale, int(h * 0.62)),
        fill=(247, 207, 185),
    )
    # Ears.
    draw.ellipse((cx - 101 * scale, int(h * 0.38), cx - 76 * scale, int(h * 0.48)), fill=(240, 194, 173))
    draw.ellipse((cx + 76 * scale, int(h * 0.38), cx + 101 * scale, int(h * 0.48)), fill=(240, 194, 173))
    # Fringe.
    draw.polygon(
        [(cx - 92 * scale, int(h * 0.29)), (cx - 30 * scale, int(h * 0.22)),
         (cx + 16 * scale, int(h * 0.30)), (cx - 12 * scale, int(h * 0.39)),
         (cx - 46 * scale, int(h * 0.32)), (cx - 72 * scale, int(h * 0.42))],
        fill=(48, 38, 62),
    )
    draw.polygon(
        [(cx + 10 * scale, int(h * 0.25)), (cx + 92 * scale, int(h * 0.30)),
         (cx + 78 * scale, int(h * 0.46)), (cx + 42 * scale, int(h * 0.34))],
        fill=(48, 38, 62),
    )
    # Eyes.
    for side in (-1, 1):
        ex = cx + side * 38 * scale
        ey = int(h * 0.42)
        draw.ellipse((ex - 22 * scale, ey - 15 * scale, ex + 22 * scale, ey + 15 * scale), fill=(255, 255, 255))
        draw.ellipse((ex - 12 * scale, ey - 12 * scale, ex + 12 * scale, ey + 12 * scale), fill=(42, 154, 160))
        draw.ellipse((ex - 6 * scale, ey - 7 * scale, ex + 6 * scale, ey + 7 * scale), fill=(16, 24, 34))
        draw.ellipse((ex - 9 * scale, ey - 10 * scale, ex - 3 * scale, ey - 4 * scale), fill=(255, 255, 255))
        # Lashes.
        draw.arc((ex - 26 * scale, ey - 22 * scale, ex + 26 * scale, ey + 10 * scale),
                 200, 340, fill=(38, 30, 44), width=3 * scale)
    # Nose and mouth.
    draw.line([(cx, int(h * 0.45)), (cx - 4 * scale, int(h * 0.50))], fill=(214, 154, 136), width=2 * scale)
    draw.arc((cx - 20 * scale, int(h * 0.51), cx + 20 * scale, int(h * 0.56)),
             10, 170, fill=(164, 62, 76), width=3 * scale)
    # Blush.
    for side in (-1, 1):
        bx = cx + side * 66 * scale
        draw.ellipse((bx - 18 * scale, int(h * 0.48), bx + 18 * scale, int(h * 0.51)),
                     fill=(246, 166, 158))
    # Coat collar and zip.
    draw.polygon(
        [(cx - 92 * scale, int(h * 0.72)), (cx, int(h * 0.84)), (cx + 92 * scale, int(h * 0.72)),
         (cx + 64 * scale, int(h * 0.80)), (cx, int(h * 0.90)), (cx - 64 * scale, int(h * 0.80))],
        fill=(120, 32, 44),
    )
    draw.line([(cx, int(h * 0.84)), (cx, h)], fill=(240, 200, 150), width=2 * scale)
    # A few snow/light specks.
    for i in range(28):
        x = int((math.sin(i * 12.9898) * 43758.5453) % 1 * w)
        y = int((math.sin(i * 78.233) * 12543.123) % 1 * h)
        r = (2 + i % 4) * scale
        draw.ellipse((x - r, y - r, x + r, y + r), fill=(190, 226, 224))

    image = image.resize(size, Image.Resampling.LANCZOS)
    path.parent.mkdir(parents=True, exist_ok=True)
    image.save(path, format="PNG", optimize=True)
    print(f"wrote {path} ({path.stat().st_size} bytes)")


if __name__ == "__main__":
    make(Path(__file__).resolve().parents[1] / "tests" / "fixtures" / "anime_reference.png")
