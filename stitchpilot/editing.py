"""Hand editing of stitches: pure functions over a list of ColorBlocks (each returns a new list).

A point reference is (block index, row index into that block's cmds). Only STITCH rows are editable.

Each edit is also recorded as an Op that doesn't depend on stitch numbers: "erase / move this thread's
stitches in this area" or "sew this run", in fractions of the design's width and height. When settings
change and the picture re-converts, replay() puts the edits back on the new stitches in the same places.
"""
from __future__ import annotations

from dataclasses import dataclass, replace

import numpy as np
import shapely
from shapely import affinity
from shapely.geometry import LineString, Point

from . import stitches as st

MAX_ADDED_STITCH = 3.0  # mm: long clicks in the Add tool are split into stitches this long


def copy_blocks(blocks) -> list:
    return [replace(b, cmds=b.cmds.copy()) for b in blocks]


def points(blocks) -> tuple[np.ndarray, np.ndarray]:
    """All editable needle points of visible blocks: (xy mm (N,2), refs (N,2) = block, row)."""
    xy, refs = [], []
    for bi, b in enumerate(blocks):
        if not b.visible or not len(b.cmds):
            continue
        rows = np.nonzero(b.cmds[:, 2] == st.STITCH)[0]
        xy.append(b.cmds[rows, :2])
        refs.append(np.column_stack([np.full(len(rows), bi), rows]))
    if not xy:
        return np.zeros((0, 2)), np.zeros((0, 2), int)
    return np.vstack(xy), np.vstack(refs).astype(int)


def nearest(blocks, xy_mm, max_mm: float):
    pts, refs = points(blocks)
    if not len(pts):
        return None
    d = np.hypot(*(pts - xy_mm).T)
    i = int(d.argmin())
    return tuple(refs[i]) if d[i] <= max_mm else None


def in_rect(blocks, x0, y0, x1, y1) -> set:
    pts, refs = points(blocks)
    m = (pts[:, 0] >= min(x0, x1)) & (pts[:, 0] <= max(x0, x1)) & (pts[:, 1] >= min(y0, y1)) & (pts[:, 1] <= max(y0, y1))
    return {tuple(r) for r in refs[m]}


def run_of(blocks, ref) -> set:
    """Every point of the continuous stitch run containing ref (between jumps/trims)."""
    bi, row = ref
    f = blocks[bi].cmds[:, 2]
    a = row
    while a > 0 and f[a - 1] == st.STITCH:
        a -= 1
    b = row
    while b < len(f) - 1 and f[b + 1] == st.STITCH:
        b += 1
    return {(bi, r) for r in range(a, b + 1)}


def _clean(cmds: np.ndarray) -> np.ndarray:
    """Drop jumps that lead nowhere and repeated trims left behind by deletions."""
    keep = np.ones(len(cmds), bool)
    f = cmds[:, 2]
    for i in range(len(cmds)):
        nxt = f[i + 1] if i + 1 < len(cmds) else None
        if f[i] == st.JUMP and nxt is not None and nxt == st.JUMP:
            keep[i] = False  # only the last of several jumps matters
        elif f[i] == st.JUMP and nxt != st.STITCH and nxt != st.JUMP:
            keep[i] = False
        elif f[i] == st.TRIM and (i == 0 or f[i - 1] == st.TRIM or f[i - 1] == st.JUMP):
            keep[i] = False
    out = cmds[keep]
    while len(out) and out[-1, 2] == st.JUMP:
        out = out[:-1]
    return out


def delete(blocks, refs: set) -> list:
    out = copy_blocks(blocks)
    by_block: dict[int, list[int]] = {}
    for bi, row in refs:
        by_block.setdefault(bi, []).append(row)
    for bi, rows in by_block.items():
        out[bi].cmds = _clean(np.delete(out[bi].cmds, rows, axis=0))
    return [b for b in out if (b.cmds[:, 2] == st.STITCH).any()]


def move(blocks, refs: set, dx: float, dy: float) -> list:
    out = copy_blocks(blocks)
    for bi, row in refs:
        out[bi].cmds[row, 0] += dx
        out[bi].cmds[row, 1] += dy
        # a jump that lands on this point must follow it
        if row > 0 and out[bi].cmds[row - 1, 2] == st.JUMP:
            out[bi].cmds[row - 1, :2] = out[bi].cmds[row, :2]
    return out


def _steps(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    n = max(1, int(np.ceil(np.hypot(*(b - a)) / MAX_ADDED_STITCH)))
    return np.array([a + (b - a) * (k / n) for k in range(1, n + 1)])


def insert_after(blocks, ref, xy_mm) -> tuple[list, tuple]:
    """Sew from the selected point to xy (in short stitches); returns the new last point's ref."""
    out = copy_blocks(blocks)
    bi, row = ref
    c = out[bi].cmds
    new = _steps(c[row, :2], np.asarray(xy_mm, float))
    rows = np.column_stack([new, np.full(len(new), st.STITCH)])
    out[bi].cmds = np.vstack([c[:row + 1], rows, c[row + 1:]])
    return out, (bi, row + len(new))


def new_run(blocks, bi: int, xy_mm) -> tuple[list, tuple]:
    """Start a new run in block bi at xy: trim, jump there, first needle point."""
    out = copy_blocks(blocks)
    c = out[bi].cmds
    x, y = xy_mm
    add = []
    if len(c) and c[-1, 2] == st.STITCH:
        add.append((c[-1, 0], c[-1, 1], st.TRIM))
    add += [(x, y, st.JUMP), (x, y, st.STITCH)]
    out[bi].cmds = np.vstack([c, np.array(add, float)]) if len(c) else np.array(add, float)
    return out, (bi, len(out[bi].cmds) - 1)


# ===================================================================== edits that survive re-converting
REACH = 1.0  # mm around edited stitches that counts as "this area" when edits are re-applied
KEEP = 0.25  # mm around the thread's stitches that stayed, cut back out of that area
JOIN = 0.05  # mm: an added run that starts where its thread's last stitch ends carries on from it


@dataclass
class Op:
    kind: str  # "erase" | "move" | "add"
    rgb: tuple  # the thread (block source colour) it applies to
    area: object = None  # erase / move: shapely geometry, in fractions of the design size
    path: tuple = ()  # add: the run's points ((x, y), ...), in fractions
    dx: float = 0.0  # move, in fractions
    dy: float = 0.0

    def to_dict(self) -> dict:
        d = {"kind": self.kind, "rgb": list(self.rgb)}
        if self.area is not None:
            d["area"] = shapely.to_wkt(self.area, rounding_precision=6)
        if self.path:
            d["path"] = [[round(x, 6), round(y, 6)] for x, y in self.path]
        if self.kind == "move":
            d["dx"], d["dy"] = self.dx, self.dy
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Op":
        return cls(d["kind"], tuple(d["rgb"]), shapely.from_wkt(d["area"]) if d.get("area") else None,
                   tuple(tuple(p) for p in d.get("path", ())), float(d.get("dx", 0)), float(d.get("dy", 0)))


def _runs(c: np.ndarray, rows) -> list:
    """Consecutive needle points as lines (single points stay points)."""
    rows = sorted(rows)
    out, run = [], [rows[0]]
    for r in rows[1:] + [None]:
        if r is not None and r == run[-1] + 1:
            run.append(r)
            continue
        pts = c[run, :2]
        out.append(LineString(pts) if len(pts) > 1 else Point(pts[0]))
        if r is not None:
            run = [r]
    return out


def _area(blocks, bi: int, rows: list[int], w: float, h: float):
    """The area covered by these needle points of block bi, as fractions. Stitches of the same thread that
    stay are cut out of it, so re-applying the edit doesn't reach into its neighbours."""
    c = blocks[bi].cmds
    zone = shapely.union_all([g.buffer(REACH, 8) for g in _runs(c, rows)])
    picked = set(rows)
    x0, y0, x1, y1 = zone.bounds
    near = (c[:, 2] == st.STITCH) & (c[:, 0] >= x0) & (c[:, 0] <= x1) & (c[:, 1] >= y0) & (c[:, 1] <= y1)
    kept = [int(r) for r in np.nonzero(near)[0] if int(r) not in picked]
    if kept:
        zone = zone.difference(shapely.union_all([g.buffer(KEEP, 8) for g in _runs(c, kept)]))
    return affinity.scale(zone, 1 / w, 1 / h, origin=(0, 0))


def _by_block(refs) -> dict[int, list[int]]:
    out: dict[int, list[int]] = {}
    for bi, row in refs:
        out.setdefault(int(bi), []).append(int(row))
    return out


def record_delete(blocks, refs, w: float, h: float) -> list[Op]:
    return [Op("erase", tuple(blocks[bi].source_rgb), _area(blocks, bi, rows, w, h))
            for bi, rows in _by_block(refs).items()]


def record_move(blocks, refs, dx: float, dy: float, w: float, h: float) -> list[Op]:
    return [Op("move", tuple(blocks[bi].source_rgb), _area(blocks, bi, rows, w, h), dx=dx / w, dy=dy / h)
            for bi, rows in _by_block(refs).items()]


def record_add(rgb, pts_mm, w: float, h: float) -> list[Op]:
    return [Op("add", tuple(rgb), path=tuple((float(x) / w, float(y) / h) for x, y in pts_mm))]


def _block_for(blocks, rgb) -> int | None:
    """The block sewn in this thread; if the new conversion has no such colour, the closest one."""
    if not blocks:
        return None
    dist = [sum((int(a) - int(b)) ** 2 for a, b in zip(bl.source_rgb, rgb)) for bl in blocks]
    return int(np.argmin(dist))


def _inside(blocks, bi: int, area, w: float, h: float) -> set:
    c = blocks[bi].cmds
    rows = np.nonzero(c[:, 2] == st.STITCH)[0]
    if not len(rows):
        return set()
    zone = affinity.scale(area, w, h, origin=(0, 0))
    shapely.prepare(zone)
    hit = shapely.contains_xy(zone, c[rows, 0], c[rows, 1])
    return {(bi, int(r)) for r in rows[hit]}


def _sew(blocks, bi: int, pts) -> list:
    """Add a run to block bi: carries on from the block's last stitch if it starts there, else a new run."""
    pts = np.asarray(pts, float)
    c = blocks[bi].cmds
    if len(c) and c[-1, 2] == st.STITCH and np.hypot(*(c[-1, :2] - pts[0])) <= JOIN:
        out = copy_blocks(blocks)
        rows = np.column_stack([pts[1:], np.full(len(pts) - 1, st.STITCH)])
        out[bi].cmds = np.vstack([c, rows])
        return out
    out, _ = new_run(blocks, bi, pts[0])
    if len(pts) > 1:
        rows = np.column_stack([pts[1:], np.full(len(pts) - 1, st.STITCH)])
        out[bi].cmds = np.vstack([out[bi].cmds, rows])
    return out


def apply_op(blocks, op: Op, w: float, h: float) -> list:
    bi = _block_for(blocks, op.rgb)
    if bi is None:
        return blocks
    if op.kind == "add":
        return _sew(blocks, bi, [(x * w, y * h) for x, y in op.path]) if op.path else blocks
    refs = _inside(blocks, bi, op.area, w, h)
    if not refs:
        return blocks
    if op.kind == "erase":
        return delete(blocks, refs)
    return move(blocks, refs, op.dx * w, op.dy * h)


def replay(blocks, steps, w: float, h: float, done=frozenset()) -> list[list]:
    """The stitches after each step of edits (a step is a list of Ops), starting from freshly converted blocks.
    Ops whose id() is in done were already applied by the conversion (erased areas). Returns
    [blocks, after step 1, after step 2, ...]."""
    states = [copy_blocks(blocks)]
    for step in steps:
        cur = states[-1]
        for op in step:
            if id(op) not in done:
                cur = apply_op(cur, op, w, h)
        states.append(cur)
    return states


def orientation(rotate: int, mirror: bool):
    """Where a point of the picture (in fractions) ends up after rotating clockwise and mirroring."""
    def f(x, y):
        for _ in range((rotate // 90) % 4):
            x, y = 1 - y, x
        return (1 - x, y) if mirror else (x, y)
    return f


def reorient(steps, before: tuple[int, bool], after: tuple[int, bool]) -> list[list]:
    """Moves recorded edits along when the picture is rotated or mirrored."""
    # the change is affine: work it out from where three corners of the picture go
    src = [orientation(*before)(*p) for p in [(0, 0), (1, 0), (0, 1)]]
    dst = [orientation(*after)(*p) for p in [(0, 0), (1, 0), (0, 1)]]
    A = np.array([[x, y, 1] for x, y in src], float)
    mx, my = np.linalg.solve(A, [p[0] for p in dst]), np.linalg.solve(A, [p[1] for p in dst])
    a, b, xo, d, e, yo = mx[0], mx[1], mx[2], my[0], my[1], my[2]
    pt = lambda x, y: (a * x + b * y + xo, d * x + e * y + yo)  # noqa: E731
    out = []
    for step in steps:
        new = []
        for op in step:
            area = affinity.affine_transform(op.area, [a, b, d, e, xo, yo]) if op.area is not None else None
            path = tuple(pt(x, y) for x, y in op.path)
            dx, dy = a * op.dx + b * op.dy, d * op.dx + e * op.dy
            new.append(replace(op, area=area, path=path, dx=dx, dy=dy))
        out.append(new)
    return out
