"""Create a picture with AI (OpenAI's image models), drawn to be easy to embroider.

The user describes an idea; this adds what makes artwork stitch well (flat solid colours, a small
palette, bold shapes, no tiny details, one centred subject on a plain or transparent background) and
returns the pictures as RGBA arrays. In the app the request goes through StitchPilot's AI service
(StitchPilot Plus, see account.py), which counts it against the account's picture allowance.
"""
from __future__ import annotations

import base64
import io

import numpy as np

from . import account

MODEL = "gpt-image-2.5-flare"  # OpenAI's fast everyday image model (supports transparent backgrounds)

STYLES = {
    "Cute sticker": "a cute kawaii sticker-style illustration with chunky rounded shapes and a thick dark outline",
    "Flat clipart": "a clean flat vector clipart illustration",
    "Logo / emblem": "a simple, bold logo-style emblem",
    "Patch / badge": "an embroidered patch style badge with a solid outer border shape",
    "Line art": "simple line art in one dark colour, bold strokes of even thickness, no filled areas",
    "Retro": "a retro 1970s-style flat illustration with warm colours",
}
QUALITY = {"Draft (cheapest)": "low", "Good": "medium", "Best": "high"}
SIZES = {"Square": "1024x1024", "Wide": "1536x1024", "Tall": "1024x1536"}


class ImageGenError(Exception):
    pass


def build_prompt(idea: str, style: str = "Cute sticker", colours: int = 5, transparent: bool = True) -> str:
    """The user's idea plus what makes artwork stitch well."""
    look = STYLES.get(style, STYLES["Flat clipart"])
    colours = max(1, min(12, int(colours)))
    background = ("on a fully transparent background" if transparent
                  else "on a plain, flat white background")
    return (
        f"{look}: {idea.strip()}.\n\n"
        "This artwork will be turned into machine embroidery, so draw it to stitch cleanly:\n"
        f"- flat, solid areas of colour only, at most {colours} colours in total (counting outlines)\n"
        "- no gradients, shading, highlights, glow, textures, noise or photo-like detail\n"
        "- bold, simple shapes; no tiny details, hairlines or specks; lines clearly thick\n"
        f"- one subject, centred, filling most of the frame with a small margin, {background}\n"
        "- no text, letters or numbers unless the idea above asks for them, and then large and simple"
    )


def _friendly(e: Exception, client) -> Exception:
    """An error message people can act on (and, through the AI service, the account's own problems)."""
    import openai

    status = getattr(e, "status_code", None)
    body = getattr(e, "body", None)
    if client is not None and str(getattr(client, "base_url", "")).rstrip("/").endswith("/openai/v1"):
        problem = account.gateway_error(status, body)
        if problem is not None:
            return problem
        if status in (502, 503):
            return ImageGenError("StitchPilot's AI service had a problem. Try again in a minute.")
    text = str(e).lower()
    if isinstance(e, openai.AuthenticationError):
        return ImageGenError("The OpenAI API key was rejected.")
    if isinstance(e, openai.PermissionDeniedError):
        return ImageGenError("This OpenAI key can't use image generation. Some organisations must be verified "
                             "on platform.openai.com before they can create images.")
    if isinstance(e, openai.RateLimitError):
        if "quota" in text or "billing" in text:
            return ImageGenError("The OpenAI account is out of credit.")
        return ImageGenError("OpenAI is busy (rate limited). Try again in a minute.")
    if isinstance(e, openai.BadRequestError):
        if "moderation" in text or "safety" in text:
            return ImageGenError("OpenAI declined this picture under its content rules. Try describing it differently.")
        return ImageGenError(f"OpenAI couldn't make that picture: {getattr(e, 'message', e)}")
    if isinstance(e, openai.APIStatusError):
        return ImageGenError(f"OpenAI returned an error ({e.status_code}). Try again in a moment.")
    if isinstance(e, openai.APIConnectionError):
        return ImageGenError("Couldn't reach the AI service. Check your internet connection.")
    return ImageGenError(str(e))  # e.g. no key configured


def _rgba(b64: str) -> np.ndarray:
    from PIL import Image

    im = Image.open(io.BytesIO(base64.b64decode(b64)))
    return np.array(im.convert("RGBA"))


def generate(idea: str, *, style: str = "Cute sticker", colours: int = 5, count: int = 2,
             quality: str = "low", transparent: bool = True, size: str = "1024x1024",
             api_key: str | None = None, client=None, model: str = MODEL) -> list[np.ndarray]:
    """Pictures (RGBA arrays) for an idea. Raises ImageGenError with a message people can act on."""
    import openai

    if not idea.strip():
        raise ImageGenError("Describe the picture you'd like first.")
    try:
        client = client or openai.OpenAI(api_key=api_key or None, max_retries=2, timeout=240)
        res = client.images.generate(
            model=model, prompt=build_prompt(idea, style, colours, transparent),
            n=max(1, min(4, int(count))), size=size, quality=quality,
            background="transparent" if transparent else "opaque",
            # WebP keeps the transparency at a fraction of PNG's size (a few versions fit in one response)
            output_format="webp", output_compression=92)
    except openai.OpenAIError as e:
        raise _friendly(e, client) from None
    out = [_rgba(d.b64_json) for d in (res.data or []) if getattr(d, "b64_json", None)]
    if not out:
        raise ImageGenError("OpenAI didn't send a picture back. Try again.")
    return out
