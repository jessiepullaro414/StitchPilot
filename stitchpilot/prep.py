"""Image preparation: background removal, scaling, colour reduction and clean-up.

Output is a label map (one palette index per pixel, -1 = not stitched) at a
known resolution in pixels per millimetre, ready for region extraction.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, replace
from typing import Callable

import cv2
import numpy as np

from .settings import Settings

Progress = Callable[[float, str], None]
MAX_WORK_PX = 2400
MAX_PPMM = 10.0


@dataclass
class Prepared:
    labels: np.ndarray  # HxW int16, -1 = empty
    palette: list[tuple[int, int, int]]
    ppmm: float  # pixels per mm of the label map
    size_mm: tuple[float, float]
    source_rgba: np.ndarray  # cropped original (for display)

    def color_preview(self) -> np.ndarray:
        """RGBA image of the reduced-colour artwork."""
        h, w = self.labels.shape
        out = np.zeros((h, w, 4), np.uint8)
        pal = np.array(self.palette, np.uint8).reshape(-1, 3)
        m = self.labels >= 0
        out[m, :3] = pal[self.labels[m]]
        out[m, 3] = 255
        return out


# ---------------------------------------------------------------- colour utils
def rgb_to_lab(rgb: np.ndarray) -> np.ndarray:
    """uint8 RGB (...,3) -> float32 CIELAB (L 0-100)."""
    shp = rgb.shape
    flat = rgb.reshape(-1, 1, 3).astype(np.float32) / 255.0
    return cv2.cvtColor(flat, cv2.COLOR_RGB2Lab).reshape(shp).astype(np.float32)


def lab_to_rgb(lab: np.ndarray) -> np.ndarray:
    shp = lab.shape
    flat = lab.reshape(-1, 1, 3).astype(np.float32)
    rgb = cv2.cvtColor(flat, cv2.COLOR_Lab2RGB).reshape(shp)
    return np.clip(np.round(rgb * 255), 0, 255).astype(np.uint8)


def _nearest(lab_px: np.ndarray, centers: np.ndarray, chunk: int = 400_000) -> tuple[np.ndarray, np.ndarray]:
    idx = np.empty(len(lab_px), np.int16)
    dist = np.empty(len(lab_px), np.float32)
    for s in range(0, len(lab_px), chunk):
        d = np.linalg.norm(lab_px[s:s + chunk, None, :] - centers[None, :, :], axis=2)
        idx[s:s + chunk] = d.argmin(1)
        dist[s:s + chunk] = d.min(1)
    return idx, dist


# ---------------------------------------------------------------- background
def _border_pixels(img: np.ndarray, t: int = 3) -> np.ndarray:
    return np.concatenate([img[:t].reshape(-1, img.shape[-1]), img[-t:].reshape(-1, img.shape[-1]),
                           img[:, :t].reshape(-1, img.shape[-1]), img[:, -t:].reshape(-1, img.shape[-1])])


def _flood_background(lab: np.ndarray, bg_lab: np.ndarray, tol: float, enclosed: bool) -> np.ndarray:
    """Foreground mask: removes pixels close to bg colour that touch the border."""
    dist = np.linalg.norm(lab - bg_lab, axis=2)
    close = (dist < tol).astype(np.uint8)
    n, cc = cv2.connectedComponents(close, connectivity=8)
    border_ids = np.unique(_border_pixels(cc[..., None], 1))
    border_ids = border_ids[border_ids > 0]
    bg = np.isin(cc, border_ids)
    if enclosed:  # holes such as the inside of letters; stricter match so pale colours survive
        bg |= dist < min(tol, 9.0)
    return ~bg


def _grabcut(rgb: np.ndarray, init_fg: np.ndarray | None) -> np.ndarray:
    h, w = rgb.shape[:2]
    s = min(1.0, 640 / max(h, w))
    small = cv2.resize(rgb, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_AREA)
    sh, sw = small.shape[:2]
    mask = np.full((sh, sw), cv2.GC_PR_FGD, np.uint8)
    if init_fg is not None:
        f = cv2.resize(init_fg.astype(np.uint8), (sw, sh), interpolation=cv2.INTER_NEAREST)
        mask[f == 0] = cv2.GC_PR_BGD
    b = max(2, int(0.01 * max(sh, sw)))
    mask[:b], mask[-b:], mask[:, :b], mask[:, -b:] = cv2.GC_BGD, cv2.GC_BGD, cv2.GC_BGD, cv2.GC_BGD
    bgd, fgd = np.zeros((1, 65), np.float64), np.zeros((1, 65), np.float64)
    cv2.grabCut(cv2.cvtColor(small, cv2.COLOR_RGB2BGR), mask, None, bgd, fgd, 5, cv2.GC_INIT_WITH_MASK)
    fg = np.isin(mask, (cv2.GC_FGD, cv2.GC_PR_FGD)).astype(np.uint8)
    return cv2.resize(fg, (w, h), interpolation=cv2.INTER_LINEAR) > 0


def _ai_matte(rgba: np.ndarray) -> np.ndarray | None:
    """Neural background removal with rembg, if installed."""
    try:
        from rembg import remove  # type: ignore
    except Exception:
        return None
    out = remove(rgba[..., :3])
    return np.asarray(out)[..., 3] >= 128


def foreground_mask(rgba: np.ndarray, s: Settings, enclosed: bool = False) -> tuple[np.ndarray, str]:
    """Return (mask, description of what was done)."""
    alpha = rgba[..., 3]
    if s.background == "keep":
        return alpha >= 128, "kept"
    if (alpha < 200).mean() > 0.01:
        return alpha >= 128, "used transparency"
    rgb = rgba[..., :3]
    lab = rgb_to_lab(rgb)
    if s.background_rgb is not None:
        bg_lab = rgb_to_lab(np.array(s.background_rgb, np.uint8).reshape(1, 1, 3))[0, 0]
        share = 1.0
    else:
        border = _border_pixels(lab)
        bg_lab = np.median(border, axis=0)
        share = float((np.linalg.norm(border - bg_lab, axis=1) < 10).mean())
    fg = _flood_background(lab, bg_lab, 14.0, enclosed) if share > 0.55 else None
    if s.background == "ai":
        m = _ai_matte(rgba)
        if m is not None:
            return m, "AI matte (rembg)"
        return _grabcut(rgb, fg), "smart cut-out (GrabCut)"
    if fg is None:
        return np.ones(alpha.shape, bool), "no plain background found"
    return fg, "removed plain background"


# ---------------------------------------------------------------- quantize
def _kmeans(samples: np.ndarray, k: int, init: np.ndarray | None = None) -> np.ndarray:
    if init is not None:
        # refine suggested colours towards the real pixels, ignoring edge blends far from them
        c = init.astype(np.float32).copy()
        for _ in range(8):
            idx, d = _nearest(samples, c)
            for j in range(len(c)):
                sel = samples[(idx == j) & (d < 12)]
                if len(sel):
                    c[j] = sel.mean(0)
        return c
    cv2.setRNGSeed(1234)
    crit = (cv2.TERM_CRITERIA_EPS + cv2.TERM_CRITERIA_MAX_ITER, 30, 0.2)
    _, _, c = cv2.kmeans(samples, k, None, crit, 3, cv2.KMEANS_PP_CENTERS)
    return c


def _merge_close(centers: np.ndarray, counts: np.ndarray, thr: float) -> tuple[np.ndarray, np.ndarray]:
    centers, counts = centers.copy(), counts.astype(np.float64).copy()
    while len(centers) > 1:
        d = np.linalg.norm(centers[:, None] - centers[None], axis=2)
        np.fill_diagonal(d, 1e9)
        i, j = np.unravel_index(d.argmin(), d.shape)
        if d[i, j] >= thr:
            break
        tot = counts[i] + counts[j]
        centers[i] = (centers[i] * counts[i] + centers[j] * counts[j]) / max(tot, 1)
        counts[i] = tot
        centers, counts = np.delete(centers, j, 0), np.delete(counts, j)
    return centers, counts


def _drop_edge_blends(centers: np.ndarray, counts: np.ndarray) -> np.ndarray:
    """Drop tiny clusters that are just anti-aliasing blends between two others."""
    share = counts / counts.sum()
    keep = np.ones(len(centers), bool)
    for c in range(len(centers)):
        if share[c] > 0.02:
            continue
        for a in range(len(centers)):
            for b in range(a + 1, len(centers)):
                if c in (a, b) or not keep[a] or not keep[b] or share[a] < share[c] or share[b] < share[c]:
                    continue
                ab = centers[b] - centers[a]
                t = np.clip(np.dot(centers[c] - centers[a], ab) / max(1e-6, np.dot(ab, ab)), 0.05, 0.95)
                if np.linalg.norm(centers[a] + t * ab - centers[c]) < 6.0:
                    keep[c] = False
    return centers[keep]


def _unexplained(px: np.ndarray, centers: np.ndarray, tol: float = 10.0) -> int:
    """Count pixels that are not close to any blend between two palette colours."""
    if len(px) == 0 or len(centers) < 2:
        return len(px)
    best = np.full(len(px), np.inf)
    for a in range(len(centers)):
        for b in range(a + 1, len(centers)):
            ab = centers[b] - centers[a]
            t = np.clip((px - centers[a]) @ ab / max(1e-6, ab @ ab), 0, 1)
            best = np.minimum(best, np.linalg.norm(centers[a] + t[:, None] * ab - px, axis=1))
    return int((best > tol).sum())


def quantize(lab: np.ndarray, mask: np.ndarray, s: Settings) -> np.ndarray:
    """Return palette centres (K,3) in LAB."""
    px = lab[mask]
    rng = np.random.default_rng(0)
    samples = px[rng.choice(len(px), min(len(px), 40_000), replace=False)].astype(np.float32)
    if s.palette:
        init = rgb_to_lab(np.array(s.palette, np.uint8).reshape(-1, 1, 3)).reshape(-1, 3)
        centers = _kmeans(samples, len(init), init)
    elif s.num_colors > 0:
        centers = _kmeans(samples, min(s.num_colors, len(samples)))
    else:
        # enough colours when the average error is low AND no distinct colour is left over
        # (pixels that are neither near a centre nor a blend of two centres = missing colour)
        thr = 9.0 - 4.5 * s.detail
        for k in range(2, 15):
            centers = _kmeans(samples, k)
            _, d = _nearest(samples, centers)
            if d.mean() < thr and _unexplained(samples[d > 14], centers) < 0.004 * len(samples):
                break
    idx, _ = _nearest(samples, centers)
    counts = np.bincount(idx, minlength=len(centers))
    centers, counts = centers[counts > 0], counts[counts > 0]
    thr = s.merge_de if s.merge_de is not None else (5.0 if (s.palette or s.num_colors) else 8.0)
    centers, counts = _merge_close(centers, counts, thr)
    if not s.palette and s.num_colors == 0:
        centers = _drop_edge_blends(centers, counts)
    return centers


# ---------------------------------------------------------------- clean-up
def mode_filter(labels: np.ndarray, n_classes: int, radius: int) -> np.ndarray:
    """Majority filter on a label map (0..n_classes-1)."""
    if radius < 1:
        return labels
    k = 2 * radius + 1
    best = np.full(labels.shape, -1.0, np.float32)
    out = labels.copy()
    for c in range(n_classes):
        score = cv2.blur((labels == c).astype(np.float32), (k, k))
        score += (labels == c) * 1e-3  # ties keep the original label
        upd = score > best
        best[upd], out[upd] = score[upd], c
    return out


def merge_small(labels: np.ndarray, n_classes: int, min_px: int) -> np.ndarray:
    lab = labels.copy()
    kernel = np.ones((3, 3), np.uint8)
    for _ in range(3):
        changed = False
        for c in range(n_classes):
            n, cc, stats, _ = cv2.connectedComponentsWithStats((lab == c).astype(np.uint8), connectivity=8)
            small = np.nonzero(stats[1:, cv2.CC_STAT_AREA] < min_px)[0] + 1
            for i in small[np.argsort(stats[small, cv2.CC_STAT_AREA])]:
                x, y, w, h = stats[i, :4]
                x0, y0 = max(0, x - 1), max(0, y - 1)
                x1, y1 = min(lab.shape[1], x + w + 1), min(lab.shape[0], y + h + 1)
                sub = cc[y0:y1, x0:x1] == i
                ring = cv2.dilate(sub.astype(np.uint8), kernel).astype(bool) & ~sub
                win = lab[y0:y1, x0:x1]
                nb = win[ring]
                nb = nb[nb != c]
                if len(nb) == 0:
                    continue
                win[sub] = np.bincount(nb).argmax()
                changed = True
        if not changed:
            break
    return lab


THIN_MM = 1.0  # narrower than this can't be sewn as a proper satin


def thin_parts(mask: np.ndarray, ppmm: float) -> np.ndarray:
    """Pixels of a shape that belong to parts narrower than THIN_MM."""
    d = max(3, int(round(THIN_MM * ppmm)) | 1)
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (d, d))
    m = mask.astype(np.uint8)
    opened = cv2.morphologyEx(m, cv2.MORPH_OPEN, k)
    thin = (m > 0) & (opened == 0)
    # ignore the slivers an opening shaves off corners of wide shapes
    near_wide = cv2.dilate(opened, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))) > 0
    n, cc, stats, _ = cv2.connectedComponentsWithStats(thin.astype(np.uint8), connectivity=8)
    keep = np.zeros(n, bool)
    for i in range(1, n):
        if stats[i, cv2.CC_STAT_AREA] >= d * d:  # long enough to be a real line, not a corner
            keep[i] = True
    out = keep[cc] & thin
    return out | (thin & ~near_wide)


def fix_thin(labels: np.ndarray, empty: int, ppmm: float, mode: str) -> np.ndarray:
    """Thicken hair-thin lines to a sewable width, or remove them."""
    lab = labels.copy()
    r = max(1, int(round(THIN_MM * ppmm / 2)))
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
    thin_all = np.zeros(lab.shape, bool)
    for c in range(empty):
        thin = thin_parts(lab == c, ppmm)
        if not thin.any():
            continue
        thin_all |= thin
        if mode == "thicken":
            grown = cv2.dilate(thin.astype(np.uint8), k) > 0
            lab[grown] = c
    if mode == "remove" and thin_all.any():
        # hand each removed pixel to the nearest pixel that stays
        src = (~thin_all).astype(np.uint8)
        _, idx = cv2.distanceTransformWithLabels(1 - src, cv2.DIST_L2, 3, labelType=cv2.DIST_LABEL_PIXEL)
        ys, xs = np.nonzero(src)
        lut = np.zeros(idx.max() + 1, np.int16)
        lut[idx[ys, xs]] = lab[ys, xs]
        lab[thin_all] = lut[idx[thin_all]]
    return lab


# ---------------------------------------------------------------- main entry
def _crop_to(rgba: np.ndarray, mask: np.ndarray) -> np.ndarray | None:
    ys, xs = np.nonzero(mask)
    if len(xs) == 0:
        return None
    out = rgba[ys.min():ys.max() + 1, xs.min():xs.max() + 1].copy()
    out[..., 3] = np.where(mask[ys.min():ys.max() + 1, xs.min():xs.max() + 1], 255, 0)
    return out


def _fit(wpx: int, hpx: int, s: Settings) -> float:
    return min(s.max_width_mm / wpx, s.max_height_mm / hpx)


def inputs(s: Settings) -> tuple:
    """Every setting prepare() reads (directly or through foreground_mask, quantize and lettering). While these
    stay the same, the last Prepared can be reused: stitch settings alone don't need the picture worked out again.
    A copy, so later edits to the settings' lists can't change it."""
    return copy.deepcopy((s.background, s.background_rgb, s.remove_enclosed_bg, s.max_width_mm, s.max_height_mm,
                          s.rotate, s.mirror, s.detail, s.smooth, s.num_colors, s.palette, s.merge_de,
                          s.min_region_mm2, s.thin_lines, s.texts, s.text_fixes))


def prepare(rgba: np.ndarray | None, s: Settings, progress: Progress | None = None,
            ppmm_cap: float = MAX_PPMM) -> Prepared:
    prog = progress or (lambda f, m: None)
    prog(0.02, "Finding the design…")
    crop = None
    if rgba is not None:
        if s.text_fixes:  # AI text clean-up: redraw the picture's text crisply first
            from .textfix import apply_fixes

            prog(0.01, "Redrawing text…")
            rgba = apply_fixes(rgba, s.text_fixes)
        if s.rotate % 360:
            rgba = np.ascontiguousarray(np.rot90(rgba, -(s.rotate % 360) // 90))
        if s.mirror:
            rgba = np.ascontiguousarray(rgba[:, ::-1])
        # work on a bounded copy so huge photos stay fast
        h0, w0 = rgba.shape[:2]
        pre = min(1.0, 3000 / max(h0, w0))
        if pre < 1:
            rgba = cv2.resize(rgba, (int(w0 * pre), int(h0 * pre)), interpolation=cv2.INTER_AREA)
        mask, _ = foreground_mask(rgba, s, s.remove_enclosed_bg)
        crop = _crop_to(rgba, mask)
    texts = [t for t in s.texts if t.text.strip()]
    text_only_mpp = None
    if texts:
        from .lettering import compose

        img = crop
        if img is None:  # lettering on its own keeps the requested letter height (unless it won't fit)
            crop = compose(None, texts, 0.1)
            text_only_mpp = min(0.1, _fit(crop.shape[1], crop.shape[0], s))
        else:
            mpp = _fit(*img.shape[1::-1], s)
            for _ in range(3):  # text size depends on the final scale, which depends on the text
                crop = compose(img, texts, mpp)
                mpp = _fit(crop.shape[1], crop.shape[0], s)
        if s.palette:
            s = replace(s, palette=list(s.palette) + [t.rgb for t in texts])
    if crop is None or not (crop[..., 3] >= 128).any():
        raise ValueError("Nothing left to stitch after removing the background. Try Background: Stitch everything.")
    cmask = crop[..., 3] >= 128

    # ---- physical size and working resolution
    hpx, wpx = cmask.shape
    mm_per_px = text_only_mpp or _fit(wpx, hpx, s)
    w_mm, h_mm = wpx * mm_per_px, hpx * mm_per_px
    ppmm = min(ppmm_cap, MAX_WORK_PX / max(w_mm, h_mm))
    W, H = max(8, round(w_mm * ppmm)), max(8, round(h_mm * ppmm))
    interp = cv2.INTER_AREA if W < wpx else cv2.INTER_CUBIC
    rgb = crop[..., :3].copy()
    if cmask.any():  # stop transparent/background colour bleeding into edges
        rgb[~cmask] = np.median(rgb[cmask], axis=0).astype(np.uint8)
    rgb = cv2.resize(rgb, (W, H), interpolation=interp)
    m = cv2.resize(cmask.astype(np.float32), (W, H), interpolation=cv2.INTER_LINEAR) > 0.5
    pad = 3
    rgb = cv2.copyMakeBorder(rgb, pad, pad, pad, pad, cv2.BORDER_REPLICATE)
    m = np.pad(m, pad)

    # ---- edge-preserving smoothing (stronger for "bold")
    prog(0.12, "Smoothing…")
    strength = 1.0 - s.detail
    if strength > 0.4:
        rgb = cv2.medianBlur(rgb, 3 if strength < 0.75 else 5)
    rgb = cv2.bilateralFilter(rgb, 0, 18 + 30 * strength, max(1.0, ppmm * (0.25 + 0.6 * strength)))

    # ---- colour reduction
    prog(0.22, "Picking thread colours…")
    lab = rgb_to_lab(rgb)
    centers = quantize(lab, m, s)
    idx, _ = _nearest(lab.reshape(-1, 3), centers)
    k = len(centers)
    labels = np.where(m.reshape(-1), idx, k).reshape(m.shape).astype(np.int16)  # k = empty

    # ---- clean-up: smooth jaggies, then absorb specks too small to sew
    prog(0.35, "Cleaning up shapes…")
    radius = max(1, round(ppmm * (0.15 + 0.35 * strength + 0.4 * s.smooth)))
    labels = mode_filter(labels, k + 1, radius)
    min_px = max(4, int(s.min_region_mm2 * ppmm * ppmm))
    labels = merge_small(labels, k + 1, min_px)
    if s.thin_lines != "keep":
        labels = fix_thin(labels, k, ppmm, s.thin_lines)
        labels = merge_small(labels, k + 1, min_px)
    labels = mode_filter(labels, k + 1, 1)
    labels[labels == k] = -1

    # drop unused colours and renumber
    used = [c for c in range(k) if (labels == c).any()]
    remap = np.full(k + 1, -1, np.int16)
    remap[used] = np.arange(len(used))
    labels = np.where(labels >= 0, remap[np.clip(labels, 0, k)], -1).astype(np.int16)
    palette = [tuple(int(v) for v in c) for c in lab_to_rgb(centers[used].reshape(-1, 1, 3)).reshape(-1, 3)]
    return Prepared(labels, palette, ppmm, (w_mm, h_mm), crop)
