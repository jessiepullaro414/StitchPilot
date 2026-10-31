"""Lettering: render text with any installed font and place it above, below or over the picture."""
from __future__ import annotations

import numpy as np

from .settings import TextItem

GAP_MM = 3.0  # space between picture and text lines
TEXT_GAP = 255  # compose(layer=True): inside a line of text, between the letters


def ensure_qt() -> None:
    from PySide6.QtGui import QGuiApplication

    if QGuiApplication.instance() is None:  # headless / CLI use
        ensure_qt.app = QGuiApplication([])  # type: ignore[attr-defined]


FRAMES = {"none": "No frame", "circle": "Circle", "double": "Double circle", "scallop": "Scalloped circle",
          "diamond": "Diamond", "square": "Rounded square"}
FRAME_MM = 1.6  # frame line width: wide enough to sew as a satin line


def _font(item: TextItem, cap_px: float):
    from PySide6.QtGui import QFont, QFontMetricsF

    font = QFont(item.font)
    font.setBold(item.bold)
    font.setItalic(item.italic)
    font.setPixelSize(100)
    cap = QFontMetricsF(font).capHeight() or 70.0
    font.setPixelSize(max(4, round(100 * cap_px / cap)))
    return font


def _fill_path(path, rgb) -> np.ndarray:
    """RGBA image of a filled QPainterPath, tightly cropped."""
    from PySide6.QtGui import QBrush, QColor, QImage, QPainter

    r = path.boundingRect()
    w, h = int(r.width()) + 4, int(r.height()) + 4
    img = QImage(w, h, QImage.Format.Format_RGBA8888)
    img.fill(0)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.translate(2 - r.left(), 2 - r.top())
    p.fillPath(path, QBrush(QColor(*(int(v) for v in rgb))))
    p.end()
    return _crop(np.frombuffer(img.constBits(), np.uint8).reshape(h, img.bytesPerLine() // 4, 4)[:, :w].copy())


def _crop(arr: np.ndarray) -> np.ndarray:
    ys, xs = np.nonzero(arr[..., 3] > 20)
    if len(xs) == 0:
        return np.zeros((1, 1, 4), np.uint8)
    return arr[ys.min():ys.max() + 1, xs.min():xs.max() + 1]


def arc_path(text: str, font, arc_deg: float):
    """The text as one outline path, its baseline bent round a circle: arc_deg > 0 arches over the
    top of the circle (letters stand on the outside), < 0 curves along the bottom (letters inside)."""
    from PySide6.QtGui import QFontMetricsF, QPainterPath, QTransform

    fm = QFontMetricsF(font)
    total = fm.horizontalAdvance(text)
    radius = total / np.radians(abs(arc_deg))
    up = arc_deg > 0
    out = QPainterPath()
    for i, ch in enumerate(text):
        if not ch.strip():
            continue
        adv = fm.horizontalAdvance(ch)
        s = fm.horizontalAdvance(text[:i]) + adv / 2 - total / 2  # glyph centre along the baseline
        a = s / radius
        g = QPainterPath()
        g.addText(-adv / 2, 0, font, ch)
        if up:  # centre of the circle below the text
            x, y, rot = radius * np.sin(a), -radius * np.cos(a), np.degrees(a)
        else:  # centre above: the ends of the line curl up
            x, y, rot = radius * np.sin(a), radius * np.cos(a), -np.degrees(a)
        t = QTransform()
        t.translate(x, y)
        t.rotate(rot)
        out.addPath(t.map(g))
    return out


def monogram_path(item: TextItem, cap_px: float):
    """Initials (the middle one of three bigger, as in classic monograms) in an optional frame."""
    from PySide6.QtCore import QPointF, Qt
    from PySide6.QtGui import QPainterPath, QPainterPathStroker

    letters = "".join(item.text.split())[:3] or "A"
    sizes = [1.0] if len(letters) == 1 else [1.0, 1.0] if len(letters) == 2 else [0.7, 1.0, 0.7]
    path = QPainterPath()
    x = 0.0
    for ch, k in zip(letters, sizes):
        font = _font(item, cap_px * k)
        g = QPainterPath()
        g.addText(0, 0, font, ch)
        r = g.boundingRect()
        gap = 0.12 * cap_px * k if x else 0.0
        # letters sit side by side on their ink, vertically centred on the big one
        g.translate(x + gap - r.left(), -(r.top() + r.height() / 2))
        path.addPath(g)
        x = g.boundingRect().right()
    path.translate(-x / 2, 0)
    path.setFillRule(Qt.FillRule.WindingFill)
    if item.frame == "none" or item.frame not in FRAMES:
        return path
    r = path.boundingRect()
    w, h = r.width() / 2, r.height() / 2
    gap = 0.22 * cap_px
    line = max(1.0, FRAME_MM * cap_px / max(item.height_mm, 1e-6))
    shape = QPainterPath()
    if item.frame in ("circle", "double", "scallop"):
        rad = max(np.hypot(w, h * 0.92), h) + gap + line / 2
        if item.frame == "scallop":
            n = max(10, int(round(2 * np.pi * rad / (2.4 * line + 0.25 * rad)))) // 2 * 2
            th = np.linspace(0, 2 * np.pi, n * 24, endpoint=False)
            rr = rad + 0.09 * rad * np.abs(np.sin(n * th / 2))
            pts = [QPointF(float(a), float(b)) for a, b in zip(rr * np.sin(th), -rr * np.cos(th))]
            shape.moveTo(pts[0])
            for q in pts[1:]:
                shape.lineTo(q)
            shape.closeSubpath()
        else:
            shape.addEllipse(QPointF(0, 0), rad, rad)
            if item.frame == "double":
                outer = rad + 2.2 * line
                shape.addEllipse(QPointF(0, 0), outer, outer)
    elif item.frame == "diamond":
        a = w + h + gap * 1.5 + line
        shape.moveTo(0, -a)
        shape.lineTo(a, 0)
        shape.lineTo(0, a)
        shape.lineTo(-a, 0)
        shape.closeSubpath()
    else:  # rounded square
        a = max(w, h) + gap + line / 2
        shape.addRoundedRect(-a, -a, 2 * a, 2 * a, 0.25 * a, 0.25 * a)
    stroker = QPainterPathStroker()
    stroker.setWidth(line)
    stroker.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
    ring = stroker.createStroke(shape)
    ring.setFillRule(Qt.FillRule.WindingFill)
    path.addPath(ring)
    return path


def render_text(item: TextItem, cap_px: float) -> np.ndarray:
    """RGBA image of the text, tightly cropped, with capital letters cap_px tall."""
    from PySide6.QtCore import QRectF, Qt
    from PySide6.QtGui import QColor, QFontMetricsF, QImage, QPainter

    ensure_qt()
    if item.kind == "monogram":
        return _fill_path(monogram_path(item, cap_px), item.rgb)
    font = _font(item, cap_px)
    if abs(item.arc) >= 5:
        line = " ".join(item.text.split())  # a curved line is one line
        if line:
            return _fill_path(arc_path(line, font, max(-300.0, min(300.0, item.arc))), item.rgb)
    fm = QFontMetricsF(font)
    lines = item.text.split("\n")
    w = int(max(fm.horizontalAdvance(t) for t in lines) + fm.height()) + 4
    h = int(fm.lineSpacing() * len(lines) + fm.height()) + 4
    img = QImage(w, h, QImage.Format.Format_RGBA8888)
    img.fill(0)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.TextAntialiasing)
    p.setFont(font)
    p.setPen(QColor(*item.rgb))
    p.drawText(QRectF(0, 0, w, h), Qt.AlignmentFlag.AlignCenter, item.text)
    p.end()
    return _crop(np.frombuffer(img.constBits(), np.uint8).reshape(h, img.bytesPerLine() // 4, 4)[:, :w].copy())


def _paste(dst: np.ndarray, src: np.ndarray, x: int, y: int) -> None:
    """Alpha-composite src onto dst at (x, y)."""
    h, w = src.shape[:2]
    a = src[..., 3:4].astype(np.float32) / 255
    region = dst[y:y + h, x:x + w].astype(np.float32)
    out = region.copy()
    out[..., :3] = src[..., :3] * a + region[..., :3] * (1 - a)
    out[..., 3:4] = np.maximum(region[..., 3:4], src[..., 3:4])
    dst[y:y + h, x:x + w] = out.astype(np.uint8)


def compose(img: np.ndarray | None, texts: list[TextItem], mm_per_px: float,
            layer: bool = False) -> np.ndarray | tuple[np.ndarray, np.ndarray]:
    """Stack text above/below the (already cut-out, cropped) picture, or centre it on top.

    With layer=True also returns where the lettering is: 1 + the item's index in texts on its letters,
    TEXT_GAP elsewhere in a line of text set above or below the picture, 0 everywhere else."""
    pieces = {pos: [(i, render_text(t, t.height_mm / mm_per_px)) for i, t in enumerate(texts)
                    if t.position == pos and t.text.strip()]
              for pos in ("above", "below", "over")}
    gap = int(round(GAP_MM / mm_per_px))
    has_img = img is not None and img.size > 4
    if not has_img and pieces["over"]:  # nothing to put "over": stack it instead
        pieces["below"] = pieces["over"] + pieces["below"]
        pieces["over"] = []
    rows = [r for _, r in pieces["above"]] + ([img] if has_img else []) + [r for _, r in pieces["below"]]
    if not rows:
        return (np.zeros((1, 1, 4), np.uint8), np.zeros((1, 1), np.uint8)) if layer else np.zeros((1, 1, 4), np.uint8)
    W = max(r.shape[1] for r in rows)
    H = sum(r.shape[0] for r in rows) + gap * (len(rows) - 1)
    canvas = np.zeros((H, W, 4), np.uint8)
    where = np.zeros((H, W), np.uint8)
    item = {id(r): i for pos in pieces.values() for i, r in pos}
    y = 0
    img_y = 0
    for r in rows:
        x = (W - r.shape[1]) // 2
        if r is img:
            img_y = y
            canvas[y:y + r.shape[0], x:x + r.shape[1]] = r
        else:
            _paste(canvas, r, x, y)
            box = where[y:y + r.shape[0], x:x + r.shape[1]]
            box[:] = TEXT_GAP
            box[r[..., 3] >= 128] = 1 + item[id(r)]
        y += r.shape[0] + gap
    for i, r in pieces["over"]:
        ih, iw = img.shape[:2]
        rh, rw = min(r.shape[0], ih), min(r.shape[1], W)
        x, y = (W - rw) // 2, img_y + (ih - rh) // 2
        _paste(canvas, r[:rh, :rw], x, y)
        where[y:y + rh, x:x + rw][r[:rh, :rw, 3] >= 128] = 1 + i
    return (canvas, where) if layer else canvas
