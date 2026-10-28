"""Text clean-up: replace blurry or pixelated text in a picture with crisp lettering.

Claude says what the words are and roughly where (ai.find_text). Everything else happens here:
the box is tightened to the actual letters, the text and background colours are measured, the
installed font that best matches the letter shapes is picked, and finally the old letters are painted
out and redrawn cleanly before digitizing.
"""
from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

from .lettering import ensure_qt
from .settings import TextFix

# candidate fonts per style, most common first (only installed ones are tried)
STYLE_FONTS = {
    "sans": ["Arial", "Helvetica", "Segoe UI", "Verdana", "Tahoma", "Calibri", "Trebuchet MS", "Futura", "Gill Sans",
             "Century Gothic", "Franklin Gothic Medium", "DejaVu Sans", "Liberation Sans", "Noto Sans"],
    "serif": ["Times New Roman", "Georgia", "Garamond", "Cambria", "Palatino Linotype", "Book Antiqua",
              "Baskerville", "DejaVu Serif", "Liberation Serif", "Noto Serif"],
    "slab": ["Rockwell", "Courier New", "Roboto Slab", "Georgia"],
    "rounded": ["Arial Rounded MT Bold", "Comic Sans MS", "Verdana", "Segoe UI", "Nunito"],
    "display": ["Impact", "Arial Black", "Bahnschrift", "Franklin Gothic Medium", "Haettenschweiler", "Segoe UI Black"],
    "script": ["Segoe Script", "Brush Script MT", "Lucida Handwriting", "Snell Roundhand", "Apple Chancery",
               "Monotype Corsiva", "Freestyle Script", "Kunstler Script", "Vladimir Script", "Pacifico"],
    "handwritten": ["Segoe Print", "Comic Sans MS", "Ink Free", "Bradley Hand", "Marker Felt", "Chalkboard"],
}
K = 4  # colours told apart in a text box: background, letters, and room for edges and other artwork
UPSCALE_TO = 1600  # small pictures are enlarged first, so the new letters are drawn crisply


@dataclass
class Measured:
    """What the picture shows inside one text box (pixels of the picture it was measured on)."""
    box: tuple[int, int, int, int]  # tight box round the letters: x0, y0, x1, y1
    mask: np.ndarray  # letter pixels inside the box
    text_rgb: tuple[int, int, int]
    bg_rgb: tuple[int, int, int]


def flat_rgb(rgba: np.ndarray) -> np.ndarray:
    """The picture on white (transparent parts have no meaningful colour)."""
    a = rgba[..., 3:4].astype(np.float32) / 255
    return np.ascontiguousarray((rgba[..., :3] * a + 255 * (1 - a)).astype(np.uint8))


def _lab(rgb: np.ndarray) -> np.ndarray:
    return cv2.cvtColor(rgb.reshape(-1, 1, 3).astype(np.uint8), cv2.COLOR_RGB2LAB).reshape(-1, 3).astype(np.float32)


def measure(rgb: np.ndarray, box: tuple[float, float, float, float]) -> Measured | None:
    """Find the letters inside a rough box (fractions of the picture)."""
    H, W = rgb.shape[:2]
    x0, y0, x1, y1 = box
    bw, bh = (x1 - x0) * W, (y1 - y0) * H
    if bw < 4 or bh < 3:
        return None
    pad = 0.15 * bh + 3
    X0, Y0 = int(max(0, x0 * W - pad)), int(max(0, y0 * H - pad))
    X1, Y1 = int(min(W, x1 * W + pad)), int(min(H, y1 * H + pad))
    crop = rgb[Y0:Y1, X0:X1]
    h, w = crop.shape[:2]
    lab = _lab(crop)
    _, lbl, centres = cv2.kmeans(lab, K, None, (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 20, 0.5), 3,
                                 cv2.KMEANS_PP_CENTERS)
    lbl = lbl.reshape(h, w)
    border = np.concatenate([lbl[0], lbl[-1], lbl[:, 0], lbl[:, -1]])
    bg = int(np.bincount(border, minlength=K).argmax())

    def inside(m: np.ndarray) -> np.ndarray:
        """Parts of m not touching the padded edge: letters are inside the box, other artwork isn't."""
        n, cc, stats, _ = cv2.connectedComponentsWithStats(m.astype(np.uint8), connectivity=8)
        keep = np.zeros(n, bool)
        for i in range(1, n):
            x, y, ww, hh, a = stats[i]
            keep[i] = x > 0 and y > 0 and x + ww < w and y + hh < h
        if keep.any():  # and no specks (JPEG noise): a dot on an i is still a few % of a letter
            keep &= stats[:, cv2.CC_STAT_AREA] >= max(4, 0.015 * stats[keep, cv2.CC_STAT_AREA].max())
        return keep[cc]

    # the letters: the colour (other than the background) with the most pixels in shapes inside the box
    found = {c: inside(lbl == c) for c in range(K) if c != bg}
    fg = max(found, key=lambda c: found[c].sum())
    if found[fg].sum() < 10:
        return None
    # add their soft edges: next to a letter and nearer the letter colour than the background
    d_fg = np.linalg.norm(lab - centres[fg], axis=1).reshape(h, w)
    d_bg = np.linalg.norm(lab - centres[bg], axis=1).reshape(h, w)
    near = cv2.dilate(found[fg].astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
    mask = found[fg] | (near & (d_fg < d_bg))
    ys, xs = np.nonzero(mask)
    tight = (X0 + int(xs.min()), Y0 + int(ys.min()), X0 + int(xs.max()) + 1, Y0 + int(ys.max()) + 1)
    inner = mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    sel = crop[mask & (d_fg < np.percentile(d_fg[mask], 50))]
    text_rgb = tuple(int(v) for v in np.median(sel if len(sel) else crop[mask], axis=0))
    bg_rgb = tuple(int(v) for v in np.median(crop[lbl == bg], axis=0))
    return Measured(tight, inner, text_rgb, bg_rgb)


def _text_path(text: str, font_name: str, bold: bool, italic: bool):
    from PySide6.QtGui import QFont, QPainterPath

    f = QFont(font_name)
    f.setBold(bold)
    f.setItalic(italic)
    f.setPixelSize(200)
    p = QPainterPath()
    p.addText(0, 0, f, text)
    return p


def _draw(path, size: tuple[int, int], rgb=(0, 0, 0)) -> np.ndarray:
    """The path stretched to fill w x h exactly (that is how it is fitted to the letters' box), as RGBA."""
    from PySide6.QtGui import QBrush, QColor, QImage, QPainter

    w, h = size
    r = path.boundingRect()
    img = QImage(w, h, QImage.Format.Format_RGBA8888)
    img.fill(0)
    if r.width() > 0 and r.height() > 0:
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.scale(w / r.width(), h / r.height())
        p.translate(-r.left(), -r.top())
        p.fillPath(path, QBrush(QColor(*rgb)))
        p.end()
    return np.frombuffer(img.constBits(), np.uint8).reshape(h, img.bytesPerLine() // 4, 4)[:, :w].copy()


def installed(names: list[str]) -> list[str]:
    from PySide6.QtGui import QFontDatabase

    ensure_qt()
    have = {f.lower(): f for f in QFontDatabase.families()}
    return [have[n.lower()] for n in names if n.lower() in have]


def match_font(text: str, m: Measured, style: str = "sans", guess: str = "", italic: bool = False
               ) -> tuple[str, bool, float]:
    """The installed font (and weight) whose letters overlap the picture's letters best:
    (family, bold, overlap 0..1)."""
    ensure_qt()
    names = installed(([guess] if guess else []) + STYLE_FONTS.get(style, []) + STYLE_FONTS["sans"][:3])
    names = list(dict.fromkeys(names)) or ["Arial"]
    h, w = m.mask.shape
    k = min(1.0, 72 / max(h, 1))  # compare at about 72 px tall
    size = (max(8, round(w * k)), max(8, round(h * k)))
    target = cv2.resize(m.mask.astype(np.uint8) * 255, size, interpolation=cv2.INTER_AREA) > 127
    best = (names[0], True, -1.0)
    for name in names:
        for bold in (True, False):
            got = _draw(_text_path(text, name, bold, italic), size)[..., 3] > 127
            union = (got | target).sum()
            iou = float((got & target).sum() / union) if union else 0.0
            if name == guess:
                iou += 0.02  # Claude recognised it: a small nudge
            if iou > best[2]:
                best = (name, bold, iou)
    return best[0], best[1], min(1.0, best[2])


def apply_fixes(rgba: np.ndarray, fixes: list[TextFix]) -> np.ndarray:
    """The picture with each fixed text painted out and redrawn in clean lettering."""
    fixes = [f for f in fixes if f.text.strip()]
    if not fixes:
        return rgba
    ensure_qt()
    h0, w0 = rgba.shape[:2]
    k = UPSCALE_TO / max(h0, w0)
    out = cv2.resize(rgba, (round(w0 * k), round(h0 * k)), interpolation=cv2.INTER_CUBIC) if k > 1 else rgba.copy()
    H, W = out.shape[:2]
    for f in fixes:
        m = measure(flat_rgb(out), f.box)
        if m is None:
            continue
        x0, y0, x1, y1 = m.box
        # paint the old letters out, a little beyond their soft edges
        hole = np.zeros((H, W), np.uint8)
        hole[y0:y1, x0:x1] = m.mask
        grow = max(2, round(0.06 * (y1 - y0)))
        hole = cv2.dilate(hole, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * grow + 1, 2 * grow + 1)))
        rgb = np.ascontiguousarray(out[..., :3])
        out[..., :3] = cv2.inpaint(rgb, hole * 255, 3, cv2.INPAINT_TELEA)
        # and draw the new ones into the same box
        path = _text_path(" ".join(f.text.split()), f.font, f.bold, f.italic)
        r = path.boundingRect()
        bw, bh = x1 - x0, y1 - y0
        sx, sy = bw / max(r.width(), 1e-6), bh / max(r.height(), 1e-6)
        if not 0.8 <= sx / sy <= 1.25:  # very different proportions: keep the font's, centred in the box
            s = min(sx, sy)
            nw, nh = max(2, round(r.width() * s)), max(2, round(r.height() * s))
            x0, y0 = x0 + (bw - nw) // 2, y0 + (bh - nh) // 2
            bw, bh = nw, nh
        x0, y0 = max(0, x0), max(0, y0)
        bw, bh = min(bw, W - x0), min(bh, H - y0)
        if bw < 2 or bh < 2:
            continue
        ink = _draw(path, (bw, bh), m.text_rgb)
        a = ink[..., 3:4].astype(np.float32) / 255
        region = out[y0:y0 + bh, x0:x0 + bw].astype(np.float32)
        region[..., :3] = ink[..., :3] * a + region[..., :3] * (1 - a)
        region[..., 3:] = np.maximum(region[..., 3:], ink[..., 3:])
        out[y0:y0 + bh, x0:x0 + bw] = region.astype(np.uint8)
    return out
