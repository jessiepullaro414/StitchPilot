"""Image -> stitches. Ties preparation, region analysis and stitch generation together."""
from __future__ import annotations

from dataclasses import dataclass, field, replace

import cv2
import numpy as np
from shapely.geometry import Point, Polygon
from shapely.ops import nearest_points, unary_union

from . import patterns as pat
from . import stitches as st
from .routing import CoverMap
from .prep import Prepared, Progress, prepare
from .settings import Settings, ShapeEdit

FILL_ANGLES = [45, 135, 15, 105, 75, 165, 0, 90]


@dataclass
class ColorBlock:
    rgb: tuple[int, int, int]
    cmds: np.ndarray  # (N,3): x mm, y mm, flag (stitches.STITCH/JUMP/TRIM)
    area_mm2: float
    kinds: dict[str, int] = field(default_factory=dict)
    thread: str = ""  # matched thread name, if any
    visible: bool = True
    source_rgb: tuple[int, int, int] | None = None  # colour picked from the image (before edits)
    fabric: list[np.ndarray] | None = None  # appliqué fabric outline rings (mm), for the preview
    under: np.ndarray | None = None  # per command: covered by later stitches (preview draws it first)

    def __post_init__(self):
        if self.source_rgb is None:
            self.source_rgb = self.rgb

    @property
    def stitch_count(self) -> int:
        return int((self.cmds[:, 2] == st.STITCH).sum()) if len(self.cmds) else 0


@dataclass
class Shape:
    """One connected area of one colour, as digitized: what shape editing works on."""
    rgb: tuple[int, int, int]  # image colour (ColorBlock.source_rgb)
    geom: object  # shapely geometry (mm), for picking and outlines
    kind: str  # stitch type used: "fill", "satin", "run", "outline" or "skip"
    auto_kind: str  # what StitchPilot would choose on its own
    angle: float | None  # fill angle used (fill only)
    area_mm2: float
    width_mm: float  # typical stroke width
    fx: float  # a point well inside the shape, as a fraction of the design size
    fy: float
    edit: ShapeEdit | None = None
    pattern: str = "plain"  # fill pattern used (fill only)

    @property
    def anchor(self) -> tuple[float, float]:
        return self.fx, self.fy


@dataclass
class Design:
    blocks: list[ColorBlock]
    width_mm: float
    height_mm: float
    prepared: Prepared | None  # None for designs opened from an embroidery file
    notes: list[str] = field(default_factory=list)
    shapes: list[Shape] = field(default_factory=list)  # empty for embroidery files

    def shape_at(self, x: float, y: float, slack: float = 0.4) -> Shape | None:
        """The shape under a point (mm); where shapes overlap or nearly touch, the smallest wins."""
        from shapely.geometry import Point as _P

        p = _P(x, y)
        hits = [sh for sh in self.shapes if sh.geom is not None and sh.geom.distance(p) <= slack]
        return min(hits, key=lambda sh: sh.area_mm2) if hits else None

    def find_shape(self, rgb, fx: float, fy: float) -> Shape | None:
        """The shape of this colour at a remembered point (e.g. after re-converting)."""
        x, y = fx * self.width_mm, fy * self.height_mm
        same = [sh for sh in self.shapes if sum((a - b) ** 2 for a, b in zip(sh.rgb, rgb)) < 300]
        from shapely.geometry import Point as _P

        p = _P(x, y)
        best = min(same, key=lambda sh: sh.geom.distance(p), default=None) if same else None
        return best if best is not None and best.geom.distance(p) < 2.0 else None

    @property
    def active(self) -> list[ColorBlock]:
        return [b for b in self.blocks if b.visible and len(b.cmds)]

    @property
    def stitch_count(self) -> int:
        return sum(b.stitch_count for b in self.active)

    @property
    def trim_count(self) -> int:
        return sum(int((b.cmds[:, 2] == st.TRIM).sum()) for b in self.active)

    @property
    def stop_count(self) -> int:
        return sum(int((b.cmds[:, 2] == st.STOP).sum()) for b in self.active)

    def est_minutes(self, spm: int = 650) -> float:
        n = len(self.active)
        return (self.stitch_count / spm + max(0, n - 1) * 0.5 + self.trim_count * 0.08
                + self.stop_count * 1.5)

    def bounds(self) -> tuple[float, float, float, float]:
        pts = [b.cmds[b.cmds[:, 2] == st.STITCH, :2] for b in self.active]
        pts = [p for p in pts if len(p)]
        if not pts:
            return 0, 0, self.width_mm, self.height_mm
        a = np.vstack(pts)
        return float(a[:, 0].min()), float(a[:, 1].min()), float(a[:, 0].max()), float(a[:, 1].max())


@dataclass
class _Region:
    color: int
    mask: np.ndarray
    ox: int
    oy: int
    area_mm2: float
    kind: str = "fill"
    geom: object = None
    paths: list = field(default_factory=list)  # (pts mm, closed)
    w90: float = 0.0  # typical stroke width (mm)
    anchor: tuple[float, float] = (0.5, 0.5)  # a point well inside, as a fraction of the design size
    edit: ShapeEdit | None = None
    hints: list = field(default_factory=list)  # per path: satin clips and corners at junctions (mm)
    dt: np.ndarray | None = None


def _analyse(r: _Region, ppmm: float, s: Settings) -> None:
    dt = cv2.distanceTransform(r.mask, cv2.DIST_L2, 5)
    r.dt = dt
    polys = st.mask_to_polygons(r.mask, r.ox, r.oy, ppmm, s.min_region_mm2)
    r.geom = unary_union(polys) if polys else None
    skel = st.join_through(st.skeleton_paths(r.mask, dt), dt, overlap=0.3 * ppmm)
    widths, length = [], 0.0
    off = np.array([r.ox + 0.5, r.oy + 0.5])
    for pts, closed, free0, free1, cut0, cut1, clips, corners in skel:
        ix = pts.astype(int)
        hw = dt[ix[:, 1], ix[:, 0]]
        widths.append((2 * hw - 1) / ppmm)
        # a branch stops at the edge of the stroke it joins, tucked 0.3 mm underneath
        pts = _cut_back(pts, cut0 - 0.3 * ppmm, cut1 - 0.3 * ppmm)
        mm = (pts + (r.ox + 0.5, r.oy + 0.5)) / ppmm
        if len(mm) >= 2 and not closed:  # skeletons stop short of the tips: extend free ends
            for end, free in ((0, free0), (-1, free1)):
                if not free:
                    continue
                other = mm[min(4, len(mm) - 1)] if end == 0 else mm[max(-5, -len(mm))]
                d = mm[end] - other
                n = np.hypot(*d)
                if n > 1e-6:
                    ext = mm[end] + d / n * (hw[end] / ppmm)
                    mm = np.vstack([ext, mm]) if end == 0 else np.vstack([mm, ext])
        if len(mm) > 1:
            length += float(np.linalg.norm(np.diff(mm, axis=0), axis=1).sum())
        r.paths.append((mm, closed))
        r.hints.append({"clips": [((q + off) / ppmm, nrm, rad / ppmm) for q, nrm, rad in clips],
                        "corners": [(q + off) / ppmm for q in corners]})
    w = np.concatenate(widths) if widths else np.array([0.0])
    w90 = float(np.percentile(w, 90))
    r.w90 = w90
    if r.geom is None or r.geom.is_empty or w90 < 1.0:
        r.kind = "run"
    elif w90 <= s.satin_max_width and length >= 1.2 * w90:
        r.kind = "satin"
    else:
        r.kind = "fill"


def _cut_back(pts: np.ndarray, cut0: float, cut1: float) -> np.ndarray:
    """Shorten a polyline by cut0 at its start and cut1 at its end (px); never below a third of it."""
    if len(pts) < 3 or (cut0 <= 0 and cut1 <= 0):
        return pts
    arc = np.concatenate([[0], np.cumsum(np.linalg.norm(np.diff(pts, axis=0), axis=1))])
    total = arc[-1]
    a, b = max(0.0, cut0), max(0.0, cut1)
    if a + b > total * 2 / 3:
        a, b = a * total * 2 / 3 / (a + b), b * total * 2 / 3 / (a + b)
    keep = (arc >= a) & (arc <= total - b)
    return pts[keep] if keep.sum() >= 2 else pts


def _halfwidth_fn(r: _Region, ppmm: float):
    h, w = r.dt.shape
    dt = cv2.dilate(r.dt, np.ones((3, 3), np.uint8))  # tolerate centreline smoothing

    def f(pts_mm: np.ndarray) -> np.ndarray:
        c = np.clip(np.round(pts_mm[:, 0] * ppmm - 0.5 - r.ox).astype(int), 0, w - 1)
        rr = np.clip(np.round(pts_mm[:, 1] * ppmm - 0.5 - r.oy).astype(int), 0, h - 1)
        return np.maximum(0.25, (dt[rr, c] - 0.5) / ppmm)

    return f


def shape_angle(mask: np.ndarray, fallback: float, min_elongation: float = 1.4) -> float:
    """Fill angle along the shape's length (its long axis), so rows follow the shape. Roundish shapes
    have no clear length and keep the fallback angle."""
    m = cv2.moments(mask.astype(np.uint8), binaryImage=True)
    if m["m00"] < 4:
        return fallback
    a, b, c = m["mu20"] / m["m00"], m["mu11"] / m["m00"], m["mu02"] / m["m00"]
    spread = np.sqrt(((a - c) / 2) ** 2 + b * b)
    big, small = (a + c) / 2 + spread, (a + c) / 2 - spread
    if small <= 1e-9 or np.sqrt(big / small) < min_elongation:
        return fallback
    return float(np.degrees(0.5 * np.arctan2(2 * b, a - c))) % 180


def _area(geom):
    return st.TravelArea(geom) if geom is not None and not geom.is_empty else None


def _wanted(r: _Region) -> str:
    """The stitch type to use: the user's choice, else the automatic one."""
    return r.edit.kind if r.edit is not None and r.edit.kind != "auto" else r.kind


def _pattern(r: _Region) -> str:
    return r.edit.pattern if r.edit is not None and r.edit.pattern in pat.PATTERNS else "plain"


def _hides(r: _Region) -> bool:
    """Whether the region's stitching will hide travel run underneath it."""
    kind = _wanted(r)
    return r.geom is not None and (kind == "satin" or kind == "fill" and _pattern(r) not in pat.OPEN)


def _region_runs(r: _Region, ppmm: float, s: Settings, angle: float, pos, exit=None) -> tuple[list, str]:
    """Return ([(runs, travel area), ...], kind actually used). A fill tries to finish near `exit`."""
    kind = _wanted(r)
    k = max(0.5, min(2.0, r.edit.density)) if r.edit is not None else 1.0
    if k != 1.0:
        s = replace(s, fill_spacing=s.fill_spacing / k, satin_spacing=s.satin_spacing / k)
    if kind == "outline":
        if r.geom is not None:
            # nothing fills the inside, so travel across it would show: no travel area
            return [(st.outline_run(r.geom, 2.0, pos, holes=True, triple=True), None)], "outline"
        kind = "run"
    if kind == "run" or (kind == "satin" and r.geom is None):
        return [(st.bean_runs(r.paths, s.run_stitch_len, pos), _area(r.geom))], "run"
    if kind == "satin" and r.paths:
        forced = _wanted(r) != r.kind
        if forced:  # the user asked for satin: let the columns be as wide as the shape (within reason)
            s = replace(s, satin_max_width=min(12.0, max(s.satin_max_width, r.w90 + 0.5)))
        runs = st.satin_runs(r.paths, r.geom, _halfwidth_fn(r, ppmm), s, pos, r.hints)
        # satin can't follow blobby or branching shapes well: fall back to fill if it leaves gaps
        if runs and (forced or st.coverage(runs, r.mask, r.ox, r.oy, ppmm, s.satin_spacing * 1.5) >= 0.85):
            return [(runs, _area(r.geom))], "satin"
    pattern = _pattern(r)
    if pattern in pat.OPEN:  # open decorative fill: no underlay, and travel across it would show
        top = r.geom.buffer(s.pull_comp / 4)
        runs = (pat.cross_runs(top, pat.CROSS_MM / k ** 0.5, pos) if pattern == "cross"
                else pat.motif_runs(top, pattern, pat.MOTIF_MM / k ** 0.5, pos))
        if runs:
            return [(runs, None)], "fill"
    groups = []
    entry = pos
    if s.underlay and r.area_mm2 > 6:
        inner = r.geom.buffer(-0.5)
        if not inner.is_empty and inner.area > 2:
            runs = st.fill_runs(inner, angle + 90, 2.0, 3.0, pos)
            if runs:
                groups.append((runs, _area(inner)))
                pos = runs[-1][-1]
    top = r.geom.buffer(s.pull_comp / 2)
    if exit is not None and entry is not None:
        # start the top layer back where the thread came in (getting there is hidden under it), so
        # it can sweep across the shape and finish on the side the thread leaves from
        pos = entry
    if pattern == "waves":
        groups.append((pat.wave_runs(top, angle, s.fill_spacing, pos, exit), _area(top)))
    else:
        groups.append((st.fill_runs(top, angle, s.fill_spacing, s.fill_stitch_len, pos, exit), _area(top)))
    return groups, "fill"


def digitize(rgba: np.ndarray, s: Settings, progress: Progress | None = None,
             prepared: Prepared | None = None) -> Design:
    prog = progress or (lambda f, m: None)
    prep = prepared or prepare(rgba, s, prog)
    labels, ppmm = _erase(prep.labels, prep.palette, s.erase), prep.ppmm

    # ---- split every colour into connected regions
    prog(0.45, "Analysing shapes…")
    regions: dict[int, list[_Region]] = {}
    min_px = 0.3 * ppmm * ppmm
    for c in range(len(prep.palette)):
        n, cc, stats, _ = cv2.connectedComponentsWithStats((labels == c).astype(np.uint8), connectivity=8)
        for i in range(1, n):
            x, y, w, h, a = stats[i]
            if a < min_px:
                continue
            x0, y0 = max(0, x - 1), max(0, y - 1)
            sub = (cc[y0:y + h + 1, x0:x + w + 1] == i).astype(np.uint8)
            regions.setdefault(c, []).append(_Region(c, sub, x0, y0, a / ppmm ** 2))
    total = sum(len(v) for v in regions.values()) or 1
    done = 0
    for rs in regions.values():
        for r in rs:
            _analyse(r, ppmm, s)
            y, x = np.unravel_index(int(np.argmax(r.dt)), r.dt.shape)  # the point deepest inside
            r.anchor = ((r.ox + x + 0.5) / labels.shape[1], (r.oy + y + 0.5) / labels.shape[0])
            done += 1
            prog(0.45 + 0.2 * done / total, "Analysing shapes…")
    _attach_edits(regions, prep.palette, s.shape_edits, labels.shape, ppmm)

    # ---- order colours: the user's order if they set one, else big background areas first,
    # fine detail last
    def auto_key(c):
        rs = regions[c]
        area = sum(r.area_mm2 for r in rs)
        detail = sum(r.area_mm2 for r in rs if r.kind == "run") / max(area, 1e-6)
        return (detail > 0.5, -area)

    user = _match(prep.palette, s.color_order)
    colours = sorted(regions, key=lambda c: (user[c] if user[c] is not None else len(s.color_order), auto_key(c)))
    skipped = _match(prep.palette, s.skip_colors)
    applique = {c: [r for r in regions[c] if r.geom is not None and r.area_mm2 >= 20]
                if _is_applique(prep.palette[c], s) else [] for c in colours}

    # ---- which regions will hide travel stitches: fills and satins that really get sewn
    flat = [(bi, r) for bi, c in enumerate(colours) for r in regions[c]]
    index = {id(r): i for i, (_, r) in enumerate(flat)}
    cover = CoverMap(labels.shape, ppmm, [r for _, r in flat], [bi for bi, _ in flat],
                     [_hides(r) and skipped[colours[bi]] is None and r not in applique[colours[bi]]
                      for bi, r in flat])
    shapes: list[Shape] = []

    blocks: list[ColorBlock] = []
    done = 0
    last = None
    for bi, c in enumerate(colours):
        seq = st.Sequencer(s)
        router = cover.router(bi)
        kinds: dict[str, int] = {}
        angle = s.fill_angle if s.fill_angle is not None else FILL_ANGLES[bi % len(FILL_ANGLES)]
        todo = list(regions[c])
        fabric = None
        big = applique[c]
        if big:
            # one solid fabric piece per shape; details in other colours are sewn on top of it
            geom = unary_union([Polygon(p.exterior) for r in big for p in st._polys(r.geom)])
            _applique(seq, geom, s)
            fabric = [p for p, _ in st.ring_paths(geom)]
            kinds["appliqué"] = len(big)
            todo = [r for r in todo if r not in big]
            done += len(big)
        for r in [r for r in todo if _wanted(r) == "skip"]:  # removed by the user: listed, not sewn
            shapes.append(_shape(r, prep.palette[c], "skip", None, ppmm))
            todo.remove(r)
            done += 1
        seq.router = router
        # shapes that can reach each other by hidden travel form a group: finish a group before
        # moving on, so the thread is only cut between groups
        group = dict(zip(map(id, todo), router.groups([index[id(r)] for r in todo])))
        here, cur = (seq.pos if seq.pos is not None else last), None
        while todo:
            if here is None:
                i = 0
            else:
                p = Point(here)
                i = min(range(len(todo)), key=lambda j: (
                    todo[j].edit.order if todo[j].edit is not None else 0,  # "sew first / last"
                    group[id(todo[j])] != cur,
                    todo[j].geom.distance(p) if todo[j].geom is not None
                    else min(np.hypot(*(q[0][0] - here)) for q in todo[j].paths) if todo[j].paths else 1e9))
            r = todo.pop(i)
            cur = group[id(r)]
            ang = shape_angle(r.mask, angle) if s.fill_angle is None and s.fill_follow else angle
            if r.edit is not None and r.edit.angle is not None:
                ang = float(r.edit.angle)
            groups, kind = _region_runs(r, ppmm, s, ang, here, _exit_towards(r, todo, group, cur, router, index[id(r)]))
            kinds[kind] = kinds.get(kind, 0) + 1
            directed = kind == "fill" and _pattern(r) not in pat.OPEN  # motifs and crosses have no direction
            shapes.append(_shape(r, prep.palette[c], kind, ang if directed else None, ppmm))
            for gi, (runs, area) in enumerate(groups):
                under = kind == "fill" and gi < len(groups) - 1  # fill underlay: the top layer hides it
                for run in runs:
                    seq.add_run(run, area, shows=not under)
            here = seq.pos
            done += 1
            prog(0.65 + 0.33 * done / total, "Generating stitches…")
        last = seq.pos
        seq.trim()
        cmds = seq.result()
        if len(cmds):
            blocks.append(ColorBlock(prep.palette[c], cmds, sum(r.area_mm2 for r in regions[c]), kinds,
                                     fabric=fabric, under=seq.under()))

    if s.border != "none":
        prog(0.99, "Adding border…")
        b = _border(prep.labels, ppmm, s)  # around the whole design, erased areas or not
        if b is not None:
            blocks.append(b)

    w_mm, h_mm = labels.shape[1] / ppmm, labels.shape[0] / ppmm
    design = Design(blocks, w_mm, h_mm, prep, shapes=shapes)
    design.notes = _notes(design, s)
    prog(1.0, "Done")
    return design


def _erase(labels: np.ndarray, palette, erase) -> np.ndarray:
    """Areas erased by hand (fractions of the design) taken out of their thread's colour, so the shapes
    and stitches around them are worked out afresh."""
    if not erase or not len(palette):
        return labels
    import shapely

    out = labels.copy()
    H, W = labels.shape
    pal = np.asarray(palette, float)
    for rgb, wkt in erase:
        c = int(np.argmin(((pal - np.asarray(rgb, float)) ** 2).sum(axis=1)))  # closest thread now
        area = shapely.from_wkt(wkt)
        mask = np.zeros((H, W), np.uint8)
        for poly in getattr(area, "geoms", [area]):
            if poly.geom_type != "Polygon" or poly.is_empty:
                continue
            ring = lambda r: (np.asarray(r.coords) * (W * 16, H * 16)).round().astype(np.int32)  # noqa: E731
            cv2.fillPoly(mask, [ring(poly.exterior)], 1, cv2.LINE_8, 4)
            for hole in poly.interiors:
                cv2.fillPoly(mask, [ring(hole)], 0, cv2.LINE_8, 4)
        out[(mask > 0) & (out == c)] = -1
    return out


def _attach_edits(regions: dict, palette, edits: list[ShapeEdit], shape_px, ppmm: float) -> None:
    """Find each edited shape again: same colour, containing (or within 2 mm of) its remembered point."""
    H, W = shape_px
    for e in edits:
        ci = _match(palette, [tuple(e.rgb)])
        cands = [r for c, rs in regions.items() if ci[c] == 0 for r in rs]
        if not cands:
            continue
        x, y = e.fx * W, e.fy * H  # px
        best, dist = None, 2.0 * ppmm
        for r in cands:
            h, w = r.mask.shape
            cx, cy = int(x) - r.ox, int(y) - r.oy
            if 0 <= cx < w and 0 <= cy < h and r.mask[cy, cx]:
                best, dist = r, 0.0
                break
            if r.geom is not None:
                d = r.geom.distance(Point(x / ppmm, y / ppmm)) * ppmm
                if d < dist:
                    best, dist = r, d
        if best is not None:
            best.edit = e


def _shape(r: _Region, rgb, kind: str, angle, ppmm: float) -> Shape:
    geom = r.geom
    if geom is None or geom.is_empty:  # hair-thin: pick it by its centreline
        from shapely.geometry import LineString, MultiLineString

        lines = [LineString(p) for p, _ in r.paths if len(p) >= 2]
        geom = MultiLineString(lines).buffer(0.4) if lines else None
    return Shape(tuple(int(v) for v in rgb), geom, kind, r.kind, angle, r.area_mm2, r.w90,
                 float(r.anchor[0]), float(r.anchor[1]), r.edit, _pattern(r) if kind == "fill" else "plain")


def _exit_towards(r: _Region, todo: list, group: dict, cur, router, i: int) -> np.ndarray | None:
    """Where to leave region r: on its edge where hidden travel can start, facing the nearest shape
    still to sew in its group."""
    if r.geom is None:
        return None
    nxt = [t for t in todo if group[id(t)] == cur and t.geom is not None]
    if not nxt:
        return None
    t = min(nxt, key=lambda t: r.geom.distance(t.geom))
    toward = np.asarray(nearest_points(t.geom, r.geom)[0].coords[0])
    p = router.exit_point(i, toward)
    return p if p is not None else np.asarray(nearest_points(r.geom, t.geom)[0].coords[0])


def _match(palette, keys, tol: float = 10.0) -> list[int | None]:
    """For each palette colour, the index of the closest key colour (within tol ΔE), else None."""
    if not keys:
        return [None] * len(palette)
    from .prep import rgb_to_lab

    lab = rgb_to_lab(np.array(palette, np.uint8).reshape(-1, 1, 3)).reshape(-1, 3)
    klab = rgb_to_lab(np.array(keys, np.uint8).reshape(-1, 1, 3)).reshape(-1, 3)
    d = np.linalg.norm(lab[:, None] - klab[None], axis=2)
    return [int(row.argmin()) if row.min() < tol else None for row in d]


def _is_applique(rgb, s: Settings) -> bool:
    if not s.applique:
        return False
    from .prep import rgb_to_lab

    lab = rgb_to_lab(np.array(rgb, np.uint8).reshape(1, 1, 3)).reshape(3)
    labs = rgb_to_lab(np.array(s.applique, np.uint8).reshape(-1, 1, 3)).reshape(-1, 3)
    return bool(np.linalg.norm(labs - lab, axis=1).min() < 12)


def _applique(seq: st.Sequencer, geom, s: Settings) -> None:
    """Placement line, stop (lay fabric), tack-down, stop (trim fabric), satin cover."""
    for run in st.outline_run(geom, 2.5, seq.pos):
        seq.add_run(run)
    seq.stop()
    for run in st.outline_run(geom, 2.5, seq.pos, inset=0.6, holes=True):
        seq.add_run(run)
    seq.stop()
    for run in st.outline_satin(geom, 3.0, s, seq.pos):
        seq.add_run(run)


def _border(labels: np.ndarray, ppmm: float, s: Settings) -> ColorBlock | None:
    """Patch edge / outline around the whole design, sewn last."""
    polys = st.mask_to_polygons((labels >= 0).astype(np.uint8), 0, 0, ppmm, 1e9)
    if not polys:
        return None
    # close small gaps so separate pieces get one smooth edge; the border sits just outside the
    # design, overlapping its edge by 0.5 mm so no fabric shows between them
    outside = s.border_offset + (s.border_width / 2 - 0.5 if s.border == "satin" else 0.6)
    geom = unary_union(polys).buffer(2.0).buffer(-2.0 + outside).simplify(0.05)
    seq = st.Sequencer(s)
    if s.border == "satin":
        runs = st.outline_satin(geom, s.border_width, s, None, holes=False)
    else:
        runs = st.outline_run(geom, 2.0, None, triple=True)
    for run in runs:
        seq.add_run(run)
    seq.trim()
    cmds = seq.result()
    if not len(cmds):
        return None
    return ColorBlock(tuple(s.border_rgb), cmds, 0.0, {"border": 1})


def _notes(d: Design, s: Settings) -> list[str]:
    from .analysis import dense_share

    notes = []
    share = dense_share(d)
    if share > 0.03:
        notes.append(f"{share:.0%} of the design is very dense (stitches piling up). That can break needles "
                     "or pucker fabric – check the Density view, try a lighter fabric preset or fewer overlaps.")
    if d.stitch_count > 60_000:
        notes.append(f"High stitch count ({d.stitch_count:,}). Consider fewer colours or a lower detail level.")
    if d.trim_count > 120:
        notes.append(f"{d.trim_count} trims – lots of small separate pieces. Lower the detail level to merge them.")
    runs = sum(b.kinds.get("run", 0) for b in d.blocks)
    if runs > 40:
        notes.append("Many hair-thin shapes are sewn as bean stitch. A larger design size will turn them into satin.")
    if len(d.blocks) > 12:
        notes.append(f"{len(d.blocks)} thread colours – home machines need a stop for each one.")
    return notes
