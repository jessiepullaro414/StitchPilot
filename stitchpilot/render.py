"""Realistic-ish stitch preview renderer (numpy/OpenCV, no GUI needed)."""
from __future__ import annotations

import cv2
import numpy as np

from . import stitches as st
from .digitize import Design

THREAD_MM = 0.38  # visual thread width
FABRIC = (245, 243, 238)


def layout(design: Design, px_per_mm: float, margin_mm: float = 3.0,
           hoop_mm: tuple[float, float] | None = None) -> tuple[int, int, tuple[float, float]]:
    """Canvas (width px, height px, top-left in design mm) covering the design and optional hoop."""
    x0, y0, x1, y1 = design.bounds()
    if hoop_mm:
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        x0, x1 = min(x0, cx - hoop_mm[0] / 2), max(x1, cx + hoop_mm[0] / 2)
        y0, y1 = min(y0, cy - hoop_mm[1] / 2), max(y1, cy + hoop_mm[1] / 2)
    ox, oy = x0 - margin_mm, y0 - margin_mm
    return (int((x1 - x0 + 2 * margin_mm) * px_per_mm) + 1, int((y1 - y0 + 2 * margin_mm) * px_per_mm) + 1,
            (ox, oy))


def render(design: Design, px_per_mm: float = 8.0, background=FABRIC, show_jumps: bool = False,
           upto: int | None = None, margin_mm: float = 3.0,
           hoop_mm: tuple[float, float] | None = None) -> np.ndarray:
    """Return an RGB image of the sewn-out design. `upto` limits the number of stitches drawn."""
    W, H, (ox, oy) = layout(design, px_per_mm, margin_mm, hoop_mm)
    img = np.empty((H, W, 3), np.uint8)
    img[:] = background
    sh = 4  # sub-pixel bits for cv2 drawing
    scale = px_per_mm * (1 << sh)
    if hoop_mm:
        x0, y0, x1, y1 = design.bounds()
        cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
        lum = sum(background) / 3
        col = (90, 90, 90) if lum > 128 else (200, 200, 200)
        for inset in (0.0, 2.0):
            a = (int((cx - hoop_mm[0] / 2 + inset - ox) * px_per_mm), int((cy - hoop_mm[1] / 2 + inset - oy) * px_per_mm))
            b = (int((cx + hoop_mm[0] / 2 - inset - ox) * px_per_mm), int((cy + hoop_mm[1] / 2 - inset - oy) * px_per_mm))
            cv2.rectangle(img, a, b, col, 1, cv2.LINE_AA)
        cv2.drawMarker(img, (int((cx - ox) * px_per_mm), int((cy - oy) * px_per_mm)), col,
                       cv2.MARKER_CROSS, int(6 * px_per_mm), 1, cv2.LINE_AA)
    t_out = max(1, int(round(THREAD_MM * px_per_mm)))
    t_in = max(1, int(round(THREAD_MM * px_per_mm * 0.45)))
    remaining = upto if upto is not None else 1 << 62
    for b in design.active:
        if remaining <= 0:
            break
        rgb = np.array(b.rgb, float)
        dark = tuple(int(v) for v in rgb * 0.62)
        base = tuple(int(v) for v in rgb)
        light = tuple(int(v) for v in np.minimum(255, rgb * 1.08 + 28))
        if b.fabric:  # appliqué: show the fabric piece under its cover stitch
            rings = [np.round((r - (ox, oy)) * scale).astype(np.int32).reshape(-1, 1, 2) for r in b.fabric]
            tint = tuple(int(v) for v in rgb * 0.9 + 12)
            cv2.fillPoly(img, rings, tint, cv2.LINE_AA, sh)
        c = b.cmds
        p = np.round((c[:, :2] - (ox, oy)) * scale).astype(np.int64)
        segs = []
        for i in range(1, len(c)):
            if c[i, 2] == st.STITCH and c[i - 1, 2] in (st.STITCH, st.JUMP):
                segs.append((i - 1, i))
                remaining -= 1
                if remaining <= 0:
                    break
            elif c[i, 2] == st.JUMP and show_jumps:
                cv2.line(img, tuple(p[i - 1]), tuple(p[i]), (200, 60, 60), 1, cv2.LINE_AA, sh)
        # stitches that end up underneath (fill underlay, hidden travel) are drawn first, so the top
        # layer covers them as it does on fabric
        under = b.under if b.under is not None and len(b.under) == len(c) else None
        layers = [segs] if under is None else [[s for s in segs if under[s[1]]], [s for s in segs if not under[s[1]]]]
        # three passes give each stitch a rounded, slightly shiny look
        for layer in layers:
            for col, t in ((dark, t_out), (base, max(1, t_out - 1)), (light, t_in)):
                for a, bb in layer:
                    cv2.line(img, tuple(p[a]), tuple(p[bb]), col, t, cv2.LINE_AA, sh)
    return img
