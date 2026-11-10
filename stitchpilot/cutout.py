"""Cutting the subject out of a busy photo ("Cut out subject" in Background).

Three ways, best first:
- a cut-out from StitchPilot Plus (BiRefNet on StitchPilot's AI service, gateway/cutout): asked for by the
  person, one AI action per picture. Kept for that picture (remember()) and saved in the project, so
  changing settings never asks again.
- ISNet, an open cut-out model run on this computer (onnxruntime). Free; downloaded once (MODEL, ~180 MB)
  when the person agrees, checked against its SHA-256 before it's used. onnxruntime has no build for Intel
  Macs, so there it isn't offered (runtime_ok()).
- GrabCut (OpenCV): always there, but it often keeps background or loses parts of the subject.

matte() picks the best one available and caches the answer per picture.
"""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import sys
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np


@dataclass(frozen=True)
class Model:
    name: str
    file: str
    url: str
    sha256: str
    size: int


# ISNet "general use" (Qin et al., Apache-2.0), as published by the rembg project
MODEL = Model("ISNet", "isnet-general-use.onnx",
              "https://github.com/danielgatis/rembg/releases/download/v0.0.0/isnet-general-use.onnx",
              "60920e99c45464f2ba57bee2ad08c919a52bbf852739e96947fbb4358c0d964a", 178_648_008)
SIZE = 1024  # the model's input size
CLOUD_PX = 1536  # longest side sent to StitchPilot Plus

ON_DEVICE, CLOUD, BASIC = "on-device AI", "StitchPilot Plus", "basic"
MODEL_DIR: Path | None = None  # set by the app (next to its other data); else the user's cache folder


class CutoutError(Exception):
    """Shown to people as is."""


def model_dir() -> Path:
    if os.environ.get("STITCHPILOT_MODELS"):
        return Path(os.environ["STITCHPILOT_MODELS"])
    if MODEL_DIR is not None:
        return MODEL_DIR
    if sys.platform == "win32":
        base = Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME") or Path.home() / ".cache")
    return base / "StitchPilot" / "models"


def model_path() -> Path:
    return model_dir() / MODEL.file


def runtime_ok() -> bool:
    """Whether this computer can run the on-device model (onnxruntime is installed: not on Intel Macs)."""
    try:
        return importlib.util.find_spec("onnxruntime") is not None
    except (ImportError, ValueError):
        return False


def model_ready() -> bool:
    """Whether the on-device model is downloaded (its size is checked here, its hash when downloaded)."""
    p = model_path()
    return p.is_file() and p.stat().st_size == MODEL.size


def download(progress=None, cancelled=lambda: False) -> Path:
    """Downloads the on-device model, checking its size and SHA-256. progress(done_bytes, total_bytes)."""
    dest = model_path()
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    h, done = hashlib.sha256(), 0
    req = urllib.request.Request(MODEL.url, headers={"user-agent": "StitchPilot"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
            while chunk := r.read(1 << 20):
                done += len(chunk)
                if done > MODEL.size:
                    raise CutoutError("The download is larger than it should be, so it was thrown away.")
                h.update(chunk)
                f.write(chunk)
                if progress:
                    progress(done, MODEL.size)
                if cancelled():
                    raise CutoutError("Download cancelled.")
        if done != MODEL.size or h.hexdigest() != MODEL.sha256:
            raise CutoutError("The download didn't match the expected file, so it was thrown away. Try again later.")
        os.replace(part, dest)
        return dest
    except PermissionError:
        raise CutoutError(f"StitchPilot can't write to {dest.parent}.") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise CutoutError("The download stopped. Check your internet connection and try again.") from None
    finally:
        part.unlink(missing_ok=True)


# ---------------------------------------------------------------- the models
_session = None
_session_lock = threading.Lock()


def _ort_session():
    global _session
    with _session_lock:
        if _session is None:
            import onnxruntime as ort  # only when it's used: it's big

            opts = ort.SessionOptions()
            opts.log_severity_level = 3
            _session = ort.InferenceSession(str(model_path()), opts, providers=["CPUExecutionProvider"])
        return _session


def model_input(rgb: np.ndarray, mean, std) -> np.ndarray:
    """The picture as the model wants it: SIZE x SIZE, scaled to its brightest value, normalised, NCHW."""
    im = cv2.resize(rgb, (SIZE, SIZE), interpolation=cv2.INTER_AREA if max(rgb.shape[:2]) > SIZE else cv2.INTER_CUBIC)
    im = im.astype(np.float32) / max(float(im.max()), 1e-6)
    im = (im - np.array(mean, np.float32)) / np.array(std, np.float32)
    return im.transpose(2, 0, 1)[None]


def _stretch(pred: np.ndarray) -> np.ndarray:
    lo, hi = float(pred.min()), float(pred.max())
    return (pred - lo) / max(hi - lo, 1e-6)


def on_device(rgba: np.ndarray) -> np.ndarray:
    """The subject (alpha 0..255, uint8) found by the on-device model."""
    if not model_ready():
        raise CutoutError("The cut-out model isn't downloaded.")
    sess = _ort_session()
    x = model_input(rgba[..., :3], (0.485, 0.456, 0.406), (1.0, 1.0, 1.0))
    pred = _stretch(sess.run(None, {sess.get_inputs()[0].name: x})[0][0, 0])
    h, w = rgba.shape[:2]
    return (cv2.resize(pred, (w, h), interpolation=cv2.INTER_LINEAR) * 255).clip(0, 255).astype(np.uint8)


def cloud(rgba: np.ndarray, token: str) -> np.ndarray:
    """The subject (alpha 0..255, uint8) from StitchPilot Plus. Uses one AI action."""
    from . import account

    h, w = rgba.shape[:2]
    k = min(1.0, CLOUD_PX / max(h, w))
    rgb = rgba[..., :3]
    if k < 1:
        rgb = cv2.resize(rgb, (round(w * k), round(h * k)), interpolation=cv2.INTER_AREA)
    ok, png = cv2.imencode(".png", cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR))
    if not ok:
        raise CutoutError("Couldn't prepare the picture.")
    body = json.dumps({"image": base64.b64encode(png.tobytes()).decode()}).encode()
    req = urllib.request.Request(account._gateway() + "/cutout", data=body, method="POST",
                                 headers={"content-type": "application/json", "authorization": f"Bearer {token}",
                                          "user-agent": "StitchPilot"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            doc = json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            err = json.loads(e.read() or b"{}")
        except ValueError:
            err = {}
        refusal = account.gateway_error(e.code, err)
        if refusal is not None:
            raise refusal from None
        raise CutoutError((err.get("error") or {}).get("message") or "StitchPilot's AI service couldn't cut out "
                          "this picture. Try again in a minute.") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise CutoutError("Couldn't reach StitchPilot's AI service. Check your internet connection.") from None
    try:
        mask = cv2.imdecode(np.frombuffer(base64.b64decode(doc["mask"]), np.uint8), cv2.IMREAD_GRAYSCALE)
    except (KeyError, TypeError, ValueError):
        mask = None
    if mask is None:
        raise CutoutError("StitchPilot's AI service sent back something unexpected. Try again in a minute.")
    return cv2.resize(mask, (w, h), interpolation=cv2.INTER_LINEAR)


# ---------------------------------------------------------------- per picture
_cache: dict[str, tuple[np.ndarray, str]] = {}  # picture key -> (alpha, how)
_cache_lock = threading.Lock()


def key(rgba: np.ndarray) -> str:
    return hashlib.sha1(np.ascontiguousarray(rgba).tobytes() + repr(rgba.shape).encode()).hexdigest()


def remember(rgba: np.ndarray, alpha: np.ndarray, how: str = CLOUD) -> None:
    """Keep a cut-out for this picture (a Plus cut-out, or one loaded from a project)."""
    with _cache_lock:
        _cache[key(rgba)] = (alpha, how)


def known(rgba: np.ndarray) -> tuple[np.ndarray, str] | None:
    with _cache_lock:
        return _cache.get(key(rgba))


def matte(rgba: np.ndarray, basic) -> tuple[np.ndarray, str]:
    """(alpha 0..255, how) for the subject of this picture: a Plus cut-out if there is one, else the on-device
    model if it's downloaded, else basic(rgba) (GrabCut, a bool mask)."""
    k = key(rgba)
    usable = model_ready() and runtime_ok()
    with _cache_lock:
        hit = _cache.get(k)
    if hit is not None and (hit[1] != BASIC or not usable):
        return hit
    if usable:
        try:
            res = (on_device(rgba), ON_DEVICE)
        except Exception:  # a broken download or a missing runtime: fall back rather than fail the design
            res = (basic(rgba).astype(np.uint8) * 255, BASIC)
    else:
        res = (basic(rgba).astype(np.uint8) * 255, BASIC)
    with _cache_lock:
        _cache[k] = res
    return res


def to_png(alpha: np.ndarray) -> bytes:
    ok, png = cv2.imencode(".png", alpha)
    return png.tobytes() if ok else b""


def from_png(data: bytes) -> np.ndarray | None:
    try:
        return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_GRAYSCALE)
    except cv2.error:
        return None
