"""Stitch density analysis: how much thread lands on each square millimetre."""
from __future__ import annotations

import cv2
import numpy as np

from . import stitches as st

CELL_MM = 0.5
# thread length per mm^2: a normal fill is ~2.5, satin ~5, fill + satin on top ~8
HEAVY, DENSE = 6.5, 9.0


def _segments(design) -> np.ndarray:
    segs = []
    for b in design.active:
        c = b.cmds
        if len(c) < 2:
            continue
        ok = (c[1:, 2] == st.STITCH) & np.isin(c[:-1, 2], (st.STITCH, st.JUMP))
        segs.append(np.hstack([c[:-1, :2][ok], c[1:, :2][ok]]))
    return np.vstack(segs) if segs else np.zeros((0, 4))


def density_grid(design, cell: float = CELL_MM) -> tuple[np.ndarray, tuple[float, float]]:
    """Thread length (mm) per mm^2 on a grid; returns (grid, origin_mm)."""
    segs = _segments(design)
    x0, y0, x1, y1 = design.bounds()
    x0, y0 = x0 - 1, y0 - 1
    W, H = int((x1 - x0) / cell) + 3, int((y1 - y0) / cell) + 3
    if not len(segs):
        return np.zeros((H, W), np.float32), (x0, y0)
    a, b = segs[:, :2], segs[:, 2:]
    L = np.hypot(*(b - a).T)
    n = np.maximum(1, np.ceil(L / 0.1)).astype(int)
    idx = np.repeat(np.arange(len(segs)), n)
    t = (np.arange(n.sum()) - np.repeat(np.cumsum(n) - n, n) + 0.5) / np.repeat(n, n)
    pts = a[idx] + (b - a)[idx] * t[:, None]
    w = np.repeat(L / n, n)  # thread length each sample stands for
    gx = ((pts[:, 0] - x0) / cell).astype(int).clip(0, W - 1)
    gy = ((pts[:, 1] - y0) / cell).astype(int).clip(0, H - 1)
    grid = np.zeros((H, W), np.float64)
    np.add.at(grid, (gy, gx), w)
    grid = cv2.GaussianBlur(grid.astype(np.float32), (0, 0), 1.0 / cell * 0.6) / (cell * cell)
    return grid, (x0, y0)


def dense_share(design) -> float:
    """Fraction of the stitched area that is dangerously dense."""
    g, _ = density_grid(design)
    stitched = g > 0.8
    return float((g[stitched] > DENSE).mean()) if stitched.any() else 0.0


def density_image(design, px_per_mm: float, margin_mm: float, canvas: tuple[int, int],
                  origin: tuple[float, float]) -> np.ndarray:
    """RGB heat map aligned with render(): blue = light, green = normal, yellow = heavy, red = too dense."""
    g, (gx0, gy0) = density_grid(design)
    W, H = canvas
    out = np.full((H, W, 3), 250, np.uint8)
    stops = np.array([0.0, 1.5, 4.0, HEAVY, DENSE, 12.0])
    cols = np.array([[250, 250, 250], [170, 205, 245], [120, 200, 120], [245, 215, 70], [235, 80, 60],
                     [150, 20, 30]], np.float32)
    # sample the grid at each output pixel
    ys, xs = np.mgrid[0:H, 0:W]
    mx = xs / px_per_mm + origin[0]
    my = ys / px_per_mm + origin[1]
    gi = ((my - gy0) / CELL_MM).astype(int)
    gj = ((mx - gx0) / CELL_MM).astype(int)
    inside = (gi >= 0) & (gi < g.shape[0]) & (gj >= 0) & (gj < g.shape[1])
    v = np.zeros((H, W), np.float32)
    v[inside] = g[gi[inside], gj[inside]]
    for ch in range(3):
        out[..., ch] = np.interp(v, stops, cols[:, ch]).astype(np.uint8)
    return out
