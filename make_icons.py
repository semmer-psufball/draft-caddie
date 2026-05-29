#!/usr/bin/env python
"""One-off icon generator for the PWA. Run locally once; commit the PNGs.

  pip install pillow
  python make_icons.py
"""
from pathlib import Path
from PIL import Image, ImageDraw, ImageFont

OUT = Path(__file__).resolve().parent / "icons"
BG = (15, 20, 25)          # #0f1419
ACCENT = (41, 128, 185)    # #2980b9
BALL = (139, 94, 60)       # football brown


def font(size):
    for name in ("seguibl.ttf", "segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except Exception:
            continue
    return ImageFont.load_default()


def make(size):
    img = Image.new("RGBA", (size, size), BG + (255,))
    d = ImageDraw.Draw(img)
    pad = int(size * 0.10)
    # rounded accent tile
    d.rounded_rectangle([pad, pad, size - pad, size - pad],
                        radius=int(size * 0.18), fill=(22, 28, 36, 255),
                        outline=ACCENT + (255,), width=max(2, size // 90))
    # football
    cx, cy = size / 2, size * 0.42
    rx, ry = size * 0.26, size * 0.16
    d.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=BALL + (255,))
    d.line([cx - rx * 0.5, cy, cx + rx * 0.5, cy], fill=(245, 245, 245, 255),
           width=max(2, size // 80))
    for i in range(-2, 3):
        x = cx + i * rx * 0.18
        d.line([x, cy - ry * 0.28, x, cy + ry * 0.28], fill=(245, 245, 245, 255),
               width=max(1, size // 130))
    # slot label
    f = font(int(size * 0.30))
    txt = "9"
    bb = d.textbbox((0, 0), txt, font=f)
    tw, th = bb[2] - bb[0], bb[3] - bb[1]
    d.text(((size - tw) / 2 - bb[0], size * 0.60 - bb[1]), txt, font=f, fill=(228, 230, 234, 255))
    OUT.mkdir(parents=True, exist_ok=True)
    img.save(OUT / f"icon-{size}.png")
    print(f"wrote {OUT / f'icon-{size}.png'}")


if __name__ == "__main__":
    make(192)
    make(512)
