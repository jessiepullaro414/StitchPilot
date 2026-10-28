"""Real-size placement template: print the design at 1:1 with centre crosshairs, hoop outline and a
calibration ruler, for positioning it on a garment. Designs bigger than the paper are tiled across
pages that overlap, with alignment crosses to tape them together."""
from __future__ import annotations

import math

from PySide6.QtCore import QLocale, QMarginsF, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QFont, QPageLayout, QPageSize, QPainter, QPdfWriter, QPen
from PySide6.QtWidgets import QApplication

from .render import layout, render

DPI = 300
MARGIN = 12.0  # mm, safe for any printer
HEADER = 24.0  # mm band at the top of each page for title, page number and ruler
OVERLAP = 12.0  # mm repeated on neighbouring pages when tiling
ARM = 12.0  # mm the crosshair reaches beyond the design / hoop


def default_paper() -> str:
    return "Letter" if QLocale().measurementSystem() == QLocale.MeasurementSystem.ImperialUSSystem else "A4"


def _plan(content_w: float, content_h: float, paper: str):
    """Pick orientation and tile grid that need the fewest pages."""
    size = QPageSize(QPageSize.PageSizeId.Letter if paper == "Letter" else QPageSize.PageSizeId.A4)
    pw0, ph0 = size.size(QPageSize.Unit.Millimeter).width(), size.size(QPageSize.Unit.Millimeter).height()
    best = None
    for orient, (pw, ph) in ((QPageLayout.Orientation.Portrait, (pw0, ph0)),
                             (QPageLayout.Orientation.Landscape, (ph0, pw0))):
        aw, ah = pw - 2 * MARGIN, ph - 2 * MARGIN - HEADER  # area for the drawing
        cols = 1 if content_w <= aw else math.ceil((content_w - OVERLAP) / (aw - OVERLAP))
        rows = 1 if content_h <= ah else math.ceil((content_h - OVERLAP) / (ah - OVERLAP))
        if best is None or cols * rows < best[0]:
            best = (cols * rows, orient, pw, ph, aw, ah, cols, rows)
    return size, best


def write_template(path: str, design, hoop_mm: tuple[float, float] | None, title: str,
                   paper: str | None = None) -> int:
    """Write the PDF; returns the number of pages."""
    paper = paper or default_paper()
    x0, y0, x1, y1 = design.bounds()
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
    bx0, by0, bx1, by1 = x0, y0, x1, y1
    if hoop_mm:
        bx0, bx1 = min(bx0, cx - hoop_mm[0] / 2), max(bx1, cx + hoop_mm[0] / 2)
        by0, by1 = min(by0, cy - hoop_mm[1] / 2), max(by1, cy + hoop_mm[1] / 2)
    bx0, by0, bx1, by1 = bx0 - ARM, by0 - ARM, bx1 + ARM, by1 + ARM
    cw, ch = bx1 - bx0, by1 - by0
    size, (pages, orient, pw, ph, aw, ah, cols, rows) = _plan(cw, ch, paper)
    # centre the content when it fits on one page in a direction
    if cols == 1:
        bx0 -= (aw - cw) / 2
    if rows == 1:
        by0 -= (ah - ch) / 2

    ppm = 10.0
    img_layout = layout(design, ppm, 0.0)
    image = render(design, ppm, (255, 255, 255), margin_mm=0.0)
    from .gui import to_qimage  # local import: gui imports this module too
    qimg = to_qimage(image)
    ix, iy = img_layout[2]

    pdf = QPdfWriter(path)
    pdf.setResolution(DPI)
    pdf.setPageLayout(QPageLayout(size, orient, QMarginsF(0, 0, 0, 0), QPageLayout.Unit.Millimeter))
    pdf.setTitle(f"{title} – placement template")
    d = DPI / 25.4  # device px per mm
    family = QApplication.font().family() if QApplication.instance() else "Arial"

    def font(pt, bold=False):
        f = QFont(family)
        f.setPointSizeF(pt)
        f.setBold(bold)
        return f

    p = QPainter(pdf)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    n = 0
    for r in range(rows):
        for c in range(cols):
            if n:
                pdf.newPage()
            n += 1
            tx0 = bx0 + c * (aw - OVERLAP)
            ty0 = by0 + r * (ah - OVERLAP)
            ox, oy = MARGIN, MARGIN + HEADER  # page mm where (tx0, ty0) lands

            def P(x, y):  # design mm -> device px on this page
                return QPointF((ox + x - tx0) * d, (oy + y - ty0) * d)

            # ---- header
            p.setPen(QColor("#4b3550"))
            p.setFont(font(13, True))
            p.drawText(QRectF(MARGIN * d, MARGIN * d, (pw - 2 * MARGIN) * d, 8 * d),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, f"{title} – placement template")
            p.setFont(font(8.5))
            p.setPen(QColor("#6d6a80"))
            info = (f"Design {x1 - x0:.0f} × {y1 - y0:.0f} mm ({(x1 - x0) / 25.4:.2f} × {(y1 - y0) / 25.4:.2f} in)"
                    + (f" · hoop {hoop_mm[0]:.0f} × {hoop_mm[1]:.0f} mm" if hoop_mm else "")
                    + (f" · page {r + 1}-{c + 1} ({n} of {pages}): line up the + marks and tape" if pages > 1 else ""))
            p.drawText(QRectF(MARGIN * d, (MARGIN + 8) * d, (pw - 2 * MARGIN - 70) * d, 6 * d),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, info)
            p.drawText(QRectF(MARGIN * d, (MARGIN + 13) * d, (pw - 2 * MARGIN - 70) * d, 6 * d),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter,
                       "Print at 100% / Actual size. Check the ruler: if it isn't exactly 50 mm (2 in), "
                       "turn off 'fit to page'.")
            # calibration ruler: 50 mm and 2 inches
            rx, ry = (pw - MARGIN - 52) * d, (MARGIN + 3) * d
            p.setPen(QPen(QColor("#2b2840"), 0.35 * d))
            for length, label, yoff in ((50.0, "50 mm", 0.0), (50.8, "2 in", 9.0)):
                y = ry + yoff * d
                p.drawLine(QPointF(rx, y), QPointF(rx + length * d, y))
                ticks = 10.0 if label == "50 mm" else 25.4 / 4
                k = 0
                while k * ticks <= length + 1e-6:
                    t = 2.2 if (label == "50 mm" and k % 5 == 0) or (label == "2 in" and k % 4 == 0) else 1.2
                    p.drawLine(QPointF(rx + k * ticks * d, y), QPointF(rx + k * ticks * d, y + t * d))
                    k += 1
                p.setFont(font(7))
                p.drawText(QRectF(rx, y + 2.5 * d, length * d, 4 * d), Qt.AlignmentFlag.AlignCenter, label)

            # ---- drawing area (clipped to this page's tile)
            p.save()
            p.setClipRect(QRectF(MARGIN * d, (MARGIN + HEADER) * d, aw * d, ah * d))
            p.setOpacity(0.55)  # light enough to see the garment through, and to save ink
            tl, br = P(ix, iy), P(ix + qimg.width() / ppm, iy + qimg.height() / ppm)
            p.drawImage(QRectF(tl, br), qimg)
            p.setOpacity(1.0)
            accent = QColor("#ff4f9b")
            if hoop_mm:
                p.setPen(QPen(QColor("#8a7a96"), 0.35 * d, Qt.PenStyle.DashLine))
                p.setBrush(Qt.BrushStyle.NoBrush)
                p.drawRoundedRect(QRectF(P(cx - hoop_mm[0] / 2, cy - hoop_mm[1] / 2),
                                         P(cx + hoop_mm[0] / 2, cy + hoop_mm[1] / 2)), 6 * d, 6 * d)
            # centre crosshair through design and hoop
            p.setPen(QPen(accent, 0.4 * d))
            p.drawLine(P(cx, by0 + (0 if rows > 1 else (ah - ch) / 2)), P(cx, by1 - ARM / 2))
            p.drawLine(P(bx0 + (0 if cols > 1 else (aw - cw) / 2), cy), P(bx1 - ARM / 2, cy))
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.drawEllipse(P(cx, cy), 2.5 * d, 2.5 * d)
            p.setFont(font(8, True))
            p.setPen(accent)
            top = (y0 if not hoop_mm else min(y0, cy - hoop_mm[1] / 2)) - 4
            p.drawText(QRectF(P(cx + 2, top - 5), P(cx + 30, top + 1)),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "▲ TOP")
            p.drawText(QRectF(P(cx + 3.5, cy + 1), P(cx + 30, cy + 7)),
                       Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, "centre")
            # design outline box (where stitches land)
            p.setPen(QPen(QColor("#4b3550"), 0.25 * d, Qt.PenStyle.DotLine))
            p.drawRect(QRectF(P(x0, y0), P(x1, y1)))
            # alignment crosses in the overlaps between pages
            p.setPen(QPen(QColor("#2b2840"), 0.3 * d))
            # page k starts at bx0 + k*(aw - OVERLAP); the previous page ends OVERLAP mm later
            xs = [bx0 + k * (aw - OVERLAP) + OVERLAP / 2 for k in range(1, cols)]
            ys = [by0 + k * (ah - OVERLAP) + OVERLAP / 2 for k in range(1, rows)]
            for gx in xs or [None]:
                for gy in ys or [None]:
                    pts = []
                    if gx is not None:
                        pts += [(gx, by0 + ch * f) for f in (0.15, 0.5, 0.85)]
                    if gy is not None:
                        pts += [(bx0 + cw * f, gy) for f in (0.15, 0.5, 0.85)]
                    for (mx, my) in pts:
                        q = P(mx, my)
                        p.drawLine(QPointF(q.x() - 4 * d, q.y()), QPointF(q.x() + 4 * d, q.y()))
                        p.drawLine(QPointF(q.x(), q.y() - 4 * d), QPointF(q.x(), q.y() + 4 * d))
            p.restore()
    p.end()
    return pages
