"""Stitch generators: tatami fill, satin columns along a centreline, running/bean stitch.

All coordinates are millimetres in design space (x right, y down).
A generator returns a list of "runs": arrays of needle points sewn continuously.
The Sequencer joins runs with short connectors, travel stitches or trims.
"""
from __future__ import annotations

import math

import cv2
import numpy as np
import shapely
from shapely import affinity
from scipy.ndimage import gaussian_filter1d
from shapely.geometry import LineString, MultiPolygon, Polygon
from shapely.ops import unary_union
from skimage.morphology import skeletonize

STITCH, JUMP, TRIM, COLOR_CHANGE, STOP = 0, 1, 2, 3, 4


# ===================================================================== geometry
def mask_to_polygons(mask: np.ndarray, ox: int, oy: int, ppmm: float, min_hole_mm2: float,
                     bare: np.ndarray | None = None) -> list[Polygon]:
    """Smooth vector outline (mm) of a binary mask crop whose top-left is (ox, oy) px.

    Holes smaller than min_hole_mm2 are filled in (another colour sewn on top covers them), except holes
    that are bare fabric where `bare` (same crop) is set: the eye of a small e must stay open."""
    contours, hier = cv2.findContours(mask.astype(np.uint8), cv2.RETR_CCOMP, cv2.CHAIN_APPROX_NONE)
    if hier is None:
        return []
    hier = hier[0]

    def conv(c: np.ndarray) -> np.ndarray:
        pts = c[:, 0, :].astype(np.float64)
        if len(pts) >= 8:
            pts = gaussian_filter1d(pts, 1.0, axis=0, mode="wrap")
        return (pts + (ox + 0.5, oy + 0.5)) / ppmm

    out: list[Polygon] = []
    for i, c in enumerate(contours):
        if hier[i][3] != -1 or len(c) < 3:
            continue
        holes = []
        j = hier[i][2]
        while j != -1:
            if len(contours[j]) >= 3:
                h = Polygon(conv(contours[j]))
                if h.is_valid and (h.area >= min_hole_mm2 or bare is not None and _is_bare(contours[j], bare)):
                    holes.append(h.exterior.coords)
            j = hier[j][0]
        poly = Polygon(conv(c), holes)
        # contours run through pixel centres: grow by half a pixel to restore true size
        poly = poly.buffer(0.5 / ppmm, quad_segs=4).simplify(0.02)
        out.extend(_polys(poly))
    return out


def _is_bare(contour: np.ndarray, bare: np.ndarray) -> bool:
    """Whether a hole (a contour of the mask) is mostly bare fabric."""
    inside = np.zeros(bare.shape, np.uint8)
    cv2.drawContours(inside, [contour], -1, 1, -1)
    inside &= ~cv2.drawContours(np.zeros_like(inside), [contour], -1, 1, 1).astype(bool)  # not the edge pixels
    n = int(inside.sum())
    return n > 0 and float(bare[inside > 0].mean()) > 0.5


def _polys(g) -> list[Polygon]:
    if g.is_empty:
        return []
    if isinstance(g, Polygon):
        return [g]
    if isinstance(g, MultiPolygon):
        return list(g.geoms)
    return [p for p in getattr(g, "geoms", []) if isinstance(p, Polygon)]


def _rings(geom) -> list[np.ndarray]:
    out = []
    for poly in _polys(geom):
        out.append(np.asarray(poly.exterior.coords))
        out.extend(np.asarray(r.coords) for r in poly.interiors)
    return out


def _rot(pts: np.ndarray, a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.column_stack([pts[:, 0] * c - pts[:, 1] * s, pts[:, 0] * s + pts[:, 1] * c])


def resample(path: np.ndarray, step: float, closed: bool = False) -> np.ndarray:
    if closed:
        path = np.vstack([path, path[:1]])
    seg = np.linalg.norm(np.diff(path, axis=0), axis=1)
    cum = np.concatenate([[0], np.cumsum(seg)])
    total = cum[-1]
    if total < 1e-9:
        return path[:1]
    n = max(1, int(math.ceil(total / step)))
    t = np.linspace(0, total, n + 1)
    return np.column_stack([np.interp(t, cum, path[:, 0]), np.interp(t, cum, path[:, 1])])


def follow(path: np.ndarray, max_step: float, closed: bool = False, tol: float = 0.04,
           min_step: float = 0.5) -> np.ndarray:
    """Stitch points along a path: up to max_step apart on straight stretches, closer on curves and at
    corners, so no stitch strays more than tol (mm) from the line. Even spacing would cut small curves
    (letters, little circles) into polygons."""
    fine = resample(path, 0.05, closed)
    n = len(fine)
    if n < 3:
        return fine
    reach = max(2, int(max_step / 0.05))
    out, i = [0], 0
    while i < n - 1:
        j = min(n - 1, i + reach)
        if j < n - 1 and n - 1 - j < min_step / 0.05:  # don't leave a tiny last stitch
            j = min(n - 1, i + max(int(min_step / 0.05), (n - 1 - i) // 2))
        a = fine[i]
        while j > i + 1:
            d = fine[j] - a
            ln = float(np.hypot(*d))
            mid = fine[i + 1:j] - a
            if ln < 1e-9 or float(np.abs(mid[:, 0] * d[1] - mid[:, 1] * d[0]).max()) / ln <= tol:
                break
            j = max(i + 1, min(j - 1, i + int(0.8 * (j - i))))
        if j - i < min_step / 0.05 and n - 1 - i >= min_step / 0.05:  # tight corner: keep stitches sewable
            j = i + int(min_step / 0.05)
        out.append(j)
        i = j
    return fine[out]


# ===================================================================== skeleton
def skeleton_paths(mask: np.ndarray, dt: np.ndarray) -> list[tuple[np.ndarray, bool, bool, bool]]:
    """Centreline paths of a mask as (pts[x,y] px, closed, start_free, end_free).

    Spurs shorter than the local stroke width are pruned and paths are merged
    through 2-way junctions so each path is as long as possible.
    """
    sk = skeletonize(mask > 0)
    ys, xs = np.nonzero(sk)
    n = len(xs)
    if n == 0:
        return []
    if n == 1:
        return [(np.array([[xs[0], ys[0]]], float), False, True, True)]
    H, W = sk.shape
    idx = -np.ones((H + 2, W + 2), np.int64)
    idx[ys + 1, xs + 1] = np.arange(n)
    S = idx >= 0
    adj: list[list[int]] = [[] for _ in range(n)]

    def link(m: np.ndarray, dy: int, dx: int):
        yy, xx = np.nonzero(m)
        for a, b in zip(idx[yy, xx].tolist(), idx[yy + dy, xx + dx].tolist()):
            adj[a].append(b)
            adj[b].append(a)

    c = S[:-1, :-1]
    link(S[:-1, :-1] & S[:-1, 1:], 0, 1)
    link(S[:-1, :-1] & S[1:, :-1], 1, 0)
    # diagonal links only where there is no orthogonal shortcut (keeps degree honest)
    link(c & S[1:, 1:] & ~S[:-1, 1:] & ~S[1:, :-1], 1, 1)
    sub = S[:-1, 1:]
    m = sub & S[1:, :-1] & ~S[:-1, :-1] & ~S[1:, 1:]
    yy, xx = np.nonzero(m)
    for a, b in zip(idx[yy, xx + 1].tolist(), idx[yy + 1, xx].tolist()):
        adj[a].append(b)
        adj[b].append(a)

    deg = [len(a) for a in adj]  # a list: walked one pixel at a time, numpy indexing would be slow here
    seen: set[tuple[int, int]] = set()
    raw: list[list[int]] = []

    def key(a, b):
        return (a, b) if a < b else (b, a)

    for s in [i for i, k in enumerate(deg) if k != 2]:
        if deg[s] == 0:
            raw.append([s])
        for nb in adj[s]:
            if key(s, nb) in seen:
                continue
            seen.add(key(s, nb))
            path, prev, cur = [s, nb], s, nb
            while deg[cur] == 2:
                nxt = adj[cur][0] if adj[cur][0] != prev else adj[cur][1]
                if key(cur, nxt) in seen:
                    break
                seen.add(key(cur, nxt))
                path.append(nxt)
                prev, cur = cur, nxt
            raw.append(path)
    for s in range(n):  # pure loops
        if deg[s] == 2 and (key(s, adj[s][0]) not in seen or key(s, adj[s][1]) not in seen):
            path, prev, cur = [s], s, adj[s][0]
            seen.add(key(s, cur))
            while cur != s:
                path.append(cur)
                nxt = adj[cur][0] if adj[cur][0] != prev else adj[cur][1]
                if key(cur, nxt) in seen and nxt != s:
                    break
                seen.add(key(cur, nxt))
                prev, cur = cur, nxt
            path.append(s)
            raw.append(path)

    # ---- prune spurs, then merge through 2-way nodes
    def node_deg(paths):
        d: dict[int, int] = {}
        for p in paths:
            d[p[0]] = d.get(p[0], 0) + 1
            d[p[-1]] = d.get(p[-1], 0) + 1
        return d

    def plen(p):
        pts = np.column_stack([xs[p], ys[p]])
        return float(np.linalg.norm(np.diff(pts, axis=0), axis=1).sum()) if len(p) > 1 else 0.0

    paths = [p for p in raw if len(p) > 0]
    for _ in range(3):
        d = node_deg(paths)
        keep = []
        for p in paths:
            a, b = p[0], p[-1]
            if len(paths) > 1 and a != b:
                for free, junc in ((a, b), (b, a)):
                    if d[free] == 1 and d[junc] >= 3 and plen(p) < 1.2 * dt[ys[junc], xs[junc]] + 1:
                        break
                else:
                    keep.append(p)
                    continue
                d[a] -= 1
                d[b] -= 1
                continue
            keep.append(p)
        if len(keep) == len(paths):
            break
        paths = keep
    merged = True
    while merged:
        merged = False
        d = node_deg(paths)
        for node, cnt in d.items():
            if cnt != 2:
                continue
            ends = [i for i, p in enumerate(paths) if p[0] == node or p[-1] == node]
            if len(ends) != 2:
                continue
            i, j = ends
            p, q = paths[i], paths[j]
            if p[-1] != node:
                p = p[::-1]
            if q[0] != node:
                q = q[::-1]
            paths = [x for k, x in enumerate(paths) if k not in (i, j)] + [p + q[1:]]
            merged = True
            break

    d = node_deg(paths)
    out = []
    for p in paths:
        pts = np.column_stack([xs[p], ys[p]]).astype(float)
        closed = len(p) > 2 and p[0] == p[-1]
        out.append((pts, closed, d.get(p[0], 0) == 1, d.get(p[-1], 0) == 1))
    return out


def join_through(paths, dt: np.ndarray, max_turn: float = 40.0, overlap: float = 0.0):
    """At junctions, join the two strokes that carry straight on (the bar of a T, the legs of an A's
    sides) into one path. Other strokes ending there become branches: they stop at the
    through-stroke's edge (tucked `overlap` px under it) so their stitches don't pile on top of it.

    paths: skeleton_paths() output. Returns [(pts px, closed, start_free, end_free, cut0, cut1,
    clips, corners)]: cut0/cut1 are how far (px) to cut each end back; clips are half-planes
    (point, normal, radius) the satin must stay on the positive side of, near that point; corners are
    points where the joined stroke bends and should be mitred.
    """
    items = [[p.copy(), c, f0, f1, 0.0, 0.0, [], []] for p, c, f0, f1 in paths]

    def plen(p):
        return float(np.linalg.norm(np.diff(p, axis=0), axis=1).sum()) if len(p) > 1 else 0.0

    # an X crossing comes out of the skeleton as two junctions joined by a stub: make it one junction
    merged = True
    while merged:
        merged = False
        for k, (pts, closed, f0, f1, *_) in enumerate(items):
            if closed or f0 or f1 or len(pts) < 2:
                continue
            n0, n1 = tuple(pts[0]), tuple(pts[-1])
            if n0 == n1:
                continue
            w = max(dt[int(n0[1]), int(n0[0])], dt[int(n1[1]), int(n1[0])])
            if plen(pts) > 1.2 * w + 2:
                continue
            others = [it for j, it in enumerate(items) if j != k]
            if sum((tuple(it[0][0]) in (n0, n1)) + (tuple(it[0][-1]) in (n0, n1)) for it in others) < 4:
                continue
            mid = np.round((pts[0] + pts[-1]) / 2)
            for it in others:
                if tuple(it[0][0]) in (n0, n1):
                    it[0] = np.vstack([mid, it[0]])
                if tuple(it[0][-1]) in (n0, n1):
                    it[0] = np.vstack([it[0], mid])
            items = others
            merged = True
            break

    def leaving(pts, end, r):  # direction from the junction into the stroke, measured ~r px away
        seq = pts if end == 0 else pts[::-1]
        arc = np.concatenate([[0], np.cumsum(np.linalg.norm(np.diff(seq, axis=0), axis=1))])
        j = min(len(seq) - 1, int(np.searchsorted(arc, r)))
        return _unit(seq[j] - seq[0]) if j > 0 else None

    done: set[tuple] = set()
    changed = True
    while changed:
        changed = False
        nodes: dict[tuple, list] = {}
        for i, (pts, closed, f0, f1, *_) in enumerate(items):
            if closed or len(pts) < 2:
                continue
            for end, free in ((0, f0), (1, f1)):
                if not free:
                    nodes.setdefault(tuple(pts[0 if end == 0 else -1]), []).append((i, end))
        for node, ends in nodes.items():
            # one stroke runs through; a second one crossing (X) stops at its edge, so the crossing
            # isn't sewn twice
            if node in done or len(ends) < 3 or len({i for i, _ in ends}) < len(ends):
                continue
            r = 2.5 * float(dt[int(node[1]), int(node[0])]) + 2
            dirs = [leaving(items[i][0], e, r) for i, e in ends]
            best = None
            for a in range(len(ends)):
                for b in range(a + 1, len(ends)):
                    if dirs[a] is None or dirs[b] is None:
                        continue
                    straight = -float(dirs[a] @ dirs[b])  # 1 = carries straight on
                    if straight > math.cos(math.radians(max_turn)) and (best is None or straight > best[0]):
                        best = (straight, a, b)
            if best is None:
                continue
            _, a, b = best
            (ia, ea), (ib, eb) = ends[a], ends[b]
            A, B = items[ia], items[ib]
            pa = A[0] if ea == 1 else A[0][::-1]  # ends at the node
            pb = B[0] if eb == 0 else B[0][::-1]  # starts at the node
            fa = A[2] if ea == 1 else A[3]
            fb = B[3] if eb == 0 else B[2]
            ca = A[4] if ea == 1 else A[5]
            cb = B[5] if eb == 0 else B[4]
            # the through-stroke is as wide as the junction: cut the other strokes back by that much
            hw_node = float(dt[int(node[1]), int(node[0])])
            for k, (i, e) in enumerate(ends):
                if k in (a, b) or dirs[k] is None:
                    continue
                # a slanted branch meets the edge further out; its stitches are clipped along the edge
                slant = abs(float(dirs[k][0] * dirs[a][1] - dirs[k][1] * dirs[a][0]))
                items[i][4 + e] = max(items[i][4 + e], hw_node / max(slant, 0.5))
                nrm = np.array([-dirs[a][1], dirs[a][0]])
                if float(nrm @ dirs[k]) < 0:
                    nrm = -nrm
                items[i][6].append((np.array(node) + nrm * (hw_node - overlap), nrm, 3 * hw_node + 2))
            # the skeleton wobbles through a junction: run straight across it instead
            ra = min(1.5 * hw_node, plen(pa) / 3)
            rb = min(1.5 * hw_node, plen(pb) / 3)
            arc_a = np.concatenate([[0], np.cumsum(np.linalg.norm(np.diff(pa[::-1], axis=0), axis=1))])
            arc_b = np.concatenate([[0], np.cumsum(np.linalg.norm(np.diff(pb, axis=0), axis=1))])
            pa = pa[:max(1, len(pa) - int(np.searchsorted(arc_a, ra)))]
            pb = pb[min(len(pb) - 1, int(np.searchsorted(arc_b, rb))):]
            gap = float(np.hypot(*(pb[0] - pa[-1])))
            bridge = pa[-1] + (pb[0] - pa[-1]) * np.linspace(0, 1, max(2, int(gap) + 1))[1:-1, None]
            bend = [np.array(node)] if best[0] < math.cos(math.radians(15)) else []
            joined = [np.vstack([pa, bridge, pb]), False, fa, fb, ca, cb, A[6] + B[6], A[7] + B[7] + bend]
            items = [it for k, it in enumerate(items) if k not in (ia, ib)] + [joined]
            done.add(node)
            changed = True
            break
    return [tuple(it) for it in items]


# ===================================================================== fill
def _scan_rows(poly_rot: Polygon, spacing: float):
    rings = _rings(poly_rot)
    e0 = np.concatenate([r[:-1] for r in rings])
    e1 = np.concatenate([r[1:] for r in rings])
    y0, y1 = e0[:, 1], e1[:, 1]
    miny, maxy = min(y0.min(), y1.min()), max(y0.max(), y1.max())
    k0 = int(math.floor(miny / spacing - 0.5)) + 1
    k1 = int(math.ceil(maxy / spacing - 0.5))
    rows = []
    for k in range(k0, k1 + 1):
        y = (k + 0.5) * spacing  # global grid keeps neighbouring regions aligned
        cr = (y0 <= y) != (y1 <= y)
        if not cr.any():
            continue
        xs = e0[cr, 0] + (y - y0[cr]) * (e1[cr, 0] - e0[cr, 0]) / (y1[cr] - y0[cr])
        xs.sort()
        if len(xs) % 2:
            xs = xs[:-1]
        rows.append((k, y, xs.reshape(-1, 2)))
    return rows


def _chunks(rows, min_len: float):
    open_, done = [], []
    for k, y, segs in rows:
        new_open, taken = [], set()
        for xa, xb in segs:
            if xb - xa < min_len:
                continue
            hit = None
            for ci, c in enumerate(open_):
                if ci in taken:
                    continue
                _, _, la, lb = c[-1]
                if xa < lb and xb > la:
                    hit = ci
                    break
            if hit is None:
                new_open.append([(k, y, xa, xb)])
            else:
                taken.add(hit)
                open_[hit].append((k, y, xa, xb))
                new_open.append(open_[hit])
        done += [c for ci, c in enumerate(open_) if ci not in taken]
        open_ = new_open
    return done + open_


def _row_pts(k, y, xa, xb, L, reverse, stagger=4):
    off = (k % stagger) / stagger * L
    first = math.ceil((xa - off) / L) * L + off
    grid = np.arange(first, xb, L)
    grid = grid[(grid > xa + 0.5) & (grid < xb - 0.5)]
    xs = np.concatenate([[xa], grid, [xb]])
    if reverse:
        xs = xs[::-1]
    return np.column_stack([xs, np.full(len(xs), y)])


def _chunk_ends(ch, rev_rows: bool, first_rev: bool):
    """Start and end point of a chunk sewn in the given direction (rotated frame)."""
    rows = ch[::-1] if rev_rows else ch
    _, y0, a0, b0 = rows[0]
    _, y1, a1, b1 = rows[-1]
    last_rev = ((len(rows) - 1) % 2 == 1) != first_rev
    return np.array([b0 if first_rev else a0, y0]), np.array([a1 if last_rev else b1, y1])


def _all_chunk_ends(chunks):
    """Start and end points of every chunk for each way of sewing it: two (n, 4, 2) arrays, way = 2*rev_rows + first_rev."""
    starts, ends = np.empty((len(chunks), 4, 2)), np.empty((len(chunks), 4, 2))
    for ci, ch in enumerate(chunks):
        for w in range(4):
            starts[ci, w], ends[ci, w] = _chunk_ends(ch, w >= 2, w % 2 == 1)
    return starts, ends


def _plan_chunks(chunks, pos, first=None, ends=None):
    """Greedy order: always the nearest chunk start next. Returns ([(ci, rev_rows, first_rev)], end).
    ends: _all_chunk_ends(chunks), when planning the same chunks several times."""
    starts, finish = ends if ends is not None else _all_chunk_ends(chunks)
    left = np.ones(len(chunks), bool)
    plan = []
    for _ in range(len(chunks)):
        if first is not None:
            (ci, rr, fr), first = first, None
            w = 2 * rr + fr
        else:
            idx = np.flatnonzero(left)
            if pos is None:
                ci, w = int(idx[0]), 0
            else:
                diff = starts[idx] - pos
                j = int(np.argmin(np.hypot(diff[..., 0], diff[..., 1])))  # first of equals, as before
                ci, w = int(idx[j // 4]), j % 4
        left[ci] = False
        plan.append((ci, w >= 2, w % 2 == 1))
        pos = finish[ci, w]
    return plan, pos


def fill_runs(poly: Polygon, angle_deg: float, spacing: float, stitch_len: float,
              start: np.ndarray | None, end: np.ndarray | None = None) -> list[np.ndarray]:
    """Tatami fill: rows split into monotone chunks, each sewn back and forth.

    Starts near `start`; with `end` given, it also tries to finish near there (where the thread goes
    next), so leaving doesn't mean crossing rows already sewn.
    """
    a = math.radians(angle_deg)
    runs: list[np.ndarray] = []
    pos = None if start is None else _rot(start[None], -a)[0]
    goal = None if end is None else _rot(end[None], -a)[0]
    parts = _polys(poly)
    for pi, part in enumerate(parts):
        pr = affinity.rotate(part, -angle_deg, origin=(0, 0), use_radians=False)
        chunks = _chunks(_scan_rows(pr, spacing), 0.15)
        if not chunks:
            continue
        ends = _all_chunk_ends(chunks)
        plan, _ = _plan_chunks(chunks, pos, ends=ends)
        if goal is not None and pi == len(parts) - 1:
            # try other first moves; extra distance at the start costs as much as at the end
            def cost(cand):
                (ci, rr, fr), finish = cand[0][0], cand[1]
                p0 = _chunk_ends(chunks[ci], rr, fr)[0]
                return (0.0 if pos is None else float(np.hypot(*(p0 - pos)))) + float(np.hypot(*(finish - goal)))

            cands = [_plan_chunks(chunks, pos, (ci, rr, fr), ends)
                     for ci in range(min(len(chunks), 12)) for rr in (False, True) for fr in (False, True)]
            plan = min(cands + [_plan_chunks(chunks, pos, ends=ends)], key=cost)[0]
        for ci, rev_rows, first_rev in plan:
            rows = chunks[ci][::-1] if rev_rows else chunks[ci]
            for i, (k, y, xa, xb) in enumerate(rows):
                runs.append(_row_pts(k, y, xa, xb, stitch_len, (i % 2 == 1) != first_rev))
        pos = runs[-1][-1]
    return [_rot(r, a) for r in runs]


# ===================================================================== satin
def _cross(a, b):
    return a[..., 0] * b[..., 1] - a[..., 1] * b[..., 0]


def _ray_dist(P: np.ndarray, D: np.ndarray, e0: np.ndarray, e1: np.ndarray, tmax: np.ndarray) -> np.ndarray:
    """Distance along each ray P+tD to the first polygon edge (capped at tmax)."""
    e = e1 - e0
    out = np.empty(len(P))
    for s in range(0, len(P), 256):
        p, d = P[s:s + 256, None, :], D[s:s + 256, None, :]
        qp = e0[None] - p
        den = _cross(d, e[None])
        with np.errstate(divide="ignore", invalid="ignore"):
            t = _cross(qp, e[None]) / den
            u = _cross(qp, d) / den
        ok = (np.abs(den) > 1e-12) & (u >= 0) & (u <= 1) & (t > 1e-6)
        out[s:s + 256] = np.where(ok, t, np.inf).min(1)
    return np.minimum(out, tmax)


CORNER_DEG = 50.0  # a centreline turning more than this within about one stroke width is a corner
SATIN_MIN_MM = 0.8  # narrowest satin column: thinner ones look like a run and sink into the fabric


def _unit(v: np.ndarray) -> np.ndarray:
    n = float(np.hypot(*v))
    return v / n if n > 1e-9 else np.array([1.0, 0.0])


def _turns(c: np.ndarray, closed: bool, w: int) -> np.ndarray:
    """Turn angle (degrees) at each point, measured w samples either side."""
    n = len(c)
    i = np.arange(n)
    if closed:
        b, f = c[(i - w) % n], c[(i + w) % n]
    else:
        b, f = c[np.maximum(i - w, 0)], c[np.minimum(i + w, n - 1)]
    v1, v2 = c - b, f - c
    n1, n2 = np.linalg.norm(v1, axis=1), np.linalg.norm(v2, axis=1)
    cos = (v1 * v2).sum(1) / np.maximum(n1 * n2, 1e-12)
    ang = np.degrees(np.arccos(np.clip(cos, -1, 1)))
    ang[(n1 < 1e-9) | (n2 < 1e-9)] = 0.0
    return ang


def find_corners(c: np.ndarray, closed: bool, w: int) -> list[int]:
    """Indices of sharp corners. A curve turns gradually (half the window, half the turn); a corner
    turns all at once, so the turn barely drops when the window is halved. Turning more than 100°
    within the window fans out the stitches whatever its shape, so that counts as a corner too."""
    n = len(c)
    if w < 2 or n < 2 * w + 3:
        return []
    big, small = _turns(c, closed, w), _turns(c, closed, max(1, w // 2))
    ok = (big > CORNER_DEG) & ((small > 0.6 * big) | (big > 100))
    if not closed:
        ok[:w] = ok[n - w:] = False
    picked: list[int] = []
    for i in np.nonzero(ok)[0][np.argsort(-big[ok])]:
        if all(min(abs(i - j), n - abs(i - j)) >= 2 * w if closed else abs(i - j) >= 2 * w for j in picked):
            picked.append(int(i))
    return sorted(picked)


def _split_at_corners(c: np.ndarray, closed: bool, hw: np.ndarray, step: float, forced=()):
    """Cut a centreline at its sharp corners into columns that each end in a mitre.

    Returns [(pts, is_extension, mitres, corner_hw)], where mitres (P, n, end, span) are half-planes to keep
    ((x - P)·n >= 0) over the span points at that end of the column. Near each corner the column is
    straightened and extended past the corner so the outside of the corner gets covered; the mitre then cuts
    it off cleanly. No fan of stitches.
    """
    # the window spans the rounded bit a skeleton makes at a corner (blunt letter tips bend twice)
    w = max(2, int(round(float(np.clip(1.6 * np.median(hw), 0.8, 4.0)) / step)))
    corners = find_corners(c, closed, w)
    n = len(c)
    for k in forced:  # bends at junctions, known from the skeleton
        if (closed or w <= k < n - w) and all(min(abs(k - j), n - abs(k - j)) >= 2 * w for j in corners):
            corners = sorted(corners + [int(k)])
    if not corners:
        return [(c, np.zeros(len(c), bool), [], None)]
    n = len(c)

    def at(i):
        return c[i % n] if closed else c[min(max(i, 0), n - 1)]

    info = {}
    for k in corners:  # stroke directions measured beyond the rounded bit
        u_in = _unit(at(k - w) - at(k - 2 * w)) if closed or k >= 2 * w else _unit(c[k] - at(k - w))
        u_out = _unit(at(k + 2 * w) - at(k + w)) if closed or k + 2 * w < n else _unit(at(k + w) - c[k])
        m = u_out - u_in
        nm = np.array([-m[1], m[0]])
        nm = nm / max(1e-9, float(np.hypot(*nm)))
        info[k] = (c[k], u_in, u_out, nm, float(hw[k]))
    if closed:
        c = np.vstack([c[corners[0]:], c[:corners[0]], c[corners[0]:corners[0] + 1]])
        bounds = [k - corners[0] if k >= corners[0] else k - corners[0] + n for k in corners] + [n]
        keys = corners + [corners[0]]
    else:
        bounds, keys = [0] + corners + [n - 1], [None] + corners + [None]

    out = []
    for j in range(len(bounds) - 1):
        seg = c[bounds[j]:bounds[j + 1] + 1]
        ext = np.zeros(len(seg), bool)
        mitres, chw = [], []
        for end in ("start", "end"):
            k = keys[j] if end == "start" else keys[j + 1]
            if k is None:
                continue
            P, u_in, u_out, nm, h = info[k]
            if end == "start":
                seg, ext = seg[::-1], ext[::-1]
            # straighten the last stretch before the corner, then run on past it
            arc = np.concatenate([[0], np.cumsum(np.linalg.norm(np.diff(seg[::-1], axis=0), axis=1))])
            total = arc[-1]
            trim = min(w * step, 0.35 * total)
            cut = len(seg) - 1 - int(np.searchsorted(arc, trim))
            # the mitre only cuts this end of the column (counted in points along it): the same line can
            # cross the far side of a curving stroke (C, S, the eye of an e), which must be kept
            span = int((trim + 2.0 * h) / step) + 1
            if cut >= 1:
                u = u_in if end == "end" else -u_out
                reach = trim + min(4.0 * h, 5.0)
                more = seg[cut] + u * (np.arange(1, int(reach / step) + 1) * step)[:, None]
                seg = np.vstack([seg[:cut + 1], more])
                ext = np.concatenate([ext[:cut + 1], np.ones(len(more), bool)])
                span = len(seg) - cut + int(2.0 * h / step)
            back = -u_in if end == "end" else u_out  # this column lies on the side of P - u_in / P + u_out
            mitres.append((P, nm if float(back @ nm) >= 0 else -nm, end, span))
            chw.append(h)
            if end == "start":
                seg, ext = seg[::-1], ext[::-1]
        if len(seg) >= 2:
            out.append((seg, ext, mitres, max(chw) if chw else None))
    return out


def _short_stitches(c, L, R, keep):
    """On tight curves the inner edge gets crowded: pull every other stitch on that side in."""
    dc = np.linalg.norm(np.gradient(c, axis=0), axis=1)
    for side, parity in ((L, 0), (R, 1)):
        ratio = gaussian_filter1d(np.linalg.norm(np.gradient(side, axis=0), axis=1) / np.maximum(dc, 1e-9), 2.0)
        i = np.arange(len(c))
        sel = (ratio < 0.6) & (i % 2 == parity) & ((i // 2) % 2 == 1) & keep
        side[sel] = c[sel] + (side[sel] - c[sel]) * 0.55


def satin_runs(paths_mm, poly: Polygon, halfwidth, s, start: np.ndarray | None,
               hints=None, report: dict | None = None) -> list[np.ndarray]:
    """Satin columns following centreline paths, with centre-walk + zigzag underlay.

    Sharp corners are split into two columns meeting at a mitre; tight inner curves get short stitches.
    paths_mm: list of (pts, closed) centrelines in mm; halfwidth(pts) -> local half widths.
    hints: per path, {"clips": [(point, normal, radius)], "corners": [point]} from join_through (mm).
    report: if given, gets "fan_mm", how much of the satin fans out (a blob, better filled).
    """
    rings = _rings(poly)
    e0 = np.concatenate([r[:-1] for r in rings])
    e1 = np.concatenate([r[1:] for r in rings])
    max_half = s.satin_max_width / 2.0
    pc = s.pull_comp / 2.0
    pieces = []
    for pi, (pts, closed) in enumerate(paths_mm):
        hint = hints[pi] if hints else {}
        # narrow columns sew a little more open (up to 25%): at full density thread piles up in them
        width = 2 * float(np.median(halfwidth(resample(pts, 0.5, closed)))) + 2 * pc
        step = s.satin_spacing / 2.0 * (1 + 0.25 * float(np.clip((1.6 - width) / 0.8, 0, 1)))
        c0 = resample(pts, step, closed)
        if len(c0) < 2:
            continue
        if closed and len(c0) > 2 and np.allclose(c0[0], c0[-1]):
            c0 = c0[:-1]
        forced = [int(np.argmin(np.hypot(*(c0 - q).T))) for q in hint.get("corners", [])]
        cols = _split_at_corners(c0, closed, np.minimum(halfwidth(c0), max_half), step, forced)
        for c, ext, mitres, chw in cols:
            whole = closed and not mitres
            sm = gaussian_filter1d(c, 4.0, axis=0, mode="wrap" if whole else "nearest")
            tan = np.gradient(sm, axis=0)
            tan /= np.maximum(1e-9, np.linalg.norm(tan, axis=1))[:, None]
            nrm = np.column_stack([-tan[:, 1], tan[:, 0]])
            hw = np.minimum(halfwidth(c), max_half)
            if chw is not None:  # past the corner the shape is wider than the local measure says
                hw[ext] = min(chw, max_half)
            cap = 1.3 * hw + 0.25
            inside = shapely.contains_xy(poly, c[:, 0], c[:, 1])
            # an extension stops for good the first time it leaves the shape (walking outwards)
            body = np.nonzero(~ext)[0]
            if len(body):
                for idx in (np.arange(body[0] - 1, -1, -1), np.arange(body[-1] + 1, len(c))):
                    gone = False
                    for i in idx:
                        gone = gone or not inside[i]
                        inside[i] = not gone
            dl = _ray_dist(c, nrm, e0, e1, cap)
            dr = _ray_dist(c, -nrm, e0, e1, cap)
            short = np.maximum(0.0, SATIN_MIN_MM - 2 * pc - dl - dr) / 2  # widen hairlines evenly
            dl, dr = dl + short, dr + short
            L = c + nrm * (dl + pc)[:, None]
            R = c - nrm * (dr + pc)[:, None]
            keep = inside.copy()
            plain = np.ones(len(c), bool)
            # each cut only applies near its corner (mitres) or junction (edge clips)
            idx = np.arange(len(c))
            clips = [(P, nm, idx < span if end == "start" else idx >= len(c) - span) for P, nm, end, span in mitres]
            clips += [(P, nm, np.hypot(*(c - P).T) <= radius) for P, nm, radius in hint.get("clips", [])]
            for P, nm, near in clips:
                sl, sr = (L - P) @ nm, (R - P) @ nm
                sl, sr = np.where(near, sl, 1.0), np.where(near, sr, 1.0)
                keep &= (sl >= 0) | (sr >= 0)
                with np.errstate(divide="ignore", invalid="ignore"):
                    X = L + (R - L) * (sl / (sl - sr))[:, None]
                fl, fr = (sl < 0) & (sr >= 0), (sr < 0) & (sl >= 0)
                L[fl], R[fr] = X[fl], X[fr]
                plain &= ~(fl | fr)
            if report is not None:  # stitches fanning out from a point: the column is wider than its bend
                dc = np.linalg.norm(np.diff(c, axis=0), axis=1)
                inner = np.minimum(np.linalg.norm(np.diff(L, axis=0), axis=1),
                                   np.linalg.norm(np.diff(R, axis=0), axis=1)) / np.maximum(dc, 1e-9)
                fan = (inner < 0.25) & (hw[1:] > 0.6) & keep[1:] & ~ext[1:]
                report["fan_mm"] = report.get("fan_mm", 0.0) + float(dc[fan].sum())
            if np.median(hw) > 1.0:
                _short_stitches(c, L, R, plain & ~ext)
            # split where the centreline leaves the shape (or is cut away by a mitre)
            seg_ids = np.cumsum(~keep)
            for sid in np.unique(seg_ids[keep]):
                sel = np.nonzero(keep & (seg_ids == sid))[0]
                if len(sel) >= 2:
                    pieces.append((c[sel], L[sel], R[sel], hw[sel]))

    runs: list[np.ndarray] = []
    pos = start
    while pieces:
        if pos is None:
            i, flip = 0, False
        else:
            ds = [(min(np.hypot(*(p[0][0] - pos)), 1e9), j, False) for j, p in enumerate(pieces)]
            ds += [(np.hypot(*(p[0][-1] - pos)), j, True) for j, p in enumerate(pieces)]
            _, i, flip = min(ds)
        c, L, R, hw = pieces.pop(i)
        if flip:
            c, L, R, hw = c[::-1], R[::-1], L[::-1], hw[::-1]
        seq = []
        if s.underlay:
            seq.append(resample(c, 1.8))  # centre walk out
            if np.median(hw) > 1.2:  # zigzag underlay back
                zi = np.arange(len(c) - 1, -1, -1)[:: max(1, int(round(2.0 / s.satin_spacing)))]  # ~1 mm apart
                ins = 0.55
                zl = c[zi] + (L[zi] - c[zi]) * ins
                zr = c[zi] + (R[zi] - c[zi]) * ins
                z = np.empty((2 * len(zi), 2))
                z[0::2], z[1::2] = zl, zr
                seq.append(z)
                seq.append(c[:1])
            else:  # odd number of underlay passes: sew the satin back towards the start
                c, L, R = c[::-1], R[::-1], L[::-1]
        z = np.empty((len(c), 2))
        z[0::2], z[1::2] = L[0::2], R[1::2]
        seq.append(z)
        run = np.vstack(seq)
        runs.append(run)
        pos = run[-1]
    return runs


def coverage(runs: list[np.ndarray], mask: np.ndarray, ox: int, oy: int, ppmm: float, thick: float) -> float:
    canvas = np.zeros(mask.shape, np.uint8)
    t = max(1, int(round(thick * ppmm)))
    for r in runs:
        px = np.round(r * ppmm - (ox + 0.5, oy + 0.5)).astype(np.int32)
        cv2.polylines(canvas, [px.reshape(-1, 1, 2)], False, 1, t)
    m = mask > 0
    return float((canvas[m] > 0).mean()) if m.any() else 1.0


# ===================================================================== running / bean
def bean_runs(paths_mm, stitch_len: float, start: np.ndarray | None, triple: bool = True) -> list[np.ndarray]:
    lines = []
    for pts, closed in paths_mm:
        if len(pts) == 1:
            lines.append(np.vstack([pts, pts + (0.3, 0)]))
            continue
        sm = gaussian_filter1d(pts, 1.5, axis=0, mode="wrap" if closed else "nearest") if len(pts) > 4 else pts
        r = follow(sm, stitch_len, closed)
        if len(r) >= 2:
            lines.append(r)
    runs, pos = [], start
    while lines:
        if pos is None:
            i, flip = 0, False
        else:
            ds = [(np.hypot(*(l[0] - pos)), j, False) for j, l in enumerate(lines)]
            ds += [(np.hypot(*(l[-1] - pos)), j, True) for j, l in enumerate(lines)]
            _, i, flip = min(ds)
        l = lines.pop(i)
        if flip:
            l = l[::-1]
        if triple:
            out = [l[0]]
            for a, b in zip(l[:-1], l[1:]):
                out += [b, a, b]
            l = np.array(out)
        runs.append(l)
        pos = l[-1]
    return runs


# ===================================================================== outlines (borders, appliqué)
def ring_paths(geom, holes: bool = True) -> list[tuple[np.ndarray, bool]]:
    out = []
    for p in _polys(geom):
        rings = [p.exterior] + (list(p.interiors) if holes else [])
        out += [(np.asarray(r.coords)[:-1], True) for r in rings if r.length > 1.0]
    return out


def outline_satin(geom, width: float, s, start, holes: bool = True) -> list[np.ndarray]:
    """Satin band of the given width centred on the outline (patch edges, appliqué cover)."""
    from dataclasses import replace

    paths = ring_paths(geom, holes)
    if not paths:
        return []
    band = unary_union([LineString(np.vstack([p, p[:1]])).buffer(width / 2) for p, _ in paths])
    ss = replace(s, satin_max_width=width + 1.0, pull_comp=s.pull_comp)
    return satin_runs(paths, band, lambda pts: np.full(len(pts), width / 2), ss, start)


def outline_run(geom, stitch_len: float, start, inset: float = 0.0, holes: bool = False,
                triple: bool = False) -> list[np.ndarray]:
    g = geom.buffer(-inset) if inset else geom
    return bean_runs(ring_paths(g, holes), stitch_len, start, triple=triple)


# ===================================================================== sequencing
class TravelArea:
    """Where travel stitches may go for one region: its (slightly grown) inside and its outlines."""

    def __init__(self, geom):
        self.inside = geom.buffer(0.25)
        shapely.prepare(self.inside)
        self.rings = [r for p in _polys(geom) for r in (p.exterior, *p.interiors)]


class Sequencer:
    """Turns runs into machine commands with tie-ins, tie-offs, travel and trims."""

    def __init__(self, s, max_stitch: float = 7.0):
        self.s = s
        self.max_stitch = max_stitch
        self.cmds: list[tuple[float, float, int]] = []
        self.pos: np.ndarray | None = None
        self.last_dir = np.array([1.0, 0.0])
        self.cut = True  # thread is cut (or about to jump): next run starts with a jump + tie-in
        self.router = None  # routing.Router: hidden travel under stitching still to come
        self.covered: list[tuple[int, int]] = []  # command ranges that end up underneath other stitches

    def _stitch_to(self, p: np.ndarray):
        if self.pos is not None:
            d = p - self.pos
            dist = float(np.hypot(*d))
            if dist < 0.2:  # too-short stitches shred thread
                return
            n = int(math.ceil(dist / self.max_stitch))
            for i in range(1, n):
                q = self.pos + d * (i / n)
                self.cmds.append((q[0], q[1], STITCH))
            self.last_dir = d / dist
        self.cmds.append((p[0], p[1], STITCH))
        self.pos = p

    def _tie(self, p: np.ndarray, direction: np.ndarray):
        u = direction / max(1e-9, float(np.hypot(*direction)))
        q = p + u * 0.6
        for pt in (q, p, q, p):
            self.cmds.append((pt[0], pt[1], STITCH))

    def tie_off(self):
        if self.pos is not None and self.cmds and self.cmds[-1][2] == STITCH:
            self._tie(self.pos, -self.last_dir)

    def trim(self):
        if self.cmds and self.cmds[-1][2] == STITCH and not self.cut:
            self.tie_off()
            if getattr(self.s, "trims", True):  # machines without a cutter just jump
                self.cmds.append((self.pos[0], self.pos[1], TRIM))
        self.cut = True

    def stop(self):
        """Pause the machine (appliqué: place or trim fabric)."""
        self.trim()
        if self.cmds:
            self.cmds.append((self.pos[0], self.pos[1], STOP))

    def color_change(self):
        self.trim()
        if self.cmds:
            self.cmds.append((self.pos[0], self.pos[1], COLOR_CHANGE))

    def _start_at(self, p: np.ndarray, nxt: np.ndarray | None):
        self.cmds.append((p[0], p[1], JUMP))
        self.pos = p
        self.cut = False
        self._tie(p, (nxt - p) if nxt is not None and np.hypot(*(nxt - p)) > 1e-6 else np.array([1.0, 0.0]))

    def _travel(self, a: np.ndarray, b: np.ndarray, area: "TravelArea | None") -> np.ndarray | None:
        """Hidden-ish route from a to b inside the shape: straight, else along its outline."""
        if area is None:
            return None
        d = float(np.hypot(*(b - a)))
        if d < 25 and area.inside.contains(LineString([a, b])):
            straight = resample(np.array([a, b]), 2.5)
            # a shortcut across rows this colour has already sewn would show on top of them
            if self.router is None or self.router.exposed(straight) <= 3.0:
                return straight
        pa, pb = shapely.points(a), shapely.points(b)
        for ring in area.rings:
            if ring.distance(pa) > 0.6 or ring.distance(pb) > 0.6:
                continue
            L = ring.length
            ta, tb = ring.project(pa), ring.project(pb)
            fwd = (tb - ta) % L
            if min(fwd, L - fwd) > 3 * d + 15:
                break
            # follow the outline closely: chords between far-apart points would cut across notches
            # (beside the crossbar of a small t) where they show
            ts = ta + (np.linspace(0, fwd, max(2, int(fwd / 0.1) + 2)) if fwd <= L - fwd
                       else -np.linspace(0, L - fwd, max(2, int((L - fwd) / 0.1) + 2)))
            route = np.array([ring.interpolate(t % L).coords[0] for t in ts])
            return np.vstack([a, follow(route, 2.0, tol=0.08), b])
        return None

    def add_run(self, pts: np.ndarray, area: "TravelArea | None" = None, shows: bool = True):
        """Sew a run. shows=False for underlay: it will be covered, so travel may cross it."""
        if len(pts) == 0:
            return
        p0 = pts[0]
        nxt = pts[1] if len(pts) > 1 else None
        gap = float(np.hypot(*(p0 - self.pos))) if self.pos is not None else 0.0
        if self.pos is None or self.cut:
            self._start_at(p0, nxt)
        # a short hop is sewn straight across only inside the shape: between small letters it would show
        elif gap > 1.5 or gap > 0.5 and (area is None or not area.inside.contains(LineString([self.pos, p0]))):
            route, hidden = None, False
            if self.router is not None:  # first choice: hidden under stitching still to come
                route = self.router.route(self.pos, p0, max_len=min(3 * gap + 20, 80.0))
                hidden = route is not None
            if route is None:
                route = self._travel(self.pos, p0, area)
            if route is None:
                self.trim()
                self._start_at(p0, nxt)
            else:
                i0 = len(self.cmds)
                for q in route[1:-1]:
                    self._stitch_to(q)
                if hidden:
                    self.covered.append((i0, len(self.cmds)))
        i0 = len(self.cmds)
        for p in pts:
            self._stitch_to(np.asarray(p, float))
        if not shows:
            self.covered.append((i0, len(self.cmds)))
        if shows and self.router is not None:
            self.router.mark(np.asarray(pts, float))

    def result(self) -> np.ndarray:
        return np.array(self.cmds, np.float64).reshape(-1, 3)

    def under(self) -> np.ndarray:
        """Per command: True where the stitch ends up underneath others (for the preview)."""
        m = np.zeros(len(self.cmds), bool)
        for a, b in self.covered:
            m[a:b] = True
        return m
