"""Appliqué cut files: the fabric pieces as SVG (Cricut Design Space, CanvasWorkspace) or DXF
(ScanNCut, Silhouette, most cutting software), at real size in millimetres.

Each fabric colour is its own group/layer, so a Cricut puts each fabric on its own mat. The pieces are
cut to the placement line; the satin edge covers the cut edge by about 1.5 mm each side.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from .digitize import Design

MARGIN = 5.0  # mm around the pieces


def pieces(design: Design) -> list[tuple[tuple[int, int, int], str, list[np.ndarray]]]:
    """(colour, name, outlines in mm) for every appliqué colour, in sewing order."""
    out = []
    for b in design.blocks:
        if b.fabric and b.visible:
            rings = [np.asarray(r, float) for r in b.fabric if len(r) >= 3]
            if rings:
                out.append((tuple(b.rgb), b.thread or "#%02x%02x%02x" % tuple(b.rgb), rings))
    return out


def _frame(parts) -> tuple[np.ndarray, float, float]:
    pts = np.vstack([r for _, _, rings in parts for r in rings])
    mn = pts.min(0) - MARGIN
    w, h = pts.max(0) + MARGIN - mn
    return mn, float(w), float(h)


def write_svg(design: Design, path: str | Path) -> Path:
    parts = pieces(design)
    if not parts:
        raise ValueError("This design has no appliqué pieces.")
    mn, w, h = _frame(parts)
    out = [f'<?xml version="1.0" encoding="UTF-8"?>',
           f'<svg xmlns="http://www.w3.org/2000/svg" width="{w:.2f}mm" height="{h:.2f}mm" '
           f'viewBox="0 0 {w:.3f} {h:.3f}">',
           "<!-- StitchPilot appliqué pieces, real size. One group per fabric. -->"]
    for i, (rgb, name, rings) in enumerate(parts, 1):
        colour = "#%02x%02x%02x" % rgb
        label = _xml(f"Fabric {i} - {name}")
        out.append(f'<g id="fabric{i}" fill="{colour}" stroke="none"><title>{label}</title>')
        for r in rings:
            q = r - mn
            d = "M" + " L".join(f"{x:.3f},{y:.3f}" for x, y in q) + " Z"
            out.append(f'<path d="{d}"/>')
        out.append("</g>")
    out.append("</svg>")
    path = Path(path)
    path.write_text("\n".join(out) + "\n", encoding="utf-8")
    return path


def write_dxf(design: Design, path: str | Path) -> Path:
    """Plain R12 DXF (closed POLYLINEs, one layer per fabric), which every cutter program reads.
    DXF y points up, so the design is flipped to keep it the right way round."""
    parts = pieces(design)
    if not parts:
        raise ValueError("This design has no appliqué pieces.")
    mn, w, h = _frame(parts)
    L = ["0", "SECTION", "2", "HEADER", "9", "$ACADVER", "1", "AC1009", "9", "$INSUNITS", "70", "4",
         "9", "$EXTMIN", "10", "0.0", "20", "0.0", "9", "$EXTMAX", "10", f"{w:.3f}", "20", f"{h:.3f}",
         "0", "ENDSEC",
         "0", "SECTION", "2", "TABLES", "0", "TABLE", "2", "LAYER", "70", str(len(parts))]
    names = [f"FABRIC{i}" for i in range(1, len(parts) + 1)]
    for name, (rgb, _, _) in zip(names, parts):
        L += ["0", "LAYER", "2", name, "70", "0", "62", str(_aci(rgb)), "6", "CONTINUOUS"]
    L += ["0", "ENDTAB", "0", "ENDSEC", "0", "SECTION", "2", "ENTITIES"]
    for name, (_, _, rings) in zip(names, parts):
        for r in rings:
            q = r - mn
            L += ["0", "POLYLINE", "8", name, "66", "1", "70", "1", "10", "0.0", "20", "0.0", "30", "0.0"]
            for x, y in q:
                L += ["0", "VERTEX", "8", name, "10", f"{x:.3f}", "20", f"{h - y:.3f}", "30", "0.0"]
            L += ["0", "SEQEND", "8", name]
    L += ["0", "ENDSEC", "0", "EOF"]
    path = Path(path)
    path.write_text("\n".join(L) + "\n", encoding="ascii")
    return path


def write(design: Design, path: str | Path) -> Path:
    return write_dxf(design, path) if Path(path).suffix.lower() == ".dxf" else write_svg(design, path)


def _xml(s: str) -> str:
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


# a few AutoCAD colour indexes, so layers show roughly in their fabric colour
_ACI = {1: (255, 0, 0), 2: (255, 255, 0), 3: (0, 255, 0), 4: (0, 255, 255), 5: (0, 0, 255), 6: (255, 0, 255),
        7: (0, 0, 0), 8: (128, 128, 128), 9: (192, 192, 192), 30: (255, 127, 0)}


def _aci(rgb) -> int:
    return min(_ACI, key=lambda k: sum((a - b) ** 2 for a, b in zip(_ACI[k], rgb)))
