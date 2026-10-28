"""Hidden travel: route the thread between shapes underneath stitching that hasn't been sewn yet.

A trim (cut, tie-off, tie-in) is slow and leaves tails. When two shapes of one colour are separated
only by areas that a later colour will fill, or by another shape of the same colour that is still
to come, the thread can run there instead: those stitches end up buried.

Everything works on a coarse grid (GRID px per mm) of "which region covers this spot".
"""
from __future__ import annotations

import math

import cv2
import numpy as np
from shapely.geometry import LineString
from skimage.graph import MCP_Geometric

GRID = 4.0  # px per mm
SEWN = 8.0  # cost of running over a shape of this colour that is already sewn (it shows)
MAX_EXPOSED = 2.0  # mm of travel allowed on top of sewn stitches (leaving / reaching a shape)
# along the very edge of a sewn shape, travel reads as an outline in the same colour (an "edge walk")
EDGE_COST = 2.5
MAX_EDGE = 12.0  # mm
SNAP = 1.25  # mm: start and end points may sit this far outside the coverable area


class CoverMap:
    """Which region covers each grid cell, plus each region's colour block and whether it covers."""

    def __init__(self, shape_px: tuple[int, int], ppmm: float, regions, block_of, covers):
        H, W = shape_px
        full = np.full((H, W), -1, np.int32)
        for i, r in enumerate(regions):
            h, w = r.mask.shape
            view = full[r.oy:r.oy + h, r.ox:r.ox + w]
            view[r.mask > 0] = i
        gh, gw = max(1, int(round(H / ppmm * GRID))), max(1, int(round(W / ppmm * GRID)))
        self.rid = cv2.resize(full, (gw, gh), interpolation=cv2.INTER_NEAREST)
        self.block_of = np.asarray(block_of, np.int32)
        self.covers = np.asarray(covers, bool)
        self.n = len(regions)
        # each region's grid box, grown 3 cells so neighbours show up around it
        sy, sx = gh / H, gw / W
        self.boxes = [(max(0, int(r.oy * sy) - 3), min(gh, int(math.ceil((r.oy + r.mask.shape[0]) * sy)) + 4),
                       max(0, int(r.ox * sx) - 3), min(gw, int(math.ceil((r.ox + r.mask.shape[1]) * sx)) + 4))
                      for r in regions]

    def router(self, block: int) -> "Router":
        return Router(self, block)


class Router:
    """Travel routes for one colour block. Call mark(pts) with every run as it is sewn."""

    def __init__(self, cm: CoverMap, block: int):
        self.cm = cm
        later = cm.covers & (cm.block_of > block)
        mine = cm.covers & (cm.block_of == block)
        self.lut = np.full(cm.n + 1, np.inf)  # index rid + 1 (so -1, no region, maps to inf)
        self.lut[1:][later | mine] = 1.0
        ok = np.isfinite(self.lut[cm.rid + 1]).astype(np.uint8)
        # keep a quarter millimetre clear of anything that won't be covered
        self.ok = cv2.erode(ok, np.ones((3, 3), np.uint8)).astype(bool)
        # cells within half a millimetre of the edge of the coverable area
        self.edge = self.ok & ~cv2.erode(self.ok.astype(np.uint8), np.ones((5, 5), np.uint8)).astype(bool)
        lat = (later[cm.rid] & (cm.rid >= 0)).astype(np.uint8)
        lat = cv2.erode(lat, np.ones((3, 3), np.uint8))
        _, self.comp = cv2.connectedComponents(lat, connectivity=8)

        self.done = np.zeros(cm.rid.shape, np.uint8)  # cells this colour has already stitched over

    def mark(self, pts: np.ndarray):
        """Record stitches as sewn: travel over them later would show."""
        if len(pts) >= 2:
            px = np.round(pts * GRID * 4 - 2).astype(np.int32).reshape(-1, 1, 2)  # 2 bits of sub-cell precision
            cv2.polylines(self.done, [px], False, 1, 2, lineType=cv2.LINE_8, shift=2)

    def groups(self, members: list[int]) -> list[int]:
        """Group ids: regions of this block that can reach each other by hidden travel share one."""
        parent = list(range(len(members)))

        def find(a):
            while parent[a] != a:
                parent[a] = parent[parent[a]]
                a = parent[a]
            return a

        owner: dict[int, int] = {}
        rid = self.cm.rid
        for k, i in enumerate(members):
            y0, y1, x0, x1 = self.cm.boxes[i]
            near = cv2.dilate((rid[y0:y1, x0:x1] == i).astype(np.uint8), np.ones((7, 7), np.uint8)) > 0
            for c in np.unique(self.comp[y0:y1, x0:x1][near]):
                if c == 0:
                    continue
                if c in owner:
                    parent[find(k)] = find(owner[c])
                else:
                    owner[c] = k
        return [find(k) for k in range(len(members))]

    def exit_point(self, i: int, toward: np.ndarray) -> np.ndarray | None:
        """Where to leave region i heading for `toward` (mm): its edge cell that borders area the
        thread can hide under, as close to `toward` as possible."""
        y0, y1, x0, x1 = self.cm.boxes[i]
        rid = self.cm.rid[y0:y1, x0:x1]
        mine = (rid == i).astype(np.uint8)
        hide = (self.ok[y0:y1, x0:x1] & (rid != i) & (self.done[y0:y1, x0:x1] == 0)).astype(np.uint8)
        edge = (mine > 0) & (cv2.dilate(hide, np.ones((3, 3), np.uint8)) > 0)
        ys, xs = np.nonzero(edge)
        if not len(ys):
            return None
        pts = (np.column_stack([xs + x0, ys + y0]) + 0.5) / GRID
        return pts[int(np.argmin(np.hypot(*(pts - toward).T)))]

    def exposed(self, path: np.ndarray) -> float:
        """Length (mm) of a path that runs on top of stitches this colour has already sewn."""
        if len(path) < 2:
            return 0.0
        fine = densify(path, 0.25)
        mid = (fine[1:] + fine[:-1]) / 2
        g = np.clip((mid * GRID).astype(int), 0, (self.done.shape[1] - 1, self.done.shape[0] - 1))
        return float((np.hypot(*np.diff(fine, axis=0).T) * (self.done[g[:, 1], g[:, 0]] > 0)).sum())

    def route(self, a: np.ndarray, b: np.ndarray, max_len: float) -> np.ndarray | None:
        """Travel points from a to b (mm) that stay hidden, or None."""
        rid = self.cm.rid
        H, W = rid.shape
        d = float(np.hypot(*(b - a)))
        margin = int(min(15.0, 0.5 * d + 4) * GRID)
        ga, gb = a * GRID, b * GRID
        x0 = max(0, int(min(ga[0], gb[0])) - margin)
        y0 = max(0, int(min(ga[1], gb[1])) - margin)
        x1 = min(W, int(max(ga[0], gb[0])) + margin + 1)
        y1 = min(H, int(max(ga[1], gb[1])) + margin + 1)
        if x1 - x0 < 2 or y1 - y0 < 2:
            return None
        cost = np.where(self.ok[y0:y1, x0:x1], self.lut[rid[y0:y1, x0:x1] + 1], np.inf)
        done = self.done[y0:y1, x0:x1] > 0
        edge = self.edge[y0:y1, x0:x1]
        cost[done & ~edge] = SEWN
        cost[done & edge] = EDGE_COST
        sa = self._snap(cost, ga - (x0, y0))
        sb = self._snap(cost, gb - (x0, y0))
        if sa is None or sb is None:
            return None
        m = MCP_Geometric(np.where(np.isfinite(cost), cost, -1), fully_connected=True)
        cum, _ = m.find_costs([sa], [sb])
        if not np.isfinite(cum[sb]) or cum[sb] < 0:
            return None
        cells = np.asarray(m.traceback(sb))
        steps = np.hypot(*np.diff(cells, axis=0).T) / GRID
        on = cost[tuple(cells[1:].T)]
        if (steps * (on >= SEWN)).sum() > MAX_EXPOSED or (steps * (on == EDGE_COST)).sum() > MAX_EDGE:
            return None
        pts = (cells[:, ::-1] + (x0 + 0.5, y0 + 0.5)) / GRID
        path = np.vstack([a, pts, b])
        line = LineString(path).simplify(0.2)
        path = np.asarray(line.coords)
        if line.length > max_len:
            return None
        return densify(path, 2.5)

    @staticmethod
    def _snap(cost: np.ndarray, g: np.ndarray) -> tuple[int, int] | None:
        """Nearest usable cell to grid point g within SNAP mm (row, col)."""
        r = int(math.ceil(SNAP * GRID))
        cy, cx = int(g[1]), int(g[0])
        y0, x0 = max(0, cy - r), max(0, cx - r)
        win = np.isfinite(cost[y0:cy + r + 1, x0:cx + r + 1])
        ys, xs = np.nonzero(win)
        if not len(ys):
            return None
        dd = (ys + y0 - cy) ** 2 + (xs + x0 - cx) ** 2
        k = int(dd.argmin())
        if dd[k] > r * r:
            return None
        return int(ys[k] + y0), int(xs[k] + x0)


def densify(path: np.ndarray, step: float) -> np.ndarray:
    """Split segments longer than step, keeping every corner (so the route never cuts a corner)."""
    out = [path[0]]
    for p, q in zip(path[:-1], path[1:]):
        n = max(1, int(math.ceil(float(np.hypot(*(q - p))) / step)))
        out.extend(p + (q - p) * (k / n) for k in range(1, n + 1))
    return np.array(out)
