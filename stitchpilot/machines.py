"""Machine profiles: pick your machine and StitchPilot picks the file format, thread chart and hoops.

Hoop lists are the sizes commonly used with each machine family, not a guarantee for every model:
the app always says to check the machine's manual.
"""
from __future__ import annotations

from dataclasses import dataclass

from .settings import HOOPS


@dataclass(frozen=True)
class Machine:
    name: str
    format: str  # label from export.FORMATS
    chart: str | None  # export.THREAD_CHARTS key, or None
    hoops: tuple[str, ...]  # HOOPS keys; empty = every hoop
    default_hoop: str | None = None


PES = "PES – Brother, Baby Lock, Bernette"
DST = "DST – Tajima (universal, most machines)"
JEF = "JEF – Janome, Elna, Kenmore"
EXP = "EXP – Melco, Bernina"
VP3 = "VP3 – Husqvarna Viking, Pfaff"
XXX = "XXX – Singer"

H64, H100, H100_130, H140, H130_180 = (
    '2.5" x 2.5" (patch, 64 x 64 mm)', '4" x 4"  (100 x 100 mm)', '4" x 5"  (100 x 130 mm)',
    '5.5" x 5.5" (140 x 140 mm)', '5" x 7"  (130 x 180 mm)')
H170_200, H200, H240_150, H260_150, H145_255 = (
    '6.7" x 7.9" (170 x 200 mm)', '8" x 8"  (200 x 200 mm)', '9.4" x 5.9" (240 x 150 mm)',
    '10.2" x 5.9" (260 x 150 mm)', '5.7" x 10" (145 x 255 mm)')
H160_260, H200_280, H200_300, H360_200, H210_400 = (
    '6" x 10" (160 x 260 mm)', '7.9" x 11" (200 x 280 mm)', '8" x 12" (200 x 300 mm)',
    '14" x 8" (360 x 200 mm)', '8.3" x 15.7" (210 x 400 mm)')

ANY = "Any machine (all hoops)"
MACHINES: dict[str, Machine] = {m.name: m for m in [
    Machine(ANY, PES, "Brother", ()),
    Machine("Brother SE600 / SE630 / PE535 (4×4)", PES, "Brother", (H64, H100), H100),
    Machine("Brother PE800 / PE900 / SE1900 / SE2000 (5×7)", PES, "Brother", (H64, H100, H130_180), H130_180),
    Machine("Brother / Baby Lock 6×10 machines", PES, "Brother", (H64, H100, H130_180, H160_260), H160_260),
    Machine("Brother / Baby Lock multi-needle (8×12)", PES, "Brother",
            (H64, H100, H130_180, H160_260, H200_300), H200_300),
    Machine("Janome Memory Craft 400E", JEF, "Janome", (H140, H200), H200),
    Machine("Janome Memory Craft 500E", JEF, "Janome", (H140, H170_200, H200_280), H200_280),
    Machine("Janome / Elna (other)", JEF, "Janome", (H100, H140, H170_200, H200, H200_280), H140),
    Machine("Singer Legacy SE300 / SE9180", XXX, "Sew / generic", (H100, H260_150), H260_150),
    Machine("Bernina (embroidery module)", EXP, None, (H100_130, H145_255, H210_400), H145_255),
    Machine("Husqvarna Viking / Pfaff", VP3, "Husqvarna Viking", (H100, H240_150, H360_200), H240_150),
    Machine("Commercial multi-needle (Tajima, Ricoma, SWF…)", DST, None,
            (H100, H130_180, H160_260, H200, H200_300, H360_200, H210_400), H130_180),
]}
DEFAULT_MACHINE = ANY


def hoops_for(name: str) -> list[str]:
    m = MACHINES.get(name)
    return list(m.hoops) if m and m.hoops else list(HOOPS)
