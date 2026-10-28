"""Digitizing settings, hoop sizes and fabric presets."""
from __future__ import annotations

from dataclasses import dataclass, field, replace

# name -> (width mm, height mm) of the sewable field
HOOPS: dict[str, tuple[float, float]] = {
    '2.5" x 2.5" (patch, 64 x 64 mm)': (64, 64),
    '4" x 4"  (100 x 100 mm)': (100, 100),
    '4" x 5"  (100 x 130 mm)': (100, 130),
    '5.5" x 5.5" (140 x 140 mm)': (140, 140),
    '5" x 7"  (130 x 180 mm)': (130, 180),
    '6.7" x 7.9" (170 x 200 mm)': (170, 200),
    '8" x 8"  (200 x 200 mm)': (200, 200),
    '9.4" x 5.9" (240 x 150 mm)': (240, 150),
    '10.2" x 5.9" (260 x 150 mm)': (260, 150),
    '5.7" x 10" (145 x 255 mm)': (145, 255),
    '6" x 10" (160 x 260 mm)': (160, 260),
    '7.9" x 11" (200 x 280 mm)': (200, 280),
    '8" x 12" (200 x 300 mm)': (200, 300),
    '14" x 8" (360 x 200 mm)': (360, 200),
    '8.3" x 15.7" (210 x 400 mm)': (210, 400),
}
DEFAULT_HOOP = '4" x 4"  (100 x 100 mm)'

# fabric -> overrides. Knits stretch, so they need more underlay and pull comp;
# towels need a denser top layer so the pile doesn't poke through.
FABRICS: dict[str, dict] = {
    "Woven (cotton, denim, canvas)": dict(fill_spacing=0.40, satin_spacing=0.40, pull_comp=0.20, underlay=True),
    "Knit (t-shirt, polo)": dict(fill_spacing=0.42, satin_spacing=0.42, pull_comp=0.35, underlay=True),
    "Towel / fleece": dict(fill_spacing=0.36, satin_spacing=0.36, pull_comp=0.30, underlay=True),
    "Cap / heavy twill": dict(fill_spacing=0.40, satin_spacing=0.40, pull_comp=0.25, underlay=True),
    "Light (silk, sheer)": dict(fill_spacing=0.48, satin_spacing=0.45, pull_comp=0.10, underlay=False),
}
DEFAULT_FABRIC = "Woven (cotton, denim, canvas)"


@dataclass
class TextItem:
    text: str
    font: str = "Arial"
    bold: bool = True
    italic: bool = False
    height_mm: float = 12.0  # capital-letter height
    rgb: tuple[int, int, int] = (20, 20, 20)
    position: str = "below"  # "below", "above" the picture, or "over" (centred on it)
    arc: float = 0.0  # degrees the line bends: + arches over (rainbow), - curves under (smile)
    kind: str = "text"  # "text", or "monogram" (1-3 initials, the middle of three bigger)
    frame: str = "none"  # monogram frame, one of lettering.FRAMES


@dataclass
class TextFix:
    """Text in the picture redrawn as clean lettering (AI text clean-up). The box is where the text
    sits, as fractions of the picture's width and height (before rotating or mirroring)."""
    text: str
    box: tuple[float, float, float, float]  # left, top, right, bottom
    font: str = "Arial"
    bold: bool = True
    italic: bool = False


@dataclass
class ShapeEdit:
    """The user's changes to one shape. The shape is found again by its colour and a point inside
    it, given as a fraction of the design's width and height (so it survives resizing)."""
    rgb: tuple[int, int, int]  # image colour of the shape
    fx: float
    fy: float
    kind: str = "auto"  # "auto", "fill", "satin", "outline" or "skip" (not sewn)
    angle: float | None = None  # fill angle (degrees); None = automatic
    density: float = 1.0  # 1.2 = 20% denser, 0.85 = 15% lighter
    order: int = 0  # -1: sew first in its colour, 1: sew last
    pattern: str = "plain"  # fill pattern, one of patterns.PATTERNS

    def is_default(self) -> bool:
        return (self.kind == "auto" and self.angle is None and self.density == 1.0 and self.order == 0
                and self.pattern == "plain")


@dataclass
class Settings:
    # size: the design is scaled to fit inside this box (mm)
    max_width_mm: float = 95.0
    max_height_mm: float = 95.0

    # colours: 0 = choose automatically
    num_colors: int = 0
    palette: list[tuple[int, int, int]] | None = None  # fixed seed colours (from AI)

    # background: "keep", "auto" (alpha / solid border colour), "ai" (rembg / grabcut)
    background: str = "auto"
    background_rgb: tuple[int, int, int] | None = None
    remove_enclosed_bg: bool = True  # also drop background-coloured holes (e.g. inside letters)

    detail: float = 0.5  # 0 = bold & simple, 1 = keep small details

    # simplification (None/defaults = automatic from `detail`)
    merge_de: float | None = None  # merge colours closer than this (CIELAB ΔE)
    min_shape_mm2: float | None = None  # absorb shapes and holes smaller than this
    thin_lines: str = "keep"  # "keep", "thicken" (to ~1 mm) or "remove"
    smooth: float = 0.0  # 0..1 extra edge smoothing

    # stitch parameters (mm)
    fill_spacing: float = 0.40
    fill_stitch_len: float = 3.5
    fill_angle: float | None = None  # None = automatic (see fill_follow)
    fill_follow: bool = True  # automatic angle: along each shape's length (roundish shapes vary per colour)
    satin_spacing: float = 0.40  # peak-to-peak zigzag spacing
    satin_max_width: float = 7.0
    run_stitch_len: float = 2.0
    pull_comp: float = 0.20
    underlay: bool = True
    min_jump_trim: float = 2.0  # travel longer than this is trimmed instead of stitched
    trims: bool = True  # False: machine has no thread cutter, so connect with jumps only

    fabric: str = DEFAULT_FABRIC

    # orientation of the picture before digitizing
    rotate: int = 0  # degrees clockwise: 0, 90, 180, 270
    mirror: bool = False

    # lettering added to the picture (or on its own)
    texts: list[TextItem] = field(default_factory=list)
    # text in the picture replaced by clean lettering
    text_fixes: list[TextFix] = field(default_factory=list)

    # colours (image colours, matched loosely) sewn as appliqué instead of fill
    applique: list[tuple[int, int, int]] = field(default_factory=list)

    # sewing order chosen by the user (image colours, matched loosely); others follow automatically
    color_order: list[tuple[int, int, int]] = field(default_factory=list)
    # colours the user skips: they won't be sewn, so nothing may be hidden underneath them
    skip_colors: list[tuple[int, int, int]] = field(default_factory=list)

    # per-shape changes (stitch type, fill angle, density, order, removed)
    shape_edits: list[ShapeEdit] = field(default_factory=list)
    # areas erased by hand: (thread rgb, area as WKT in fractions of the design), cut out before shapes are found
    erase: list[tuple[tuple[int, int, int], str]] = field(default_factory=list)

    # border around the whole design: "none", "run" (bean outline) or "satin" (patch edge)
    border: str = "none"
    border_rgb: tuple[int, int, int] = (30, 30, 30)
    border_width: float = 3.0  # mm, satin only
    border_offset: float = 0.0  # mm the border sits outside the design edge

    def with_fabric(self, fabric: str) -> "Settings":
        return replace(self, fabric=fabric, **FABRICS.get(fabric, {}))

    @property
    def min_region_mm2(self) -> float:
        if self.min_shape_mm2 is not None:
            return self.min_shape_mm2
        # bold: drop specks under ~6 mm^2, detailed: keep down to ~0.8 mm^2
        return 6.0 - 5.2 * max(0.0, min(1.0, self.detail))

    def simplified(self, amount: float) -> "Settings":
        """Settings for the one 'Simplicity' slider: 0 = keep every detail, 1 = bold and simple."""
        m = max(0.0, min(1.0, amount))
        return replace(
            self, detail=1.0 - m,
            merge_de=round(5.0 + 15.0 * m, 1),
            min_shape_mm2=round(0.8 + 11.2 * m * m + 2.0 * m, 1),
            thin_lines="keep" if m < 0.45 else "thicken",
            smooth=round(max(0.0, m - 0.3) / 0.7, 2),
        )
