# -*- coding: utf-8 -*-
"""Render assets/icon-180.png from the same geometry as icon.svg.

The awesome-dsh-plugin list uses a raster storefront tile; this draws it with
Pillow so the repository keeps one source of truth for the artwork.
"""
from __future__ import annotations

import pathlib

from PIL import Image, ImageDraw

SIZE = 180
BG = (231, 240, 238)
TEAL = (47, 111, 107)
FOLD = (156, 207, 200)

image = Image.new("RGBA", (SIZE, SIZE), BG)
draw = ImageDraw.Draw(image)

draw.rounded_rectangle((6, 6, SIZE - 6, SIZE - 6), radius=42, fill=BG)

# Page body.
page = (56, 40, 112, 140)
draw.rounded_rectangle(page, radius=9, fill=TEAL)

# Folded corner.
draw.polygon([(92, 40), (112, 40), (112, 60)], fill=FOLD)

# Text lines.
for y, width in ((78, 34), (94, 34), (110, 22)):
    draw.rounded_rectangle((66, y, 66 + width, y + 7), radius=3, fill=BG)

# Sync ring with a gap at the top-right and an arrow head closing it.
draw.arc((100, 80, 152, 132), start=300, end=215, fill=TEAL, width=9)
draw.polygon([(134, 74), (153, 86), (134, 96)], fill=TEAL)

out = pathlib.Path("assets/icon-180.png")
out.parent.mkdir(exist_ok=True)
image.convert("RGB").save(out, "PNG", optimize=True)
print(out, out.stat().st_size, "bytes")
