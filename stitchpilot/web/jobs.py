"""Work done in the server's worker processes: anything slow (digitizing) or using Qt (lettering,
text clean-up, PDFs). Functions here must be importable top-level functions so they can be pickled."""
from __future__ import annotations

import os
import sys
import warnings

MAX_PIXELS = 40_000_000  # bigger pictures are refused (and are pointless for embroidery)


def init_worker() -> None:
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    if sys.platform == "win32":
        os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
    from PIL import Image

    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    warnings.simplefilter("error", Image.DecompressionBombWarning)  # refuse, don't just warn


def probe(data: bytes, name: str) -> dict:
    """Check an upload: a picture (its size) or a project (its state and picture)."""
    from . import engine

    if name.lower().endswith(".stitchpilot"):
        state, src_name, src = engine.read_project(data)
        info = {"project": True, "state": state, "source_name": src_name, "source": src}
        if src is not None:
            rgba = engine.load_picture(src, src_name or "picture.png")
            info.update(width=int(rgba.shape[1]), height=int(rgba.shape[0]))
        return info
    rgba = engine.load_picture(data, name)
    if rgba.shape[0] * rgba.shape[1] > MAX_PIXELS:
        raise ValueError("This picture is too big.")
    return {"project": False, "width": int(rgba.shape[1]), "height": int(rgba.shape[0])}


def convert(data: bytes | None, name: str | None, state: dict):
    from . import engine

    rgba = engine.load_picture(data, name or "picture.png") if data is not None else None
    return engine.convert(rgba, engine.settings_from_state(state))


def text_preview(item: dict) -> bytes:
    from . import engine

    return engine.text_preview(item)


def fonts() -> list[str]:
    from . import engine

    return engine.font_list()


def template(design, hoop, title: str, paper: str) -> bytes:
    from . import engine

    return engine.template_pdf(design, hoop, title, paper)


def ai_settings(data: bytes, name: str, state: dict, key: str) -> dict:
    from .. import ai
    from . import engine

    rgba = engine.load_picture(data, name)
    s = engine.settings_from_state(state)
    a = ai.analyse(rgba, s, key)
    s2 = ai.apply(a, s)
    return {"subject": a.subject, "suitability": a.suitability, "tips": a.tips[:4], "min_width_mm": a.min_width_mm,
            "changes": {"ai_palette": [list(c) for c in s2.palette or []], "colors": "ai" if s2.palette else 0,
                        "simplicity": int(round((1 - s2.detail) * 100)), "background": s2.background,
                        "keep_inside": not s2.remove_enclosed_bg,
                        "ai_bg": list(s2.background_rgb) if s2.background_rgb else None}}


def ai_text(data: bytes, name: str, key: str) -> list[dict]:
    from .. import ai, textfix
    from . import engine

    rgba = engine.load_picture(data, name)
    flat = textfix.flat_rgb(rgba)
    rows = []
    for t, box in ai.find_text(rgba, key):
        row = {"text": t.text, "fix": None, "score": 0.0, "note": ""}
        if t.curved:
            row["note"] = "curved text can't be redrawn yet; it stays as it is"
        else:
            m = textfix.measure(flat, box)
            if m is None:
                row["note"] = "couldn't find these letters clearly enough"
            else:
                font, bold, score = textfix.match_font(t.text, m, t.style, t.font_guess, t.italic)
                row.update(fix={"text": t.text, "box": list(box), "font": font, "bold": bold, "italic": t.italic},
                           score=round(score, 2))
        rows.append(row)
    return rows
