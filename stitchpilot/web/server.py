"""StitchPilot on the web: a small Starlette server around the same engine as the desktop app.

    python -m stitchpilot.web            # http://127.0.0.1:8000
    STITCHPILOT_HOST=0.0.0.0 STITCHPILOT_PORT=8080 python -m stitchpilot.web

Built to be put on the internet: uploads and pictures are size-limited, each visitor is rate-limited,
the number of conversions running at once is capped, and nothing is written to disk. AI features use
the visitor's own Claude API key, sent with each AI request and never stored or logged.

The API is stateless: every request carries the image id and the full settings, and converted designs
are cached by what they depend on. So the same app runs as one long-lived server (local or Docker) or
as AWS Lambda (lambda_handler.py), where any request may reach a fresh instance.

Settings (environment variables):
  STITCHPILOT_MODE=lambda    run jobs inline and keep uploads/designs in S3 (STITCHPILOT_BUCKET)
  STITCHPILOT_WORKERS        conversion processes for the server (default: CPU count, at most 4)
  STITCHPILOT_AI_WORKERS     processes for AI requests (default 2)
  STITCHPILOT_TRUST_PROXY=1  behind a reverse proxy: take the visitor's address from X-Forwarded-For
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import time
from concurrent.futures import ProcessPoolExecutor
from contextlib import asynccontextmanager
from pathlib import Path

from starlette.applications import Starlette
from starlette.exceptions import HTTPException
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from .. import __version__
from . import engine, jobs, storage

log = logging.getLogger("stitchpilot.web")
STATIC = Path(__file__).parent / "static"
MAX_UPLOAD = storage.MAX_UPLOAD
MAX_STATE = 256 * 1024  # bytes of JSON per request
MAX_RESPONSE = 4 * 1024 * 1024  # Lambda responses: 6 MB after base64 (+33%); bigger files go via a download link
JOB_TIMEOUT = 180.0
LAMBDA = os.environ.get("STITCHPILOT_MODE") == "lambda"
WORKERS = int(os.environ.get("STITCHPILOT_WORKERS", min(4, os.cpu_count() or 2)))
AI_WORKERS = int(os.environ.get("STITCHPILOT_AI_WORKERS", 2))
TRUST_PROXY = os.environ.get("STITCHPILOT_TRUST_PROXY") == "1"
# per visitor: (requests, per seconds). On Lambda this is per instance only: use API limits / WAF too
LIMITS = {"upload": (20, 60), "convert": (60, 60), "file": (60, 60), "ai": (8, 60), "text": (120, 60)}

store = None  # storage.MemoryStorage or S3Storage, set at start-up
inflight: dict[str, asyncio.Future] = {}
buckets: dict[tuple[str, str], tuple[float, float]] = {}
pools: dict[str, ProcessPoolExecutor] = {}
busy = {"cpu": 0}
fonts_cache: list[str] = []


def get_store():
    global store
    if store is None:
        store = storage.from_env()
    return store


def client_ip(request: Request) -> str:
    if TRUST_PROXY and request.headers.get("x-forwarded-for"):
        # the last entry is the one our proxy added; earlier ones come from the visitor and can be made up
        return request.headers["x-forwarded-for"].split(",")[-1].strip()
    return request.client.host if request.client else "?"


def limit(request: Request, kind: str) -> None:
    """Token bucket per visitor and kind of request."""
    n, per = LIMITS[kind]
    key = (client_ip(request), kind)
    now = time.monotonic()
    tokens, last = buckets.get(key, (float(n), now))
    tokens = min(float(n), tokens + (now - last) * n / per)
    if tokens < 1:
        raise HTTPException(429, "Slow down a little – too many requests. Try again in a moment.")
    buckets[key] = (tokens - 1, now)
    if len(buckets) > 50_000:  # forget old visitors
        for k in [k for k, (_, t) in buckets.items() if now - t > 600]:
            buckets.pop(k, None)


async def run(pool: str, fn, *args):
    """Slow or Qt work: in a worker process on a server; inline on Lambda (one request per instance)."""
    if LAMBDA:
        return fn(*args)
    if pool == "cpu":
        if busy["cpu"] >= WORKERS * 4:
            raise HTTPException(503, "StitchPilot is very busy right now. Please try again in a minute.")
        busy["cpu"] += 1
    try:
        fut = asyncio.get_running_loop().run_in_executor(pools[pool], fn, *args)
        return await asyncio.wait_for(fut, JOB_TIMEOUT)
    except asyncio.TimeoutError:
        raise HTTPException(504, "That took too long. Try a smaller size or a simpler picture.")
    finally:
        if pool == "cpu":
            busy["cpu"] -= 1


async def body_json(request: Request) -> dict:
    raw = await request.body()
    if len(raw) > MAX_STATE:
        raise HTTPException(413, "Request too large.")
    try:
        data = json.loads(raw or b"{}")
    except ValueError:
        raise HTTPException(400, "Bad request.")
    if not isinstance(data, dict):
        raise HTTPException(400, "Bad request.")
    return data


def user_error(e: Exception) -> HTTPException:
    from .. import ai

    if isinstance(e, HTTPException):
        return e
    if isinstance(e, (ValueError, ai.AIError)):  # messages written for people; the desktop menu is a button here
        return HTTPException(400, (str(e) or "That didn't work.").replace("under AI ▸ API key", "under ✨ AI key"))
    log.error("unexpected error", exc_info=e)  # the traceback only: never the request (it may hold an API key)
    return HTTPException(500, "Something went wrong converting this design.")


def api_key(request: Request) -> str:
    key = request.headers.get("x-claude-key", "").strip()
    if not key:
        raise HTTPException(400, "Add your Claude API key first (✨ AI key, at the top).")
    if len(key) > 400 or not key.isascii():
        raise HTTPException(400, "That doesn't look like a Claude API key.")
    return key


def picture_of(image_id) -> tuple[str, bytes] | None:
    if image_id is None:
        return None
    img = get_store().get_upload(str(image_id))
    if img is None:
        raise HTTPException(410, "upload-again")  # the page sends the picture again and retries
    return img


async def design_for(body: dict):
    """The digitized design for (image_id, state) in a request: cached, else converted now."""
    state = body.get("state") or {}
    if not isinstance(state, dict):
        raise HTTPException(400, "Bad request.")
    image_id = body.get("image_id")
    try:
        s = engine.settings_from_state(state)
    except Exception:
        raise HTTPException(400, "Some settings in this design don't make sense.")
    if image_id is None and not any(t.text.strip() for t in s.texts):
        raise HTTPException(400, "Add a picture or some lettering first.")
    key = storage.cache_key(engine.digitize_key(image_id, s))
    st = get_store()
    d = await asyncio.to_thread(st.get_design, key)
    if d is not None:
        return d, state
    if key in inflight:  # the same conversion is already running (e.g. a double click)
        return await asyncio.shield(inflight[key]), state
    img = picture_of(image_id)
    fut = asyncio.get_running_loop().create_future()
    inflight[key] = fut
    try:
        d = await run("cpu", jobs.convert, img[1] if img else None, img[0] if img else None, state)
        await asyncio.to_thread(st.put_design, key, d)
        fut.set_result(d)
        return d, state
    except Exception as e:
        fut.set_exception(e)
        fut.exception()  # mark retrieved
        raise
    finally:
        inflight.pop(key, None)


# ---------------------------------------------------------------- routes
async def index(request: Request):
    return FileResponse(STATIC / "index.html", headers={"Cache-Control": "no-cache"})


async def options(request: Request):
    if not fonts_cache:
        try:
            fonts_cache[:] = await run("cpu", jobs.fonts)
        except Exception:
            fonts_cache[:] = ["Arial"]
    o = engine.options()
    o.update(fonts=fonts_cache, version=__version__, uploads="direct" if get_store().direct_uploads else "server",
             max_upload=MAX_UPLOAD)
    return JSONResponse(o)


async def _accept(data: bytes, name: str) -> dict:
    """Check an uploaded file and keep it (a picture, or the picture inside a project)."""
    name = Path(name or "picture.png").name[:120]
    try:
        info = await run("cpu", jobs.probe, data, name)
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(400, str(e) or "Couldn't read this file.")
    except Exception:  # not a picture we can read: an everyday mistake, not a server problem
        raise HTTPException(400, "Couldn't read this file. Try a PNG or JPG picture.")
    out = {"name": name, "project": info["project"]}
    if info["project"]:
        out["state"] = info["state"]
        src = info.get("source")
        data, name = (src, info.get("source_name") or "picture.png") if src is not None else (None, name)
        out["name"] = name
    if data is not None:
        image_id = hashlib.sha256(data).hexdigest()[:32]
        await asyncio.to_thread(get_store().put_upload, image_id, name, data)
        out.update(image_id=image_id, width=info.get("width"), height=info.get("height"))
    return out


async def upload(request: Request):
    """The file itself, posted to the server (local / Docker)."""
    if get_store().direct_uploads:
        raise HTTPException(400, "Upload with /api/upload-url.")
    if int(request.headers.get("content-length", 0)) > MAX_UPLOAD + 4096:
        raise HTTPException(413, "That file is too big (20 MB at most).")
    form = await request.form(max_files=1, max_fields=4, max_part_size=MAX_UPLOAD)
    f = form.get("file")
    if f is None or not hasattr(f, "read"):
        raise HTTPException(400, "No file received.")
    data = await f.read()
    await f.close()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "That file is too big (20 MB at most).")
    limit(request, "upload")  # after reading it: refusing mid-upload resets the connection
    return JSONResponse(await _accept(data, f.filename))


async def upload_url(request: Request):
    """Lambda: where the browser should send the file (straight to S3)."""
    body = await body_json(request)
    limit(request, "upload")
    st = get_store()
    if not st.direct_uploads:
        raise HTTPException(400, "Upload with /api/upload.")
    if int(body.get("size") or 0) > MAX_UPLOAD:
        raise HTTPException(413, "That file is too big (20 MB at most).")
    return JSONResponse(await asyncio.to_thread(st.presigned_upload))


async def upload_done(request: Request):
    """Lambda: the browser has put the file in S3; check it and keep it."""
    body = await body_json(request)
    st = get_store()
    if not st.direct_uploads:
        raise HTTPException(400, "Upload with /api/upload.")
    data = await asyncio.to_thread(st.take_incoming, str(body.get("key", "")))
    if not data:
        raise HTTPException(400, "The upload didn't arrive (or was over 20 MB). Please try again.")
    return JSONResponse(await _accept(data, str(body.get("name", "picture.png"))))


async def picture(request: Request):
    """The uploaded picture itself (the 'Original' tab, for projects opened on the server)."""
    img = get_store().get_upload(request.path_params["image_id"])
    if img is None:
        raise HTTPException(404, "Not found.")
    name, data = img
    kind = {"png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg", "webp": "image/webp", "gif": "image/gif",
            "bmp": "image/bmp"}.get(Path(name).suffix.lower().lstrip("."))
    if kind is None or len(data) > MAX_RESPONSE:
        raise HTTPException(404, "No preview for this file.")
    return Response(data, media_type=kind, headers={"Cache-Control": "private, max-age=3600"})


async def convert(request: Request):
    body = await body_json(request)  # read first: refusing mid-request resets the connection
    limit(request, "convert")
    try:
        base, state = await design_for(body)

        def finish():
            d = engine.apply_colors(base, state)
            png, lay = engine.preview(d, state, bool(body.get("hoop", True)))
            return d, png, lay

        d, png, lay = await asyncio.to_thread(finish)
    except Exception as e:
        raise user_error(e)
    return JSONResponse({"preview": "data:image/png;base64," + base64.b64encode(png).decode(), "layout": lay,
                         "shape_list": engine.shapes(d), **engine.summary(d)})


def _attachment(data: bytes, name: str, media: str) -> Response:
    if len(data) > MAX_RESPONSE and get_store().direct_uploads:  # too big for Lambda: a download link
        return JSONResponse({"url": get_store().put_download(name, data, media)})
    return Response(data, media_type=media, headers={"Content-Disposition": f'attachment; filename="{name}"',
                                                     "Cache-Control": "no-store"})


async def download(request: Request):
    body = await body_json(request)
    limit(request, "file")
    kind = str(body.get("kind"))
    stem = engine.safe_stem(body.get("name"))
    try:
        base, state = await design_for(body)
        d = await asyncio.to_thread(engine.apply_colors, base, state)
        if kind == "embroidery":
            ext = str(body.get("ext", "pes"))
            data = await asyncio.to_thread(engine.embroidery_file, d, ext)
            return _attachment(data, f"{stem}.{ext}", "application/octet-stream")
        if kind == "cut":
            ext = "dxf" if body.get("ext") == "dxf" else "svg"
            data = await asyncio.to_thread(engine.cut_file, d, ext)
            return _attachment(data, f"{stem}_cut.{ext}", "image/svg+xml" if ext == "svg" else "application/dxf")
        if kind == "template":
            hoop = engine.hoop_mm(state) if body.get("hoop", True) else None
            pdf = await run("cpu", jobs.template, d, hoop, stem, str(body.get("paper", "Letter")))
            return _attachment(pdf, f"{stem}_template.pdf", "application/pdf")
        if kind == "preview":
            png, _ = await asyncio.to_thread(engine.preview, d, state, bool(body.get("hoop", True)))
            return _attachment(png, f"{stem}_preview.png", "image/png")
        if kind == "project":
            img = picture_of(body.get("image_id"))
            data = engine.project_file(state, img[0] if img else None, img[1] if img else None,
                                       (d.width_mm, d.height_mm))
            return _attachment(data, f"{stem}.stitchpilot", "application/zip")
    except Exception as e:
        raise user_error(e)
    raise HTTPException(404, "Not found.")


async def text_preview(request: Request):
    body = await body_json(request)
    limit(request, "text")
    try:
        png = await run("cpu", jobs.text_preview, body.get("item") or {})
    except Exception as e:
        raise user_error(e)
    return Response(png, media_type="image/png", headers={"Cache-Control": "no-store"})


async def ai_settings(request: Request):
    body = await body_json(request)
    limit(request, "ai")
    key = api_key(request)
    img = picture_of(body.get("image_id") or "")
    try:
        return JSONResponse(await run("ai", jobs.ai_settings, img[1], img[0], body.get("state") or {}, key))
    except Exception as e:
        raise user_error(e)


async def ai_text(request: Request):
    body = await body_json(request)
    limit(request, "ai")
    key = api_key(request)
    img = picture_of(body.get("image_id") or "")
    try:
        return JSONResponse({"rows": await run("ai", jobs.ai_text, img[1], img[0], key)})
    except Exception as e:
        raise user_error(e)


async def ai_shapes(request: Request):
    from .. import ai

    body = await body_json(request)
    limit(request, "ai")
    key = api_key(request)
    try:
        base, state = await design_for(body)
        d = engine.apply_colors(base, state)
        s = engine.settings_from_state(state)
        advice, shapes = await asyncio.wait_for(asyncio.to_thread(ai.suggest_shapes, d, s, key), JOB_TIMEOUT)
    except Exception as e:
        raise user_error(e)
    out = []
    for x in advice.suggestions:
        sh = shapes[x.shape - 1]
        out.append({"rgb": list(sh.rgb), "fx": sh.fx, "fy": sh.fy, "kind": sh.kind, "area_mm2": round(float(sh.area_mm2)),
                    "stitch": x.stitch, "angle": x.angle, "reason": x.reason})
    return JSONResponse({"summary": advice.summary, "suggestions": out})


async def health(request: Request):
    return JSONResponse({"ok": True, "busy": busy["cpu"], "workers": 1 if LAMBDA else WORKERS})


async def http_error(request: Request, exc: HTTPException):
    return JSONResponse({"error": exc.detail}, status_code=exc.status_code)


class SecurityHeaders:
    """Basic hardening for a public site: no framing, no sniffing, scripts only from this site.
    (On AWS the page comes from Amplify, which sends the same headers; see deploy/aws.)"""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)

        async def send_with(msg):
            if msg["type"] == "http.response.start":
                h = msg.setdefault("headers", [])
                h += [(b"x-content-type-options", b"nosniff"), (b"x-frame-options", b"DENY"),
                      (b"referrer-policy", b"no-referrer"),
                      (b"content-security-policy", b"default-src 'self'; img-src 'self' data: blob:; "
                       b"style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; "
                       b"frame-ancestors 'none'; base-uri 'none'; form-action 'self'")]
            await send(msg)

        await self.app(scope, receive, send_with)


@asynccontextmanager
async def lifespan(app):
    get_store()
    if not LAMBDA:
        pools["cpu"] = ProcessPoolExecutor(WORKERS, initializer=jobs.init_worker)
        pools["ai"] = ProcessPoolExecutor(AI_WORKERS, initializer=jobs.init_worker)
    yield
    for p in pools.values():
        p.shutdown(wait=False, cancel_futures=True)


app = Starlette(
    routes=[
        Route("/", index), Route("/api/options", options), Route("/api/health", health),
        Route("/api/upload", upload, methods=["POST"]), Route("/api/upload-url", upload_url, methods=["POST"]),
        Route("/api/upload-done", upload_done, methods=["POST"]), Route("/api/picture/{image_id}", picture),
        Route("/api/convert", convert, methods=["POST"]), Route("/api/download", download, methods=["POST"]),
        Route("/api/text-preview", text_preview, methods=["POST"]),
        Route("/api/ai/settings", ai_settings, methods=["POST"]), Route("/api/ai/text", ai_text, methods=["POST"]),
        Route("/api/ai/shapes", ai_shapes, methods=["POST"]),
        Mount("/static", StaticFiles(directory=STATIC), name="static"),
    ],
    middleware=[Middleware(SecurityHeaders)],
    exception_handlers={HTTPException: http_error},
    lifespan=lifespan,
)


def main() -> None:
    import uvicorn

    jobs.init_worker()
    host = os.environ.get("STITCHPILOT_HOST", "127.0.0.1")
    port = int(os.environ.get("STITCHPILOT_PORT", 8000))
    print(f"StitchPilot web: http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}")
    uvicorn.run(app, host=host, port=port, proxy_headers=TRUST_PROXY, server_header=False, log_level="warning")
