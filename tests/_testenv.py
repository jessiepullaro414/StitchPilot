"""Shared set-up for the GUI tests: import this before anything from Qt or stitchpilot.

- runs Qt offscreen (with real fonts on Windows)
- keeps settings and autosaves in a throwaway folder, so tests never touch the user's real ones
- answers "save your changes?" with "don't save" (offscreen there's nobody to click)
"""
import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", r"C:\Windows\Fonts")
_tmp = Path(tempfile.mkdtemp(prefix="stitchpilot-test-"))
os.environ["STITCHPILOT_SETTINGS"] = str(_tmp / "settings.ini")
os.environ["STITCHPILOT_DATA"] = str(_tmp / "data")
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from stitchpilot import gui  # noqa: E402

gui.MainWindow.real_maybe_save = gui.MainWindow.maybe_save
gui.MainWindow.maybe_save = lambda self: True
TMP = _tmp
