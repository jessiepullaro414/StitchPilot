"""How hard is this picture to embroider? Score, plain-language reasons and a map of problem spots."""
from __future__ import annotations

from dataclasses import dataclass, field, replace

import cv2
import numpy as np

from .prep import Prepared, prepare, rgb_to_lab, thin_parts
from .settings import Settings

TINY_MM2 = 2.5  # shapes smaller than this sew as blobs (or not at all)
LEVELS = [(25, "Simple"), (50, "Moderate"), (75, "Complex"), (101, "Very complex")]


@dataclass
class Complexity:
    score: int  # 0 (easy) .. 100 (very hard)
    level: str  # Simple / Moderate / Complex / Very complex / Photo
    reasons: list[str] = field(default_factory=list)
    colors: int = 0
    shapes: int = 0
    tiny: int = 0
    thin_share: float = 0.0
    shading: float = 0.0
    shapes_per_cm2: float = 0.0
    problems: np.ndarray | None = None  # bool mask over Prepared.labels: tiny shapes and hair-thin lines

    @property
    def friendly(self) -> bool:
        """Meets the targets used by 'Make it embroidery-friendly'."""
        return (self.colors <= 8 and self.thin_share < 0.04 and self.shapes_per_cm2 < 1.2
                and self.tiny <= max(3, 0.05 * self.shapes))


def shading_share(rgba: np.ndarray) -> float:
    """Share of the picture covered by smooth gradients (photos, airbrushing) rather than flat colour."""
    a = rgba[..., 3] >= 128
    h, w = a.shape
    s = min(1.0, 500 / max(h, w))
    small = cv2.resize(rgba, (max(8, int(w * s)), max(8, int(h * s))), interpolation=cv2.INTER_AREA)
    m = cv2.erode((small[..., 3] >= 128).astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
    if m.sum() < 50:
        return 0.0
    L = rgb_to_lab(small[..., :3])[..., 0]
    gx, gy = cv2.Sobel(L, cv2.CV_32F, 1, 0), cv2.Sobel(L, cv2.CV_32F, 0, 1)
    g = cv2.GaussianBlur(np.hypot(gx, gy), (5, 5), 0)
    edges = cv2.dilate(cv2.Canny(small[..., :3], 60, 160), np.ones((5, 5), np.uint8)) > 0
    smooth = (g > 1.5) & (g < 30) & ~edges  # gentle change away from hard edges
    return float(smooth[m].mean())


def analyse(prep: Prepared) -> Complexity:
    labels, ppmm = prep.labels, prep.ppmm
    fg = labels >= 0
    area_cm2 = max(prep.size_mm[0] * prep.size_mm[1] / 100.0, 0.01)
    problems = np.zeros(labels.shape, bool)
    shapes = tiny = 0
    thin_px = 0
    for c in range(len(prep.palette)):
        m = labels == c
        n, cc, stats, _ = cv2.connectedComponentsWithStats(m.astype(np.uint8), connectivity=8)
        areas = stats[1:, cv2.CC_STAT_AREA] / (ppmm * ppmm)
        shapes += n - 1
        small = np.nonzero(areas < TINY_MM2)[0] + 1
        tiny += len(small)
        if len(small):
            problems |= np.isin(cc, small)
        t = thin_parts(m, ppmm)
        thin_px += int(t.sum())
        problems |= t
    thin_share = thin_px / max(1, int(fg.sum()))
    shading = shading_share(prep.source_rgba)
    density = shapes / area_cm2
    colors = len(prep.palette)

    # each factor contributes up to its weight once it reaches a "clearly hard" value
    parts = {
        "colors": 20 * min(1.0, max(0, colors - 4) / 8),
        "shapes": 25 * min(1.0, density / 2.0),
        "tiny": 20 * min(1.0, tiny / max(8.0, 0.25 * shapes)),
        "thin": 15 * min(1.0, thin_share / 0.10),
        "shading": 20 * min(1.0, shading / 0.30),
    }
    score = int(round(sum(parts.values())))
    level = next(name for limit, name in LEVELS if score < limit)
    if shading > 0.3:
        level = "Photo"
    reasons = []
    if shading > 0.2:
        reasons.append(f"{shading:.0%} of it is soft shading – thread can only do flat colour, so it will look posterised")
    if colors > 8:
        reasons.append(f"{colors} thread colours – merging similar ones saves thread changes")
    if density > 1.2:
        reasons.append(f"{shapes} separate shapes ({density:.1f} per cm²) – lots of trims and jumps")
    if tiny > max(3, 0.05 * shapes):
        reasons.append(f"{tiny} shapes smaller than {TINY_MM2:g} mm² – too small to sew cleanly")
    if thin_share > 0.04:
        reasons.append(f"{thin_share:.0%} of the design is lines thinner than 1 mm – they'll look broken")
    if not reasons:
        reasons.append("Clean shapes and few colours – should sew out well")
    return Complexity(score, level, reasons, colors, shapes, tiny, thin_share, shading, density, problems)


FAST_PPMM = 5.0  # analysis resolution: plenty for mm-based measurements, 4x faster than stitching


def analyse_source(rgba: np.ndarray | None, s: Settings) -> Complexity:
    """Complexity of the picture itself: reduced with nearly every detail kept, before any simplifying
    (just enough clean-up to ignore JPEG speckle and anti-aliasing)."""
    keep_all = replace(s.simplified(0.15), num_colors=0, palette=None)
    return analyse(prepare(rgba, keep_all, ppmm_cap=FAST_PPMM))


def find_friendly(rgba: np.ndarray | None, s: Settings, progress=None) -> tuple[float, int, Complexity]:
    """Smallest 'Simplicity' amount (and colour cap, 0 = automatic) that makes the picture
    embroidery-friendly, or the best result found."""
    prog = progress or (lambda f, m: None)
    colors = s.num_colors if 0 < s.num_colors <= 8 else 0
    levels = [0.2, 0.35, 0.5, 0.65, 0.8, 0.95]

    def check(m, cap):
        return analyse(prepare(rgba, replace(s.simplified(m), num_colors=colors), ppmm_cap=cap))

    # quick pass at low resolution to find a candidate...
    start = len(levels) - 1
    for i, m in enumerate(levels):
        prog(0.7 * i / len(levels), f"Trying simplicity {int(m * 100)}%…")
        c = check(m, FAST_PPMM)
        if c.colors > 8 and colors == 0:  # automatic colours can't get there: cap at 8 from now on
            colors = 8
            c = check(m, FAST_PPMM)
        if c.friendly:
            start = i
            break
    # ...then confirm at full resolution (fine detail survives better there), stepping up if needed
    best = None
    for m in levels[start:]:
        prog(0.75, f"Checking simplicity {int(m * 100)}% at full detail…")
        c = check(m, 10.0)
        if best is None or c.score < best[2].score:
            best = (m, colors, c)
        if c.friendly:
            return m, colors, c
    return best


def problem_overlay(prep: Prepared, problems: np.ndarray) -> np.ndarray:
    """Colours preview with problem spots in red and everything else faded."""
    img = prep.color_preview().astype(np.float32)
    fg = img[..., 3] > 0
    img[..., :3] = np.where(fg[..., None], img[..., :3] * 0.35 + 255 * 0.65, img[..., :3])
    grow = cv2.dilate(problems.astype(np.uint8), np.ones((3, 3), np.uint8)) > 0
    img[grow, :3] = (225, 30, 45)
    img[grow, 3] = 255
    return img.astype(np.uint8)
