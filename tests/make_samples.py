"""Generate sample artwork for testing (logo, line art, transparent badge, photo)."""
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

out = Path(__file__).parent / "samples"
out.mkdir(exist_ok=True)


def font(size):
    for name in ("arialbd.ttf", "arial.ttf", "DejaVuSans-Bold.ttf"):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            pass
    return ImageFont.load_default()


# 1. flat logo on white, JPEG (artifacts + anti-aliasing)
im = Image.new("RGB", (800, 800), "white")
d = ImageDraw.Draw(im)
d.ellipse((60, 60, 740, 740), fill=(20, 60, 140))
d.ellipse((110, 110, 690, 690), fill=(250, 200, 40))
d.polygon([(400, 150), (460, 330), (650, 330), (495, 440), (555, 620), (400, 510), (245, 620), (305, 440), (150, 330), (340, 330)], fill=(200, 30, 40))
d.text((400, 690), "STITCH", fill="white", font=font(56), anchor="mm")
im.save(out / "logo.jpg", quality=85)

# 2. line art (thin + medium strokes)
im = Image.new("RGB", (900, 600), "white")
d = ImageDraw.Draw(im)
d.arc((80, 80, 520, 520), 0, 300, fill="black", width=26)
d.line((560, 100, 840, 500), fill="black", width=6)
d.line((560, 500, 840, 100), fill="black", width=14)
d.rectangle((600, 250, 800, 350), outline=(0, 120, 60), width=10)
d.text((300, 300), "Hi", fill="black", font=font(120), anchor="mm")
im.save(out / "lineart.png")

# 3. transparent sticker-style PNG
im = Image.new("RGBA", (700, 700), (0, 0, 0, 0))
d = ImageDraw.Draw(im)
d.rounded_rectangle((50, 150, 650, 550), 80, fill=(30, 30, 30, 255))
d.rounded_rectangle((80, 180, 620, 520), 60, fill=(80, 200, 220, 255))
d.ellipse((150, 230, 330, 410), fill=(255, 255, 255, 255))
d.ellipse((210, 290, 270, 350), fill=(30, 30, 30, 255))
d.ellipse((370, 230, 550, 410), fill=(255, 255, 255, 255))
d.ellipse((430, 290, 490, 350), fill=(30, 30, 30, 255))
d.arc((220, 330, 480, 480), 20, 160, fill=(30, 30, 30, 255), width=14)
im.save(out / "badge.png")

# 4. photo-like gradient blob (hard case)
yy, xx = np.mgrid[0:600, 0:600]
r = np.hypot(xx - 300, yy - 300)
img = np.zeros((600, 600, 3), np.uint8)
img[..., 0] = np.clip(255 - r * 0.8, 0, 255)
img[..., 1] = np.clip(120 + 100 * np.sin(xx / 60.0), 0, 255)
img[..., 2] = np.clip(r * 0.6, 0, 255)
Image.fromarray(img).filter(ImageFilter.GaussianBlur(2)).save(out / "photo.webp")
print("samples in", out)

# 5. busy clip art: gradient sky, confetti, hairlines, many colours (should score Complex)
rng = np.random.default_rng(3)
yy, xx = np.mgrid[0:800, 0:800]
sky = np.zeros((800, 800, 3), np.uint8)
sky[..., 0] = (40 + yy * 0.15).astype(np.uint8)
sky[..., 1] = (90 + yy * 0.12).astype(np.uint8)
sky[..., 2] = (200 - yy * 0.1).astype(np.uint8)
im = Image.fromarray(sky)
d = ImageDraw.Draw(im)
for _ in range(260):
    x, y, r = rng.integers(0, 800), rng.integers(0, 800), rng.integers(2, 9)
    d.ellipse((x - r, y - r, x + r, y + r), fill=tuple(int(v) for v in rng.integers(0, 255, 3)))
for _ in range(40):
    d.line(tuple(int(v) for v in rng.integers(0, 800, 4)), fill=(250, 250, 250), width=2)
d.ellipse((250, 250, 550, 550), fill=(240, 180, 40))
d.text((400, 400), "Busy!", fill=(120, 30, 30), font=font(70), anchor="mm")
im.save(out / "busy.jpg", quality=80)
print("busy sample written")
