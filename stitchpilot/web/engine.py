"""The web app's conversion engine: the desktop app's logic without any window.

State is the same dict the desktop app saves in projects (MainWindow.get_state), so a project moves
freely between the desktop and the web app. Functions here that use Qt (lettering, text clean-up,
PDFs) run in worker processes only; see server.py.
"""
from __future__ import annotations

import io
import os
import tempfile
from dataclasses import asdict, replace
from pathlib import Path

import numpy as np

from .. import cutfiles
from .. import stitches as st
from ..digitize import ColorBlock, Design, digitize
from ..export import FORMATS, THREAD_CHARTS, export, match_threads
from ..machines import DEFAULT_MACHINE, MACHINES, hoops_for
from ..prep import rgb_to_lab
from ..settings import DEFAULT_FABRIC, DEFAULT_HOOP, FABRICS, HOOPS, Settings, ShapeEdit, TextFix, TextItem

DEFAULT_SIMPLICITY = 35  # as the desktop app
MARGIN_MM = 5.0
MAX_SIZE_MM = 400.0
PREVIEW_PX = 1100  # longest side of the preview image


# ---------------------------------------------------------------- state -> settings
def _tup(v) -> tuple[int, int, int]:
    return tuple(int(max(0, min(255, x))) for x in list(v)[:3])


def _num(state: dict, key: str, default: float, lo: float, hi: float) -> float:
    try:
        v = float(state.get(key, default))
    except (TypeError, ValueError):
        v = default
    return max(lo, min(hi, v)) if np.isfinite(v) else default


def default_state() -> dict:
    return {"machine": DEFAULT_MACHINE, "hoop": DEFAULT_HOOP, "max_w": 90.0, "max_h": 90.0,
            "fabric": DEFAULT_FABRIC, "colors": 0, "background": "auto", "keep_inside": False,
            "simplicity": DEFAULT_SIMPLICITY, "angle": "follow", "underlay": True, "texts": [], "text_fixes": [],
            "border": "none", "border_rgb": [30, 30, 30], "border_w": 3.0, "border_off": 0.0, "trims": True,
            "rotate": 0, "mirror": False, "applique": [], "overrides": [], "hidden": [], "order": [],
            "chart": "Brother", "snap": False, "format": next(iter(FORMATS)), "fabric_rgb": [245, 243, 238],
            "shape_edits": []}


def _text(t: dict) -> TextItem:
    known = {k: t[k] for k in TextItem.__dataclass_fields__ if k in t}
    item = TextItem(**{**known, "text": str(known.get("text", ""))[:200], "rgb": _tup(known.get("rgb", (20, 20, 20)))})
    item.height_mm = max(4.0, min(150.0, float(item.height_mm)))
    item.arc = max(-300.0, min(300.0, float(item.arc)))
    return item


def settings_from_state(state: dict) -> Settings:
    """The digitizing settings for a state dict (as MainWindow.settings() does for its widgets)."""
    fabric = state.get("fabric") if state.get("fabric") in FABRICS else DEFAULT_FABRIC
    simp = _num(state, "simplicity", DEFAULT_SIMPLICITY, 0, 100)
    base = Settings().with_fabric(fabric).simplified(simp / 100)
    ang = state.get("angle", "follow")
    if not isinstance(ang, (int, float)) and ang not in ("follow", "vary"):
        ang = "follow"
    bg = state.get("background") if state.get("background") in ("auto", "ai", "keep") else "auto"
    s = replace(
        base,
        max_width_mm=_num(state, "max_w", 90, 10, MAX_SIZE_MM), max_height_mm=_num(state, "max_h", 90, 10, MAX_SIZE_MM),
        background=bg, remove_enclosed_bg=not state.get("keep_inside", False),
        fill_angle=None if isinstance(ang, str) else float(ang) % 180, fill_follow=ang == "follow",
        underlay=bool(state.get("underlay", True)), trims=bool(state.get("trims", True)),
        rotate=int(state.get("rotate", 0)) % 360 // 90 * 90, mirror=bool(state.get("mirror", False)),
        texts=[_text(t) for t in state.get("texts", [])[:12] if isinstance(t, dict)],
        text_fixes=[TextFix(str(f["text"])[:200], tuple(float(v) for v in f["box"][:4]), str(f.get("font", "Arial")),
                            bool(f.get("bold", True)), bool(f.get("italic", False)))
                    for f in state.get("text_fixes", [])[:12]],
        applique=[_tup(c) for c in state.get("applique", [])[:32]],
        border=state.get("border") if state.get("border") in ("none", "run", "satin") else "none",
        border_rgb=_tup(state.get("border_rgb", (30, 30, 30))),
        border_width=_num(state, "border_w", 3.0, 1.0, 10.0), border_offset=_num(state, "border_off", 0.0, 0.0, 10.0),
        color_order=[_tup(c) for c in state.get("order", [])[:64]],
        skip_colors=[_tup(c) for c in state.get("hidden", [])[:64]],
        shape_edits=[ShapeEdit(**{**{k: e[k] for k in ShapeEdit.__dataclass_fields__ if k in e},
                                  "rgb": _tup(e["rgb"])}) for e in state.get("shape_edits", [])[:500]],
    )
    # the finer simplify and stitch settings, when a desktop project set them
    for key, field, lo, hi in (("merge", "merge_de", 1, 40), ("min_shape", "min_shape_mm2", 0.1, 50),
                               ("smooth", "smooth", 0, 100), ("fill_spacing", "fill_spacing", 0.25, 1.0),
                               ("stitch_len", "fill_stitch_len", 1.5, 7.0), ("satin_spacing", "satin_spacing", 0.25, 1.0),
                               ("satin_max", "satin_max_width", 2.0, 12.0), ("pull", "pull_comp", 0.0, 1.0)):
        if key in state:
            v = _num(state, key, getattr(s, field), lo, hi)
            s = replace(s, **{field: v / 100 if key == "smooth" else v})
    if state.get("thin") in ("keep", "thicken", "remove"):
        s = replace(s, thin_lines=state["thin"])
    n = state.get("colors", 0)
    if n == "ai" and state.get("ai_palette"):
        pal = [_tup(c) for c in state["ai_palette"][:16]]
        s = replace(s, palette=pal, num_colors=len(pal))
    elif isinstance(n, int) and 0 < n <= 16:
        s = replace(s, num_colors=n)
    if state.get("ai_bg") and s.background == "auto":
        s = replace(s, background_rgb=_tup(state["ai_bg"]))
    return s


def digitize_key(image_id: str | None, s: Settings) -> str:
    """Everything the stitches depend on (colour-only edits don't need a re-conversion)."""
    return f"{image_id}|{asdict(s)!r}"


def hoop_mm(state: dict) -> tuple[float, float]:
    name = state.get("hoop")
    if name in HOOPS:
        return HOOPS[name]
    return (_num(state, "max_w", 90, 10, MAX_SIZE_MM) + 2 * MARGIN_MM,
            _num(state, "max_h", 90, 10, MAX_SIZE_MM) + 2 * MARGIN_MM)


# ---------------------------------------------------------------- pictures
def load_picture(data: bytes, name: str) -> np.ndarray:
    """Decode an uploaded picture (any format the desktop app reads) to RGBA."""
    from ..imageio import load_image

    suffix = Path(name).suffix.lower()[:8] or ".png"
    fd, tmp = tempfile.mkstemp(suffix=suffix)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        return load_image(tmp)
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def convert(rgba: np.ndarray | None, s: Settings) -> Design:
    """Digitize; drops the cropped source picture the desktop app keeps for display (it can be big)."""
    d = digitize(rgba, s)
    if d.prepared is not None:
        d.prepared = replace(d.prepared, source_rgba=np.zeros((1, 1, 4), np.uint8))
    return d


# ---------------------------------------------------------------- colour edits
def _lab(cols) -> np.ndarray:
    return rgb_to_lab(np.array(cols, np.uint8).reshape(-1, 1, 3)).reshape(-1, 3)


def nearest_key(rgb, keys, tol: float = 10.0):
    ks = [tuple(k) for k in keys]
    if not ks:
        return None
    d = np.linalg.norm(_lab(ks) - _lab([rgb])[0], axis=1)
    return ks[int(d.argmin())] if d.min() < tol else None


def apply_colors(base: Design, state: dict) -> Design:
    """Recolour, hide, order and merge threads (as MainWindow.refresh_colors), then name the threads."""
    overrides = {_tup(k): _tup(v) for k, v in state.get("overrides", [])[:64]}
    hidden = [_tup(c) for c in state.get("hidden", [])]
    order = [_tup(c) for c in state.get("order", [])]
    blocks = []
    for b in base.blocks:
        nb = replace(b)
        k = nearest_key(b.source_rgb, overrides)
        nb.rgb = overrides[k] if k else b.source_rgb
        nb.visible = nearest_key(b.source_rgb, hidden) is None
        blocks.append(nb)
    if order:
        first = {b.source_rgb: i for i, b in enumerate(base.blocks)}

        def rank(b):
            k = nearest_key(b.source_rgb, order)
            return order.index(k) if k else len(order) + first.get(b.source_rgb, 0)

        blocks.sort(key=rank)
    d = Design(blocks, base.width_mm, base.height_mm, base.prepared, list(base.notes), base.shapes)
    chart = state.get("chart") if state.get("chart") in THREAD_CHARTS else None
    match_threads(d, chart, bool(state.get("snap", False)))
    merged: list[ColorBlock] = []  # neighbours with the same colour become one thread
    for b in d.blocks:
        if merged and b.visible and merged[-1].visible and merged[-1].rgb == b.rgb:
            m = merged[-1]
            merged[-1] = replace(m, cmds=np.vstack([m.cmds, b.cmds]), area_mm2=m.area_mm2 + b.area_mm2,
                                 kinds={k: m.kinds.get(k, 0) + b.kinds.get(k, 0) for k in {*m.kinds, *b.kinds}},
                                 fabric=(m.fabric or []) + (b.fabric or []) or None,
                                 under=None if m.under is None or b.under is None
                                 else np.concatenate([m.under, b.under]))
        else:
            merged.append(b)
    d.blocks = merged
    return d


# ---------------------------------------------------------------- what the page shows
def preview(d: Design, state: dict, show_hoop: bool = True) -> tuple[bytes, dict]:
    """PNG of the sewn-out design and how to map its pixels back to millimetres."""
    import cv2

    from ..render import layout, render

    hoop = hoop_mm(state) if show_hoop else None
    x0, y0, x1, y1 = d.bounds()
    span = max(x1 - x0, y1 - y0, *(hoop or (0, 0))) + 6
    ppm = max(2.0, min(14.0, PREVIEW_PX / span))
    fabric = _tup(state.get("fabric_rgb", (245, 243, 238)))
    img = render(d, ppm, fabric, hoop_mm=hoop)
    W, H, (ox, oy) = layout(d, ppm, 3.0, hoop)
    ok, png = cv2.imencode(".png", cv2.cvtColor(img, cv2.COLOR_RGB2BGR))
    return png.tobytes(), {"ppm": ppm, "ox": ox, "oy": oy, "w": W, "h": H}


def summary(d: Design) -> dict:
    x0, y0, x1, y1 = d.bounds()
    w, h = x1 - x0, y1 - y0
    threads = []
    for i, b in enumerate(d.blocks):
        threads.append({"i": i, "rgb": list(b.rgb), "source": list(b.source_rgb), "thread": b.thread,
                        "stitches": b.stitch_count, "kinds": b.kinds, "visible": b.visible,
                        "applique": bool(b.kinds.get("appliqué")), "border": bool(b.kinds.get("border"))})
    return {"width_mm": round(w, 1), "height_mm": round(h, 1), "stitches": d.stitch_count,
            "colours": len(d.active), "trims": d.trim_count, "stops": d.stop_count,
            "minutes": round(d.est_minutes(), 1), "threads": threads, "notes": d.notes,
            "shapes": len(d.shapes), "applique_pieces": sum(len(r) for _, _, r in cutfiles.pieces(d))}


def shapes(d: Design, limit: int = 600) -> list[dict]:
    """Every shape with its outline, so the page can pick shapes itself (smallest shape under a click wins)."""
    out = []
    for sh in sorted(d.shapes, key=lambda sh: sh.area_mm2)[:limit]:
        if sh.geom is None or sh.geom.is_empty:
            continue
        rings = []
        for g in st._polys(sh.geom.simplify(0.2)):
            for r in (g.exterior, *g.interiors):
                if len(r.coords) >= 4:
                    rings.append(np.round(np.asarray(r.coords), 2).tolist())
        out.append({"rgb": list(sh.rgb), "kind": sh.kind, "auto_kind": sh.auto_kind, "pattern": sh.pattern,
                    "angle": sh.angle, "area_mm2": round(float(sh.area_mm2), 1), "width_mm": round(float(sh.width_mm), 1),
                    "fx": sh.fx, "fy": sh.fy, "edit": asdict(sh.edit) if sh.edit is not None else None,
                    "centre": [sh.fx * d.width_mm, sh.fy * d.height_mm], "rings": rings})
    return out


# ---------------------------------------------------------------- files
def safe_stem(name: str | None) -> str:
    stem = Path(name or "design").stem
    keep = "".join(c if c.isalnum() or c in "-_ " else "_" for c in stem).strip() or "design"
    return keep[:40]


def embroidery_file(d: Design, ext: str) -> bytes:
    if ext not in FORMATS.values():
        raise ValueError("Unknown file format.")
    fd, tmp = tempfile.mkstemp(suffix="." + ext)
    os.close(fd)
    try:
        export(d, tmp)
        return Path(tmp).read_bytes()
    finally:
        os.remove(tmp)


def cut_file(d: Design, ext: str) -> bytes:
    fd, tmp = tempfile.mkstemp(suffix="." + ("dxf" if ext == "dxf" else "svg"))
    os.close(fd)
    try:
        cutfiles.write(d, tmp)
        return Path(tmp).read_bytes()
    finally:
        os.remove(tmp)


def template_pdf(d: Design, hoop: tuple[float, float] | None, title: str, paper: str) -> bytes:
    """Real-size placement template (uses Qt: run in a worker process)."""
    from ..lettering import ensure_qt
    from ..template import write_template

    ensure_qt()
    fd, tmp = tempfile.mkstemp(suffix=".pdf")
    os.close(fd)
    try:
        write_template(tmp, d, hoop, title, "A4" if paper == "A4" else "Letter")
        return Path(tmp).read_bytes()
    finally:
        os.remove(tmp)


def project_file(state: dict, source_name: str | None, source_bytes: bytes | None,
                 size_mm: tuple[float, float] | None) -> bytes:
    from .. import project

    fd, tmp = tempfile.mkstemp(suffix=project.EXT)
    os.close(fd)
    try:
        project.save(tmp, state, source_name, source_bytes, None, size_mm)
        return Path(tmp).read_bytes()
    finally:
        os.remove(tmp)


def read_project(data: bytes) -> tuple[dict, str | None, bytes | None]:
    from .. import project

    fd, tmp = tempfile.mkstemp(suffix=project.EXT)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        p = project.load(tmp)
    finally:
        os.remove(tmp)
    return p.state, p.source_name, p.source_bytes


def options() -> dict:
    """Everything the page's menus offer."""
    from .. import patterns
    from ..lettering import FRAMES

    return {
        "machines": {n: {"format": m.format, "chart": m.chart, "hoops": hoops_for(n), "default_hoop": m.default_hoop}
                     for n, m in MACHINES.items()},
        "hoops": {k: list(v) for k, v in HOOPS.items()}, "fabrics": list(FABRICS), "formats": FORMATS,
        "charts": list(THREAD_CHARTS), "patterns": patterns.PATTERNS, "frames": FRAMES,
        "defaults": default_state(), "max_size_mm": MAX_SIZE_MM,
    }


# ---------------------------------------------------------------- lettering preview / fonts (Qt: worker only)
def text_preview(item: dict) -> bytes:
    import cv2

    from ..lettering import ensure_qt, render_text

    ensure_qt()
    t = _text(item)
    if not t.text.strip():
        return b""
    arr = render_text(t, 48)
    a = arr[..., 3:4].astype(np.float32) / 255
    rgb = (arr[..., :3] * a + 255 * (1 - a)).astype(np.uint8)
    ok, png = cv2.imencode(".png", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
    return png.tobytes()


def font_list() -> list[str]:
    from PySide6.QtGui import QFontDatabase

    from ..lettering import ensure_qt

    ensure_qt()
    skip = ("Marlett", "Symbol", "Webdings", "Wingdings", "MT Extra", "Segoe MDL2", "Segoe Fluent", "HoloLens")
    fams = [f for f in QFontDatabase.families() if not f.startswith("@") and not any(s in f for s in skip)
            and QFontDatabase.writingSystems(f) and QFontDatabase.WritingSystem.Latin in QFontDatabase.writingSystems(f)]
    return sorted(set(fams), key=str.lower)[:400]


def png_bytes(arr: np.ndarray) -> bytes:
    from PIL import Image

    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, "PNG")
    return buf.getvalue()
