"""StitchPilot project files (.stitchpilot): the picture, every setting and any hand edits, in one file.

A project is a zip archive:
  project.json      settings and colour edits (see MainWindow.get_state)
  source/<name>     the original picture or embroidery file, byte for byte (keeps SVGs as vectors)
  stitches.npz      hand-edited stitches, only when the design was edited by hand
"""
from __future__ import annotations

import io
import json
import zipfile
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import __version__
from .digitize import ColorBlock

EXT = ".stitchpilot"
FORMAT = 1


@dataclass
class Project:
    state: dict
    source_name: str | None
    source_bytes: bytes | None
    blocks: list[ColorBlock] | None  # hand-edited stitches (None = re-digitize from the picture)
    size_mm: tuple[float, float] | None


def _blocks_to_npz(blocks: list[ColorBlock]) -> tuple[bytes, list[dict]]:
    arrays, meta = {}, []
    for i, b in enumerate(blocks):
        arrays[f"cmds_{i}"] = b.cmds.astype(np.float64)
        for j, ring in enumerate(b.fabric or []):
            arrays[f"fabric_{i}_{j}"] = np.asarray(ring, np.float64)
        meta.append({"rgb": list(b.rgb), "source_rgb": list(b.source_rgb), "area_mm2": b.area_mm2,
                     "kinds": b.kinds, "thread": b.thread, "visible": b.visible,
                     "fabric": len(b.fabric or [])})
    buf = io.BytesIO()
    np.savez_compressed(buf, **arrays)
    return buf.getvalue(), meta


def _blocks_from_npz(data: bytes, meta: list[dict]) -> list[ColorBlock]:
    arrays = np.load(io.BytesIO(data))
    blocks = []
    for i, m in enumerate(meta):
        fabric = [arrays[f"fabric_{i}_{j}"] for j in range(m.get("fabric", 0))] or None
        blocks.append(ColorBlock(tuple(m["rgb"]), arrays[f"cmds_{i}"], float(m.get("area_mm2", 0.0)),
                                 dict(m.get("kinds", {})), m.get("thread", ""), bool(m.get("visible", True)),
                                 tuple(m["source_rgb"]), fabric))
    return blocks


def save(path: str | Path, state: dict, source_name: str | None = None, source_bytes: bytes | None = None,
         blocks: list[ColorBlock] | None = None, size_mm: tuple[float, float] | None = None) -> Path:
    """Write a project file atomically (a crash mid-save never leaves a half-written file)."""
    path = Path(path)
    doc = {"format": FORMAT, "app": f"StitchPilot {__version__}", "state": state,
           "source": Path(source_name).name if source_name else None,
           "edited": blocks is not None, "size_mm": list(size_mm) if size_mm else None}
    tmp = path.with_name(path.name + ".saving")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        if blocks is not None:
            data, meta = _blocks_to_npz(blocks)
            doc["blocks"] = meta
            z.writestr("stitches.npz", data, compress_type=zipfile.ZIP_STORED)  # already compressed
        if source_bytes is not None and source_name:
            z.writestr(f"source/{Path(source_name).name}", source_bytes)
        z.writestr("project.json", json.dumps(doc, indent=1))
    tmp.replace(path)
    return path


def load(path: str | Path) -> Project:
    try:
        with zipfile.ZipFile(path) as z:
            doc = json.loads(z.read("project.json"))
            if doc.get("format", 0) > FORMAT:
                raise ValueError("This project was made with a newer StitchPilot. Please update to open it.")
            name = doc.get("source")
            src = z.read(f"source/{name}") if name and f"source/{name}" in z.namelist() else None
            blocks = None
            if doc.get("edited") and "stitches.npz" in z.namelist():
                blocks = _blocks_from_npz(z.read("stitches.npz"), doc.get("blocks", []))
    except (zipfile.BadZipFile, KeyError, json.JSONDecodeError) as e:
        raise ValueError(f"This isn't a StitchPilot project, or it is damaged ({e.__class__.__name__}).")
    size = tuple(doc["size_mm"]) if doc.get("size_mm") else None
    return Project(doc.get("state", {}), name, src, blocks, size)
