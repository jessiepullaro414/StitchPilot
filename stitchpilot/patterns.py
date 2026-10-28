"""Decorative fills: motif fills (hearts, stars, flowers), wavy fill and cross-stitch fill.

Motif and cross-stitch fills are open (the fabric shows between the stitches), so nothing can be
hidden under them. The wavy fill covers the shape like an ordinary fill, just with curved rows.
"""
from __future__ import annotations

import math

import numpy as np
import shapely
from shapely.geometry import LineString, Polygon

from . import stitches as st

# name -> label shown in the shape bar
PATTERNS = {"plain": "Plain rows", "waves": "Waves", "hearts": "Hearts", "stars": "Stars",
            "flowers": "Flowers", "cross": "Cross-stitch"}
OPEN = ("hearts", "stars", "flowers", "cross")  # fabric shows through: hides nothing underneath
MOTIF_MM = 5.0  # motif width
CROSS_MM = 2.2  # cross-stitch cell size


def _motif(name: str, n: int = 48) -> np.ndarray:
    """Closed outline of a motif, 1 wide, starting and ending at its anchor (the bottom, at (0, 0)).
    y points down, like everything else."""
    t = np.linspace(0, 2 * np.pi, n + 1)
    if name == "hearts":
        t = t + np.pi  # start at the bottom tip
        x = 16 * np.sin(t) ** 3
        y = -(13 * np.cos(t) - 5 * np.cos(2 * t) - 2 * np.cos(3 * t) - np.cos(4 * t))
    elif name == "stars":
        k = np.arange(11)
        a = np.radians(54 + 36 * k)  # from the tip of one leg, so it stands on the string
        r = np.where(k % 2 == 0, 1.0, 0.42)
        x, y = r * np.cos(a), r * np.sin(a)
    else:  # flowers: five round petals, starting at the bottom of the bottom one
        a = np.radians(90 + 72 * np.arange(5))
        petals = shapely.unary_union([shapely.Point(0.34 * np.cos(q), 0.34 * np.sin(q)).buffer(0.21, 8) for q in a])
        ring = np.asarray(petals.exterior.coords)[:-1]
        i = int(ring[:, 1].argmax())
        ring = st.resample(np.roll(ring, -i, axis=0), 2 * np.pi / n, closed=True)
        x, y = ring[:, 0], ring[:, 1]
    pts = np.column_stack([x, y])
    pts -= pts[0]
    return pts / (np.ptp(pts[:, 0]) or 1.0)


def _chains(pieces: list[np.ndarray], inside, max_gap: float, walk=None) -> list[np.ndarray]:
    """Join consecutive pieces into continuous runs where a short straight line inside the shape
    connects them (that line is part of the look), or `walk(a, b)` finds a path; elsewhere start a
    new run (the thread jumps)."""
    runs: list[list[np.ndarray]] = []
    for p in pieces:
        if runs:
            a, b = runs[-1][-1][-1], p[0]
            d = float(np.hypot(*(b - a)))
            link = None
            if d < 1e-6:
                link = np.empty((0, 2))
            elif d <= max_gap and inside.contains(LineString([a, b])):
                link = st.resample(np.array([a, b]), 2.0)[1:-1]
            elif walk is not None:
                link = walk(a, b)
            if link is not None:
                runs[-1] += [link, p]
                continue
        runs.append([p])
    return [np.vstack(r) for r in runs]


def _edge_walk(geom, max_len: float):
    """walk(a, b): the shorter way round the outline from a to b, both on the same outline ring."""
    rings = [r for g in st._polys(geom) for r in (g.exterior, *g.interiors)]

    def walk(a, b):
        pa, pb = shapely.points(a), shapely.points(b)
        for ring in rings:
            if ring.distance(pa) > 0.05 or ring.distance(pb) > 0.05:
                continue
            L = ring.length
            ta, tb = ring.project(pa), ring.project(pb)
            fwd = (tb - ta) % L
            span = fwd if fwd <= L - fwd else fwd - L
            if abs(span) > max_len:
                return None
            ts = ta + np.linspace(0, span, max(2, int(abs(span) / 1.0) + 2))
            pts = shapely.get_coordinates(shapely.line_interpolate_point(ring, ts % L))
            return pts[1:-1]
        return None

    return walk


def _near_first(runs: list[np.ndarray], start) -> list[np.ndarray]:
    """Sew the runs in the order that starts nearest `start` (forwards or backwards through them)."""
    if not runs or start is None:
        return runs
    if np.hypot(*(runs[-1][-1] - start)) < np.hypot(*(runs[0][0] - start)):
        return [r[::-1] for r in runs[::-1]]
    return runs


def _sweeps(segs: list, pos) -> list[np.ndarray]:
    """Order diagonal lines (key, first cell, last cell, points) for sewing, each there and back.

    Neighbouring lines that overlap form a sweep (like the chunks of an ordinary fill), sewn line after
    line so each starts right next to where the last one ended; the nearest sweep goes next."""
    sweeps, open_ = [], []
    for k, a, b, line in sorted(segs, key=lambda s: (s[0], s[1])):
        hit = next((c for c in open_ if c[-1][0] == k - 1 and a <= c[-1][2] + 1 and b + 1 >= c[-1][1]), None)
        if hit is None:
            hit = []
            sweeps.append(hit)
        else:
            open_.remove(hit)
        hit.append((k, a, b, line))
        open_.append(hit)
    out = []
    p = None if pos is None else np.asarray(pos, float)
    left = list(range(len(sweeps)))
    while left:
        def dist(i, rev):
            line = sweeps[i][-1 if rev else 0][3]
            return 0.0 if p is None else min(np.hypot(*(line[0] - p)), np.hypot(*(line[-1] - p)))

        i, rev = min(((i, rev) for i in left for rev in (False, True)), key=lambda c: dist(*c))
        left.remove(i)
        for _, _, _, line in (sweeps[i][::-1] if rev else sweeps[i]):
            if p is not None and np.hypot(*(line[-1] - p)) < np.hypot(*(line[0] - p)):
                line = line[::-1]  # enter from the nearer end
            out.append(np.vstack([line, line[::-1][1:]]))  # there and back
            p = line[0]
    return out


def motif_runs(poly, name: str, size: float, start) -> list[np.ndarray]:
    """Rows of little motifs in bean stitch, strung like bunting on a line that runs edge to edge.
    Moving to the next row follows the shape's edge, and a bean outline sewn last covers that."""
    unit = _motif(name)
    # stitches up to 1 mm, keeping the corners (petal notches, star points)
    line = shapely.segmentize(LineString(unit * size).simplify(0.06), min(1.0, size / 5))
    loop = shapely.get_coordinates(line)  # starts and ends at the anchor
    bean = np.vstack([loop, loop[::-1][1:], loop[1:]])  # there, back, there again
    top = float(-unit[:, 1].min()) * size  # how far the motif reaches above its anchor
    edge = poly.buffer(-0.5)
    if edge.is_empty:
        return st.outline_run(poly, 2.0, start, holes=True, triple=True)
    inner = edge.buffer(-0.01)
    shapely.prepare(inner)
    x0, y0, x1, y1 = edge.bounds
    dx, dy = size * 1.45, top + size * 0.55
    pieces = []
    for row, y in enumerate(np.arange(y0 + top + 0.6, y1 - 0.3, dy)):
        cut = edge.intersection(LineString([(x0 - 1, y), (x1 + 1, y)]))
        segs = sorted((g for g in getattr(cut, "geoms", [cut]) if g.geom_type == "LineString" and g.length > 0.5),
                      key=lambda g: g.bounds[0])
        xs = np.arange(x0 + (dx / 2 if row % 2 else 0), x1 + dx, dx)
        row_pieces = []
        for g in segs:
            xa, xb = g.bounds[0], g.bounds[2]
            path = [np.array([[xa, y]])]
            for x in xs[(xs > xa) & (xs < xb)]:
                if inner.contains(Polygon(loop + (x, y))):
                    path += [st.resample(np.array([path[-1][-1], (x, y)]), 2.0)[1:], bean[1:] + (x, y)]
            if len(path) == 1:
                continue  # no room for a motif here: no bare string either
            path.append(st.resample(np.array([path[-1][-1], (xb, y)]), 2.0)[1:])
            row_pieces.append(np.vstack(path))
        pieces += row_pieces if row % 2 == 0 else [p[::-1] for p in row_pieces[::-1]]
    runs = _near_first(_chains(pieces, inner, 0.0, _edge_walk(edge, 40.0)), start)
    pos = runs[-1][-1] if runs else start
    return runs + st.outline_run(edge, 2.0, pos, holes=True, triple=True)


def cross_runs(poly, cell: float, start) -> list[np.ndarray]:
    """Cross-stitch fill: an X in every grid cell inside the shape. Each family of diagonals is sewn as
    long straight lines (there and back, for a bolder cross) running corner to corner through the cells."""
    x0, y0, x1, y1 = poly.bounds
    nx, ny = int(math.ceil((x1 - x0) / cell)), int(math.ceil((y1 - y0) / cell))
    if nx < 1 or ny < 1:
        return []
    # snap the grid to multiples of the cell size, so neighbouring shapes line up
    gx, gy = math.floor(x0 / cell) * cell, math.floor(y0 / cell) * cell
    nx, ny = nx + 1, ny + 1
    ii, jj = np.meshgrid(np.arange(nx), np.arange(ny), indexing="ij")
    cx, cy = gx + (ii + 0.5) * cell, gy + (jj + 0.5) * cell
    inner = poly.buffer(-0.15 * cell)
    on = shapely.contains_xy(inner, cx, cy) if not inner.is_empty else np.zeros_like(cx, bool)
    if not on.any():
        return []
    # moving between lines: along the edge of the stitched cells (it reads as a border)
    region = shapely.unary_union(shapely.box(cx[on] - cell / 2, cy[on] - cell / 2, cx[on] + cell / 2,
                                             cy[on] + cell / 2)).simplify(cell / 100)
    walk = _edge_walk(region, 25.0)
    runs: list[np.ndarray] = []
    pos = start
    grown = poly.buffer(0.3)
    shapely.prepare(grown)
    for fam in (1, -1):  # "\" lines (i - j constant), then "/" lines (i + j constant)
        segs = []
        keys = range(-(ny - 1), nx) if fam == 1 else range(0, nx + ny - 1)
        for k in keys:
            cells = [(i, k - i if fam == -1 else i - k) for i in range(nx)]
            cells = [(i, j) for i, j in cells if 0 <= j < ny and on[i, j]]
            run = []
            for c in cells + [None]:
                if run and (c is None or c[0] != run[-1][0] + 1):
                    a, b = run[0], run[-1]
                    if fam == 1:  # top-left corner of the first cell to bottom-right of the last
                        p, q = (a[0], a[1]), (b[0] + 1, b[1] + 1)
                    else:  # bottom-left to top-right
                        p, q = (a[0], a[1] + 1), (b[0] + 1, b[1])
                    n = b[0] - a[0] + 1
                    line = np.column_stack([np.linspace(p[0], q[0], n + 1), np.linspace(p[1], q[1], n + 1)])
                    segs.append((k, a[0], b[0], np.column_stack([gx + line[:, 0] * cell, gy + line[:, 1] * cell])))
                    run = []
                if c is not None:
                    run.append(c)
        fam_runs = _chains(_sweeps(segs, pos), grown, 2.4 * cell, walk)
        runs += fam_runs
        if fam_runs:
            pos = fam_runs[-1][-1]
    return runs


def wave_runs(poly, angle_deg: float, spacing: float, start, end=None, amp: float = 1.4,
              wavelength: float = 12.0) -> list[np.ndarray]:
    """A full-coverage fill whose rows ripple like waves: an ordinary fill in a stretched frame,
    bent back afterwards."""
    a = math.radians(angle_deg)
    k = 2 * np.pi / wavelength

    def warp(xy, sign):
        xy = np.array(xy, float)
        xy[:, 1] -= sign * amp * np.sin(k * xy[:, 0])
        return xy

    flat = shapely.transform(shapely.segmentize(st.affinity.rotate(poly, -angle_deg, origin=(0, 0)), 0.4),
                             lambda xy: warp(xy, 1))
    flat = flat.buffer(0)  # warping can make a sliver cross itself
    s0 = None if start is None else warp(st._rot(np.asarray(start)[None], -a), 1)[0]
    e0 = None if end is None else warp(st._rot(np.asarray(end)[None], -a), 1)[0]
    rows = st.fill_runs(flat, 0.0, spacing, 1.8, s0, e0)  # short stitches keep the curve smooth
    return [st._rot(warp(r, -1), a) for r in rows]
