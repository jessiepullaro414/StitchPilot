"""StitchPilot Plus cut-out: finds the subject of a photo with BiRefNet (MIT licence), on AWS Lambda.

Only the AI gateway calls this function (it has no address of its own): the gateway checks the member's
allowance first, then invokes it with {"image": base64 PNG/JPEG} and gets back {"mask": base64 PNG} (the
subject in white, the same size as the picture) or {"error": message}.

The model is too big for a Lambda zip, so it lives in S3 (MODEL_BUCKET / MODEL_KEY) and is copied to /tmp
when a new instance starts, then checked against MODEL_SHA256 before it's used.
"""
from __future__ import annotations

import base64
import hashlib
import io
import logging
import os

import numpy as np
from PIL import Image

log = logging.getLogger()
log.setLevel(logging.INFO)

SIZE = 1024  # the model's input size
MAX_PX = 2048  # longest side accepted
MAX_PIXELS = 4096 * 4096  # refuse decompression bombs before decoding
MODEL_FILE = "/tmp/birefnet.onnx"

_session = None


def _load():
    global _session
    if _session is None:
        import boto3
        import onnxruntime as ort

        if not os.path.exists(MODEL_FILE):
            boto3.client("s3").download_file(os.environ["MODEL_BUCKET"], os.environ["MODEL_KEY"], MODEL_FILE + ".part")
            h = hashlib.sha256()
            with open(MODEL_FILE + ".part", "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
            if h.hexdigest() != os.environ["MODEL_SHA256"].lower():
                os.remove(MODEL_FILE + ".part")
                raise RuntimeError("the model in S3 doesn't match MODEL_SHA256")
            os.replace(MODEL_FILE + ".part", MODEL_FILE)
        opts = ort.SessionOptions()
        opts.log_severity_level = 3
        _session = ort.InferenceSession(MODEL_FILE, opts, providers=["CPUExecutionProvider"])
    return _session


def model_input(img: Image.Image) -> np.ndarray:
    """BiRefNet's input: SIZE x SIZE, scaled to the brightest value, ImageNet-normalised, NCHW."""
    im = np.asarray(img.convert("RGB").resize((SIZE, SIZE), Image.Resampling.LANCZOS), np.float32)
    im = im / max(float(im.max()), 1e-6)
    im = (im - np.array([0.485, 0.456, 0.406], np.float32)) / np.array([0.229, 0.224, 0.225], np.float32)
    return im.transpose(2, 0, 1)[None]


def cut_out(img: Image.Image, session) -> Image.Image:
    pred = session.run(None, {session.get_inputs()[0].name: model_input(img)})[0][0, 0]
    pred = 1 / (1 + np.exp(-pred))
    lo, hi = float(pred.min()), float(pred.max())
    pred = (pred - lo) / max(hi - lo, 1e-6)
    return Image.fromarray((pred * 255).clip(0, 255).astype(np.uint8), "L").resize(img.size, Image.Resampling.LANCZOS)


def decode(data: str) -> Image.Image:
    Image.MAX_IMAGE_PIXELS = MAX_PIXELS
    img = Image.open(io.BytesIO(base64.b64decode(data, validate=True)))
    if img.format not in ("PNG", "JPEG", "WEBP") or max(img.size) > MAX_PX:
        raise ValueError("unsupported picture")
    img.load()
    return img


def lambda_handler(event, context=None):
    try:
        img = decode(event["image"])
    except Exception:  # anything odd about the picture
        return {"error": "That picture couldn't be read (PNG, JPEG or WebP up to 2048 px)."}
    try:
        mask = cut_out(img, _load())
    except Exception:
        log.exception("cut-out failed")
        return {"error": "The cut-out didn't work. Try again in a minute."}
    buf = io.BytesIO()
    mask.save(buf, "PNG", optimize=True)
    return {"mask": base64.b64encode(buf.getvalue()).decode()}
