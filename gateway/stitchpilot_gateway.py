"""StitchPilot AI gateway: an AWS Lambda (Function URL) between the desktop app and Claude / OpenAI.

The app talks to it exactly as it would to Anthropic's and OpenAI's APIs (their SDKs, a different base
URL, and the app's device token in place of an API key). For each request the gateway:

1. checks it is one of StitchPilot's own kinds of request, within fixed bounds (allowed models,
   token and image limits), so the keys behind it can't be used for anything else;
2. asks the shop (stitchpilot.jessiescars.com, /api/stitchpilot/usage/claim) whether this device's
   member has AI allowance left, which also counts the request;
3. makes the real call with StitchPilot's own keys and returns the answer unchanged;
4. gives the allowance back if the AI call failed.

No dependencies beyond the Python standard library, so it deploys as a tiny zip (deploy/aws).

Environment: ANTHROPIC_API_KEY, OPENAI_API_KEY, STITCHPILOT_SHOP_URL, STITCHPILOT_GATEWAY_SECRET,
and optionally ANTHROPIC_BASE_URL / OPENAI_BASE_URL (tests point these at fakes).
"""
from __future__ import annotations

import base64
import json
import logging
import os
import urllib.error
import urllib.request

log = logging.getLogger()
log.setLevel(logging.INFO)

MAX_BODY = 5 * 1024 * 1024
CLAUDE_MODELS = {m.strip() for m in os.environ.get("ALLOWED_CLAUDE_MODELS", "claude-opus-5-5").split(",")}
IMAGE_MODELS = {m.strip() for m in os.environ.get("ALLOWED_IMAGE_MODELS", "gpt-image-2.5-flare").split(",")}
MAX_TOKENS = 16000
IMAGE_SIZES = {"1024x1024", "1536x1024", "1024x1536", "auto"}
IMAGE_QUALITY = {"low", "medium", "high", "auto"}
PASS_HEADERS = ("anthropic-version", "anthropic-beta")  # the SDK's own protocol headers


class Refused(Exception):
    def __init__(self, status: int, message: str, kind: str = "invalid_request_error", extra: dict | None = None):
        super().__init__(message)
        self.status, self.message, self.kind, self.extra = status, message, kind, extra or {}


def _http(method: str, url: str, body: bytes | None, headers: dict, timeout: float) -> tuple[int, dict, bytes]:
    """One HTTP call: (status, response headers, body). The only network code here (tests replace it)."""
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, {k.lower(): v for k, v in r.headers.items()}, r.read()
    except urllib.error.HTTPError as e:
        return e.code, {k.lower(): v for k, v in (e.headers or {}).items()}, e.read()


def _shop(path: str, payload: dict) -> tuple[int, dict]:
    url = os.environ["STITCHPILOT_SHOP_URL"].rstrip("/") + path
    status, _, body = _http("POST", url, json.dumps(payload).encode(),
                            {"content-type": "application/json",
                             "x-stitchpilot-gateway-secret": os.environ["STITCHPILOT_GATEWAY_SECRET"]}, 20)
    try:
        return status, json.loads(body or b"{}")
    except ValueError:
        return status, {}


def _error(status: int, message: str, kind: str, extra: dict | None = None) -> dict:
    # shaped like Anthropic's / OpenAI's own errors, so the SDKs raise their usual exceptions with it
    body = {"type": "error", "error": {"type": kind, "message": message, **(extra or {})}}
    return {"statusCode": status, "headers": {"content-type": "application/json"}, "body": json.dumps(body)}


def _claude_request(body: dict) -> tuple[str, int]:
    if body.get("model") not in CLAUDE_MODELS:
        raise Refused(400, "That model isn't available through StitchPilot.")
    if not isinstance(body.get("max_tokens"), int) or not 1 <= body["max_tokens"] <= MAX_TOKENS:
        raise Refused(400, f"max_tokens must be 1..{MAX_TOKENS}.")
    if body.get("stream") or body.get("tools") or body.get("mcp_servers") or body.get("container"):
        raise Refused(400, "Streaming and tools aren't available through StitchPilot.")
    msgs = body.get("messages")
    if not isinstance(msgs, list) or not 1 <= len(msgs) <= 4:
        raise Refused(400, "One short conversation per request.")
    images = sum(1 for m in msgs if isinstance(m.get("content"), list)
                 for c in m["content"] if isinstance(c, dict) and c.get("type") in ("image", "document"))
    if images > 2:
        raise Refused(400, "At most two images per request.")
    return "actions", 1


def _image_request(body: dict) -> tuple[str, int]:
    if body.get("model") not in IMAGE_MODELS:
        raise Refused(400, "That model isn't available through StitchPilot.")
    n = body.get("n", 1)
    if not isinstance(n, int) or not 1 <= n <= 4:
        raise Refused(400, "1 to 4 pictures per request.")
    if body.get("size", "auto") not in IMAGE_SIZES or body.get("quality", "auto") not in IMAGE_QUALITY:
        raise Refused(400, "That picture size or quality isn't available through StitchPilot.")
    if body.get("stream") or body.get("partial_images"):
        raise Refused(400, "Streaming isn't available through StitchPilot.")
    if not isinstance(body.get("prompt"), str) or len(body["prompt"]) > 4000:
        raise Refused(400, "The description is too long.")
    return "images", n


ROUTES = {
    "/anthropic/v1/messages": ("claude", _claude_request),
    "/openai/v1/images/generations": ("openai", _image_request),
}


def _token(headers: dict) -> str:
    tok = headers.get("x-api-key") or ""
    if not tok and headers.get("authorization", "").lower().startswith("bearer "):
        tok = headers["authorization"][7:]
    tok = tok.strip()
    if not tok.startswith("spd_") or len(tok) > 120:
        raise Refused(401, "Connect StitchPilot to your account first (AI ▸ StitchPilot Plus & account).",
                      "authentication_error")
    return tok


def _upstream(provider: str, path: str, query: str, raw: bytes, headers: dict) -> tuple[int, dict, bytes]:
    if provider == "claude":
        base = os.environ.get("ANTHROPIC_BASE_URL", "https://api.anthropic.com")
        h = {"content-type": "application/json", "x-api-key": os.environ["ANTHROPIC_API_KEY"],
             "anthropic-version": headers.get("anthropic-version", "2023-06-01")}
        if headers.get("anthropic-beta"):
            h["anthropic-beta"] = headers["anthropic-beta"]
        url = base.rstrip("/") + path[len("/anthropic"):]
    else:
        base = os.environ.get("OPENAI_BASE_URL", "https://api.openai.com")
        h = {"content-type": "application/json", "authorization": f"Bearer {os.environ['OPENAI_API_KEY']}"}
        url = base.rstrip("/") + path[len("/openai"):]
    return _http("POST", url + (f"?{query}" if query else ""), raw, h, 280)


def lambda_handler(event, context=None):
    http = event.get("requestContext", {}).get("http", {})
    method, path = http.get("method", "GET"), event.get("rawPath", "/")
    headers = {k.lower(): v for k, v in (event.get("headers") or {}).items()}
    if method == "GET" and path == "/health":
        return {"statusCode": 200, "headers": {"content-type": "application/json"}, "body": '{"ok": true}'}
    route = ROUTES.get(path)
    if method != "POST" or route is None:
        return _error(404, "Not found.", "not_found_error")
    provider, check = route
    try:
        raw = event.get("body") or ""
        raw = base64.b64decode(raw) if event.get("isBase64Encoded") else raw.encode()
        if len(raw) > MAX_BODY:
            raise Refused(413, "That request is too large.")
        try:
            body = json.loads(raw)
        except ValueError:
            raise Refused(400, "The request isn't valid JSON.")
        if not isinstance(body, dict):
            raise Refused(400, "The request isn't valid JSON.")
        kind, amount = check(body)
        token = _token(headers)

        status, claim = _shop("/api/stitchpilot/usage/claim", {"token": token, "kind": kind, "amount": amount})
        if status == 401:
            raise Refused(401, "This computer isn't connected to an account any more. Connect it again "
                               "(AI ▸ StitchPilot Plus & account).", "authentication_error")
        if status == 402:
            what = "AI pictures" if kind == "images" else "AI actions"
            msg = (f"You've used this month's {what} ({claim.get('used')} of {claim.get('limit')})."
                   if claim.get("plan") == "plus" else
                   f"You've used your free {what}. StitchPilot Plus gives you plenty more every month.")
            raise Refused(402, msg, "quota_exceeded", {"stitchpilot": claim})
        if status != 200:
            log.error("allowance check failed: %s", status)
            raise Refused(503, "StitchPilot's AI service is busy. Try again in a minute.", "overloaded_error")
    except Refused as r:
        return _error(r.status, r.message, r.kind, r.extra)

    try:
        up_status, up_headers, up_body = _upstream(provider, path, event.get("rawQueryString", ""), raw, headers)
    except Exception:  # network trouble reaching the AI provider
        log.exception("upstream call failed")
        up_status, up_headers, up_body = 0, {}, b""
    if up_status != 200:
        _shop("/api/stitchpilot/usage/refund", {"token": token, "kind": kind, "amount": amount})
        if up_status == 0:
            return _error(502, "Couldn't reach the AI service. Try again in a minute.", "api_error")
        if up_status in (401, 403):  # our key, not theirs: don't send the user to fix their settings
            log.error("provider rejected the gateway's key: %s", up_status)
            return _error(502, "StitchPilot's AI service isn't set up correctly. Please let us know.", "api_error")
    out_headers = {"content-type": up_headers.get("content-type", "application/json"),
                   "x-stitchpilot-plan": str(claim.get("plan", "")),
                   "x-stitchpilot-used": str(claim.get("used", "")),
                   "x-stitchpilot-limit": str(claim.get("limit", ""))}
    for h in ("request-id", "x-request-id"):
        if h in up_headers:
            out_headers[h] = up_headers[h]
    return {"statusCode": up_status, "headers": out_headers, "body": up_body.decode("utf-8", "replace")}
