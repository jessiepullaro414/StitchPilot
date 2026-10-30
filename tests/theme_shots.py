"""Screenshots of every theme (empty window and with a design), and a readability check of its colours."""
import sys
from pathlib import Path

import _testenv  # noqa: F401  (offscreen Qt, throwaway settings)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from stitchpilot import gui, theme  # noqa: E402
from stitchpilot.settings import TextItem  # noqa: E402

here = Path(__file__).parent
out = here / "out"


def luminance(hex_):
    c = [int(hex_.lstrip("#")[i:i + 2], 16) / 255 for i in (0, 2, 4)]
    c = [v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4 for v in c]
    return 0.2126 * c[0] + 0.7152 * c[1] + 0.0722 * c[2]


def contrast(a, b):
    la, lb = sorted((luminance(a), luminance(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


# readable text everywhere: body text 4.5:1 (WCAG AA), secondary text and highlighted labels 3:1,
# bold button text (primary, AI) 2.5:1, like the original Cute pink and lilac
for name, t in theme.THEMES.items():
    card = t["card"] if t["card"] != "transparent" else t["bg"]
    checks = [("text on background", t["text"], t["bg"], 4.5), ("text on cards", t["text"], card, 4.5),
              ("text in fields", t["text"], t["field"], 4.5), ("muted text on cards", t["muted"], card, 3.0),
              ("highlighted label", t["accent_text"], t["accent_soft"], 3.0),
              ("AI button", t["accent2"], t["accent2_soft"], 2.5), ("notes", t["note_fg"], t["note_bg"], 4.5),
              ("primary button", t["on_accent"], t["accent"], 2.5)]
    worst = min(checks, key=lambda c: contrast(c[1], c[2]) / c[3])
    print(f"{name:9s} worst: {worst[0]} {contrast(worst[1], worst[2]):.1f}:1 (needs {worst[3]})")
    for what, fg, bg, need in checks:
        assert contrast(fg, bg) >= need, f"{name}: {what} {fg} on {bg} is {contrast(fg, bg):.2f}:1 (< {need})"
    assert set(theme.HEADINGS[name]) == set(theme.HEADINGS["Cute"]), name

app = QApplication(sys.argv)
gui.apply_light_theme(app)
w = gui.MainWindow()
w.auto_ai.setChecked(False)
w.resize(1440, 900)
w.show()


def wait_idle(timeout=60):
    loop = QEventLoop()
    t = QTimer()
    t.timeout.connect(lambda: (not w.busy and not w.debounce.isActive()) and loop.quit())
    t.start(100)
    QTimer.singleShot(timeout * 1000, loop.quit)
    loop.exec()


def shot(name):
    app.processEvents()
    w.grab().save(str(out / f"theme_{name}.png"))


assert theme.current_name() == "Cute", "Cute should be the default"
assert [a.data() for a in w.theme_actions] == list(theme.THEMES), "every theme is in View ▸ Theme"
shot("cute_empty")
w.open_path(str(here / "samples" / "badge.png"))
w.texts.append(TextItem("Robot Club", height_mm=8, rgb=(255, 90, 160)))
w._refresh_texts()
wait_idle()
w.convert()
wait_idle()
for name in theme.THEMES:
    w.set_theme(name)
    assert theme.current_name() == name and w.qs.value("theme") == name
    assert w.tagline.text() == theme.THEMES[name]["tagline"]
    assert [a.data() for a in w.theme_actions if a.isChecked()] == [name]
    shot(name.lower())
w.set_theme("Midnight")
w.shape_btn.setChecked(True)  # the shape bar in the dark theme
shot("midnight_shapes")
w.shape_btn.setChecked(False)
w.set_theme("Cute")
print("theme now:", theme.current_name(), "| saved:", w.qs.value("theme"))
w.close()
