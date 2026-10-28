"""Exercise the AI assist against a mocked Messages API (no key or network needed)."""
import json
import sys
from pathlib import Path

import anthropic
import httpx2 as httpx

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from stitchpilot import ai  # noqa: E402
from stitchpilot.digitize import digitize  # noqa: E402
from stitchpilot.imageio import load_image  # noqa: E402
from stitchpilot.settings import Settings  # noqa: E402

here = Path(__file__).parent
seen = {}
reply = {
    "subject": "Round badge with a red star and the word STITCH",
    "design_type": "logo",
    "background": "plain",
    "background_hex": "#ffffff",
    "background_inside_is_background": False,  # the white lettering is part of the design
    "thread_colors": [
        {"hex": "#143c8c", "name": "Navy", "role": "outline"},
        {"hex": "#fac828", "name": "Golden yellow", "role": "main"},
        {"hex": "#c81e28", "name": "Red", "role": "main"},
        {"hex": "#ffffff", "name": "White", "role": "text"},
    ],
    "detail": 0.6,
    "min_width_mm": 70,
    "suitability": "great",
    "tips": ["Keep it at least 70 mm wide so the lettering stays readable."],
}


def handler(request: httpx.Request) -> httpx.Response:
    seen["headers"] = dict(request.headers)
    seen["body"] = json.loads(request.content)
    return httpx.Response(200, json={
        "id": "msg_test", "type": "message", "role": "assistant", "model": ai.MODEL,
        "content": [{"type": "text", "text": json.dumps(reply)}],
        "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 1000, "output_tokens": 200},
    })


client = anthropic.Anthropic(api_key="test", http_client=httpx.Client(transport=httpx.MockTransport(handler)))
rgba = load_image(here / "samples" / "logo.jpg")
a = ai.analyse(rgba, Settings(), client=client)
b = seen["body"]
assert b["model"] == "claude-opus-5-5"
assert b["fallbacks"] == "default"
assert "server-side-fallback-2026-07-01" in seen["headers"].get("anthropic-beta", "")
assert b["output_config"]["effort"] == "medium" and b["output_config"]["format"]["type"] == "json_schema"
assert b["messages"][0]["content"][0]["type"] == "image"
print("request ok:", b["model"], "| beta:", seen["headers"]["anthropic-beta"], "| keys:", sorted(b))

s = ai.apply(a, Settings())
print("applied:", s.palette, s.background, s.background_rgb, "enclosed removed:", s.remove_enclosed_bg, s.detail)
d = digitize(rgba, s)
print("design with AI settings:", len(d.blocks), "colours", [blk.rgb for blk in d.blocks], d.stitch_count, "stitches")
assert len(d.blocks) == 4, "white lettering should be kept as its own colour"
