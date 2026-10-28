"""Look and feel: Cute (default), Mint, Lavender and Midnight (dark) pastel themes, and the original Classic."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter, QPainterPath, QPalette, QPen
from PySide6.QtWidgets import QApplication

# Colours every theme shares unless it says otherwise (light themes)
_LIGHT = dict(
    style="cute", dark="", field="#ffffff", on_accent="#ffffff", checker_base="#ffffff",
    disabled="#b9adbf", disabled_line="#eee6ee", disabled_bg="#f4f0f4",
    good="#2e9e5b", fair="#b88a00", poor="#e07b00", bad="#d03030", photo="#8e44ad", warn="#c0392b",
    note_bg="#fff3d9", note_fg="#8a5300", radius="16px", ctl_radius="11px", pill="13px", h_size="14px",
)
THEMES: dict[str, dict[str, str]] = {
    "Cute": {**_LIGHT, **dict(
        label="Cute 🌸", tagline="turn your pictures into stitches ♡",
        bg="#fff5f9", card="#ffffff", card_border="#f6bcd6", border_style="dashed", text="#4b3550",
        muted="#9a84a4", accent="#ff6fae", accent_hover="#ff4f9b", accent_soft="#ffe4f0", accent_text="#d6427f",
        accent2="#9b7dff", accent2_soft="#f0eaff", input_border="#f1cddf", view_bg="#fdeef5", checker="#fbe2ee",
    )},
    "Mint": {**_LIGHT, **dict(
        label="Mint 🌿", tagline="fresh stitches from any picture 🌿",
        bg="#f0faf6", card="#ffffff", card_border="#a9dfcc", border_style="dashed", text="#24443d",
        muted="#6c8d85", accent="#1fa787", accent_hover="#178f73", accent_soft="#d9f4ea", accent_text="#137a62",
        accent2="#3d8fd6", accent2_soft="#e2f0fc", input_border="#c2e6d9", view_bg="#e6f6ef", checker="#d4efe4",
    )},
    "Lavender": {**_LIGHT, **dict(
        label="Lavender 💜", tagline="dreamy stitches from any picture ✿",
        bg="#f7f4ff", card="#ffffff", card_border="#cdbff4", border_style="dashed", text="#3b3354",
        muted="#8a82a6", accent="#8467ee", accent_hover="#7052e3", accent_soft="#ece6ff", accent_text="#6247d1",
        accent2="#d9579f", accent2_soft="#fde7f3", input_border="#ddd3f7", view_bg="#f0ecfd", checker="#e4dcfa",
    )},
    "Midnight": {**_LIGHT, **dict(
        label="Midnight 🌙", tagline="late-night stitching 🌙", dark="1",
        bg="#1c1524", card="#261d31", card_border="#5a4170", border_style="dashed", text="#f2e8f7",
        muted="#b3a2c2", accent="#ff7eb6", accent_hover="#ff9cc8", accent_soft="#47294c", accent_text="#ffa3cb",
        accent2="#b9a0ff", accent2_soft="#342a50", input_border="#4b3a5c", view_bg="#15101b", checker="#2b2238",
        field="#2e2440", on_accent="#2a1430", checker_base="#211a2b",
        disabled="#6f6280", disabled_line="#3a2f48", disabled_bg="#2a2135",
        good="#5fd38d", fair="#f2c94c", poor="#ffa94d", bad="#ff6b6b", photo="#c9a6ff", warn="#ff7b7b",
        note_bg="#4a3a1c", note_fg="#ffd98a",
    )},
    "Classic": {**_LIGHT, **dict(
        label="Classic", tagline="Pictures to machine embroidery", style="classic",
        bg="#f6f5fa", card="transparent", card_border="transparent", border_style="solid", text="#2b2840",
        muted="#6d6a80", accent="#5b4cdb", accent_hover="#4a3cc4", accent_soft="#eeebff", accent_text="#5b4cdb",
        accent2="#5b4cdb", accent2_soft="#eeebff", input_border="#cfcce0", view_bg="#ecebf2", checker="#e9e8ef",
        note_bg="#fff4e0", note_fg="#8a4b00", radius="0px", ctl_radius="5px", pill="6px", h_size="13px",
    )},
}
DEFAULT_THEME = "Cute"

_CUTE_HEADINGS = {"pic": "🖼️  Pick a picture", "size": "📏  Size & fabric", "look": "🎨  Colours & look",
                  "simp": "🧹  Simplify", "text": "💬  Lettering & border", "save": "💾  Save for your machine",
                  "design": "🧵  Your design", "threads": "🎀  Threads, in sewing order"}
HEADINGS: dict[str, dict[str, str]] = {
    "Cute": _CUTE_HEADINGS,
    "Mint": {**_CUTE_HEADINGS, "simp": "🍃  Simplify", "threads": "🌼  Threads, in sewing order"},
    "Lavender": {**_CUTE_HEADINGS, "design": "🪡  Your design", "threads": "🦋  Threads, in sewing order"},
    "Midnight": {**_CUTE_HEADINGS, "design": "🌙  Your design", "threads": "✨  Threads, in sewing order"},
    "Classic": {"pic": "1  Picture", "size": "2  Size & fabric", "look": "3  Look", "simp": "Simplify",
                "text": "4  Lettering & border", "save": "5  Save for your machine", "design": "Design",
                "threads": "Threads (in sewing order)"},
}

_current = DEFAULT_THEME


def current() -> dict[str, str]:
    return THEMES[_current]


def current_name() -> str:
    return _current


def is_cute() -> bool:
    """Rounded, playful look (every theme except Classic)."""
    return current()["style"] == "cute"


def is_dark() -> bool:
    return bool(current()["dark"])


def _check_icon(color: str) -> str:
    """A white tick on transparent, saved as PNG for the checkbox stylesheet."""
    path = Path(tempfile.gettempdir()) / f"stitchpilot_check_{color.strip('#')}.png"
    if not path.exists():
        img = QImage(32, 32, QImage.Format.Format_ARGB32)
        img.fill(0)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(color), 4.5, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        path_ = QPainterPath(QPointF(8, 17))
        path_.lineTo(14, 23)
        path_.lineTo(25, 10)
        p.drawPath(path_)
        p.end()
        img.save(str(path))
    return path.as_posix()


def _chevron(color: str, up: bool) -> str:
    """A small rounded chevron for dropdowns and number boxes."""
    path = Path(tempfile.gettempdir()) / f"stitchpilot_chev_{'up' if up else 'down'}_{color.strip('#')}.png"
    if not path.exists():
        img = QImage(24, 24, QImage.Format.Format_ARGB32)
        img.fill(0)
        p = QPainter(img)
        p.setRenderHint(QPainter.RenderHint.Antialiasing)
        p.setPen(QPen(QColor(color), 3.2, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap, Qt.PenJoinStyle.RoundJoin))
        a, b = (7, 15), (17, 15)
        tip = (12, 9) if up else (12, 15)
        if not up:
            a, b = (7, 9), (17, 9)
        path_ = QPainterPath(QPointF(*a))
        path_.lineTo(*tip)
        path_.lineTo(*b)
        p.drawPath(path_)
        p.end()
        img.save(str(path))
    return path.as_posix()


def qss(name: str) -> str:
    t = THEMES[name]
    tick = _check_icon(t["on_accent"])
    down, up = _chevron(t["muted"], False), _chevron(t["muted"], True)
    cute = t["style"] == "cute"
    extra = ""
    if cute:  # rounder, softer controls
        extra = f"""
QSlider::groove:horizontal {{ height: 6px; background: {t['accent_soft']}; border-radius: 3px; }}
QSlider::sub-page:horizontal {{ background: {t['accent']}; border-radius: 3px; }}
QSlider::handle:horizontal {{ background: {t['field']}; border: 2px solid {t['accent']}; width: 14px; height: 14px;
                              margin: -6px 0; border-radius: 9px; }}
QSlider::handle:horizontal:hover {{ background: {t['accent_soft']}; }}
QProgressBar {{ border: none; background: {t['accent_soft']}; border-radius: 6px; height: 10px; }}
QProgressBar::chunk {{ background: {t['accent']}; border-radius: 6px; }}
QScrollBar:vertical {{ background: transparent; width: 10px; margin: 2px; }}
QScrollBar::handle:vertical {{ background: {t['card_border']}; border-radius: 4px; min-height: 30px; }}
QScrollBar::add-line, QScrollBar::sub-line {{ height: 0; width: 0; }}
QScrollBar:horizontal {{ background: transparent; height: 10px; margin: 2px; }}
QScrollBar::handle:horizontal {{ background: {t['card_border']}; border-radius: 4px; min-width: 30px; }}
QLabel#title {{ font-size: 20px; font-weight: 800; color: {t['accent_text']}; }}
QWidget#threadrow {{ background: {t['bg']}; border-radius: 12px; }}
QWidget#threadrow:hover {{ background: {t['accent_soft']}; }}
QWidget#editbar {{ background: {t['card']}; border: 2px dashed {t['card_border']}; border-radius: 14px; }}
"""
    return f"""
QMainWindow, QWidget#side, QWidget#right, QScrollArea, QScrollArea > QWidget > QWidget,
QDialog, QMessageBox {{ background: {t['bg']}; }}
QWidget {{ color: {t['text']}; }}
QFrame#card {{ background: {t['card']}; border: 2px {t['border_style']} {t['card_border']};
               border-radius: {t['radius']}; }}
QFrame#card QLabel, QFrame#card QCheckBox {{ background: transparent; }}
QComboBox QAbstractItemView {{ background: {t['field']}; color: {t['text']}; selection-background-color: {t['accent_soft']};
                               selection-color: {t['text']}; border: 1px solid {t['input_border']}; }}
QMenuBar {{ background: {t['bg']}; }}
QMenuBar::item {{ padding: 4px 10px; border-radius: 8px; }}
QMenuBar::item:selected, QMenu::item:selected {{ background: {t['accent_soft']}; color: {t['text']}; }}
QMenu {{ background: {t['field']}; border: 1px solid {t['input_border']}; border-radius: 8px; padding: 4px; }}
QMenu::item {{ padding: 5px 18px; border-radius: 6px; }}
QToolTip {{ background: {t['field']}; color: {t['text']}; border: 1px solid {t['input_border']}; border-radius: 6px; }}
QLabel#h {{ font-size: {t['h_size']}; font-weight: 700; color: {t['text']}; padding: 2px 0 4px 0; }}
QLabel#muted {{ color: {t['muted']}; }}
QLabel#title {{ font-size: 18px; font-weight: 700; color: {t['accent_text']}; }}
QLabel#stat {{ font-size: 12px; color: {t['text']}; }}
QLabel#note {{ color: {t['note_fg']}; background: {t['note_bg']}; border-radius: 10px; padding: 7px; }}
QLabel#ai, QLabel#info {{ color: {t['text']}; background: {t['accent2_soft']}; border-radius: 10px; padding: 8px; }}
QPushButton, QToolButton {{ padding: 5px 12px; border-radius: {t['pill']}; border: 1.5px solid {t['input_border']};
                            background: {t['field']}; color: {t['text']}; }}
QPushButton:hover, QToolButton:hover {{ border-color: {t['accent']}; background: {t['accent_soft']}; }}
QPushButton:checked, QToolButton:checked {{ background: {t['accent_soft']}; border-color: {t['accent']};
                                            color: {t['accent_text']}; font-weight: 600; }}
QPushButton:disabled, QToolButton:disabled {{ color: {t['disabled']}; border-color: {t['disabled_line']}; }}
QToolButton#flat {{ border: none; background: transparent; padding: 2px 4px; color: {t['muted']}; }}
QToolButton#flat:hover {{ color: {t['accent']}; background: {t['accent_soft']}; border-radius: 8px; }}
QPushButton#primary {{ background: {t['accent']}; color: {t['on_accent']}; border: none; font-weight: 700; padding: 10px;
                       font-size: 13px; }}
QPushButton#primary:hover {{ background: {t['accent_hover']}; }}
QPushButton#primary:disabled {{ background: {t['card_border']}; color: {t['card']}; }}
QPushButton#drop {{ border: 2px dashed {t['card_border'] if cute else '#b8b3d6'}; background: {t['bg'] if cute else t['field']};
                    padding: 16px; font-size: 13px; color: {t['muted']}; border-radius: {t['radius'] if cute else '6px'}; }}
QPushButton#drop:hover {{ border-color: {t['accent']}; color: {t['accent_text']}; background: {t['accent_soft']}; }}
QPushButton#ai {{ background: {t['accent2_soft']}; color: {t['accent2']}; border: 1.5px solid {t['accent2_soft']};
                  font-weight: 700; }}
QPushButton#ai:hover {{ border-color: {t['accent2']}; }}
QPushButton#friendly {{ background: {t['accent_soft']}; color: {t['accent_text']}; border: 1.5px solid {t['accent_soft']};
                        font-weight: 700; }}
QPushButton#friendly:hover {{ border-color: {t['accent']}; }}
QComboBox, QDoubleSpinBox, QSpinBox, QLineEdit, QPlainTextEdit, QFontComboBox, QListWidget {{ padding: 4px 8px;
    border: 1.5px solid {t['input_border']}; border-radius: {t['ctl_radius']}; background: {t['field']};
    color: {t['text']}; }}
QComboBox:hover, QDoubleSpinBox:hover, QSpinBox:hover, QLineEdit:focus, QPlainTextEdit:focus {{
    border-color: {t['accent']}; }}
QComboBox:disabled, QDoubleSpinBox:disabled, QSpinBox:disabled {{ color: {t['disabled']};
    background: {t['disabled_bg']}; border-color: {t['disabled_line']}; }}
QComboBox::drop-down {{ subcontrol-origin: padding; subcontrol-position: center right; width: 22px; border: none; }}
QComboBox::down-arrow {{ image: url({down}); width: 11px; height: 11px; }}
QDoubleSpinBox, QSpinBox {{ padding-right: 20px; }}
QDoubleSpinBox::up-button, QDoubleSpinBox::down-button, QSpinBox::up-button, QSpinBox::down-button {{
    subcontrol-origin: border; width: 20px; border: none; background: transparent; }}
QDoubleSpinBox::up-button, QSpinBox::up-button {{ subcontrol-position: top right; margin-top: 2px; }}
QDoubleSpinBox::down-button, QSpinBox::down-button {{ subcontrol-position: bottom right; margin-bottom: 2px; }}
QDoubleSpinBox::up-arrow, QSpinBox::up-arrow {{ image: url({up}); width: 9px; height: 9px; }}
QDoubleSpinBox::down-arrow, QSpinBox::down-arrow {{ image: url({down}); width: 9px; height: 9px; }}
QTextBrowser {{ background: transparent; border: none; color: {t['text']}; }}
QListWidget::item {{ padding: 6px 4px; border-radius: 6px; }}
QListWidget::item:selected {{ background: {t['accent_soft']}; color: {t['text']}; }}
QTabWidget::pane {{ border: none; }}
QTabBar::tab {{ padding: 6px 16px; margin: 4px 3px; background: transparent; color: {t['muted']}; border: none;
                border-radius: {t['pill']}; }}
QTabBar::tab:selected {{ color: {t['accent_text']}; background: {t['accent_soft'] if cute else 'transparent'};
                         font-weight: 700; {'' if cute else f"border-bottom: 2px solid {t['accent']}; border-radius: 0;"} }}
QTabBar::tab:hover {{ color: {t['accent_text']}; }}
QStatusBar {{ color: {t['muted']}; }}
QCheckBox::indicator {{ width: 15px; height: 15px; border: 1.5px solid {t['input_border']};
                        border-radius: {'5px' if cute else '3px'}; background: {t['field']}; }}
QCheckBox::indicator:hover {{ border-color: {t['accent']}; }}
QCheckBox::indicator:checked {{ background: {t['accent']}; border-color: {t['accent']}; image: url({tick}); }}
QCheckBox::indicator:disabled {{ background: {t['disabled_bg']}; border-color: {t['disabled_line']}; }}
{extra}"""


def apply(app: QApplication, name: str) -> None:
    """Apply a theme app-wide. Pins the theme's own palette, so text stays readable whatever the
    system's light/dark setting is."""
    global _current
    _current = name if name in THEMES else DEFAULT_THEME
    t = current()
    try:
        app.styleHints().setColorScheme(Qt.ColorScheme.Dark if t["dark"] else Qt.ColorScheme.Light)
    except AttributeError:  # Qt < 6.8
        pass
    app.setStyle("Fusion")
    pal = QPalette()
    R = QPalette.ColorRole
    for role, col in ((R.Window, t["bg"]), (R.WindowText, t["text"]), (R.Base, t["field"]),
                      (R.AlternateBase, t["accent_soft"]), (R.Text, t["text"]), (R.Button, t["field"]),
                      (R.ButtonText, t["text"]), (R.ToolTipBase, t["field"]), (R.ToolTipText, t["text"]),
                      (R.Highlight, t["accent"]), (R.HighlightedText, t["on_accent"]),
                      (R.PlaceholderText, t["muted"]), (R.Link, t["accent_text"]), (R.Light, t["field"]),
                      (R.Midlight, t["accent_soft"]), (R.Mid, t["input_border"]),
                      (R.Dark, t["disabled"]), (R.Shadow, t["muted"])):
        pal.setColor(role, QColor(col))
    for role in (R.WindowText, R.Text, R.ButtonText):
        pal.setColor(QPalette.ColorGroup.Disabled, role, QColor(t["disabled"]))
    app.setPalette(pal)
    if sys.platform == "win32":
        app.setFont(QFont("Segoe UI", 9))
    elif sys.platform == "darwin":  # the Mac system font at its usual size
        f = QFont(".AppleSystemUIFont")
        f.setPointSize(13)
        app.setFont(f)
    app.setStyleSheet(qss(_current))
