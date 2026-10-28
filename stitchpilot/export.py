"""Machine file export and thread-chart matching (via pyembroidery)."""
from __future__ import annotations

from pathlib import Path

import numpy as np
import pyembroidery as pe

from . import stitches as st
from .digitize import ColorBlock, Design
from .prep import rgb_to_lab

# label shown to the user -> file extension
FORMATS: dict[str, str] = {
    "PES – Brother, Baby Lock, Bernette": "pes",
    "DST – Tajima (universal, most machines)": "dst",
    "JEF – Janome, Elna, Kenmore": "jef",
    "EXP – Melco, Bernina": "exp",
    "VP3 – Husqvarna Viking, Pfaff": "vp3",
    "XXX – Singer": "xxx",
    "PEC – Brother (older models)": "pec",
    "U01 – Barudan": "u01",
    "TBF – Tajima (with colours)": "tbf",
}
DEFAULT_FORMAT = "PES – Brother, Baby Lock, Bernette"

THREAD_CHARTS = {
    "Brother": pe.EmbThreadPec.get_thread_set,
    "Janome": pe.EmbThreadJef.get_thread_set,
    "Husqvarna Viking": pe.EmbThreadHus.get_thread_set,
    "Sew / generic": pe.EmbThreadSew.get_thread_set,
}


def _chart(name: str) -> list:
    return [t for t in THREAD_CHARTS[name]() if t is not None and t.color is not None]


def match_threads(design: Design, chart_name: str | None, snap: bool) -> None:
    """Label each block with the nearest thread from a chart (optionally recolouring to it)."""
    if not chart_name:
        for b in design.blocks:
            b.thread = ""
        return
    chart = _chart(chart_name)
    rgbs = np.array([[(t.color >> 16) & 255, (t.color >> 8) & 255, t.color & 255] for t in chart], np.uint8)
    labs = rgb_to_lab(rgbs.reshape(-1, 1, 3)).reshape(-1, 3)
    for b in design.blocks:
        lab = rgb_to_lab(np.array(b.rgb, np.uint8).reshape(1, 1, 3)).reshape(3)
        i = int(np.linalg.norm(labs - lab, axis=1).argmin())
        t = chart[i]
        code = f" #{t.catalog_number}" if getattr(t, "catalog_number", None) else ""
        b.thread = f"{t.description}{code}"
        if snap:
            b.rgb = tuple(int(v) for v in rgbs[i])


def to_pattern(design: Design) -> pe.EmbPattern:
    pattern = pe.EmbPattern()
    x0, y0, x1, y1 = design.bounds()
    cx, cy = (x0 + x1) / 2, (y0 + y1) / 2  # machines centre the design on the hoop
    for bi, b in enumerate(design.active):
        thread = pe.EmbThread()
        thread.set_color(*b.rgb)
        thread.description = b.thread or "#%02x%02x%02x" % b.rgb
        pattern.add_thread(thread)
        if bi > 0:
            pattern.add_command(pe.COLOR_CHANGE)
        for x, y, f in b.cmds:
            X, Y = (x - cx) * 10.0, (y - cy) * 10.0  # pyembroidery units are 0.1 mm
            if f == st.STITCH:
                pattern.add_stitch_absolute(pe.STITCH, X, Y)
            elif f == st.JUMP:
                pattern.add_stitch_absolute(pe.JUMP, X, Y)
            elif f == st.TRIM:
                pattern.add_command(pe.TRIM)
            elif f == st.STOP:
                pattern.add_command(pe.STOP)
    pattern.add_command(pe.END)
    return pattern


# embroidery files we can open (to view, recolour, or convert to another format)
READ_EXTS = sorted({f["extension"] for f in pe.supported_formats()
                    if f.get("reader") and f.get("category") in ("embroidery", "stitch")})


def read_design(path: str | Path) -> Design:
    """Load an existing embroidery file as a Design (no source picture)."""
    pattern = pe.read(str(path))
    if pattern is None or pattern.count_stitches() == 0:
        raise ValueError("This embroidery file has no stitches or couldn't be read.")
    blocks = []
    flag = {pe.STITCH: st.STITCH, pe.JUMP: st.JUMP, pe.TRIM: st.TRIM, pe.STOP: st.STOP}
    for stitches, thread in pattern.get_as_colorblocks():
        rows = []
        for x, y, cmd in stitches:
            f = flag.get(cmd & pe.COMMAND_MASK)
            if f is not None:
                rows.append((x / 10.0, y / 10.0, f))
        if not any(r[2] == st.STITCH for r in rows):
            continue
        t = thread if thread is not None else pe.EmbThread()
        rgb = (t.get_red(), t.get_green(), t.get_blue()) if t.color is not None else (40, 40, 40)
        b = ColorBlock(rgb, np.array(rows, np.float64), 0.0, {})
        b.thread = (t.description or "").strip()
        blocks.append(b)
    allp = np.vstack([b.cmds[:, :2] for b in blocks])
    mn = allp.min(0)
    for b in blocks:
        b.cmds[:, :2] -= mn
    w, h = allp.max(0) - mn
    return Design(blocks, float(w), float(h), None)


def export(design: Design, path: str | Path) -> Path:
    path = Path(path)
    ext = path.suffix.lower().lstrip(".")
    pattern = to_pattern(design)
    settings = {"version": 6} if ext == "pes" else {}  # v6 keeps thread colours and names
    pe.write(pattern, str(path), settings)
    return path
