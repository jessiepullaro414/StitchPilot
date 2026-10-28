"""Load almost any image format into an RGBA numpy array."""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PIL import Image, ImageOps

try:  # HEIC/HEIF/AVIF from phones
    import pillow_heif

    pillow_heif.register_heif_opener()
except ImportError:  # pragma: no cover - optional
    pass

VECTOR_EXTS = {".svg", ".svgz"}
RASTER_EXTS = {
    ".png", ".jpg", ".jpeg", ".jpe", ".jfif", ".bmp", ".dib", ".gif", ".tif", ".tiff",
    ".webp", ".ico", ".tga", ".pcx", ".ppm", ".pgm", ".pbm", ".pnm", ".psd", ".heic",
    ".heif", ".avif", ".jp2", ".dds", ".icns", ".sgi", ".xbm",
}
SUPPORTED_EXTS = sorted(RASTER_EXTS | VECTOR_EXTS)


def file_filter() -> str:
    pats = " ".join(f"*{e}" for e in SUPPORTED_EXTS)
    return f"Images ({pats});;All files (*)"


def load_image(path: str | Path, svg_size: int = 2048) -> np.ndarray:
    """Return an HxWx4 uint8 RGBA array."""
    path = Path(path)
    if path.suffix.lower() in VECTOR_EXTS:
        return _load_svg(path, svg_size)
    with Image.open(path) as im:
        if getattr(im, "is_animated", False):
            im.seek(0)
        im = ImageOps.exif_transpose(im)
        if im.mode in ("I;16", "I", "F"):
            arr = np.asarray(im, dtype=np.float32)
            arr = (255 * (arr - arr.min()) / max(1e-6, float(arr.max() - arr.min()))).astype(np.uint8)
            im = Image.fromarray(arr)
        return np.array(im.convert("RGBA"))


def _load_svg(path: Path, target: int) -> np.ndarray:
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QGuiApplication, QImage, QPainter
    from PySide6.QtSvg import QSvgRenderer

    if QGuiApplication.instance() is None:  # allow headless/CLI use
        _load_svg._app = QGuiApplication([])  # type: ignore[attr-defined]
    renderer = QSvgRenderer(str(path))
    if not renderer.isValid():
        raise ValueError(f"Could not read SVG: {path.name}")
    vb = renderer.viewBoxF()
    w0, h0 = (vb.width(), vb.height()) if vb.width() > 0 else (512.0, 512.0)
    scale = target / max(w0, h0)
    w, h = max(1, round(w0 * scale)), max(1, round(h0 * scale))
    img = QImage(w, h, QImage.Format.Format_RGBA8888)
    img.fill(0)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    renderer.render(p, QRectF(0, 0, w, h))
    p.end()
    buf = np.frombuffer(img.constBits(), np.uint8).reshape(h, img.bytesPerLine() // 4, 4)
    return buf[:, :w].copy()
