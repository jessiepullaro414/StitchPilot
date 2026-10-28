"""AI assist: Claude looks at the artwork and chooses digitizing settings like a human digitizer would."""
from __future__ import annotations

import base64
import io
import os
from dataclasses import replace
from typing import Literal

import anthropic
import numpy as np
from PIL import Image
from pydantic import BaseModel

from . import account
from .settings import Settings

MODEL = "claude-opus-5-5"

SYSTEM = """You are an expert machine-embroidery digitizer. You will be shown artwork that \
an automatic digitizer is about to convert into stitches for a home or commercial embroidery machine. \
Your job is to choose the settings a skilled human digitizer would choose.

How the digitizer works, so your answers are useful:
- It reduces the image to a small set of flat thread colours. You choose those colours. Merge \
shading, gradients, anti-aliasing and JPEG noise into the nearest flat colour; every colour you list \
costs the user a thread change, so only keep colours that matter visually. Most good designs use 2-8.
- List colours as they appear in the artwork (sampled, not idealised), as #rrggbb.
- It removes a plain background so it is not stitched. Tell it the background colour, and whether \
regions of that same colour enclosed inside the design (letter counters, gaps between shapes) are \
also background (true) or are deliberate parts of the design such as white eyes or white lettering \
that must be stitched (false).
- "detail" from 0 to 1: 0 merges small specks and simplifies boldly, 1 keeps fine detail. Line \
art and small text want higher detail; noisy photos want lower.
- Shapes narrower than about 1 mm can't be stitched well, and text shorter than about 5 mm \
becomes unreadable, so estimate the smallest overall width (mm) at which this design will sew cleanly.

Give at most four short, practical tips for this specific image (for example: simplify the \
background first, use a stabilizer suited to the design density, enlarge it, or the image is a \
photo and will look posterised). No generic advice."""


class ThreadColor(BaseModel):
    hex: str
    name: str
    role: Literal["main", "detail", "outline", "text", "highlight", "shadow"]


class Analysis(BaseModel):
    subject: str
    design_type: Literal["logo", "text", "clipart", "line_art", "illustration", "photo", "pattern"]
    background: Literal["plain", "transparent", "busy", "none"]
    background_hex: str | None
    background_inside_is_background: bool
    thread_colors: list[ThreadColor]
    detail: float
    min_width_mm: int
    suitability: Literal["great", "good", "fair", "poor"]
    tips: list[str]


class AIError(Exception):
    pass


def _hex(h: str | None) -> tuple[int, int, int] | None:
    if not h:
        return None
    h = h.strip().lstrip("#")
    if len(h) == 3:
        h = "".join(c * 2 for c in h)
    try:
        return int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    except ValueError:
        return None


def _encode(rgba: np.ndarray) -> tuple[str, bool]:
    im = Image.fromarray(rgba, "RGBA")
    transparent = bool((rgba[..., 3] < 200).mean() > 0.01)
    im.thumbnail((1024, 1024), Image.Resampling.LANCZOS)
    flat = Image.new("RGB", im.size, (255, 255, 255))
    flat.paste(im, mask=im.getchannel("A"))
    buf = io.BytesIO()
    flat.save(buf, "PNG")
    return base64.standard_b64encode(buf.getvalue()).decode(), transparent


def has_credentials(api_key: str | None) -> bool:
    return bool(api_key or os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def _ask(system: str, data: str, prompt: str, output_format, api_key: str | None,
         client: anthropic.Anthropic | None):
    """One image + text request with structured output; friendly errors."""
    try:
        client = client or anthropic.Anthropic(api_key=api_key or None, max_retries=2, timeout=120)
        resp = client.beta.messages.parse(
            model=MODEL,
            max_tokens=16000,
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            output_config={"effort": "medium"},
            system=system,
            messages=[{"role": "user", "content": [
                {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}},
                {"type": "text", "text": prompt},
            ]}],
            output_format=output_format,
        )
    except anthropic.AnthropicError as e:
        raise _friendly(e, client) from None
    if resp.stop_reason == "refusal":
        raise AIError("The AI declined to analyse this image.")
    if resp.parsed_output is None:
        raise AIError("The AI response was incomplete. Try again.")
    return resp.parsed_output


def _via_gateway(client) -> bool:
    """Requests going through StitchPilot's AI service (StitchPilot Plus) rather than a key of one's own."""
    return client is not None and str(getattr(client, "base_url", "")).rstrip("/").endswith("/anthropic")


def _friendly(e: Exception, client) -> Exception:
    """An error message people can act on (and, through the AI service, the account's own problems)."""
    status = getattr(e, "status_code", None)
    if _via_gateway(client) and isinstance(e, anthropic.APIStatusError):
        problem = account.gateway_error(status, getattr(e, "body", None))
        if problem is not None:
            return problem
        if status in (400, 502, 503):
            msg = ((getattr(e, "body", None) or {}).get("error") or {}).get("message") if isinstance(
                getattr(e, "body", None), dict) else None
            return AIError(msg or "StitchPilot's AI service had a problem. Try again in a minute.")
    if isinstance(e, anthropic.AuthenticationError):
        return AIError("The API key was rejected. Check it under AI ▸ API key.")
    if isinstance(e, anthropic.PermissionDeniedError):
        return AIError("This API key doesn't have access to the model.")
    if isinstance(e, anthropic.RateLimitError):
        return AIError("The AI service is busy (rate limited). Try again in a minute.")
    if isinstance(e, anthropic.APIStatusError):
        return AIError(f"The AI service returned an error ({e.status_code}).")
    if isinstance(e, anthropic.APIConnectionError):
        return AIError("Couldn't reach the AI service. Check your internet connection.")
    return AIError(str(e))  # e.g. no credentials configured


def analyse(rgba: np.ndarray, s: Settings, api_key: str | None = None,
            client: anthropic.Anthropic | None = None) -> Analysis:
    data, transparent = _encode(rgba)
    note = (" The original has a transparent background, shown here as white; it is already removed."
            if transparent else "")
    prompt = (f"Digitize this for a {s.max_width_mm:.0f} x {s.max_height_mm:.0f} mm area on "
              f"{s.fabric.lower()}.{note}")
    out = _ask(SYSTEM, data, prompt, Analysis, api_key, client)
    if transparent:
        out.background = "transparent"
    return out


# ---------------------------------------------------------------- per-shape suggestions
SHAPES_SYSTEM = """You are an expert machine-embroidery digitizer reviewing an automatic digitizer's \
choices, shape by shape. You see the design reduced to flat thread colours, with every shape numbered, \
and a table describing each shape and how it is stitched now.

Stitch types you can choose:
- fill: rows of tatami stitches, for areas. You also choose the direction of the rows (0-179 degrees, \
0 = left to right, 90 = top to bottom, angles turn clockwise because the image y axis points down). \
Rows that run along a shape's length or follow its flow look natural (a leaf along its spine, a \
petal from base to tip, a stripe along the stripe). Neighbouring shapes look crisper when their angles \
differ by 30 degrees or more.
- satin: smooth columns that follow a stroke, for lettering, borders, stems and outlines. Only for \
strokes about 1-8 mm wide; wider satins snag and loosen.
- outline: only a line around the edge, for shapes that are meant to read as a drawn line or that \
are too small or too detailed to fill well.
- keep: the current choice is right.

Only suggest changes that clearly improve the sew-out. Prefer few, high-value suggestions (at most 12). \
For each one give a short, friendly reason a hobbyist will understand. Use shape numbers exactly as \
shown."""


class ShapeSuggestion(BaseModel):
    shape: int
    stitch: Literal["fill", "satin", "outline", "keep"]
    angle: int | None
    reason: str


class ShapeAdvice(BaseModel):
    summary: str
    suggestions: list[ShapeSuggestion]


def _numbered(design, shapes) -> str:
    """The design's flat colours with each shape's number written on it (base64 PNG)."""
    import cv2

    img = design.prepared.color_preview()
    rgb = img[..., :3].astype(np.float32)
    a = img[..., 3:4].astype(np.float32) / 255
    flat = (rgb * a + 255 * (1 - a)).astype(np.uint8)
    h, w = flat.shape[:2]
    k = min(1.0, 1024 / max(h, w))
    if k < 1:
        flat = cv2.resize(flat, (int(w * k), int(h * k)), interpolation=cv2.INTER_AREA)
    h, w = flat.shape[:2]
    for n, sh in enumerate(shapes, 1):
        x, y = int(sh.fx * w), int(sh.fy * h)
        txt = str(n)
        (tw, th), _ = cv2.getTextSize(txt, cv2.FONT_HERSHEY_SIMPLEX, 0.5, 1)
        org = (max(0, x - tw // 2), min(h - 1, y + th // 2))
        cv2.putText(flat, txt, org, cv2.FONT_HERSHEY_SIMPLEX, 0.5, (255, 255, 255), 3, cv2.LINE_AA)
        cv2.putText(flat, txt, org, cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 0), 1, cv2.LINE_AA)
    buf = io.BytesIO()
    Image.fromarray(flat, "RGB").save(buf, "PNG")
    return base64.standard_b64encode(buf.getvalue()).decode()


def suggest_shapes(design, s: Settings, api_key: str | None = None,
                   client: anthropic.Anthropic | None = None, limit: int = 40):
    """Ask Claude for per-shape stitch types and fill angles. Returns (advice, shapes numbered 1..n)."""
    shapes = sorted((sh for sh in design.shapes if sh.kind != "skip" and sh.geom is not None),
                    key=lambda sh: -sh.area_mm2)[:limit]
    if not shapes:
        raise AIError("There are no shapes to look at yet.")
    rows = []
    for n, sh in enumerate(shapes, 1):
        x0, y0, x1, y1 = sh.geom.bounds
        now = sh.kind + (f" at {sh.angle:.0f} degrees" if sh.angle is not None else "")
        rows.append(f"{n}: colour #{sh.rgb[0]:02x}{sh.rgb[1]:02x}{sh.rgb[2]:02x}, {sh.area_mm2:.0f} mm2, "
                    f"{x1 - x0:.0f} x {y1 - y0:.0f} mm, strokes about {sh.width_mm:.1f} mm wide, now {now}")
    prompt = (f"The design is {design.width_mm:.0f} x {design.height_mm:.0f} mm on {s.fabric.lower()}.\n"
              "Shapes (numbered on the image):\n" + "\n".join(rows))
    advice = _ask(SHAPES_SYSTEM, _numbered(design, shapes), prompt, ShapeAdvice, api_key, client)
    advice.suggestions = [x for x in advice.suggestions if 1 <= x.shape <= len(shapes) and
                          (x.stitch != "keep" or x.angle is not None)]
    for x in advice.suggestions:
        if x.angle is not None:
            x.angle = int(x.angle) % 180
    return advice, shapes


# ---------------------------------------------------------------- text clean-up
TEXT_SYSTEM = """You find the lettering in artwork so it can be redrawn as clean, crisp text before \
the artwork is turned into embroidery. Blurry, pixelated or JPEG-damaged text is the most common reason \
auto-digitized logos look bad.

For every line of text in the image give:
- text: exactly as written, with the same capitals, spaces and punctuation. One entry per line; \
if a line has parts in clearly different fonts, sizes or colours, give each part separately.
- left, top, right, bottom: a box round the letters in pixels of the image you were given (0,0 is the \
top-left corner). Make it snug, with the letters fully inside.
- style: the closest family: sans, serif, slab, rounded, display (heavy or condensed poster type), \
script (joined-up), handwritten.
- bold and italic as they look.
- font_guess: the font's name if you recognise it with confidence (e.g. "Arial", "Georgia", \
"Impact"), otherwise an empty string.
- curved: true if the line follows an arc or is rotated, false if it sits on a straight, level baseline.

Skip text that is part of a photo or texture, single decorative letters inside a pattern, and text \
smaller than about 1/60 of the image height. If there is no text, return an empty list."""


class FoundText(BaseModel):
    text: str
    left: int
    top: int
    right: int
    bottom: int
    style: Literal["sans", "serif", "slab", "rounded", "display", "script", "handwritten"]
    bold: bool
    italic: bool
    font_guess: str
    curved: bool


class TextScan(BaseModel):
    texts: list[FoundText]


def find_text(rgba: np.ndarray, api_key: str | None = None, client: anthropic.Anthropic | None = None
              ) -> list[tuple[FoundText, tuple[float, float, float, float]]]:
    """The lines of text Claude sees, each with its box as fractions of the picture."""
    data, _ = _encode(rgba)
    h, w = rgba.shape[:2]
    k = min(1.0, 1024 / max(h, w))
    W, H = max(1, int(w * k)), max(1, int(h * k))  # the size Claude sees (as _encode sends it)
    scan = _ask(TEXT_SYSTEM, data, f"The image is {W} x {H} pixels. Find its text.", TextScan, api_key, client)
    out = []
    for t in scan.texts:
        x0, x1 = sorted((t.left, t.right))
        y0, y1 = sorted((t.top, t.bottom))
        box = (max(0.0, x0 / W), max(0.0, y0 / H), min(1.0, x1 / W), min(1.0, y1 / H))
        if t.text.strip() and box[2] - box[0] > 0.01 and box[3] - box[1] > 0.005:
            out.append((t, box))
    return out


def apply(a: Analysis, s: Settings) -> Settings:
    """Turn an analysis into concrete settings (the user can still change them)."""
    palette = [c for c in (_hex(t.hex) for t in a.thread_colors) if c is not None][:16]
    bg = {"plain": "auto", "transparent": "auto", "busy": "ai", "none": "keep"}[a.background]
    return replace(
        s,
        palette=palette or None,
        num_colors=len(palette),
        background=bg,
        background_rgb=_hex(a.background_hex) if a.background == "plain" else None,
        remove_enclosed_bg=a.background_inside_is_background,
        detail=float(np.clip(a.detail, 0.0, 1.0)),
    )
