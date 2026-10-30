"""StitchPilot desktop app (PySide6)."""
from __future__ import annotations

import copy
import hashlib
import math
import os
import sys
import tempfile
import threading
import time
import traceback
from dataclasses import asdict, astuple, replace
from pathlib import Path

import cv2
import numpy as np
import shapely
from PySide6.QtCore import (
    QDateTime, QEvent, QObject, QPointF, QRectF, QRunnable, QSettings, QStandardPaths, Qt, QThreadPool, QTimer,
    QUrl, Signal,
)
from PySide6.QtGui import (
    QAction, QActionGroup, QBrush, QColor, QDesktopServices, QIcon, QImage, QKeySequence, QPainter, QPainterPath,
    QPen, QPixmap, QPolygonF,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox, QFileDialog, QFormLayout, QFrame,
    QGraphicsPixmapItem, QGraphicsScene, QGraphicsView, QHBoxLayout, QLabel, QMainWindow, QMenu, QMessageBox, QProgressBar, QPushButton, QScrollArea, QSizePolicy, QSlider, QSpinBox,
    QSplitter, QTabWidget, QTextBrowser, QToolButton, QVBoxLayout, QWidget,
)

from . import __version__, ai, editing, project, theme
from . import stitches as st
from .machines import DEFAULT_MACHINE, MACHINES, hoops_for
from .template import default_paper, write_template
from .analysis import density_image
from .complexity import analyse as analyse_cx
from .complexity import analyse_source, find_friendly, problem_overlay
from . import account, cutfiles, imagegen, updates
from . import patterns as pat
from .dialogs import (
    AccountDialog, ImageGenDialog, SuggestionsDialog, TextDialog, TextFixDialog, UpdateDialog, _Result,
    write_worksheet,
)
from .digitize import ColorBlock, Design, Shape, digitize
from .export import DEFAULT_FORMAT, FORMATS, READ_EXTS, THREAD_CHARTS, export, match_threads, read_design
from .imageio import SUPPORTED_EXTS, load_image
from .prep import inputs as prep_inputs
from .prep import rgb_to_lab
from .render import FABRIC, layout, render
from .settings import DEFAULT_FABRIC, DEFAULT_HOOP, FABRICS, HOOPS, Settings, ShapeEdit, TextFix, TextItem

APP = "StitchPilot"
MARGIN_MM = 5.0
DEFAULT_SIMPLICITY = 35  # close to the original automatic behaviour: keeps thin lines and distinct colours


# ============================================================================ helpers
FROZEN = getattr(sys, "frozen", False)
MAC = sys.platform == "darwin"
# the Windows exe is portable: settings travel with it. A Mac .app bundle is read-only once installed
# (and editing it would break its signature), so it uses the normal per-user preferences instead.
PORTABLE = FROZEN and sys.platform == "win32"


def app_settings() -> QSettings:
    """Portable exe keeps its settings in an .ini next to itself; everything else uses the user profile."""
    if os.environ.get("STITCHPILOT_SETTINGS"):  # tests: never touch the user's real settings
        return QSettings(os.environ["STITCHPILOT_SETTINGS"], QSettings.Format.IniFormat)
    if PORTABLE:
        return QSettings(str(Path(sys.executable).with_suffix(".ini")), QSettings.Format.IniFormat)
    return QSettings(APP, APP)


def app_icon() -> QIcon:
    p = Path(__file__).with_name("icon.png")
    return QIcon(str(p)) if p.exists() else QIcon()


def open_filter() -> str:
    pics = " ".join(f"*{e}" for e in SUPPORTED_EXTS)
    embs = " ".join(f"*.{e}" for e in READ_EXTS)
    proj = f"*{project.EXT}"
    return (f"Everything StitchPilot opens ({proj} {pics} {embs});;StitchPilot projects ({proj});;"
            f"Pictures ({pics});;Embroidery files ({embs});;All files (*)")


def data_dir() -> Path:
    """Where autosaves live: next to the portable exe, otherwise the user's app-data folder."""
    if os.environ.get("STITCHPILOT_DATA"):
        d = Path(os.environ["STITCHPILOT_DATA"])
    elif PORTABLE:
        d = Path(sys.executable).parent / "StitchPilot data"
    else:
        d = Path(QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppDataLocation) or
                 Path.home() / ".stitchpilot")
    d.mkdir(parents=True, exist_ok=True)
    return d


def autosave_path() -> Path:
    return data_dir() / f"autosave{project.EXT}"


def is_embroidery(path: Path) -> bool:
    return path.suffix.lower().lstrip(".") in READ_EXTS and path.suffix.lower() not in SUPPORTED_EXTS


def to_qimage(arr: np.ndarray) -> QImage:
    arr = np.ascontiguousarray(arr)
    h, w = arr.shape[:2]
    fmt = QImage.Format.Format_RGBA8888 if arr.shape[2] == 4 else QImage.Format.Format_RGB888
    return QImage(arr.data, w, h, arr.strides[0], fmt).copy()


def checker_brush() -> QBrush:
    pm = QPixmap(16, 16)
    pm.fill(QColor(theme.current()["checker_base"]))
    p = QPainter(pm)
    p.fillRect(0, 0, 8, 8, QColor(theme.current()["checker"]))
    p.fillRect(8, 8, 8, 8, QColor(theme.current()["checker"]))
    p.end()
    return QBrush(pm)


def lab_of(rgb) -> np.ndarray:
    return rgb_to_lab(np.array(rgb, np.uint8).reshape(-1, 1, 3)).reshape(-1, 3)


def nearest_key(rgb, keys, tol: float = 10.0):
    """The key colour closest to rgb (within tol ΔE), so edits survive re-conversion."""
    ks = list(keys)
    if not ks:
        return None
    d = np.linalg.norm(lab_of(ks) - lab_of(rgb)[0], axis=1)
    return ks[int(d.argmin())] if d.min() < tol else None


def swatch_style(rgb, round_=True) -> str:
    r = "15px" if round_ else "6px"
    if theme.is_cute():  # a little thread spool: colour with a soft rim
        return (f"background: rgb{tuple(rgb)}; border: 3px solid {theme.current()['card_border']}; "
                f"border-radius: {r};")
    return f"background: rgb{tuple(rgb)}; border: 1px solid #00000040; border-radius: {r};"


class Signals(QObject):
    progress = Signal(int, float, str)
    done = Signal(int, object)
    failed = Signal(int, str)
    account = Signal(int, object)  # account.AccountError: not connected / out of AI allowance


class _Closed(Exception):
    pass


class Job(QRunnable):
    """Runs fn(progress) on the thread pool; results are tagged with a generation number."""

    def __init__(self, gen: int, fn, signals: Signals):
        super().__init__()
        self.gen, self.fn, self.sig = gen, fn, signals

    def _emit(self, signal, *args):
        try:
            signal.emit(self.gen, *args)
        except RuntimeError:  # the window was closed while this job was still running
            raise _Closed from None

    def run(self):
        try:
            res = self.fn(lambda f, m: self._emit(self.sig.progress, f, m))
            self._emit(self.sig.done, res)
        except _Closed:
            pass
        except account.AccountError as e:
            try:
                self._emit(self.sig.account, e)
            except _Closed:
                pass
        except Exception as e:  # report every failure to the UI rather than dying silently
            traceback.print_exc()
            try:
                self._emit(self.sig.failed, str(e) or e.__class__.__name__)
            except _Closed:
                pass


class ZoomView(QGraphicsView):
    def __init__(self, placeholder: str, checker: bool = False):
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self.item = QGraphicsPixmapItem()
        self.item.setTransformationMode(Qt.TransformationMode.SmoothTransformation)
        self.scene().addItem(self.item)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setRenderHints(QPainter.RenderHint.Antialiasing | QPainter.RenderHint.SmoothPixmapTransform)
        self.setTransformationAnchor(QGraphicsView.ViewportAnchor.AnchorUnderMouse)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.checker = checker
        self.restyle()
        self.placeholder = placeholder
        self.fit = True
        self.has_image = False

    def restyle(self):
        self.setBackgroundBrush(checker_brush() if self.checker else QBrush(QColor(theme.current()["view_bg"])))
        self.viewport().update()

    def set_image(self, img: QImage | None):
        same_size = self.has_image and img is not None and self.item.pixmap().size() == img.size()
        self.has_image = img is not None
        self.item.setPixmap(QPixmap.fromImage(img) if img is not None else QPixmap())
        self.scene().setSceneRect(self.item.boundingRect())
        if self.fit or not same_size:
            self.fit_view()
        self.viewport().update()

    def fit_view(self):
        self.fit = True
        if self.has_image:
            self.fitInView(self.item, Qt.AspectRatioMode.KeepAspectRatio)

    def wheelEvent(self, e):
        # a mouse wheel zooms; a trackpad's two-finger scroll pans (zoom with a pinch or Ctrl/⌘+scroll)
        trackpad = not e.pixelDelta().isNull() and e.phase() != Qt.ScrollPhase.NoScrollPhase
        if trackpad and not (e.modifiers() & Qt.KeyboardModifier.ControlModifier):
            super().wheelEvent(e)
            self.fit = False
            return
        f = 1.15 ** (e.angleDelta().y() / 120)
        self.scale(f, f)
        self.fit = False

    def viewportEvent(self, e):
        if e.type() == QEvent.Type.NativeGesture and e.gestureType() == Qt.NativeGestureType.ZoomNativeGesture:
            f = max(0.5, min(2.0, 1.0 + e.value()))  # trackpad pinch
            self.scale(f, f)
            self.fit = False
            return True
        return super().viewportEvent(e)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self.fit:
            self.fit_view()

    def mouseDoubleClickEvent(self, e):
        self.fit_view()

    def drawForeground(self, p: QPainter, rect):
        if not self.has_image:
            p.save()
            p.resetTransform()
            p.setPen(QColor(theme.current()["muted"]))
            f = p.font()
            f.setPointSizeF(f.pointSizeF() * 1.25)
            p.setFont(f)
            p.drawText(self.viewport().rect(), Qt.AlignmentFlag.AlignCenter, self.placeholder)
            p.restore()


KIND_NAMES = {"fill": "Fill", "satin": "Satin", "run": "Line (bean stitch)", "outline": "Outline only",
              "skip": "Removed"}


def shape_path(geom, to_scene) -> QPainterPath:
    """Outline of a shapely geometry as a painter path in scene coordinates."""
    path = QPainterPath()
    for poly in st._polys(geom) if geom is not None else []:
        for ring in (poly.exterior, *poly.interiors):
            pts = to_scene(np.asarray(ring.coords))
            path.addPolygon(QPolygonF([QPointF(x, y) for x, y in pts]))
            path.closeSubpath()
    return path


class StitchView(ZoomView):
    """Stitch preview that doubles as a stitch editor (select, move, delete, add needle points)
    and a shape editor (pick a shape, drag its fill-direction arrow)."""

    PICK_PX = 8  # click tolerance in screen pixels

    def __init__(self, win: "MainWindow"):
        super().__init__("Drop a picture here and I'll stitch it up 🧵")
        self.win = win
        self.edit = False
        self.tool = "select"
        self.sel: set = set()
        self.ppm, self.origin = 1.0, (0.0, 0.0)
        self.pts = np.zeros((0, 2))
        self.refs = np.zeros((0, 2), int)
        self._press = None  # (mm position, "move" | "band")
        self._band: QRectF | None = None
        self._delta = None
        self._pan = None
        self.shape_mode = False
        self.shape: Shape | None = None  # selected shape (shape mode)
        self._angle: float | None = None  # fill angle while its arrow is being dragged
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    # ---- coordinates
    def set_mapping(self, ppm: float, origin):
        self.ppm, self.origin = ppm, origin

    def to_mm(self, scene: QPointF) -> np.ndarray:
        return np.array([scene.x() / self.ppm + self.origin[0], scene.y() / self.ppm + self.origin[1]])

    def _scene_xy(self, mm: np.ndarray) -> np.ndarray:
        return (mm - self.origin) * self.ppm

    def tol_mm(self) -> float:
        return self.PICK_PX / max(1e-6, self.transform().m11() * self.ppm)

    def refresh_points(self):
        d = self.win.design
        self.pts, self.refs = editing.points(d.blocks) if d is not None else (np.zeros((0, 2)), np.zeros((0, 2), int))
        self.viewport().update()

    def set_edit(self, on: bool):
        self.edit = on
        self.sel.clear()
        self.setDragMode(QGraphicsView.DragMode.NoDrag if on else QGraphicsView.DragMode.ScrollHandDrag)
        self.viewport().setCursor(Qt.CursorShape.CrossCursor if on else Qt.CursorShape.OpenHandCursor)
        self.refresh_points()

    def set_shape_mode(self, on: bool):
        self.shape_mode = on
        self.shape, self._angle = None, None
        self.setDragMode(QGraphicsView.DragMode.NoDrag if on else QGraphicsView.DragMode.ScrollHandDrag)
        self.viewport().setCursor(Qt.CursorShape.PointingHandCursor if on else Qt.CursorShape.OpenHandCursor)
        self.viewport().update()

    # ---- fill-direction arrow of the selected shape (mm)
    def arrow(self, angle: float | None = None):
        """(centre, half-length vector) of the selected fill shape's direction arrow, or None."""
        sh, d = self.shape, self.win.design
        if sh is None or d is None or sh.kind != "fill" or sh.angle is None:  # motif fills have no direction
            return None
        a = math.radians(self._angle if self._angle is not None else (angle if angle is not None else sh.angle or 0))
        half = float(np.clip(math.sqrt(sh.area_mm2) * 0.35, 3.0, 14.0))
        return np.array([sh.fx * d.width_mm, sh.fy * d.height_mm]), np.array([math.cos(a), math.sin(a)]) * half

    def _shape_press(self, e):
        xy = self.to_mm(self.mapToScene(e.position().toPoint()))
        arr = self.arrow()
        if arr is not None:
            c, v = arr
            if min(np.hypot(*(xy - (c + v))), np.hypot(*(xy - (c - v)))) < self.tol_mm() * 1.6:
                self._angle = self.shape.angle or 0.0  # grabbed the handle: drag to turn it
                return
        self.win.select_shape(self.win.design.shape_at(*xy, slack=self.tol_mm()))

    # ---- mouse / keys
    def mousePressEvent(self, e):
        if not (self.edit or self.shape_mode) or self.win.design is None:
            return super().mousePressEvent(e)
        if e.button() in (Qt.MouseButton.MiddleButton, Qt.MouseButton.RightButton):
            self._pan = e.position()
            return
        if e.button() != Qt.MouseButton.LeftButton:
            return
        self.setFocus()
        if self.shape_mode:
            return self._shape_press(e)
        scene = self.mapToScene(e.position().toPoint())
        xy = self.to_mm(scene)
        if self.tool == "add":
            self.win.edit_add(xy)
            return
        ref = editing.nearest(self.win.design.blocks, xy, self.tol_mm())
        shift = bool(e.modifiers() & Qt.KeyboardModifier.ShiftModifier)
        if ref is not None:
            if shift:
                self.sel ^= {ref}
            elif ref not in self.sel:
                self.sel = {ref}
            self._press = (xy, "move")
        else:
            if not shift:
                self.sel = set()
            self._press = (xy, "band")
            self._band = QRectF(scene, scene)
        self.win.selection_changed()
        self.viewport().update()

    def mouseMoveEvent(self, e):
        if self._pan is not None:
            d = e.position() - self._pan
            self._pan = e.position()
            self.horizontalScrollBar().setValue(self.horizontalScrollBar().value() - int(d.x()))
            self.verticalScrollBar().setValue(self.verticalScrollBar().value() - int(d.y()))
            self.fit = False
            return
        if self.shape_mode and self._angle is not None:
            arr = self.arrow()
            if arr is not None:
                v = self.to_mm(self.mapToScene(e.position().toPoint())) - arr[0]
                if np.hypot(*v) > 0.5:  # turn towards the pointer, in 5° steps
                    self._angle = round(math.degrees(math.atan2(v[1], v[0])) % 180 / 5) * 5 % 180
                    self.viewport().update()
            return
        if not self.edit or self._press is None:
            return super().mouseMoveEvent(e)
        scene = self.mapToScene(e.position().toPoint())
        if self._press[1] == "move" and self.sel:
            self._delta = self.to_mm(scene) - self._press[0]
        elif self._press[1] == "band":
            self._band = QRectF(self._band.topLeft(), scene)
        self.viewport().update()

    def mouseReleaseEvent(self, e):
        if self._pan is not None:
            self._pan = None
            return
        if self.shape_mode:
            if self._angle is not None:
                a, self._angle = self._angle, None
                if self.shape is not None and a != self.shape.angle:
                    self.win.update_shape(angle=float(a))
                self.viewport().update()
            return
        if not self.edit or self._press is None:
            return super().mouseReleaseEvent(e)
        kind = self._press[1]
        if kind == "move" and self._delta is not None and np.hypot(*self._delta) > 0.05:
            self.win.edit_move(float(self._delta[0]), float(self._delta[1]))
        elif kind == "band" and self._band is not None:
            a, b = self.to_mm(self._band.topLeft()), self.to_mm(self._band.bottomRight())
            self.sel |= editing.in_rect(self.win.design.blocks, a[0], a[1], b[0], b[1])
            self.win.selection_changed()
        self._press, self._band, self._delta = None, None, None
        self.viewport().update()

    def mouseDoubleClickEvent(self, e):
        if self.shape_mode:
            return
        if not self.edit or self.win.design is None:
            return super().mouseDoubleClickEvent(e)
        ref = editing.nearest(self.win.design.blocks, self.to_mm(self.mapToScene(e.position().toPoint())),
                              self.tol_mm())
        if ref is not None and self.tool == "select":
            self.sel = editing.run_of(self.win.design.blocks, ref)
            self.win.selection_changed()
            self.viewport().update()

    def keyPressEvent(self, e):
        if self.shape_mode and self.shape is not None and e.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.win.update_shape(kind="skip")
        elif self.shape_mode and e.key() == Qt.Key.Key_Escape:
            self.win.select_shape(None)
        elif self.edit and e.key() in (Qt.Key.Key_Delete, Qt.Key.Key_Backspace):
            self.win.edit_delete()
        elif self.edit and e.key() == Qt.Key.Key_Escape:
            self.sel = set()
            self.win.selection_changed()
            self.viewport().update()
        else:
            super().keyPressEvent(e)

    # ---- overlay
    def _draw_shapes(self, p: QPainter):
        d = self.win.design
        if d is None or not self.has_image:
            return
        scale = self.transform().m11()
        acc2 = QColor(theme.current()["accent2"])
        p.setBrush(Qt.BrushStyle.NoBrush)
        for sh in d.shapes:  # shapes the user changed: dashed outline (red if removed)
            if sh.edit is not None and sh is not self.shape:
                col = QColor("#e0245e") if sh.kind == "skip" else acc2
                p.setPen(QPen(col, 1.6 / scale, Qt.PenStyle.DashLine))
                p.drawPath(shape_path(sh.geom, self._scene_xy))
        sh = self.shape
        if sh is None:
            return
        p.setBrush(QColor(255, 122, 0, 45))
        p.setPen(QPen(QColor("#ff7a00"), 2.4 / scale))
        p.drawPath(shape_path(sh.geom, self._scene_xy))
        arr = self.arrow()
        if arr is None:
            return
        c, v = arr
        a, b = self._scene_xy(c - v), self._scene_xy(c + v)
        for col, w in ((QColor("white"), 6.0), (QColor("#ff7a00"), 3.0)):  # white halo, then the arrow
            pen = QPen(col, w / scale, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
            p.setPen(pen)
            p.drawLine(QPointF(*a), QPointF(*b))
            u = (b - a) / max(1e-9, float(np.hypot(*(b - a))))
            n = np.array([-u[1], u[0]])
            for tip, s in ((b, 1), (a, -1)):  # rows run both ways: an arrowhead at each end
                for side in (1, -1):
                    q = tip - s * u * 9 / scale + n * side * 6 / scale
                    p.drawLine(QPointF(*tip), QPointF(*q))
        p.setBrush(QColor("#ff7a00"))
        p.setPen(QPen(QColor("white"), 2 / scale))
        for tip in (a, b):  # handles: drag either end to turn the fill direction
            p.drawEllipse(QPointF(*tip), 6 / scale, 6 / scale)

    def drawForeground(self, p: QPainter, rect):
        super().drawForeground(p, rect)
        if self.shape_mode:
            self._draw_shapes(p)
            return
        if not (self.edit and self.has_image and len(self.pts)):
            return
        scale = self.transform().m11()
        sp = self._scene_xy(self.pts)
        vis = ((sp[:, 0] >= rect.left()) & (sp[:, 0] <= rect.right()) &
               (sp[:, 1] >= rect.top()) & (sp[:, 1] <= rect.bottom()))
        p.setPen(Qt.PenStyle.NoPen)
        if vis.sum() < 12000 and scale * self.ppm > 2.0:  # needle points once zoomed in enough
            p.setBrush(QColor(20, 20, 40, 150))
            r = 1.6 / scale
            for x, y in sp[vis]:
                p.drawEllipse(QPointF(x, y), r, r)
        if self.sel:
            d = self.win.design
            off = self._delta if self._delta is not None else np.zeros(2)
            pen = QPen(QColor("#ff7a00"), 1.6 / scale)
            for bi, row in self.sel:
                if bi >= len(d.blocks) or row >= len(d.blocks[bi].cmds):
                    continue
                c = d.blocks[bi].cmds
                pt = self._scene_xy(c[row, :2] + off)
                p.setPen(pen)
                for nb in (row - 1, row + 1):  # the stitches touching this point
                    if 0 <= nb < len(c) and c[nb, 2] == st.STITCH and c[max(row, nb), 2] == st.STITCH:
                        q = self._scene_xy(c[nb, :2] + (off if (bi, nb) in self.sel else 0))
                        p.drawLine(QPointF(*pt), QPointF(*q))
                p.setBrush(QColor("#ff7a00"))
                p.setPen(QPen(QColor("white"), 1.0 / scale))
                r = 3.5 / scale
                p.drawEllipse(QPointF(*pt), r, r)
        if self._band is not None:
            p.setBrush(QColor(91, 76, 219, 40))
            p.setPen(QPen(QColor(theme.current()["accent"]), 1.0 / scale, Qt.PenStyle.DashLine))
            p.drawRect(self._band.normalized())


class ThreadRow(QWidget):
    def __init__(self, i: int, n: int, block: ColorBlock, win: "MainWindow"):
        super().__init__()
        self.setObjectName("threadrow")
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(6, 4, 4, 4)
        lay.setSpacing(4)
        sw = QToolButton()
        sw.setFixedSize(30, 30)
        sw.setToolTip("Change thread colour")
        sw.setStyleSheet(swatch_style(block.rgb))
        sw.clicked.connect(lambda *_: win.pick_color(i))
        lay.addWidget(sw)
        txt = QVBoxLayout()
        txt.setSpacing(0)
        name = QLabel(f"{i + 1}. {block.thread or '#%02X%02X%02X' % block.rgb}")
        kinds = ", ".join(k.replace("run", "line") for k in ("appliqué", "fill", "satin", "run", "border")
                          if block.kinds.get(k))
        sub = QLabel(f"{block.stitch_count:,} stitches" + (f" · {kinds}" if kinds else ""))
        sub.setObjectName("muted")
        txt.addWidget(name)
        txt.addWidget(sub)
        lay.addLayout(txt, 1)
        for label, tip, step, enabled in (("▲", "Sew earlier", -1, i > 0), ("▼", "Sew later", 1, i < n - 1)):
            b = QToolButton(objectName="flat")
            b.setText(label)
            b.setToolTip(tip)
            b.setEnabled(enabled)
            b.clicked.connect(lambda *_, s=step: win.move_block(i, s))
            lay.addWidget(b)
        more = QToolButton(objectName="flat")
        more.setText("⋯")
        more.setToolTip("More options for this colour")
        more.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        m = QMenu(more)
        a = m.addAction("Sew as appliqué (fabric piece + satin edge)")
        a.setCheckable(True)
        a.setChecked(bool(block.kinds.get("appliqué")))
        a.setEnabled(not block.kinds.get("border") and win.can_convert())
        a.toggled.connect(lambda v: win.set_applique(i, v))
        if block.fabric:
            m.addAction("Export cut file for the fabric pieces…", win.save_cut_file)
        m.addAction("Change colour…", lambda: win.pick_color(i))
        if i > 0:
            m.addAction("Merge into the colour above", lambda: win.merge_up(i))
        more.setMenu(m)
        lay.addWidget(more)
        cb = QCheckBox()
        cb.setToolTip("Stitch this colour")
        cb.setChecked(block.visible)
        cb.toggled.connect(lambda v: win.toggle_block(i, v))
        lay.addWidget(cb)


class TextRow(QWidget):
    def __init__(self, i: int, item: TextItem, win: "MainWindow"):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        sw = QLabel()
        sw.setFixedSize(14, 14)
        sw.setStyleSheet(swatch_style(item.rgb, False))
        lay.addWidget(sw)
        what = "monogram " if item.kind == "monogram" else "curved " if abs(item.arc) >= 5 else ""
        lab = QLabel(f"{what}“{item.text.splitlines()[0][:22]}” · {item.height_mm:g} mm")
        lab.setToolTip(f"{item.font}, {item.position}" + (f", curve {item.arc:+g}°" if what == "curved " else ""))
        lay.addWidget(lab, 1)
        e = QToolButton(objectName="flat")
        e.setText("Edit")
        e.clicked.connect(lambda *_: win.edit_text(i))
        lay.addWidget(e)
        d = QToolButton(objectName="flat")
        d.setText("✕")
        d.setToolTip("Remove")
        d.clicked.connect(lambda *_: win.remove_text(i))
        lay.addWidget(d)


class FixRow(QWidget):
    """A line of the picture's own text that is redrawn cleanly."""

    def __init__(self, i: int, fix: TextFix, win: "MainWindow"):
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lab = QLabel(f"✨ cleaned “{fix.text[:22]}” · {fix.font}{' bold' if fix.bold else ''}")
        lab.setToolTip("This text in the picture is redrawn as clean lettering before stitching")
        lay.addWidget(lab, 1)
        d = QToolButton(objectName="flat")
        d.setText("✕")
        d.setToolTip("Stop redrawing it (use the picture's own letters)")
        d.clicked.connect(lambda *_: win.remove_fix(i))
        lay.addWidget(d)


def heading(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setObjectName("h")
    return lab


# ============================================================================ main window
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP)
        self.resize(1400, 900)
        self.setAcceptDrops(True)
        self.qs = app_settings()
        self.pool = QThreadPool.globalInstance()
        self.sig = Signals()
        self.sig.progress.connect(self._on_progress)
        self.sig.done.connect(self._on_done)
        self.sig.failed.connect(self._on_failed)
        self.sig.account.connect(self._on_account_error)
        self._update_sig = _Result()
        self._update_sig.done.connect(self._update_answered)
        self._relaunch: Path | None = None  # an update was put in place: start it once this window closes
        self.gen = 0
        self.busy = self.pending = self.ai_running = False
        self.shape_ai = False  # the AI job running is "suggest for shapes" (not "choose settings")
        self.path: Path | None = None
        self.rgba: np.ndarray | None = None
        self.base: Design | None = None  # as digitized / loaded
        self.design: Design | None = None  # with the user's colour edits applied
        self.file_mode = False  # an embroidery file was opened (no picture to re-digitize)
        self.ai_palette: list | None = None
        self.ai_settings: Settings | None = None
        self.overrides: dict[tuple, tuple] = {}  # image colour -> user colour
        self.hidden: set[tuple] = set()
        self.order: list[tuple] = []  # image colours in the user's sewing order
        self.applique: list[tuple] = []
        self.texts: list[TextItem] = []
        self.text_fixes: list[TextFix] = []  # text in the picture redrawn cleanly (AI text clean-up)
        self.text_ai = False
        self.rotate, self.mirror = 0, False
        self.border_rgb = (30, 30, 30)
        self.fabric_rgb = FABRIC
        self.edited = False  # stitches were edited by hand
        # the hand edits as steps of editing.Ops, re-applied whenever the picture re-converts (redo_log: undone ones)
        self.edit_log: list[list[editing.Op]] = []
        self.redo_log: list[list[editing.Op]] = []
        self.edits_fixed = False  # edits from an older project, saved without their steps: can't be re-applied
        self._baked: set[int] = set()  # id()s of erase Ops the current conversion already cut out
        self._baking: set[int] = set()  # and those of the conversion running now
        self.source_cx = self.result_cx = self.source_key = None  # picture / current-result complexity
        # (picture, prep_inputs, Prepared, its complexity) of the last conversion: reused while only stitch settings
        # change, which skips finding the design and picking colours again (about half of a conversion)
        self._prep: tuple | None = None
        self.undo_stack: list = []
        self.redo_stack: list = []
        self.shape_edits: list[ShapeEdit] = []  # per-shape changes (stitch type, angle, density, order)
        self.shape_undo: list[list[ShapeEdit]] = []
        self.shape_redo: list[list[ShapeEdit]] = []
        self.sel_shape: tuple | None = None  # (rgb, fx, fy) of the selected shape, kept across re-conversions
        # projects
        self.project_path: Path | None = None  # the .stitchpilot file this work is saved in
        self.source_name: str | None = None  # name and bytes of the opened picture / embroidery file
        self.source_bytes: bytes | None = None
        self.source_file: Path | None = None  # readable copy of the source (original, or unpacked)
        self.dirty = False  # unsaved changes
        self._restore_blocks: list | None = None  # hand edits to put back once a project re-converts
        self._restore_log: list | None = None  # and their steps
        self.autosave = QTimer(self, interval=60_000)
        self.autosave.timeout.connect(self._autosave)
        self.autosave.start()
        self.debounce = QTimer(self, singleShot=True, interval=350)
        self.debounce.timeout.connect(self.convert)
        self.play_timer = QTimer(self, interval=40)
        self.play_timer.timeout.connect(self._play_step)

        self._build_ui()
        self._build_menu()
        self._restore()
        self.set_theme(self.qs.value("theme", theme.DEFAULT_THEME))
        self._update_enabled()

    # ------------------------------------------------------------------ UI
    def _header(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(4, 6, 4, 0)
        icon = QLabel()
        icon.setPixmap(app_icon().pixmap(44, 44))
        lay.addWidget(icon)
        txt = QVBoxLayout()
        txt.setSpacing(0)
        txt.addWidget(QLabel(APP, objectName="title"))
        self.tagline = QLabel("", objectName="muted")
        txt.addWidget(self.tagline)
        lay.addLayout(txt, 1)
        return w

    def set_theme(self, name: str):
        apply_light_theme(QApplication.instance(), name)
        name = theme.current_name()
        self.qs.setValue("theme", name)
        for key, lab in self.headings.items():
            lab.setText(theme.HEADINGS[name][key])
        self.tagline.setText(theme.current()["tagline"])
        for v in (self.v_st, self.v_den, self.v_cols, self.v_orig):
            v.restyle()
        for a in self.theme_actions:
            a.setChecked(a.data() == name)
        self.fabric_btn.setStyleSheet(swatch_style(self.fabric_rgb, False))
        self.border_col.setStyleSheet(swatch_style(self.border_rgb, False))
        self._refresh_texts()
        if self.design is not None:
            self._fill_threads(self.design.blocks)
            self.update_stats()  # its hoop warning is coloured by the theme
        self.show_complexity()
        QTimer.singleShot(0, self._fit_cx)  # re-measure once the new style has settled

    def _build_ui(self):
        split = QSplitter()
        self.setCentralWidget(split)

        # ---- left: steps
        side = QWidget(objectName="side")
        outer = QVBoxLayout(side)
        outer.setContentsMargins(14, 8, 14, 14)
        outer.setSpacing(12)
        self.headings: dict[str, QLabel] = {}

        def section(key: str, parent=None) -> QVBoxLayout:
            """A step card (a stitched patch in the cute theme) with its heading."""
            card = QFrame(objectName="card")
            lay = QVBoxLayout(card)
            lay.setContentsMargins(14, 10, 14, 14)
            lay.setSpacing(7)
            self.headings[key] = heading("")
            lay.addWidget(self.headings[key])
            (parent if parent is not None else outer).addWidget(card)
            return lay

        outer.addWidget(self._header())
        sl = section("pic")
        self.open_btn = QPushButton("Open a picture or embroidery file…\nor drop it anywhere on this window",
                                    objectName="drop")
        self.open_btn.clicked.connect(self.open_dialog)
        sl.addWidget(self.open_btn)
        self.file_lbl = QLabel("PNG, JPG, SVG, WEBP, HEIC… or PES, DST, JEF… to view and convert",
                               objectName="muted")
        self.file_lbl.setWordWrap(True)
        sl.addWidget(self.file_lbl)
        orient = QHBoxLayout()
        self.rot_btn = QPushButton("⟳ Rotate")
        self.rot_btn.setToolTip("Turn the picture 90° clockwise")
        self.rot_btn.clicked.connect(self._rotate)
        self.mir_btn = QPushButton("⇋ Mirror")
        self.mir_btn.setCheckable(True)
        self.mir_btn.setToolTip("Flip left-right")
        self.mir_btn.toggled.connect(self._mirror)
        orient.addWidget(self.rot_btn)
        orient.addWidget(self.mir_btn)
        orient.addStretch(1)
        self.gen_btn = QPushButton("✨ Create with AI", objectName="ai")
        self.gen_btn.setToolTip("No picture yet? Describe one and AI draws it, ready to embroider (uses OpenAI)")
        self.gen_btn.clicked.connect(self.create_with_ai)
        orient.addWidget(self.gen_btn)
        sl.addLayout(orient)
        self.file_info = QLabel(objectName="info")
        self.file_info.setWordWrap(True)
        self.file_info.hide()
        sl.addWidget(self.file_info)

        sl = section("size")
        f = QFormLayout()
        self.machine = QComboBox()
        self.machine.addItems(list(MACHINES))
        self.machine.setToolTip("Pick your machine: StitchPilot chooses the file format, thread chart\n"
                                "and the hoops that machine usually takes (check your manual).")
        self.machine.currentTextChanged.connect(self._machine_changed)
        f.addRow("Machine", self.machine)
        self.hoop = QComboBox()
        self.hoop.addItems(list(HOOPS) + ["Custom"])
        self.hoop.currentTextChanged.connect(self._hoop_changed)
        f.addRow("Hoop", self.hoop)
        self.w_spin, self.h_spin = QDoubleSpinBox(), QDoubleSpinBox()
        for sp in (self.w_spin, self.h_spin):
            sp.setRange(10, 400)
            sp.setDecimals(0)
            sp.setSuffix(" mm")
            sp.valueChanged.connect(self._size_edited)
        wh = QHBoxLayout()
        wh.addWidget(self.w_spin)
        wh.addWidget(QLabel("×"))
        wh.addWidget(self.h_spin)
        f.addRow("Max size", wh)
        self.fabric = QComboBox()
        self.fabric.addItems(list(FABRICS))
        self.fabric.currentTextChanged.connect(self._fabric_changed)
        f.addRow("Fabric", self.fabric)
        sl.addLayout(f)

        sl = section("look")
        self.ai_btn = QPushButton("✨  Let AI choose settings", objectName="ai")
        self.ai_btn.setToolTip("Claude looks at your picture and picks thread colours, background\n"
                               "handling and detail level like a professional digitizer.")
        self.ai_btn.clicked.connect(self.run_ai)
        sl.addWidget(self.ai_btn)
        self.ai_lbl = QLabel(objectName="ai")
        self.ai_lbl.setWordWrap(True)
        self.ai_lbl.hide()
        sl.addWidget(self.ai_lbl)
        f = QFormLayout()
        self.colors = QComboBox()
        self.colors.addItem("Automatic", 0)
        for i in range(2, 17):
            self.colors.addItem(str(i), i)
        self.colors.currentIndexChanged.connect(self.changed)
        f.addRow("Colours", self.colors)
        self.bg = QComboBox()
        self.bg.addItem("Remove plain background", "auto")
        self.bg.addItem("Cut out subject (busy photo)", "ai")
        self.bg.addItem("Stitch everything", "keep")
        self.bg.currentIndexChanged.connect(self._bg_changed)
        f.addRow("Background", self.bg)
        sl.addLayout(f)
        self.keep_inside = QCheckBox("Stitch background-coloured areas\ninside the design (e.g. white eyes)")
        self.keep_inside.toggled.connect(self.changed)
        sl.addWidget(self.keep_inside)

        # ---- simplify: complexity badge, one main slider, auto-fix, finer controls
        sl = section("simp")
        # a read-only text box (not a QLabel) so its height matches the wrapped text exactly
        self.cx_lbl = QTextBrowser()
        self.cx_lbl.setFrameShape(QFrame.Shape.NoFrame)
        self.cx_lbl.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.cx_lbl.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.cx_lbl.setStyleSheet("QTextBrowser { background: transparent; }")
        self.cx_lbl.document().setDocumentMargin(0)
        sl.addWidget(self.cx_lbl)
        f = QFormLayout()
        self.simp = QSlider(Qt.Orientation.Horizontal)
        self.simp.setRange(0, 100)
        self.simp.setValue(DEFAULT_SIMPLICITY)
        self.simp.setToolTip("Simplify the picture so it sews better: merges similar colours, removes\n"
                             "specks, thickens hair-thin lines and smooths jagged edges.")
        self.simp.valueChanged.connect(self._simp_changed)
        dl = QHBoxLayout()
        dl.addWidget(QLabel("Detailed", objectName="muted"))
        dl.addWidget(self.simp, 1)
        dl.addWidget(QLabel("Simple", objectName="muted"))
        f.addRow("Simplicity", dl)
        sl.addLayout(f)
        self.friendly_btn = QPushButton("🪄  Make it embroidery-friendly", objectName="friendly")
        self.friendly_btn.setToolTip("Finds the lightest simplification that gets the picture to at most\n"
                                     "8 colours, few tiny shapes and no hair-thin lines.")
        self.friendly_btn.clicked.connect(self.make_friendly)
        sl.addWidget(self.friendly_btn)
        row = QHBoxLayout()
        self.show_problems = QCheckBox("Show problem spots")
        self.show_problems.setToolTip("Colours view: shapes too small and lines too thin to sew are shown in red")
        self.show_problems.toggled.connect(self._colors_view_opts)
        self.compare = QCheckBox("Compare with original")
        self.compare.setToolTip("Colours view: original picture and simplified version side by side")
        self.compare.toggled.connect(self._colors_view_opts)
        row.addWidget(self.show_problems)
        row.addWidget(self.compare)
        row.addStretch(1)
        sl.addLayout(row)
        self.simp_more = QCheckBox("More simplify options")
        self.simp_more.toggled.connect(lambda v: self.simp_box.setVisible(v))
        sl.addWidget(self.simp_more)
        self.simp_box = QWidget()
        sf = QFormLayout(self.simp_box)
        sf.setContentsMargins(0, 0, 0, 0)
        self.sp_merge = QSlider(Qt.Orientation.Horizontal)
        self.sp_merge.setRange(3, 25)
        self.sp_merge.setToolTip("Colours closer than this are merged into one thread")
        sf.addRow("Merge similar colours", self.sp_merge)
        self.sp_min = QDoubleSpinBox()
        self.sp_min.setRange(0.5, 40.0)
        self.sp_min.setDecimals(1)
        self.sp_min.setSingleStep(0.5)
        self.sp_min.setSuffix(" mm²")
        self.sp_min.setToolTip("Specks and holes smaller than this are absorbed by their surroundings")
        sf.addRow("Remove shapes under", self.sp_min)
        self.thin = QComboBox()
        self.thin.addItem("Keep as they are", "keep")
        self.thin.addItem("Thicken to 1 mm", "thicken")
        self.thin.addItem("Remove", "remove")
        sf.addRow("Hair-thin lines", self.thin)
        self.sp_smooth = QSlider(Qt.Orientation.Horizontal)
        self.sp_smooth.setRange(0, 100)
        sf.addRow("Smooth edges", self.sp_smooth)
        for w in (self.sp_merge, self.sp_smooth):
            w.valueChanged.connect(self._simp_custom)
        self.sp_min.valueChanged.connect(self._simp_custom)
        self.thin.currentIndexChanged.connect(self._simp_custom)
        self.simp_box.hide()
        sl.addWidget(self.simp_box)
        self._simp_changed(DEFAULT_SIMPLICITY)

        self.adv_toggle = QCheckBox("Show advanced stitch settings")
        self.adv_toggle.toggled.connect(lambda v: self.adv.setVisible(v))
        sl.addWidget(self.adv_toggle)
        self.adv = QWidget()
        af = QFormLayout(self.adv)
        af.setContentsMargins(0, 0, 0, 0)

        def dspin(lo, hi, step, suffix=" mm", dec=2):
            sp = QDoubleSpinBox()
            sp.setRange(lo, hi)
            sp.setSingleStep(step)
            sp.setDecimals(dec)
            sp.setSuffix(suffix)
            sp.valueChanged.connect(self.changed)
            return sp

        self.sp_fill = dspin(0.25, 0.9, 0.02)
        self.sp_fill.setToolTip("Distance between fill rows. Smaller = denser.")
        af.addRow("Fill spacing", self.sp_fill)
        self.sp_len = dspin(2.0, 7.0, 0.1, dec=1)
        af.addRow("Fill stitch length", self.sp_len)
        self.sp_satin = dspin(0.25, 0.9, 0.02)
        af.addRow("Satin spacing", self.sp_satin)
        self.sp_satin_max = dspin(3.0, 10.0, 0.5, dec=1)
        self.sp_satin_max.setToolTip("Shapes narrower than this use satin; wider ones use fill.")
        af.addRow("Max satin width", self.sp_satin_max)
        self.sp_pull = dspin(0.0, 0.8, 0.05)
        self.sp_pull.setToolTip("Widens shapes to make up for fabric pulling in while stitching.")
        af.addRow("Pull compensation", self.sp_pull)
        self.angle = QComboBox()
        self.angle.addItem("Follow each shape", "follow")
        self.angle.addItem("Vary per colour", "vary")
        self.angle.setToolTip("Follow each shape: fill rows run along each shape's length.\n"
                              "Vary per colour: one angle per thread, different for each.")
        for a in range(0, 180, 15):
            self.angle.addItem(f"{a}°", a)
        self.angle.currentIndexChanged.connect(self.changed)
        af.addRow("Fill angle", self.angle)
        self.underlay = QCheckBox("Underlay (recommended)")
        self.underlay.toggled.connect(self.changed)
        af.addRow("", self.underlay)
        self.adv.hide()
        sl.addWidget(self.adv)

        sl = section("text")
        self.text_box = QVBoxLayout()
        self.text_box.setSpacing(2)
        sl.addLayout(self.text_box)
        row = QHBoxLayout()
        self.add_text_btn = QPushButton("＋ Add text")
        self.add_text_btn.setToolTip("A name or slogan above, below or over the picture – straight or curved")
        self.add_text_btn.clicked.connect(lambda *_: self.add_text())
        row.addWidget(self.add_text_btn, 1)
        self.add_mono_btn = QPushButton("＋ Monogram")
        self.add_mono_btn.setToolTip("1–3 initials in a frame – a classic for towels, bags and gifts")
        self.add_mono_btn.clicked.connect(lambda *_: self.add_text("monogram"))
        row.addWidget(self.add_mono_btn, 1)
        sl.addLayout(row)
        self.fix_text_btn = QPushButton("✨ Clean up text in the picture", objectName="ai")
        self.fix_text_btn.setToolTip("Claude finds the words in your picture; StitchPilot redraws them as crisp\n"
                                     "lettering in the closest matching font, so they stitch cleanly.\n"
                                     "Best for blurry, small or pixelated logos.")
        self.fix_text_btn.clicked.connect(self.clean_text)
        sl.addWidget(self.fix_text_btn)
        f = QFormLayout()
        self.border = QComboBox()
        self.border.addItem("No border", "none")
        self.border.addItem("Outline (bean stitch)", "run")
        self.border.addItem("Satin edge (patches)", "satin")
        self.border.currentIndexChanged.connect(self._border_changed)
        bl = QHBoxLayout()
        bl.addWidget(self.border, 1)
        self.border_col = QToolButton()
        self.border_col.setFixedSize(26, 26)
        self.border_col.setToolTip("Border thread colour")
        self.border_col.clicked.connect(self._pick_border)
        bl.addWidget(self.border_col)
        f.addRow("Border", bl)
        self.border_w = QDoubleSpinBox()
        self.border_w.setRange(1.5, 8.0)
        self.border_w.setSingleStep(0.5)
        self.border_w.setDecimals(1)
        self.border_w.setSuffix(" mm")
        self.border_w.setValue(3.0)
        self.border_w.valueChanged.connect(self.changed)
        self.border_off = QDoubleSpinBox()
        self.border_off.setRange(0.0, 15.0)
        self.border_off.setSingleStep(0.5)
        self.border_off.setDecimals(1)
        self.border_off.setSuffix(" mm")
        self.border_off.setToolTip("Gap between the design and its border")
        self.border_off.valueChanged.connect(self.changed)
        bw = QHBoxLayout()
        bw.addWidget(QLabel("width"))
        bw.addWidget(self.border_w)
        bw.addWidget(QLabel("gap"))
        bw.addWidget(self.border_off)
        f.addRow("", bw)
        sl.addLayout(f)

        sl = section("save")
        f = QFormLayout()
        self.fmt = QComboBox()
        self.fmt.addItems(list(FORMATS))
        self.fmt.currentIndexChanged.connect(lambda *_: self._mark_dirty())
        f.addRow("Format", self.fmt)
        self.chart = QComboBox()
        self.chart.addItem("Don't match", None)
        for name in THREAD_CHARTS:
            self.chart.addItem(f"{name} threads", name)
        self.chart.currentIndexChanged.connect(self.refresh_colors)
        self.chart.currentIndexChanged.connect(lambda *_: self._mark_dirty())
        f.addRow("Thread chart", self.chart)
        sl.addLayout(f)
        self.snap = QCheckBox("Use exact thread chart colours")
        self.snap.toggled.connect(self.refresh_colors)
        self.snap.toggled.connect(lambda *_: self._mark_dirty())
        sl.addWidget(self.snap)
        self.trims = QCheckBox("My machine cuts threads automatically")
        self.trims.setToolTip("Untick for machines without a thread cutter: pieces are joined with\n"
                              "jump stitches that you snip by hand afterwards.")
        self.trims.setChecked(True)
        self.trims.toggled.connect(self.changed)
        sl.addWidget(self.trims)
        self.save_btn = QPushButton("Save embroidery file…", objectName="primary")
        self.save_btn.clicked.connect(self.save)
        sl.addWidget(self.save_btn)
        extras = QHBoxLayout()
        self.proj_btn = QPushButton("💾 Save project")
        self.proj_btn.setToolTip("Keep the picture, all settings and your edits in one file to come back to (Ctrl+S)")
        self.proj_btn.clicked.connect(lambda: self.save_project())
        self.tmpl_btn = QPushButton("📐 Placement template")
        self.tmpl_btn.setToolTip("Print the design at real size with centre marks, to position it on a garment")
        self.tmpl_btn.clicked.connect(self.save_template)
        extras.addWidget(self.proj_btn)
        extras.addWidget(self.tmpl_btn)
        sl.addLayout(extras)
        outer.addStretch(1)

        scroll = QScrollArea()
        scroll.setWidget(side)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.Shape.NoFrame)
        scroll.setMinimumWidth(side.minimumSizeHint().width() + 24)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        split.addWidget(scroll)

        # ---- centre: views
        centre = QWidget()
        cl = QVBoxLayout(centre)
        cl.setContentsMargins(0, 0, 0, 0)

        # edit toolbar (only while editing stitches)
        self.edit_bar = QWidget(objectName="editbar")
        eb = QHBoxLayout(self.edit_bar)
        eb.setContentsMargins(10, 6, 10, 2)
        self.t_select = QToolButton()
        self.t_select.setText("⬚ Select / move")
        self.t_select.setCheckable(True)
        self.t_select.setChecked(True)
        self.t_select.setToolTip("Click a needle point, Shift-click to add more, drag a box,\n"
                                 "double-click to pick a whole run. Drag selected points to move them.")
        self.t_select.clicked.connect(lambda: self.set_tool("select"))
        self.t_add = QToolButton()
        self.t_add.setText("＋ Add stitches")
        self.t_add.setCheckable(True)
        self.t_add.setToolTip("Each click sews from the selected point to where you click.\n"
                              "With nothing selected, a click starts a new run in the chosen colour.")
        self.t_add.clicked.connect(lambda: self.set_tool("add"))
        eb.addWidget(self.t_select)
        eb.addWidget(self.t_add)
        eb.addWidget(QLabel("in", objectName="muted"))
        self.add_color = QComboBox()
        self.add_color.setToolTip("Colour for new runs")
        eb.addWidget(self.add_color)
        self.del_btn = QToolButton()
        self.del_btn.setText("🗑 Delete")
        self.del_btn.setToolTip("Delete the selected needle points (Del)")
        self.del_btn.clicked.connect(self.edit_delete)
        eb.addWidget(self.del_btn)
        self.undo_btn = QToolButton()
        self.undo_btn.setText("↶ Undo")
        self.undo_btn.clicked.connect(self.undo)
        self.redo_btn = QToolButton()
        self.redo_btn.setText("↷ Redo")
        self.redo_btn.clicked.connect(self.redo)
        eb.addWidget(self.undo_btn)
        eb.addWidget(self.redo_btn)
        self.sel_lbl = QLabel("", objectName="muted")
        self.sel_lbl.setWordWrap(True)
        self.sel_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        eb.addWidget(self.sel_lbl, 1)
        self.edit_bar.hide()
        cl.addWidget(self.edit_bar)
        cl.addWidget(self._build_shape_bar())
        self.edit_banner = QWidget()
        bb = QHBoxLayout(self.edit_banner)
        bb.setContentsMargins(10, 2, 10, 2)
        self.edit_msg = msg = QLabel(objectName="note")
        msg.setWordWrap(True)
        msg.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        bb.addWidget(msg, 1)
        discard = QPushButton("Discard edits")
        discard.clicked.connect(self.discard_edits)
        bb.addWidget(discard)
        self.edit_banner.hide()
        cl.addWidget(self.edit_banner)

        self.tabs = QTabWidget()
        self.v_st = StitchView(self)
        self.v_den = ZoomView("Stitch density shows up here 🌡️")
        self.v_cols = ZoomView("Your simplified colours show up here 🎨", checker=True)
        self.v_orig = ZoomView("Open a picture to get started 🌸", checker=True)
        self.tabs.addTab(self.v_st, "Stitches")
        self.tabs.addTab(self.v_den, "Density")
        self.tabs.addTab(self.v_cols, "Colours")
        self.tabs.addTab(self.v_orig, "Original")
        self.tabs.setTabToolTip(1, "Blue/green = normal, yellow = heavy, red = so dense it may break needles")
        self.tabs.currentChanged.connect(lambda *_: self.render_density())
        cl.addWidget(self.tabs, 1)
        bar = QHBoxLayout()
        bar.setContentsMargins(10, 4, 10, 6)
        self.edit_btn = QPushButton("✎ Edit stitches")
        self.edit_btn.setCheckable(True)
        self.edit_btn.setToolTip("Select, move, delete or add individual stitches (Ctrl+E)")
        self.edit_btn.toggled.connect(self.toggle_edit)
        bar.addWidget(self.edit_btn)
        self.shape_btn = QPushButton("🧩 Edit shapes")
        self.shape_btn.setCheckable(True)
        self.shape_btn.setToolTip("Click a shape to change its stitch type, fill direction, density or order,\n"
                                  "or to remove it. AI can suggest changes too. (Ctrl+M)")
        self.shape_btn.toggled.connect(self.toggle_shapes)
        bar.addWidget(self.shape_btn)
        self.play = QToolButton()
        self.play.setText("▶")
        self.play.setToolTip("Play the sew-out")
        self.play.clicked.connect(self._toggle_play)
        bar.addWidget(self.play)
        self.sim = QSlider(Qt.Orientation.Horizontal)
        self.sim.setRange(0, 1000)
        self.sim.setValue(1000)
        self.sim.setToolTip("Drag to watch the design being stitched")
        self.sim.valueChanged.connect(lambda: self.sim_timer.start())
        bar.addWidget(self.sim, 1)
        self.jumps = QCheckBox("Jumps")
        self.jumps.setToolTip("Show jump stitches (red)")
        self.jumps.toggled.connect(self.render_stitches)
        bar.addWidget(self.jumps)
        self.show_hoop = QCheckBox("Hoop")
        self.show_hoop.setToolTip("Show the hoop outline")
        self.show_hoop.setChecked(True)
        self.show_hoop.toggled.connect(self.render_stitches)
        bar.addWidget(self.show_hoop)
        self.fabric_btn = QToolButton()
        self.fabric_btn.setFixedSize(26, 26)
        self.fabric_btn.setToolTip("Fabric colour for the preview")
        self.fabric_btn.clicked.connect(self._pick_fabric)
        bar.addWidget(QLabel("Fabric", objectName="muted"))
        bar.addWidget(self.fabric_btn)
        fit = QPushButton("Fit")
        fit.clicked.connect(lambda: self.tabs.currentWidget().fit_view())
        bar.addWidget(fit)
        cl.addLayout(bar)
        self.sim_timer = QTimer(self, singleShot=True, interval=30)
        self.sim_timer.timeout.connect(self.render_stitches)
        split.addWidget(centre)

        # ---- right: summary + threads
        right = QWidget(objectName="right")
        rl = QVBoxLayout(right)
        rl.setContentsMargins(10, 8, 14, 14)
        rl.setSpacing(12)
        dl_ = section("design", rl)
        self.stats = QLabel("—", objectName="stat")
        self.stats.setTextFormat(Qt.TextFormat.RichText)
        dl_.addWidget(self.stats)
        self.notes = QLabel(objectName="note")
        self.notes.setWordWrap(True)
        self.notes.hide()
        dl_.addWidget(self.notes)
        tl_ = section("threads", rl)
        rl = tl_
        self.thread_box = QVBoxLayout()
        tw = QWidget()
        tw.setLayout(self.thread_box)
        ts = QScrollArea()
        ts.setWidget(tw)
        ts.setWidgetResizable(True)
        ts.setFrameShape(QFrame.Shape.NoFrame)
        ts.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        rl.addWidget(ts, 1)
        rl.parentWidget().setSizePolicy(QSizePolicy.Policy.Preferred, QSizePolicy.Policy.Expanding)
        hint = QLabel("Click a swatch to recolour (same colour = merged). ▲▼ change the order; ⋯ for appliqué.",
                      objectName="muted")
        hint.setWordWrap(True)
        rl.addWidget(hint)
        right.layout().setStretch(1, 1)
        right.setMinimumWidth(340)
        split.addWidget(right)
        split.setSizes([360, 700, 340])
        split.setStretchFactor(1, 1)

        self.progress = QProgressBar()
        self.progress.setMaximumWidth(220)
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.progress.hide()
        self.statusBar().addPermanentWidget(self.progress)
        self.statusBar().showMessage("Hi! Open a picture to begin 🌸")

    def _build_shape_bar(self) -> QWidget:
        self.shape_bar = QWidget(objectName="editbar")
        v = QVBoxLayout(self.shape_bar)
        v.setContentsMargins(10, 6, 10, 4)
        v.setSpacing(4)
        top = QHBoxLayout()
        self.shape_lbl = QLabel("", objectName="muted")
        self.shape_lbl.setWordWrap(True)
        self.shape_lbl.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        top.addWidget(self.shape_lbl, 1)
        self.shape_remove = QToolButton()
        self.shape_remove.setText("🗑 Remove")
        self.shape_remove.setCheckable(True)
        self.shape_remove.setToolTip("Don't sew this shape (Del). Click again to bring it back.")
        self.shape_remove.clicked.connect(lambda on: self.update_shape(kind="skip" if on else "auto"))
        top.addWidget(self.shape_remove)
        self.shape_reset = QToolButton()
        self.shape_reset.setText("↺ Reset")
        self.shape_reset.setToolTip("Back to the automatic choices for this shape")
        self.shape_reset.clicked.connect(lambda: self.update_shape(reset=True))
        top.addWidget(self.shape_reset)
        self.shape_ai_btn = QPushButton("✨ Suggest with AI", objectName="ai")
        self.shape_ai_btn.setToolTip("Claude looks at every shape and suggests a stitch type and fill direction.\n"
                                     "You choose which suggestions to use.")
        self.shape_ai_btn.clicked.connect(self.suggest_shapes)
        top.addWidget(self.shape_ai_btn)
        self.shape_undo_btn = QToolButton()
        self.shape_undo_btn.setText("↶ Undo")
        self.shape_undo_btn.clicked.connect(self.undo)
        self.shape_redo_btn = QToolButton()
        self.shape_redo_btn.setText("↷ Redo")
        self.shape_redo_btn.clicked.connect(self.redo)
        self.shape_reset_all = QToolButton()
        self.shape_reset_all.setText("Reset all")
        self.shape_reset_all.setToolTip("Undo every shape change and go back to the automatic choices")
        self.shape_reset_all.clicked.connect(self.reset_all_shapes)
        for w in (self.shape_undo_btn, self.shape_redo_btn, self.shape_reset_all):
            top.addWidget(w)
        v.addLayout(top)
        row = QHBoxLayout()
        self.shape_kind = QComboBox()
        for k, label in (("auto", "Auto"), ("fill", "Fill"), ("satin", "Satin"), ("outline", "Outline only")):
            self.shape_kind.addItem(label, k)
        self.shape_kind.insertSeparator(self.shape_kind.count())
        for k, label in pat.PATTERNS.items():
            if k != "plain":
                self.shape_kind.addItem(f"{label} fill" if k in ("waves", "cross") else label, f"fill:{k}")
        self.shape_kind.setToolTip("Fill: rows of stitches, for areas. Satin: smooth columns, for strokes and\n"
                                   "borders up to about 8 mm wide (wider satins snag). Outline only: just a line\n"
                                   "around the edge.\n\nWaves: a fill with rippling rows. Hearts, Stars, Flowers: "
                                   "rows of little motifs\n(light and lacy: the fabric shows). Cross-stitch: an X "
                                   "in every little square.")
        self.shape_kind.activated.connect(lambda _: self._shape_kind_picked(self.shape_kind.currentData()))
        row.addWidget(QLabel("Stitch", objectName="muted"))
        row.addWidget(self.shape_kind)
        self.shape_angle = QSpinBox()
        self.shape_angle.setRange(-5, 175)
        self.shape_angle.setSingleStep(5)
        self.shape_angle.setSpecialValueText("Auto")
        self.shape_angle.setSuffix("°")
        self.shape_angle.setToolTip("Direction of the fill rows. You can also drag the arrow on the shape.")
        self.shape_angle.editingFinished.connect(self._shape_angle_edited)
        row.addWidget(QLabel("Direction", objectName="muted"))
        row.addWidget(self.shape_angle)
        self.shape_density = QSpinBox()
        self.shape_density.setRange(70, 150)
        self.shape_density.setSingleStep(5)
        self.shape_density.setSuffix(" %")
        self.shape_density.setToolTip("Stitch density for this shape: above 100% is denser (better coverage),\n"
                                      "below is lighter (softer, fewer stitches).")
        self.shape_density.editingFinished.connect(
            lambda: self.update_shape(density=self.shape_density.value() / 100))
        row.addWidget(QLabel("Density", objectName="muted"))
        row.addWidget(self.shape_density)
        self.shape_order = QComboBox()
        for k, label in ((0, "Auto order"), (-1, "Sew first"), (1, "Sew last")):
            self.shape_order.addItem(label, k)
        self.shape_order.setToolTip("When this shape is sewn among the shapes of its colour")
        self.shape_order.activated.connect(lambda _: self.update_shape(order=self.shape_order.currentData()))
        row.addWidget(self.shape_order)
        for combo in (self.shape_kind, self.shape_order):  # let them shrink on small screens
            combo.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(9)
        row.addStretch(1)
        v.addLayout(row)
        self.shape_bar.hide()
        return self.shape_bar

    def _build_menu(self):
        m = self.menuBar().addMenu("&File")
        m.addAction(QAction("&New lettering design", self, shortcut=QKeySequence.StandardKey.New,
                            triggered=lambda *_: self.new_text_design()))
        m.addAction(QAction("New &monogram", self, triggered=lambda *_: self.new_text_design("monogram")))
        m.addAction(QAction("&Create a picture with AI…", self, shortcut="Ctrl+Shift+N",
                            triggered=lambda *_: self.create_with_ai()))
        m.addAction(QAction("&Open picture, embroidery file or project…", self,
                            shortcut=QKeySequence.StandardKey.Open, triggered=self.open_dialog))
        self.recent_menu = m.addMenu("Open &recent")
        self.recent_menu.aboutToShow.connect(self._fill_recent)
        m.addSeparator()
        m.addAction(QAction("&Save project", self, shortcut=QKeySequence.StandardKey.Save,
                            triggered=lambda: self.save_project()))
        m.addAction(QAction("Save project &as…", self, shortcut=QKeySequence.StandardKey.SaveAs,
                            triggered=lambda: self.save_project(as_new=True)))
        m.addSeparator()
        m.addAction(QAction("Save &embroidery file…", self, shortcut="Ctrl+Shift+E", triggered=self.save))
        m.addAction(QAction("Print placement &template (PDF)…", self, shortcut="Ctrl+Shift+P",
                            triggered=self.save_template))
        m.addAction(QAction("Print &worksheet (PDF)…", self, shortcut=QKeySequence.StandardKey.Print,
                            triggered=self.save_worksheet))
        m.addAction(QAction("Export &preview image…", self, triggered=self.save_preview))
        self.cut_act = QAction("Export appliqué &cut file (SVG / DXF)…", self, triggered=self.save_cut_file)
        self.cut_act.setToolTip("The fabric pieces at real size, for a Cricut, ScanNCut or Silhouette")
        m.addAction(self.cut_act)
        m.addSeparator()
        m.addAction(QAction("&Batch convert pictures…", self, triggered=self.batch))
        m.addSeparator()
        m.addAction(QAction("&Quit", self, shortcut=QKeySequence.StandardKey.Quit, triggered=self.close))
        m = self.menuBar().addMenu("&Edit")
        m.addAction(QAction("&Undo", self, shortcut=QKeySequence.StandardKey.Undo, triggered=self.undo))
        m.addAction(QAction("&Redo", self, shortcut=QKeySequence.StandardKey.Redo, triggered=self.redo))
        m.addSeparator()
        m.addAction(QAction("&Edit stitches", self, shortcut="Ctrl+E", triggered=self.edit_btn.toggle))
        m.addAction(QAction("Edit &shapes", self, shortcut="Ctrl+M", triggered=self.shape_btn.toggle))
        m.addAction(QAction("&Discard all stitch edits", self, triggered=self.discard_edits))
        m = self.menuBar().addMenu("&View")
        tm = m.addMenu("Theme")
        group = QActionGroup(self)
        self.theme_actions = []
        for name, t in theme.THEMES.items():
            a = QAction(t["label"], self, checkable=True)
            a.setData(name)
            a.triggered.connect(lambda _=False, n=name: self.set_theme(n))
            group.addAction(a)
            tm.addAction(a)
            self.theme_actions.append(a)
        m = self.menuBar().addMenu("&AI")
        m.addAction(QAction("Let AI choose settings", self, shortcut="Ctrl+I", triggered=self.run_ai))
        m.addAction(QAction("StitchPilot Plus && account…", self, triggered=lambda *_: self.show_account()))
        self.auto_ai = QAction("Run AI automatically on new pictures", self, checkable=True)
        m.addAction(self.auto_ai)
        m = self.menuBar().addMenu("&Help")
        m.addAction(QAction("Tips for great results", self, triggered=self.show_tips))
        m.addAction(QAction("Check for updates…", self, triggered=lambda *_: self.check_updates()))
        self.auto_update = QAction("Check for updates automatically", self, checkable=True)
        m.addAction(self.auto_update)
        m.addAction(QAction(f"About {APP}", self, triggered=lambda: QMessageBox.about(
            self, APP, f"<b>{APP} {__version__}</b><br>Turns pictures into machine embroidery files.<br>"
            "Files are written with pyembroidery.")))

    # ------------------------------------------------------------------ settings <-> UI
    def _restore(self):
        q = self.qs
        self._quiet = True
        machine = q.value("machine", DEFAULT_MACHINE)
        self.machine.setCurrentText(machine if machine in MACHINES else DEFAULT_MACHINE)
        self._machine_changed(self.machine.currentText())
        self.hoop.setCurrentText(q.value("hoop", DEFAULT_HOOP))
        self._hoop_changed(self.hoop.currentText())
        if self.hoop.currentText() == "Custom":
            self.w_spin.setValue(float(q.value("w", 95)))
            self.h_spin.setValue(float(q.value("h", 95)))
        self.fabric.setCurrentText(q.value("fabric", DEFAULT_FABRIC))
        self._load_stitch_params(Settings().with_fabric(self.fabric.currentText()))
        self.fmt.setCurrentText(q.value("format", DEFAULT_FORMAT))
        self.chart.setCurrentIndex(max(0, self.chart.findData(q.value("chart", "Brother"))))
        self.auto_ai.setChecked(q.value("auto_ai", "true") == "true")
        self.auto_update.setChecked(q.value("auto_update", "true") == "true")
        self.trims.setChecked(q.value("trims", "true") == "true")
        fab = q.value("fabric_rgb", "")
        if fab:
            self.fabric_rgb = tuple(int(v) for v in str(fab).split(","))
        self._quiet = False
        self.fabric_btn.setStyleSheet(swatch_style(self.fabric_rgb, False))
        self.border_col.setStyleSheet(swatch_style(self.border_rgb, False))
        self._border_changed()

    def closeEvent(self, e):
        if not self.maybe_save():
            e.ignore()
            return
        self._remove_autosave()  # a clean exit: nothing to recover next time
        q = self.qs
        q.setValue("machine", self.machine.currentText())
        q.setValue("hoop", self.hoop.currentText())
        q.setValue("w", self.w_spin.value())
        q.setValue("h", self.h_spin.value())
        q.setValue("fabric", self.fabric.currentText())
        q.setValue("format", self.fmt.currentText())
        q.setValue("chart", self.chart.currentData() or "")
        q.setValue("auto_ai", "true" if self.auto_ai.isChecked() else "false")
        q.setValue("trims", "true" if self.trims.isChecked() else "false")
        q.setValue("fabric_rgb", ",".join(str(v) for v in self.fabric_rgb))
        q.setValue("auto_update", "true" if self.auto_update.isChecked() else "false")
        if self._relaunch:
            q.sync()  # the new copy reads these settings as it starts
            try:
                updates.relaunch(self._relaunch)
            except OSError:
                pass  # it's in place: it starts the next time StitchPilot is opened
        super().closeEvent(e)

    def _load_stitch_params(self, s: Settings):
        widgets = (self.sp_fill, self.sp_len, self.sp_satin, self.sp_satin_max, self.sp_pull, self.underlay)
        for w in widgets:
            w.blockSignals(True)
        self.sp_fill.setValue(s.fill_spacing)
        self.sp_len.setValue(s.fill_stitch_len)
        self.sp_satin.setValue(s.satin_spacing)
        self.sp_satin_max.setValue(s.satin_max_width)
        self.sp_pull.setValue(s.pull_comp)
        self.underlay.setChecked(s.underlay)
        for w in widgets:
            w.blockSignals(False)

    def settings(self) -> Settings:
        ang = self.angle.currentData()  # "follow", "vary" or a fixed angle
        s = Settings(
            max_width_mm=self.w_spin.value(), max_height_mm=self.h_spin.value(),
            detail=1.0 - self.simp.value() / 100, background=self.bg.currentData(),
            merge_de=float(self.sp_merge.value()), min_shape_mm2=self.sp_min.value(),
            thin_lines=self.thin.currentData(), smooth=self.sp_smooth.value() / 100,
            remove_enclosed_bg=not self.keep_inside.isChecked(),
            fill_spacing=self.sp_fill.value(), fill_stitch_len=self.sp_len.value(),
            fill_angle=None if isinstance(ang, str) else ang, fill_follow=ang == "follow",
            satin_spacing=self.sp_satin.value(),
            satin_max_width=self.sp_satin_max.value(), pull_comp=self.sp_pull.value(),
            underlay=self.underlay.isChecked(), fabric=self.fabric.currentText(),
            trims=self.trims.isChecked(), rotate=self.rotate, mirror=self.mirror,
            texts=list(self.texts), text_fixes=list(self.text_fixes), applique=list(self.applique),
            border=self.border.currentData(), border_rgb=self.border_rgb,
            border_width=self.border_w.value(), border_offset=self.border_off.value(),
            color_order=list(self.order), skip_colors=list(self.hidden),
            shape_edits=[replace(e) for e in self.shape_edits],
            erase=[(op.rgb, shapely.to_wkt(op.area, rounding_precision=6)) for op in self._erase_ops()],
        )
        n = self.colors.currentData()
        if n == "ai" and self.ai_palette:
            s = replace(s, palette=self.ai_palette, num_colors=len(self.ai_palette))
        elif isinstance(n, int) and n > 0:
            s = replace(s, num_colors=n)
        if self.ai_settings is not None and s.background == "auto":
            s = replace(s, background_rgb=self.ai_settings.background_rgb)
        return s

    def hoop_mm(self) -> tuple[float, float]:
        name = self.hoop.currentText()
        if name in HOOPS:
            return HOOPS[name]
        return self.w_spin.value() + 2 * MARGIN_MM, self.h_spin.value() + 2 * MARGIN_MM

    def _set_hoop_list(self, names: list[str], keep: str):
        self.hoop.blockSignals(True)
        self.hoop.clear()
        self.hoop.addItems(names + ["Custom"])
        self.hoop.setCurrentText(keep if keep in names or keep == "Custom" else names[-1])
        self.hoop.blockSignals(False)

    def _machine_changed(self, name: str):
        m = MACHINES.get(name)
        if m is None:
            return
        current = self.hoop.currentText()
        self._set_hoop_list(hoops_for(name), current)
        if getattr(self, "_quiet", False):  # restoring settings / a project: only the hoop list changes
            return
        self.fmt.setCurrentText(m.format)
        self.chart.setCurrentIndex(max(0, self.chart.findData(m.chart)))
        if m.hoops and current not in m.hoops and m.default_hoop:
            self.hoop.blockSignals(True)
            self.hoop.setCurrentText(m.default_hoop)
            self.hoop.blockSignals(False)
        self._hoop_changed(self.hoop.currentText())
        if name != DEFAULT_MACHINE:
            self.statusBar().showMessage(
                f"Set up for {name}: {m.format.split(' –')[0]} files and its usual hoops "
                "(check your manual for which ones fit).", 10000)
        self._mark_dirty()

    def _hoop_changed(self, name: str):
        if name in HOOPS:
            w, h = HOOPS[name]
            for sp, v in ((self.w_spin, w), (self.h_spin, h)):
                sp.blockSignals(True)
                sp.setValue(v - 2 * MARGIN_MM)
                sp.blockSignals(False)
        self.changed()
        self.render_stitches()

    def _size_edited(self):
        name = self.hoop.currentText()
        if name in HOOPS and (self.w_spin.value(), self.h_spin.value()) != tuple(v - 2 * MARGIN_MM for v in HOOPS[name]):
            self.hoop.blockSignals(True)
            self.hoop.setCurrentText("Custom")
            self.hoop.blockSignals(False)
        self.changed()

    def _fabric_changed(self, name: str):
        self._load_stitch_params(Settings().with_fabric(name))
        self.changed()

    def _simp_changed(self, v: int):
        """Main Simplicity slider: drive the finer controls, then re-convert."""
        s = Settings().simplified(v / 100)
        widgets = (self.sp_merge, self.sp_min, self.thin, self.sp_smooth)
        for w in widgets:
            w.blockSignals(True)
        self.sp_merge.setValue(int(round(s.merge_de)))
        self.sp_min.setValue(s.min_shape_mm2)
        self.thin.setCurrentIndex(self.thin.findData(s.thin_lines))
        self.sp_smooth.setValue(int(round(s.smooth * 100)))
        for w in widgets:
            w.blockSignals(False)
        self.changed()

    def _simp_custom(self, *_):
        self.changed()

    def _colors_view_opts(self, *_):
        self.tabs.setCurrentWidget(self.v_cols)
        self.update_colors_view()

    def update_colors_view(self):
        d = self.design
        if d is None or d.prepared is None:
            self.v_cols.set_image(None)
            return
        prep = d.prepared
        cx = getattr(self, "result_cx", None)
        if self.show_problems.isChecked() and cx is not None and cx.problems is not None \
                and cx.problems.shape == prep.labels.shape:
            img = problem_overlay(prep, cx.problems)
        else:
            img = prep.color_preview()
        if self.compare.isChecked():
            src = prep.source_rgba
            h, w = img.shape[:2]
            src = cv2.resize(src, (w - 6, h - 6), interpolation=cv2.INTER_AREA)
            src = cv2.copyMakeBorder(src, 3, 3, 3, 3, cv2.BORDER_CONSTANT, value=(0, 0, 0, 0))
            gap = np.zeros((h, max(8, w // 30), 4), np.uint8)
            img = np.hstack([src, gap, img])
        self.v_cols.set_image(to_qimage(img))

    def show_complexity(self):
        src, res = getattr(self, "source_cx", None), getattr(self, "result_cx", None)
        if src is None:
            self._set_cx_text(f"<span style='color:{theme.current()['muted']}'>"
                              "Open a picture to see how complex it is.</span>")
            return
        t = theme.current()
        colors = {"Simple": t["good"], "Moderate": t["fair"], "Complex": t["poor"], "Very complex": t["bad"],
                  "Photo": t["photo"]}

        def pill(c):
            return f"<b style='color:{colors.get(c.level, theme.current()['text'])}'>● {c.level}</b> ({c.score}/100)"

        text = f"Picture: {pill(src)}<br><span style='color:{theme.current()['muted']}'>" + \
               "<br>".join(f"– {r}" for r in src.reasons[:4]) + "</span>"
        if res is not None:
            ok = "✓ embroidery-friendly" if res.friendly else "can be simpler"
            text += f"<br>Result now: {pill(res)} – {res.colors} colours, {res.shapes} shapes · {ok}"
            if not res.friendly and src.level in ("Complex", "Very complex", "Photo"):
                text += "<br><b>Tip:</b> click <i>Make it embroidery-friendly</i>."
        self._set_cx_text(text)

    def _set_cx_text(self, html: str):
        self.cx_lbl.setHtml(html)
        self._fit_cx()

    def _fit_cx(self):
        doc = self.cx_lbl.document()
        doc.setTextWidth(max(150, self.cx_lbl.viewport().width()))
        self.cx_lbl.setFixedHeight(int(doc.size().height()) + 4)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if hasattr(self, "cx_lbl"):
            QTimer.singleShot(0, self._fit_cx)

    def make_friendly(self):
        if not self.can_convert() or self.edits_fixed:
            return
        s, rgba = self.settings(), self.rgba
        self.gen += 1
        self.busy = True
        self.progress.setRange(0, 100)
        self.progress.show()
        self.pool.start(Job(self.gen, lambda p: ("friendly", find_friendly(rgba, s, p)), self.sig))

    def _apply_friendly(self, res):
        m, colors, cx = res
        self._quiet = True
        if colors and not (isinstance(self.colors.currentData(), int) and 0 < self.colors.currentData() <= colors):
            self.colors.setCurrentIndex(self.colors.findData(colors))
        self.simp.setValue(int(round(m * 100)))
        self._quiet = False
        msg = (f"Simplicity set to {int(m * 100)}%" + (f", {colors} colours max" if colors else "") +
               (" – embroidery-friendly." if cx.friendly else " – that's as simple as it gets without losing the picture."))
        self.statusBar().showMessage(msg, 10000)
        self.convert()

    def _bg_changed(self):
        self.keep_inside.setEnabled(self.bg.currentData() == "auto")
        self.changed()

    def _border_changed(self):
        b = self.border.currentData()
        self.border_col.setEnabled(b != "none")
        self.border_w.setEnabled(b == "satin")
        self.border_off.setEnabled(b != "none")
        self.changed()

    def _pick_border(self):
        c = QColorDialog.getColor(QColor(*self.border_rgb), self, "Border thread colour")
        if c.isValid():
            self.border_rgb = (c.red(), c.green(), c.blue())
            self.border_col.setStyleSheet(swatch_style(self.border_rgb, False))
            self.changed()

    def _pick_fabric(self):
        c = QColorDialog.getColor(QColor(*self.fabric_rgb), self, "Fabric colour (preview only)")
        if c.isValid():
            self.fabric_rgb = (c.red(), c.green(), c.blue())
            self.fabric_btn.setStyleSheet(swatch_style(self.fabric_rgb, False))
            self.render_stitches()
            self._mark_dirty()

    def _rotate(self):
        before = (self.rotate, self.mirror)
        self.rotate = (self.rotate + 90) % 360
        self._reorient_edits(before)
        self._orientation_changed()

    def _mirror(self, v: bool):
        before = (self.rotate, self.mirror)
        self.mirror = v
        self._reorient_edits(before)
        self._orientation_changed()

    def _reorient_edits(self, before):
        after = (self.rotate, self.mirror)
        if after != before and not self.edits_fixed:
            self.edit_log = editing.reorient(self.edit_log, before, after)
            self.redo_log = editing.reorient(self.redo_log, before, after)

    def _orientation_changed(self):
        if self.rgba is not None:
            img = np.rot90(self.rgba, -self.rotate // 90)
            self.v_orig.set_image(to_qimage(img[:, ::-1] if self.mirror else img))
        self.changed()

    def can_convert(self) -> bool:
        return not self.file_mode and (self.rgba is not None or bool(self.texts))

    def changed(self, *_):
        if getattr(self, "_quiet", False) or not self.can_convert():
            return
        self._mark_dirty()
        if self.edits_fixed:
            self.statusBar().showMessage("Settings are paused: this project's stitch edits were saved by an older "
                                         "StitchPilot and can't be re-applied. Click 'Discard edits' to re-convert.",
                                         8000)
            return
        self.debounce.start()

    def _update_enabled(self):
        self.ai_btn.setEnabled(self.rgba is not None and not self.file_mode)
        for w in (self.rot_btn, self.mir_btn):
            w.setEnabled(self.rgba is not None and not self.file_mode)
        self.add_text_btn.setEnabled(not self.file_mode)
        self.add_mono_btn.setEnabled(not self.file_mode)
        self.fix_text_btn.setEnabled(self.rgba is not None and not self.file_mode and not self.ai_running)
        self.friendly_btn.setEnabled(self.can_convert() and not self.edits_fixed)
        self.shape_btn.setEnabled(self.design is not None and bool(self.design.shapes) and not self.edits_fixed
                                  and not self.file_mode)
        for w in (self.simp, self.show_problems, self.compare, self.simp_more):
            w.setEnabled(not self.file_mode)
        ok = self.design is not None and self.design.stitch_count > 0
        self.save_btn.setEnabled(ok)
        self.tmpl_btn.setEnabled(ok)
        self.cut_act.setEnabled(ok and any(b.fabric for b in self.design.active))
        self.proj_btn.setEnabled(self._has_work())

    # ------------------------------------------------------------------ lettering
    def _refresh_texts(self):
        while self.text_box.count():
            w = self.text_box.takeAt(0).widget()
            if w:
                w.setParent(None)
                w.deleteLater()
        for i, t in enumerate(self.texts):
            self.text_box.addWidget(TextRow(i, t, self))
        for i, f in enumerate(self.text_fixes):
            self.text_box.addWidget(FixRow(i, f, self))

    def clean_text(self):
        """AI text clean-up: find the picture's text, match fonts, then let the user pick what to redraw."""
        if self.rgba is None or self.ai_running or self.busy:
            if self.busy:
                self.statusBar().showMessage("Still stitching… try again in a moment.", 4000)
            return
        client = self._claude()
        if client is None:
            return
        self.busy = self.ai_running = self.text_ai = True
        self.gen += 1
        rgba = self.rgba
        self.fix_text_btn.setText("✨ Reading the text…")
        self.fix_text_btn.setEnabled(False)
        self.progress.setRange(0, 0)
        self.progress.show()
        self.statusBar().showMessage("AI is reading the text in your picture…")

        def run(p):
            from . import textfix

            flat = textfix.flat_rgb(rgba)
            rows = []
            for t, box in ai.find_text(rgba, client=client):
                if t.curved:
                    rows.append((t.text, None, 0.0, "curved text can't be redrawn yet; it stays as it is"))
                    continue
                m = textfix.measure(flat, box)
                if m is None:
                    rows.append((t.text, None, 0.0, "couldn't find these letters clearly enough"))
                    continue
                font, bold, score = textfix.match_font(t.text, m, t.style, t.font_guess, t.italic)
                rows.append((t.text, TextFix(t.text, box, font, bold, t.italic), score, ""))
            return "text_ai", rows

        self.pool.start(Job(self.gen, run, self.sig))

    def _show_text_fixes(self, rows):
        self.ai_running = self.text_ai = False
        self.fix_text_btn.setText("✨ Clean up text in the picture")
        if not rows:
            QMessageBox.information(self, APP, "No text found in this picture.")
            return
        dlg = TextFixDialog(self, rows)
        if dlg.exec():
            picked = dlg.chosen()
            if picked:
                old = {f.box for f in picked}
                self.text_fixes = [f for f in self.text_fixes if f.box not in old] + picked
                self._refresh_texts()
                self.changed()

    def remove_fix(self, i: int):
        del self.text_fixes[i]
        self._refresh_texts()
        self.changed()

    def add_text(self, kind: str = "text"):
        if kind == "monogram":  # centred on the picture, or on its own
            default = TextItem("", kind="monogram", frame="circle", height_mm=30.0,
                               position="over" if self.rgba is not None else "below")
        else:
            default = TextItem("")
        dlg = TextDialog(self, default)
        if dlg.exec() and dlg.item().text:
            self.texts.append(dlg.item())
            self._refresh_texts()
            self.changed()

    def edit_text(self, i: int):
        dlg = TextDialog(self, self.texts[i])
        if dlg.exec():
            if dlg.item().text:
                self.texts[i] = dlg.item()
            else:
                del self.texts[i]
            self._refresh_texts()
            self.changed()

    def remove_text(self, i: int):
        del self.texts[i]
        self._refresh_texts()
        if self.can_convert():
            self.changed()
        else:
            self._clear_design()

    def new_text_design(self, kind: str = "text"):
        if not self.maybe_save():
            return
        self._reset(None)
        self.path = self.project_path = None
        self.source_name = self.source_bytes = self.source_file = None
        self.file_lbl.setText("<b>Lettering design</b> (no picture)")
        self._set_dirty(False)
        self.add_text(kind)

    # ------------------------------------------------------------------ open
    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        urls = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if urls:
            self.open_path(urls[0])

    def open_dialog(self):
        start = self.qs.value("last_dir", str(Path.home() / "Pictures"))
        fn, _ = QFileDialog.getOpenFileName(self, "Open a picture or embroidery file", start, open_filter())
        if fn:
            self.open_path(fn)

    def _clear_design(self):
        self.base = self.design = None
        self.v_st.set_image(None)
        self.v_den.set_image(None)
        self.v_cols.set_image(None)
        self.stats.setText("—")
        self.notes.hide()
        self._fill_threads([])
        self._update_enabled()

    def _reset(self, rgba):
        self.rgba = rgba
        self.gen += 1  # drop results of any job still running for the previous picture
        self.busy = self.pending = self.ai_running = self.shape_ai = False
        self.file_mode = False
        self._restore_blocks = self._restore_log = None
        self.shape_ai_btn.setText("✨ Suggest with AI")
        self.texts.clear()  # lettering belongs to the previous design
        self.text_fixes.clear()
        self.text_ai = False
        self.fix_text_btn.setText("✨ Clean up text in the picture")
        self._refresh_texts()
        self.ai_btn.setText("✨  Let AI choose settings")
        self.overrides.clear()
        self.hidden.clear()
        self.order.clear()
        self.applique.clear()
        self.shape_edits.clear()  # shapes belong to the previous picture
        self.shape_undo.clear()
        self.shape_redo.clear()
        self.sel_shape = None
        self.rotate, self.mirror = 0, False
        self.mir_btn.blockSignals(True)
        self.mir_btn.setChecked(False)
        self.mir_btn.blockSignals(False)
        self.ai_palette, self.ai_settings = None, None
        self._set_ai_colors_item(None)
        self.ai_lbl.hide()
        self.file_info.hide()
        self._forget_edits()
        self.source_cx = self.result_cx = self.source_key = self._prep = None
        self.show_complexity()
        self._clear_design()
        self.v_orig.set_image(to_qimage(rgba) if rgba is not None else None)

    def open_path(self, fn: str):
        path = Path(fn)
        if path.suffix.lower() == project.EXT:
            self.open_project(path)
            return
        if not self.maybe_save():
            return
        try:
            data = path.read_bytes()
            d, rgba = self._load_source(path)
        except Exception as e:
            QMessageBox.warning(self, APP, f"Couldn't open that file:\n{e}")
            return
        self.path = path
        self.qs.setValue("last_dir", str(path.parent))
        self._reset(rgba)
        self.project_path = None
        self.source_name, self.source_bytes, self.source_file = path.name, data, path
        self._add_recent(path)
        self._show_source(d)
        self._set_dirty(False)
        if rgba is not None:
            # automatically only on Plus: free tries are for trying AI on purpose, not used up by opening files
            if self.auto_ai.isChecked() and self._token() and self.qs.value("ai_plan") == "plus":
                self.run_ai()
            else:
                self.convert()

    def _load_source(self, path: Path) -> tuple[Design | None, np.ndarray | None]:
        """(design, None) for an embroidery file, (None, picture) for a picture."""
        if is_embroidery(path):
            return read_design(path), None
        return None, load_image(path)

    def _show_source(self, d: Design | None):
        """Show what was opened: an embroidery file goes straight to the preview, a picture waits to convert."""
        name = self.source_name or "Lettering design"
        if d is not None:  # embroidery file: view, recolour, reorder, convert
            self.file_mode = True
            self.texts.clear()
            self._refresh_texts()
            self.file_lbl.setText(f"<b>{name}</b> · embroidery file")
            self.file_info.setText("Opened an existing embroidery file. You can recolour, reorder or skip "
                                   "colours, check the density and save it in another machine format. "
                                   "Picture settings don't apply.")
            self.file_info.show()
            self.base = d
            self.refresh_colors()
        elif self.rgba is not None:
            h, w = self.rgba.shape[:2]
            self.file_lbl.setText(f"<b>{name}</b> · {w}×{h}px")
        else:
            self.file_lbl.setText("<b>Lettering design</b> (no picture)")
        self._update_enabled()

    # ------------------------------------------------------------------ projects
    def _title_name(self) -> str | None:
        if self.project_path:
            return self.project_path.stem
        if self.source_name:
            return self.source_name
        return "Lettering design" if self.texts else None

    def _update_title(self):
        name = self._title_name()
        self.setWindowTitle(f"{name}{' •' if self.dirty else ''} – {APP}" if name else APP)
        self.setWindowModified(self.dirty)

    def _has_work(self) -> bool:
        return self.rgba is not None or self.file_mode or bool(self.texts)

    def _set_dirty(self, dirty: bool):
        self.dirty = dirty and self._has_work()
        self._update_title()

    def _mark_dirty(self):
        if not getattr(self, "_quiet", False):
            self._set_dirty(True)

    def maybe_save(self) -> bool:
        """Ask before throwing away unsaved work. Returns False if the user cancels."""
        if not self.dirty or not self._has_work():
            return True
        box = QMessageBox(self)
        box.setWindowTitle(APP)
        box.setIcon(QMessageBox.Icon.Question)
        box.setText(f"Save your changes to “{self._title_name()}” as a project?")
        box.setInformativeText("A project keeps the picture, every setting and your stitch edits, "
                               "so you can pick up where you left off.")
        save = box.addButton("Save project…", QMessageBox.ButtonRole.AcceptRole)
        discard = box.addButton("Don't save", QMessageBox.ButtonRole.DestructiveRole)
        box.addButton(QMessageBox.StandardButton.Cancel)
        box.setDefaultButton(save)
        box.exec()
        if box.clickedButton() is save:
            return self.save_project()
        return box.clickedButton() is discard

    def get_state(self) -> dict:
        """Everything needed to rebuild this design exactly (saved in projects and autosaves)."""
        return {
            "machine": self.machine.currentText(), "hoop": self.hoop.currentText(),
            "max_w": self.w_spin.value(), "max_h": self.h_spin.value(), "fabric": self.fabric.currentText(),
            "colors": self.colors.currentData(), "ai_palette": [list(c) for c in (self.ai_palette or [])],
            "ai_bg": list(self.ai_settings.background_rgb) if self.ai_settings and self.ai_settings.background_rgb
            else None,
            "background": self.bg.currentData(), "keep_inside": self.keep_inside.isChecked(),
            "simplicity": self.simp.value(), "merge": self.sp_merge.value(), "min_shape": self.sp_min.value(),
            "thin": self.thin.currentData(), "smooth": self.sp_smooth.value(),
            "fill_spacing": self.sp_fill.value(), "stitch_len": self.sp_len.value(),
            "satin_spacing": self.sp_satin.value(), "satin_max": self.sp_satin_max.value(),
            "pull": self.sp_pull.value(), "angle": self.angle.currentData(), "underlay": self.underlay.isChecked(),
            "texts": [asdict(t) for t in self.texts], "border": self.border.currentData(),
            "text_fixes": [asdict(f) for f in self.text_fixes],
            "border_rgb": list(self.border_rgb), "border_w": self.border_w.value(),
            "border_off": self.border_off.value(), "trims": self.trims.isChecked(),
            "rotate": self.rotate, "mirror": self.mirror,
            "applique": [list(c) for c in self.applique],
            "overrides": [[list(k), list(v)] for k, v in self.overrides.items()],
            "hidden": [list(c) for c in self.hidden], "order": [list(c) for c in self.order],
            "chart": self.chart.currentData(), "snap": self.snap.isChecked(), "format": self.fmt.currentText(),
            "fabric_rgb": list(self.fabric_rgb),
            "shape_edits": [asdict(e) for e in self.shape_edits],
        }

    def set_state(self, s: dict):
        """Put saved settings back into the window without triggering conversions."""
        tup = lambda v: tuple(int(x) for x in v)  # noqa: E731
        self._quiet = True
        try:
            if s.get("machine") in MACHINES:
                self.machine.setCurrentText(s["machine"])
                self._machine_changed(s["machine"])
            hoop = s.get("hoop")
            if hoop and self.hoop.findText(hoop) < 0:
                hoop = "Custom"
            if hoop:
                self.hoop.setCurrentText(hoop)
            for sp, k in ((self.w_spin, "max_w"), (self.h_spin, "max_h")):
                if k in s:
                    sp.setValue(float(s[k]))
            if s.get("fabric") in FABRICS:
                self.fabric.setCurrentText(s["fabric"])
            self.ai_palette = [tup(c) for c in s.get("ai_palette") or []] or None
            self.ai_settings = Settings(background_rgb=tup(s["ai_bg"])) if s.get("ai_bg") else None
            if s.get("colors") == "ai" and self.ai_palette:
                self._set_ai_colors_item(len(self.ai_palette))
            else:
                self._set_ai_colors_item(None)
                self.colors.setCurrentIndex(max(0, self.colors.findData(s.get("colors", 0))))
            self.bg.setCurrentIndex(max(0, self.bg.findData(s.get("background", "auto"))))
            self.keep_inside.setChecked(bool(s.get("keep_inside", False)))
            self.simp.setValue(int(s.get("simplicity", DEFAULT_SIMPLICITY)))
            self.sp_merge.setValue(int(s.get("merge", self.sp_merge.value())))
            self.sp_min.setValue(float(s.get("min_shape", self.sp_min.value())))
            self.thin.setCurrentIndex(max(0, self.thin.findData(s.get("thin", "keep"))))
            self.sp_smooth.setValue(int(s.get("smooth", self.sp_smooth.value())))
            for sp, k in ((self.sp_fill, "fill_spacing"), (self.sp_len, "stitch_len"),
                          (self.sp_satin, "satin_spacing"), (self.sp_satin_max, "satin_max"),
                          (self.sp_pull, "pull")):
                if k in s:
                    sp.setValue(float(s[k]))
            self.angle.setCurrentIndex(max(0, self.angle.findData(s.get("angle"))))
            self.underlay.setChecked(bool(s.get("underlay", True)))
            self.texts = [TextItem(**{**t, "rgb": tup(t["rgb"])}) for t in s.get("texts", [])]
            self.text_fixes = [TextFix(**{**f, "box": tuple(f["box"])}) for f in s.get("text_fixes", [])]
            self._refresh_texts()
            self.border.setCurrentIndex(max(0, self.border.findData(s.get("border", "none"))))
            self.border_rgb = tup(s.get("border_rgb", self.border_rgb))
            self.border_w.setValue(float(s.get("border_w", 3.0)))
            self.border_off.setValue(float(s.get("border_off", 0.0)))
            self.trims.setChecked(bool(s.get("trims", True)))
            self.rotate, self.mirror = int(s.get("rotate", 0)), bool(s.get("mirror", False))
            self.mir_btn.setChecked(self.mirror)
            self.applique = [tup(c) for c in s.get("applique", [])]
            self.overrides = {tup(k): tup(v) for k, v in s.get("overrides", [])}
            self.hidden = {tup(c) for c in s.get("hidden", [])}
            self.order = [tup(c) for c in s.get("order", [])]
            self.shape_edits = [ShapeEdit(**{**e, "rgb": tup(e["rgb"])}) for e in s.get("shape_edits", [])]
            self.shape_undo.clear()
            self.shape_redo.clear()
            self.chart.setCurrentIndex(max(0, self.chart.findData(s.get("chart"))))
            self.snap.setChecked(bool(s.get("snap", False)))
            if s.get("format") in FORMATS:
                self.fmt.setCurrentText(s["format"])
            if s.get("fabric_rgb"):
                self.fabric_rgb = tup(s["fabric_rgb"])
            self._border_changed()  # enables the border controls (no conversion while quiet)
        finally:
            self._quiet = False
        self.fabric_btn.setStyleSheet(swatch_style(self.fabric_rgb, False))
        self.border_col.setStyleSheet(swatch_style(self.border_rgb, False))

    def save_project(self, as_new: bool = False, path: Path | None = None) -> bool:
        if not self._has_work():
            return False
        if path is None:
            path = self.project_path
            if path is None or as_new:
                start = self.qs.value("project_dir", self.qs.value("save_dir", str(Path.home())))
                fn, _ = QFileDialog.getSaveFileName(self, "Save project",
                                                    str(Path(start) / f"{Path(self._stem()).stem}{project.EXT}"),
                                                    f"StitchPilot project (*{project.EXT})")
                if not fn:
                    return False
                path = Path(fn if fn.lower().endswith(project.EXT) else fn + project.EXT)
        try:
            self._write_project(path)
        except Exception as e:
            QMessageBox.warning(self, APP, f"Couldn't save the project:\n{e}")
            return False
        self.project_path = path
        self.qs.setValue("project_dir", str(path.parent))
        self._add_recent(path)
        self._set_dirty(False)
        self._remove_autosave()
        self.statusBar().showMessage(f"Project saved: {path.name} 💾", 6000)
        return True

    def _write_project(self, path: Path, extra: dict | None = None):
        d = self.design
        blocks = editing.copy_blocks(d.blocks) if (self.edited and d is not None) else None
        if blocks is None and self.file_mode and self.base is not None and self.source_bytes is None:
            blocks = editing.copy_blocks(self.base.blocks)
        state = {**self.get_state(), **(extra or {})}
        if blocks is not None and self.edited and not self.edits_fixed and not self.file_mode:
            state["edit_log"] = [[op.to_dict() for op in step] for step in self.edit_log]
        project.save(path, state, self.source_name, self.source_bytes, blocks,
                     (d.width_mm, d.height_mm) if d is not None else None)

    def open_project(self, path: Path, recovered_from: Path | None = None) -> bool:
        """Open a .stitchpilot file. `recovered_from` is set when restoring an autosave."""
        if recovered_from is None and not self.maybe_save():
            return False
        try:
            proj = project.load(path)
            src_file = None
            if proj.source_bytes is not None and proj.source_name:
                digest = hashlib.sha1(proj.source_bytes).hexdigest()[:12]
                cache = Path(tempfile.gettempdir()) / "StitchPilot" / digest
                cache.mkdir(parents=True, exist_ok=True)
                src_file = cache / proj.source_name
                if not src_file.exists():
                    src_file.write_bytes(proj.source_bytes)
            d, rgba = self._load_source(src_file) if src_file else (None, None)
        except Exception as e:
            QMessageBox.warning(self, APP, f"Couldn't open that project:\n{e}")
            return False
        self._reset(rgba)
        self.path = src_file
        self.source_name, self.source_bytes, self.source_file = proj.source_name, proj.source_bytes, src_file
        orig = proj.state.get("_project_path")
        self.project_path = (Path(orig) if orig else None) if recovered_from else path
        self.set_state(proj.state)
        if d is None and proj.blocks is not None and rgba is None and not proj.state.get("texts"):
            d = Design(proj.blocks, *(proj.size_mm or (100, 100)), None)  # embroidery file without its source
        self._show_source(d)
        if rgba is not None:  # show the picture with its saved rotation / mirroring
            self._quiet = True
            self._orientation_changed()
            self._quiet = False
        if proj.blocks is not None:
            if self.file_mode:
                self._set_blocks_loaded(proj.blocks)
            else:
                self._restore_blocks = proj.blocks  # put back after the picture re-converts
                try:
                    self._restore_log = [[editing.Op.from_dict(o) for o in step]
                                         for step in proj.state["edit_log"]] if "edit_log" in proj.state else None
                except (KeyError, TypeError, ValueError):
                    self._restore_log = None
        if not self.file_mode and self.can_convert():
            self.convert()
        if recovered_from is None:
            self.qs.setValue("project_dir", str(path.parent))
            self._add_recent(path)
        self._set_dirty(recovered_from is not None)
        return True

    def _set_blocks_loaded(self, blocks):
        """Hand-edited stitches from a project become the design (no undo history before this point)."""
        d = self.base
        w, h = (d.width_mm, d.height_mm) if d is not None else (100.0, 100.0)
        self.base = Design(blocks, w, h, d.prepared if d is not None else None, list(d.notes) if d else [])
        self.order = [b.source_rgb for b in blocks]
        self.edited = True
        self._show_edit_banner()
        self.refresh_colors()

    # ------------------------------------------------------------------ autosave & recovery
    def _autosave(self):
        if not (self.dirty and self._has_work()):
            return
        try:
            extra = {"_project_path": str(self.project_path) if self.project_path else None,
                     "_saved_at": QDateTime.currentDateTime().toString(Qt.DateFormat.ISODate)}
            self._write_project(autosave_path(), extra)
        except Exception:  # autosave must never interrupt work
            traceback.print_exc()

    def _remove_autosave(self):
        try:
            autosave_path().unlink(missing_ok=True)
        except OSError:
            pass

    def check_recovery(self):
        """After a crash or power cut, offer the work the autosave kept."""
        path = autosave_path()
        if not path.exists():
            return
        try:
            state = project.load(path).state
        except Exception:
            self._remove_autosave()
            return
        when = QDateTime.fromString(state.get("_saved_at", ""), Qt.DateFormat.ISODate)
        what = Path(state["_project_path"]).stem if state.get("_project_path") else "your design"
        ans = QMessageBox.question(
            self, APP, f"StitchPilot didn't close properly last time.\n\nRestore the unsaved work on {what}"
            + (f" from {when.toString('d MMM, h:mm ap')}" if when.isValid() else "") + "?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No, QMessageBox.StandardButton.Yes)
        if ans == QMessageBox.StandardButton.Yes:
            self.open_project(path, recovered_from=path)
            self.statusBar().showMessage("Restored your unsaved work 🌸 Save it as a project to keep it.", 10000)
        else:
            self._remove_autosave()

    # ------------------------------------------------------------------ recent files
    def _add_recent(self, path: Path):
        items = [str(path)] + [p for p in self._recent() if Path(p) != Path(path)]
        self.qs.setValue("recent", items[:10])

    def _recent(self) -> list[str]:
        v = self.qs.value("recent", [])
        return [v] if isinstance(v, str) else list(v or [])

    def _fill_recent(self):
        self.recent_menu.clear()
        items = [p for p in self._recent() if Path(p).exists()]
        if not items:
            a = self.recent_menu.addAction("Nothing yet")
            a.setEnabled(False)
            return
        for p in items:
            icon = "💾 " if p.lower().endswith(project.EXT) else ""
            self.recent_menu.addAction(f"{icon}{Path(p).name}", lambda p=p: self.open_path(p))
        self.recent_menu.addSeparator()
        self.recent_menu.addAction("Clear list", lambda: self.qs.setValue("recent", []))

    # ------------------------------------------------------------------ convert
    def convert(self):
        if not self.can_convert():
            return
        if self.busy:
            self.pending = True
            return
        self.busy, self.pending = True, False
        self.gen += 1
        s, rgba = self.settings(), self.rgba
        self._baking = {id(op) for op in self._erase_ops()}
        key = (id(rgba), s.background, s.background_rgb, s.remove_enclosed_bg, s.max_width_mm, s.max_height_mm,
               s.rotate, s.mirror, tuple(astuple(t) for t in s.texts), tuple(astuple(f) for f in s.text_fixes))
        need_source = key != self.source_key
        pkey = (rgba, prep_inputs(s))
        reuse = self._prep if self._prep is not None and self._prep[0] is rgba and self._prep[1] == pkey[1] else None

        def run(p):
            d = digitize(rgba, s, p, reuse[2] if reuse else None)
            p(0.99, "Checking how complex it is…")
            src = analyse_source(rgba, s) if need_source else None
            return "design", (d, src, reuse[3] if reuse else analyse_cx(d.prepared), key, pkey)

        self.progress.setRange(0, 100)
        self.progress.show()
        self.pool.start(Job(self.gen, run, self.sig))

    def _on_progress(self, gen: int, f: float, msg: str):
        if gen == self.gen:
            self.progress.setValue(int(f * 100))
            self.statusBar().showMessage(msg)

    def _on_done(self, gen: int, res):
        if gen != self.gen:  # result for a picture/settings that has since changed
            return
        kind, val = res
        self.busy = False
        self.progress.setRange(0, 100)
        self.progress.hide()
        if kind == "ai":
            self._apply_ai(val)  # starts a conversion with the new settings
        elif kind == "shape_ai":
            self._show_suggestions(*val)
        elif kind == "text_ai":
            self._show_text_fixes(val)
        elif kind == "friendly":
            self._apply_friendly(val)
        elif kind == "batch":
            ok, failed, folder = val
            msg = f"Converted {ok} picture(s) into {folder}."
            if failed:
                msg += "\n\nCouldn't convert:\n" + "\n".join(failed[:12])
            QMessageBox.information(self, APP, msg)
            QDesktopServices.openUrl(QUrl.fromLocalFile(folder))
        else:
            d, src, res, key, pkey = val
            if src is not None:
                self.source_cx, self.source_key = src, key
            self.result_cx = res
            self._prep = (*pkey, d.prepared, res)
            self.base = d
            self._baked = self._baking
            if self._restore_blocks is not None:  # a project with hand edits: put them back
                blocks, self._restore_blocks = self._restore_blocks, None
                log, self._restore_log = self._restore_log, None
                self.edit_log, self.redo_log, self.edits_fixed = log or [], [], log is None
                self._set_blocks_loaded(blocks)
            elif self.edited and not self.edits_fixed:  # settings changed: put the hand edits back on top
                self._reapply_edits()
            else:
                self.refresh_colors()
            self.update_colors_view()
            self.show_complexity()
            self.statusBar().showMessage("All stitched up! Check the preview, then save 💾", 8000)
            if self.pending:
                self.convert()
        self._update_enabled()

    def _on_account_error(self, gen: int, err: account.AccountError):
        """An AI request was refused for the account's sake: tidy up like any failure, then offer the fix."""
        if gen != self.gen:
            return
        self._quiet_failure = True
        try:
            self._on_failed(gen, str(err))
        finally:
            self._quiet_failure = False
        self.account_problem(err)

    def _warn(self, text: str):
        if not getattr(self, "_quiet_failure", False):
            QMessageBox.warning(self, APP, text)

    def _on_failed(self, gen: int, msg: str):
        if gen != self.gen:
            return
        self.busy = False
        self.progress.setRange(0, 100)
        self.progress.hide()
        self.statusBar().showMessage(f"Problem: {msg}", 15000)
        if self.ai_running and self.text_ai:
            self.ai_running = self.text_ai = False
            self.fix_text_btn.setText("✨ Clean up text in the picture")
            self._warn(f"Text clean-up didn't work:\n{msg}")
            if self.pending:
                self.convert()
        elif self.ai_running and self.shape_ai:
            self.ai_running = self.shape_ai = False
            self.shape_ai_btn.setText("✨ Suggest with AI")
            self._fill_shape_panel()
            self._warn(f"AI suggestions didn't work:\n{msg}")
            if self.pending:
                self.convert()
        elif self.ai_running:
            self.ai_running = False
            self.ai_btn.setText("✨  Let AI choose settings")
            self._warn(f"AI assist didn't work:\n{msg}\n\nUsing automatic settings instead.")
            self.convert()
        else:
            QMessageBox.warning(self, APP, f"Couldn't convert this picture:\n{msg}")
            if self.pending:
                self.convert()
        self._update_enabled()

    # ------------------------------------------------------------------ colours / threads
    def refresh_colors(self, *_):
        """Apply the user's colour edits, order and merges to the digitized design."""
        base = self.base
        if base is None:
            return
        blocks = []
        for b in base.blocks:
            nb = replace(b)
            k = nearest_key(b.source_rgb, self.overrides)
            nb.rgb = self.overrides[k] if k else b.source_rgb
            nb.visible = nearest_key(b.source_rgb, self.hidden) is None
            blocks.append(nb)

        def rank(b):
            k = nearest_key(b.source_rgb, self.order)
            return self.order.index(k) if k else len(self.order) + base.blocks.index(
                next(x for x in base.blocks if x.source_rgb == b.source_rgb))

        if self.order:
            blocks.sort(key=rank)
        d = Design(blocks, base.width_mm, base.height_mm, base.prepared, list(base.notes), base.shapes)
        match_threads(d, self.chart.currentData(), self.snap.isChecked())
        merged: list[ColorBlock] = []  # neighbours with the same colour become one thread
        for b in d.blocks:
            if merged and b.visible and merged[-1].visible and merged[-1].rgb == b.rgb:
                m = merged[-1]
                merged[-1] = replace(m, cmds=np.vstack([m.cmds, b.cmds]), area_mm2=m.area_mm2 + b.area_mm2,
                                     kinds={k: m.kinds.get(k, 0) + b.kinds.get(k, 0) for k in {*m.kinds, *b.kinds}},
                                     fabric=(m.fabric or []) + (b.fabric or []) or None,
                                     under=None if m.under is None or b.under is None
                                     else np.concatenate([m.under, b.under]))
            else:
                merged.append(b)
        d.blocks = merged
        self.design = d
        self._fill_threads(d.blocks)
        self.update_stats()
        self.render_stitches()
        self.render_density()
        self.v_st.sel = {r for r in self.v_st.sel if r[0] < len(d.blocks) and r[1] < len(d.blocks[r[0]].cmds)}
        self.v_st.refresh_points()
        keep = self.add_color.currentIndex()
        self.add_color.clear()
        for i, b in enumerate(d.blocks):
            self.add_color.addItem(f"{i + 1}. {b.thread or '#%02X%02X%02X' % b.rgb}")
        self.add_color.setCurrentIndex(min(max(0, keep), len(d.blocks) - 1))
        self.selection_changed()
        self._reselect_shape()

    def _fill_threads(self, blocks):
        while self.thread_box.count():
            w = self.thread_box.takeAt(0).widget()
            if w:
                w.setParent(None)
                w.deleteLater()
        for i, b in enumerate(blocks):
            self.thread_box.addWidget(ThreadRow(i, len(blocks), b, self))
        self.thread_box.addStretch(1)

    def _current_order(self) -> list[tuple]:
        return [b.source_rgb for b in self.design.blocks]

    def pick_color(self, i: int):
        b = self.design.blocks[i]
        c = QColorDialog.getColor(QColor(*b.rgb), self, "Thread colour")
        if c.isValid():
            self.overrides[b.source_rgb] = (c.red(), c.green(), c.blue())
            self.snap.setChecked(False)
            self.refresh_colors()
            self._mark_dirty()

    def move_block(self, i: int, step: int):
        order = self._current_order()
        j = i + step
        if 0 <= j < len(order):
            order[i], order[j] = order[j], order[i]
            self.order = order
            self.refresh_colors()
            self._mark_dirty()
            self.changed()  # travel hidden under later colours must be re-planned for the new order

    def merge_up(self, i: int):
        above = self.design.blocks[i - 1]
        self.overrides[self.design.blocks[i].source_rgb] = above.rgb
        self.snap.setChecked(False)
        self.refresh_colors()
        self._mark_dirty()

    def set_applique(self, i: int, on: bool):
        src = self.design.blocks[i].source_rgb
        k = nearest_key(src, self.applique)
        if on and k is None:
            self.applique.append(src)
        elif not on and k is not None:
            self.applique.remove(k)
        self.changed()

    def toggle_block(self, i: int, visible: bool):
        src = self.design.blocks[i].source_rgb
        if visible:
            self.hidden.discard(nearest_key(src, self.hidden))
        else:
            self.hidden.add(src)
        self.design.blocks[i].visible = visible
        self._mark_dirty()
        self.update_stats()
        self.render_stitches()
        self.render_density()
        self._update_enabled()
        self.changed()  # nothing may be hidden under a colour that won't be sewn

    def update_stats(self):
        d = self.design
        x0, y0, x1, y1 = d.bounds()
        hw, hh = self.hoop_mm()
        fits = (x1 - x0) <= hw - 2 and (y1 - y0) <= hh - 2
        warn = "" if fits else f" <span style='color:{theme.current()['warn']}'>⚠ larger than the hoop</span>"
        stops = f"<tr><td>Stops</td><td><b>{d.stop_count}</b> (appliqué)</td></tr>" if d.stop_count else ""
        self.stats.setText(
            f"<table cellspacing=4>"
            f"<tr><td>Size</td><td><b>{x1 - x0:.0f} × {y1 - y0:.0f} mm</b> "
            f"({(x1 - x0) / 25.4:.2f} × {(y1 - y0) / 25.4:.2f} in){warn}</td></tr>"
            f"<tr><td>Stitches</td><td><b>{d.stitch_count:,}</b></td></tr>"
            f"<tr><td>Colours</td><td><b>{len(d.active)}</b> ({max(0, len(d.active) - 1)} changes)</td></tr>"
            f"<tr><td>Trims</td><td><b>{d.trim_count}</b></td></tr>{stops}"
            f"<tr><td>Sew time</td><td><b>≈ {d.est_minutes():.0f} min</b></td></tr></table>")
        self.notes.setText("\n\n".join(d.notes))
        self.notes.setVisible(bool(d.notes))

    def _preview_ppm(self) -> float:
        d = self.design
        hw, hh = self.hoop_mm() if self.show_hoop.isChecked() else (0, 0)
        return min(12.0, 2000 / max(d.width_mm, d.height_mm, hw, hh, 1))

    def render_stitches(self, *_):
        d = self.design
        if d is None:
            self.v_st.set_image(None)
            return
        frac = self.sim.value() / 1000
        upto = None if frac >= 1 else int(d.stitch_count * frac)
        hoop = self.hoop_mm() if self.show_hoop.isChecked() else None
        ppm = self._preview_ppm()
        self.v_st.set_mapping(ppm, layout(d, ppm, 3.0, hoop)[2])
        self.v_st.set_image(to_qimage(render(d, ppm, self.fabric_rgb, show_jumps=self.jumps.isChecked(),
                                             upto=upto, hoop_mm=hoop)))

    # ------------------------------------------------------------------ stitch editing
    def toggle_edit(self, on: bool):
        if on and self.design is None:
            self.edit_btn.setChecked(False)
            return
        if on and self.shape_btn.isChecked():
            self.shape_btn.setChecked(False)
        if self.play_timer.isActive():
            self._toggle_play()
        self.sim.setValue(1000)
        self.tabs.setCurrentWidget(self.v_st)
        self.edit_bar.setVisible(on)
        self.v_st.set_edit(on)
        self.set_tool("select")
        self.selection_changed()

    def set_tool(self, tool: str):
        self.v_st.tool = tool
        self.t_select.setChecked(tool == "select")
        self.t_add.setChecked(tool == "add")

    def selection_changed(self):
        n = len(self.v_st.sel)
        self.del_btn.setEnabled(n > 0)
        self.undo_btn.setEnabled(bool(self.undo_stack))
        self.redo_btn.setEnabled(bool(self.redo_stack))
        if n == 1 and self.design is not None:
            bi, row = next(iter(self.v_st.sel))
            if bi < len(self.design.blocks):
                self.add_color.setCurrentIndex(bi)
        hint = ("Click needle points to select them · Shift adds · drag a box · double-click picks a whole run · "
                "middle/right-drag pans") if n == 0 else f"{n} point(s) selected · Del deletes · drag to move"
        if self.v_st.tool == "add":
            hint = "Click to sew from the selected point" if n == 1 else "Click to start a new run"
        self.sel_lbl.setText(hint)

    def _set_blocks(self, blocks):
        d = self.design
        self.base = Design(blocks, d.width_mm, d.height_mm, d.prepared, list(d.notes), d.shapes)
        self.order = [b.source_rgb for b in blocks]
        self.edited = True
        self._show_edit_banner()
        self.refresh_colors()
        self._mark_dirty()
        self._update_enabled()

    def _show_edit_banner(self):
        self.edit_msg.setText(
            "This project's hand edits were saved by an older StitchPilot: picture settings won't re-convert the "
            "design until you discard them." if self.edits_fixed else
            "Your hand edits are kept: when you change settings they're put back in the same places.")
        self.edit_banner.setVisible(self.edited and not self.file_mode)

    def _forget_edits(self):
        self.edited = self.edits_fixed = False
        self._baked, self._baking = set(), set()
        self.edit_log.clear()
        self.redo_log.clear()
        self.undo_stack.clear()
        self.redo_stack.clear()
        self.edit_banner.hide()
        self.v_st.sel = set()

    def _commit(self, blocks, ops: list, sel=None):
        self.undo_stack.append(editing.copy_blocks(self.design.blocks))
        del self.undo_stack[:-60]
        self.redo_stack.clear()
        self.edit_log.append(ops)
        self.redo_log.clear()
        self.v_st.sel = set(sel or ())
        self._set_blocks(blocks)

    def _erase_ops(self) -> list:
        """Erased areas to cut out when converting: done that way, the stitches around them are worked out
        properly (rows, underlay, travel) rather than having stitches picked out of them."""
        if not self.edited or self.edits_fixed:
            return []
        return [op for step in self.edit_log for op in step if op.kind == "erase"]

    def _reapply_edits(self):
        """The picture re-converted with new settings: redo the hand edits on the new stitches, and rebuild undo."""
        self.refresh_colors()  # the new stitches as the edits see them: colour edits, order and merges applied
        d = self.design
        states = editing.replay(d.blocks, self.edit_log + self.redo_log[::-1], d.width_mm, d.height_mm, self._baked)
        n = len(self.edit_log)
        self.undo_stack[:] = states[:n][-60:]
        self.redo_stack[:] = states[n + 1:][::-1]
        self.v_st.sel = set()
        self._set_blocks(states[n])

    def edit_delete(self):
        d = self.design
        if d is not None and self.v_st.sel:
            self._commit(editing.delete(d.blocks, self.v_st.sel),
                         editing.record_delete(d.blocks, self.v_st.sel, d.width_mm, d.height_mm))

    def edit_move(self, dx: float, dy: float):
        d = self.design
        sel = set(self.v_st.sel)
        self._commit(editing.move(d.blocks, sel, dx, dy),
                     editing.record_move(d.blocks, sel, dx, dy, d.width_mm, d.height_mm), sel)

    def edit_add(self, xy):
        d = self.design
        if d is None or not d.blocks:
            return
        sel = self.v_st.sel
        if len(sel) == 1:
            anchor = next(iter(sel))
            blocks, ref = editing.insert_after(d.blocks, anchor, xy)
            bi, row = anchor
            pts = blocks[bi].cmds[row:ref[1] + 1, :2]  # from the selected point to the new last one
        else:
            bi = max(0, self.add_color.currentIndex())
            blocks, ref = editing.new_run(d.blocks, bi, xy)
            pts = [xy]
        self._commit(blocks, editing.record_add(d.blocks[bi].source_rgb, pts, d.width_mm, d.height_mm), {ref})

    def undo(self):
        if self.v_st.shape_mode:
            return self._shape_history(self.shape_undo, self.shape_redo)
        if self.undo_stack and self.design is not None:
            self.redo_stack.append(editing.copy_blocks(self.design.blocks))
            step = self.edit_log.pop() if self.edit_log else []
            if step:
                self.redo_log.append(step)
            self.v_st.sel = set()
            self._set_blocks(self.undo_stack.pop())
            self._reconvert_for(step)

    def redo(self):
        if self.v_st.shape_mode:
            return self._shape_history(self.shape_redo, self.shape_undo)
        if self.redo_stack and self.design is not None:
            self.undo_stack.append(editing.copy_blocks(self.design.blocks))
            step = self.redo_log.pop() if self.redo_log else []
            if step:
                self.edit_log.append(step)
            self.v_st.sel = set()
            self._set_blocks(self.redo_stack.pop())
            self._reconvert_for(step)

    def _reconvert_for(self, step):
        # an erase that the conversion cut out (or one to cut out again) needs the picture converted again
        if self._baked and any(op.kind == "erase" for op in step):
            self.convert()

    def discard_edits(self):
        if not self.edited:
            return
        self._forget_edits()
        self._mark_dirty()
        if self.file_mode and self.source_file:
            self.base = read_design(self.source_file)
            self.order.clear()
            self.refresh_colors()
        else:
            self.order.clear()
            self.convert()

    # ------------------------------------------------------------------ shape editing
    def toggle_shapes(self, on: bool):
        why = None
        if on and (self.design is None or not self.design.shapes):
            why = "Open a picture first: shapes come from converting a picture."
        elif on and self.edited:
            why = ("This project's stitch edits were saved by an older StitchPilot and can't be re-applied after "
                   "shape changes. Discard them first.") if self.edits_fixed else None
        if why:
            self.shape_btn.setChecked(False)
            self.statusBar().showMessage(why, 8000)
            return
        if on and self.edit_btn.isChecked():
            self.edit_btn.setChecked(False)
        if self.play_timer.isActive():
            self._toggle_play()
        self.sim.setValue(1000)
        self.tabs.setCurrentWidget(self.v_st)
        self.shape_bar.setVisible(on)
        self.v_st.set_shape_mode(on)
        if not on:
            self.sel_shape = None
        self._fill_shape_panel()

    def select_shape(self, sh: Shape | None):
        self.sel_shape = (sh.rgb, sh.fx, sh.fy) if sh is not None else None
        self.v_st.shape = sh
        self._fill_shape_panel()
        self.v_st.viewport().update()

    def _reselect_shape(self):
        """After a re-conversion, find the selected shape again (it is a new object now)."""
        if not self.v_st.shape_mode:
            return
        d = self.design
        sh = d.find_shape(*self.sel_shape) if self.sel_shape and d is not None else None
        self.v_st.shape = sh
        self._fill_shape_panel()
        self.v_st.viewport().update()

    def _fill_shape_panel(self):
        sh = self.v_st.shape
        e = sh.edit if sh is not None else None
        for w in (self.shape_kind, self.shape_angle, self.shape_density, self.shape_order, self.shape_remove,
                  self.shape_reset):
            w.blockSignals(True)
            w.setEnabled(sh is not None)
        try:
            if sh is None:
                self.shape_kind.setItemText(0, "Auto")
                self.shape_kind.setCurrentIndex(0)
                self.shape_angle.setValue(-5)  # "Auto"
                self.shape_density.setValue(100)
                self.shape_order.setCurrentIndex(0)
                self.shape_remove.setChecked(False)
                n = len(self.design.shapes) if self.design is not None else 0
                self.shape_lbl.setText(f"Click a shape to change it · {n} shapes · Esc deselects · "
                                       "middle/right-drag pans")
                return
            removed = sh.kind == "skip"
            auto = KIND_NAMES.get(sh.auto_kind, sh.auto_kind)
            used = KIND_NAMES.get(sh.kind, sh.kind)
            if sh.kind == "fill" and sh.pattern != "plain":
                used = pat.PATTERNS.get(sh.pattern, used) + " fill"
            thread = next((b.thread for b in self.design.blocks if nearest_key(b.source_rgb, [sh.rgb])), "")
            self.shape_lbl.setText(
                f"<b>{used}</b> · {sh.area_mm2:.0f} mm² · about {sh.width_mm:.1f} mm wide"
                + (f" · thread {thread}" if thread else "")
                + (" · <i>changed by you</i>" if e is not None else "")
                + (" · drag the arrow to turn the fill" if sh.kind == "fill" and sh.angle is not None else ""))
            self.shape_kind.setItemText(0, f"Auto ({auto})")
            kind = e.kind if e is not None and e.kind not in ("skip",) else "auto"
            if kind == "fill" and e.pattern != "plain":
                kind = f"fill:{e.pattern}"
            self.shape_kind.setCurrentIndex(max(0, self.shape_kind.findData(kind)))
            self.shape_kind.setEnabled(not removed)
            self.shape_angle.setValue(int(e.angle) if e is not None and e.angle is not None else -5)
            self.shape_angle.setToolTip("Direction of the fill rows" + (
                f" (automatic: {sh.angle:.0f}°)" if sh.angle is not None else "") + ". You can also drag the arrow.")
            self.shape_angle.setEnabled(not removed and sh.kind == "fill" and sh.pattern not in pat.OPEN)
            self.shape_density.setValue(int(round((e.density if e is not None else 1.0) * 100)))
            self.shape_density.setEnabled(not removed and sh.kind in ("fill", "satin"))
            self.shape_order.setCurrentIndex(max(0, self.shape_order.findData(e.order if e is not None else 0)))
            self.shape_order.setEnabled(not removed)
            self.shape_remove.setChecked(removed)
            self.shape_reset.setEnabled(e is not None)
        finally:
            for w in (self.shape_kind, self.shape_angle, self.shape_density, self.shape_order, self.shape_remove,
                      self.shape_reset):
                w.blockSignals(False)
            self.shape_undo_btn.setEnabled(bool(self.shape_undo))
            self.shape_redo_btn.setEnabled(bool(self.shape_redo))
            self.shape_reset_all.setEnabled(bool(self.shape_edits))
            self.shape_ai_btn.setEnabled(self.design is not None and bool(self.design.shapes) and not self.ai_running)

    def _shape_kind_picked(self, data: str):
        kind, _, pattern = data.partition(":")
        self.update_shape(kind=kind, pattern=pattern or "plain")

    def _shape_angle_edited(self):
        v = self.shape_angle.value()
        self.update_shape(angle=None if v < 0 else float(v))

    def _edit_of(self, sh: Shape) -> int | None:
        """Index in self.shape_edits of the edit that applies to this shape."""
        if sh.edit is not None:
            for i, e in enumerate(self.shape_edits):
                if e == sh.edit:
                    return i
        return None

    def update_shape(self, reset: bool = False, sh: Shape | None = None, snapshot: bool = True, **changes):
        """Change the selected shape (or `sh`) and re-convert."""
        sh = sh or self.v_st.shape
        if sh is None or self.design is None:
            return
        if snapshot:
            self._shape_snapshot()
        i = self._edit_of(sh)
        old = self.shape_edits[i] if i is not None else ShapeEdit(sh.rgb, sh.fx, sh.fy)
        new = ShapeEdit(sh.rgb, old.fx, old.fy) if reset else replace(old, **changes)
        if i is not None:
            self.shape_edits.pop(i)
        if not new.is_default():
            self.shape_edits.append(new)
        if snapshot:
            self._shape_changed()

    def _shape_snapshot(self):
        self.shape_undo.append(copy.deepcopy(self.shape_edits))
        del self.shape_undo[:-60]
        self.shape_redo.clear()

    def _shape_changed(self):
        self._mark_dirty()
        self._fill_shape_panel()
        self.changed()  # re-convert with the new shape edits
        self.statusBar().showMessage("Re-stitching with your change…", 3000)

    def _shape_history(self, src: list, dst: list):
        if src:
            dst.append(copy.deepcopy(self.shape_edits))
            self.shape_edits = src.pop()
            self._shape_changed()

    def reset_all_shapes(self):
        if self.shape_edits:
            self._shape_snapshot()
            self.shape_edits = []
            self._shape_changed()

    def suggest_shapes(self):
        d = self.design
        if d is None or not d.shapes or self.ai_running or self.busy:
            if self.busy:
                self.statusBar().showMessage("Still stitching… try again in a moment.", 4000)
            return
        client = self._claude()
        if client is None:
            return
        self.busy = self.ai_running = self.shape_ai = True
        self.gen += 1
        s = self.settings()
        self.shape_ai_btn.setText("✨ Looking at your shapes…")
        self.shape_ai_btn.setEnabled(False)
        self.progress.setRange(0, 0)
        self.progress.show()
        self.statusBar().showMessage("AI is reviewing every shape…")
        self.pool.start(Job(self.gen, lambda p: ("shape_ai", ai.suggest_shapes(d, s, client=client)), self.sig))

    def _show_suggestions(self, advice, shapes):
        self.ai_running = self.shape_ai = False
        self.shape_ai_btn.setText("✨ Suggest with AI")
        self._fill_shape_panel()
        if not advice.suggestions:
            QMessageBox.information(self, APP, (advice.summary or "") + "\n\nNo changes suggested: "
                                    "the automatic choices look right.")
            return
        rows = []
        for x in advice.suggestions:
            sh = shapes[x.shape - 1]
            what = {"keep": KIND_NAMES.get(sh.kind, sh.kind), "fill": "Fill", "satin": "Satin",
                    "outline": "Outline only"}[x.stitch]
            if x.angle is not None and (x.stitch == "fill" or (x.stitch == "keep" and sh.kind == "fill")):
                what += f" at {x.angle}°"
            rows.append((f"{KIND_NAMES.get(sh.kind, sh.kind)} shape ({sh.area_mm2:.0f} mm²) → {what}: {x.reason}",
                         x.reason))

        def pick(i):
            if 0 <= i < len(advice.suggestions):
                self.select_shape(shapes[advice.suggestions[i].shape - 1])

        dlg = SuggestionsDialog(self, advice.summary, rows, pick)
        if not dlg.exec():
            return
        self.apply_suggestions([(shapes[advice.suggestions[i].shape - 1], advice.suggestions[i])
                                for i in dlg.chosen()])

    def apply_suggestions(self, picked):
        """Turn chosen AI suggestions into shape edits (one undo step) and re-convert."""
        if not picked:
            return
        self._shape_snapshot()
        for sh, x in picked:
            changes = {}
            if x.stitch != "keep":
                changes["kind"] = x.stitch
            if x.angle is not None and (x.stitch == "fill" or (x.stitch == "keep" and sh.kind == "fill")):
                changes["angle"] = float(x.angle)
            if changes:
                self.update_shape(sh=sh, snapshot=False, **changes)
        self._shape_changed()
        self.statusBar().showMessage(f"Using {len(picked)} AI suggestion(s)… (Ctrl+Z undoes them)", 6000)

    def render_density(self):
        if self.design is None or self.tabs.currentWidget() is not self.v_den:
            return
        ppm = min(8.0, self._preview_ppm())
        W, H, origin = layout(self.design, ppm, 3.0)
        self.v_den.set_image(to_qimage(density_image(self.design, ppm, 3.0, (W, H), origin)))

    def _toggle_play(self):
        if self.play_timer.isActive():
            self.play_timer.stop()
            self.play.setText("▶")
        elif self.design is not None:
            if self.sim.value() >= 1000:
                self.sim.setValue(0)
            self.tabs.setCurrentWidget(self.v_st)
            self.play.setText("⏸")
            self.play_timer.start()

    def _play_step(self):
        v = self.sim.value() + 4
        self.sim.setValue(min(1000, v))
        if v >= 1000:
            self._toggle_play()

    # ------------------------------------------------------------------ AI
    # AI runs through StitchPilot Plus (account.py): this computer is connected to an account once, and
    # the AI service checks that account's allowance on every request.
    def _token(self) -> str | None:
        return self.qs.value("cloud_token", "") or None

    def _set_token(self, token: str | None):
        if token:
            self.qs.setValue("cloud_token", token)
        else:
            self.qs.remove("cloud_token")
            self.qs.remove("ai_plan")

    def show_account(self, reason: str = ""):
        dlg = AccountDialog(self, self._token, self._set_token, reason)
        dlg.exec()
        if dlg.info:  # remembered for "run AI automatically" (Plus only)
            self.qs.setValue("ai_plan", dlg.info.get("plan", "free"))

    def _connected(self) -> bool:
        if not self._token():
            self.show_account("AI features need StitchPilot Plus, or a free account to try them: connect this "
                              "computer first.")
        return bool(self._token())

    def _claude(self):
        """The Claude client for an AI request (through the AI service), or None if not connected."""
        if not self._connected():
            return None
        try:
            return account.anthropic_client(self._token())
        except account.AccountError as e:
            QMessageBox.information(self, APP, str(e))
            return None

    def _openai(self):
        """The image client for "Create with AI", or None if not connected."""
        if not self._connected():
            return None
        try:
            return account.openai_client(self._token())
        except account.AccountError as e:
            QMessageBox.information(self, APP, str(e))
            return None

    def account_problem(self, err: account.AccountError):
        """Explain an account refusal and offer the way forward (connect, or get Plus)."""
        if isinstance(err, account.NeedsAccount):
            self._set_token(None)
            self.show_account(str(err))
        elif isinstance(err, account.OutOfAllowance):
            self.qs.setValue("ai_plan", err.plan)
            box = QMessageBox(QMessageBox.Icon.Information, APP, str(err), parent=self)
            if err.plan != "plus":
                plus = box.addButton("✨ Get StitchPilot Plus", QMessageBox.ButtonRole.AcceptRole)
                box.addButton("Not now", QMessageBox.ButtonRole.RejectRole)
                box.exec()
                if box.clickedButton() is plus:
                    QDesktopServices.openUrl(QUrl(account.SHOP_URL))
            else:
                box.exec()
        else:
            QMessageBox.warning(self, APP, str(err))

    def create_with_ai(self):
        """Describe a picture, let AI draw it, then open it like any other picture."""
        dlg = ImageGenDialog(self, self._openai)
        if not dlg.exec() or dlg.chosen() is None:
            return
        idea = dlg.idea.toPlainText().strip()
        try:
            path = self._save_generated(dlg.chosen(), idea)
        except OSError as e:
            QMessageBox.warning(self, APP, f"Couldn't save the picture:\n{e}")
            return
        self.open_path(str(path))
        if self.path == path:
            self.statusBar().showMessage(f"AI picture saved as {path.name} in {path.parent} ✨", 10000)

    def _save_generated(self, rgba: np.ndarray, idea: str) -> Path:
        """Keep the picture as a PNG (Pictures/StitchPilot AI), so it can be reopened or used elsewhere."""
        import time

        from PIL import Image

        folder = Path(self.qs.value("ai_pictures_dir", str(Path.home() / "Pictures" / "StitchPilot AI")))
        folder.mkdir(parents=True, exist_ok=True)
        words = "".join(c if c.isalnum() else " " for c in idea.lower()).split()[:5]
        path = folder / f"{'-'.join(words) or 'ai-picture'}-{time.strftime('%Y%m%d-%H%M%S')}.png"
        Image.fromarray(rgba, "RGBA").save(path)
        return path

    def run_ai(self):
        if self.rgba is None or self.ai_running or self.file_mode:
            return
        client = self._claude()
        if client is None:
            return
        self.busy = self.ai_running = True
        self.gen += 1
        rgba, s = self.rgba, self.settings()
        self.ai_btn.setText("✨  Looking at your picture…")
        self.ai_btn.setEnabled(False)
        self.progress.setRange(0, 0)
        self.progress.show()
        self.statusBar().showMessage("AI is studying the picture…")
        self.pool.start(Job(self.gen, lambda p: ("ai", ai.analyse(rgba, s, client=client)), self.sig))

    def _set_ai_colors_item(self, n: int | None):
        self.colors.blockSignals(True)
        if self.colors.itemData(0) == "ai":
            self.colors.removeItem(0)
        if n:
            self.colors.insertItem(0, f"AI palette ({n})", "ai")
            self.colors.setCurrentIndex(0)
        elif self.colors.currentIndex() < 0:
            self.colors.setCurrentIndex(0)
        self.colors.blockSignals(False)

    def _apply_ai(self, a: ai.Analysis):
        self.ai_running = False
        self.ai_btn.setText("✨  Ask AI again")
        s = ai.apply(a, self.settings())
        self.ai_settings = s
        self.ai_palette = s.palette
        self._quiet = True
        self._set_ai_colors_item(len(s.palette) if s.palette else None)
        self.simp.setValue(int(round((1 - s.detail) * 100)))
        self.bg.setCurrentIndex(max(0, self.bg.findData(s.background)))
        self.keep_inside.setChecked(not s.remove_enclosed_bg)
        self._quiet = False
        fit = {"great": "Great fit for embroidery", "good": "Good fit for embroidery",
               "fair": "Will work with some simplification", "poor": "Hard to embroider well"}[a.suitability]
        tips = "".join(f"<li>{t}</li>" for t in a.tips[:4])
        size_warn = ""
        if min(self.w_spin.value(), self.h_spin.value()) < a.min_width_mm:
            size_warn = f"<br>⚠ Details may be lost below about {a.min_width_mm} mm wide."
        self.ai_lbl.setText(f"<b>{a.subject}</b><br>{fit} · {len(s.palette or [])} thread colours"
                            f"{size_warn}<ul style='margin-left:-20px'>{tips}</ul>")
        self.ai_lbl.show()
        self.statusBar().showMessage("AI settings applied")
        self.convert()

    # ------------------------------------------------------------------ save / export
    def _stem(self) -> str:
        if self.project_path:
            return self.project_path.stem
        if self.path:
            return self.path.stem
        return (self.texts[0].text.split()[0] if self.texts else "design")[:30]

    def save(self):
        d = self.design
        if d is None or d.stitch_count == 0:
            return
        ext = FORMATS[self.fmt.currentText()]
        start = self.qs.value("save_dir", str(self.path.parent if self.path else Path.home()))
        default = str(Path(start) / f"{self._stem()}.{ext}")
        fn, _ = QFileDialog.getSaveFileName(self, "Save embroidery file", default,
                                            f"{self.fmt.currentText()} (*.{ext})")
        if not fn:
            return
        if not fn.lower().endswith("." + ext):
            fn += "." + ext
        try:
            p = export(d, fn)
        except Exception as e:
            QMessageBox.warning(self, APP, f"Couldn't save the file:\n{e}")
            return
        self.qs.setValue("save_dir", str(p.parent))
        box = QMessageBox(self)
        box.setWindowTitle(APP)
        box.setText(f"Saved <b>{p.name}</b><br>{d.stitch_count:,} stitches, {len(d.active)} colours.")
        extra = " Appliqué colours pause the machine twice: lay the fabric, then trim it." if d.stop_count else ""
        box.setInformativeText("Copy it to your machine (USB stick or software) and stitch a test "
                               "on scrap fabric with the same stabilizer first." + extra)
        show = box.addButton("Show in folder", QMessageBox.ButtonRole.ActionRole)
        sheet = box.addButton("Print worksheet…", QMessageBox.ButtonRole.ActionRole)
        box.addButton(QMessageBox.StandardButton.Ok)
        box.exec()
        if box.clickedButton() is show:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(p.parent)))
        elif box.clickedButton() is sheet:
            self.save_worksheet()

    def save_preview(self):
        if self.design is None:
            return
        fn, _ = QFileDialog.getSaveFileName(self, "Export preview", f"{self._stem()}_preview.png",
                                            "PNG image (*.png)")
        if fn:
            to_qimage(render(self.design, 12, self.fabric_rgb)).save(fn)

    def save_cut_file(self):
        d = self.design
        if d is None or not cutfiles.pieces(d):
            QMessageBox.information(self, APP, "There are no appliqué pieces in this design yet.\n\n"
                                    "Click ⋯ next to a thread colour and choose “Sew as appliqué”.")
            return
        start = self.qs.value("save_dir", str(Path.home()))
        svg, dxf = "SVG for Cricut Design Space, CanvasWorkspace (*.svg)", "DXF for ScanNCut, Silhouette (*.dxf)"
        fn, flt = QFileDialog.getSaveFileName(self, "Save appliqué cut file",
                                              str(Path(start) / f"{Path(self._stem()).stem}_cut.svg"),
                                              f"{svg};;{dxf}")
        if not fn:
            return
        ext = ".dxf" if flt == dxf else ".svg"
        if Path(fn).suffix.lower() not in (".svg", ".dxf"):
            fn += ext
        try:
            cutfiles.write(d, fn)
        except Exception as e:
            QMessageBox.warning(self, APP, f"Couldn't save the cut file:\n{e}")
            return
        n = sum(len(r) for _, _, r in cutfiles.pieces(d))
        self.statusBar().showMessage(
            f"Cut file saved: {n} piece{'s' if n != 1 else ''} at real size. Cut them, then lay each one on its "
            "placement line instead of trimming fabric at the machine ✂️", 12000)

    def save_template(self):
        d = self.design
        if d is None or d.stitch_count == 0:
            return
        start = self.qs.value("save_dir", str(Path.home()))
        paper = self.qs.value("paper", default_paper())
        fn, flt = QFileDialog.getSaveFileName(
            self, "Save placement template", str(Path(start) / f"{Path(self._stem()).stem}_template.pdf"),
            "PDF on Letter paper (*.pdf);;PDF on A4 paper (*.pdf)",
            "PDF on Letter paper (*.pdf)" if paper == "Letter" else "PDF on A4 paper (*.pdf)")
        if not fn:
            return
        paper = "A4" if "A4" in flt else "Letter"
        self.qs.setValue("paper", paper)
        hoop = self.hoop_mm() if self.show_hoop.isChecked() else None
        try:
            pages = write_template(fn, d, hoop, Path(self._stem()).stem, paper)
        except Exception as e:
            QMessageBox.warning(self, APP, f"Couldn't make the template:\n{e}")
            return
        self.statusBar().showMessage(
            f"Template saved ({pages} page{'s' if pages > 1 else ''}). Print at 100% / Actual size 📐", 10000)
        QDesktopServices.openUrl(QUrl.fromLocalFile(fn))

    def save_worksheet(self):
        d = self.design
        if d is None or d.stitch_count == 0:
            return
        start = self.qs.value("save_dir", str(Path.home()))
        fn, _ = QFileDialog.getSaveFileName(self, "Save worksheet", str(Path(start) / f"{self._stem()}_worksheet.pdf"),
                                            "PDF (*.pdf)")
        if not fn:
            return
        img = to_qimage(render(d, min(10.0, 1600 / max(d.width_mm, d.height_mm)), self.fabric_rgb))
        write_worksheet(fn, d, img, self._stem(), self.fmt.currentText())
        QDesktopServices.openUrl(QUrl.fromLocalFile(fn))

    def batch(self):
        start = self.qs.value("last_dir", str(Path.home() / "Pictures"))
        pics = " ".join(f"*{e}" for e in SUPPORTED_EXTS)
        files, _ = QFileDialog.getOpenFileNames(self, "Pictures to convert", start, f"Pictures ({pics})")
        if not files:
            return
        folder = QFileDialog.getExistingDirectory(self, "Save the embroidery files in…", str(Path(files[0]).parent))
        if not folder:
            return
        s = self.settings()
        # colours, order and shape changes belong to this picture, not the others
        s = replace(s, palette=None, num_colors=s.num_colors if not s.palette else 0, texts=[], applique=[],
                    text_fixes=[],
                    color_order=[], skip_colors=[], shape_edits=[])
        ext = FORMATS[self.fmt.currentText()]
        chart, snap = self.chart.currentData(), self.snap.isChecked()

        def run(progress):
            ok, failed = 0, []
            for i, f in enumerate(files):
                progress(i / len(files), f"Converting {Path(f).name} ({i + 1}/{len(files)})…")
                try:
                    d = digitize(load_image(f), s)
                    match_threads(d, chart, snap)
                    export(d, Path(folder) / f"{Path(f).stem}.{ext}")
                    ok += 1
                except Exception as e:
                    failed.append(f"{Path(f).name}: {e}")
            return "batch", (ok, failed, folder)

        self.gen += 1
        self.busy = True
        self.progress.setRange(0, 100)
        self.progress.show()
        self.pool.start(Job(self.gen, run, self.sig))

    # ------------------------------------------------------------------ updates
    def check_updates(self, quiet: bool = False):
        """Looks for a newer StitchPilot. quiet: the automatic check (at most daily, says nothing unless there is one)."""
        if quiet:
            if not (updates.configured() and self.auto_update.isChecked()):
                return
            if time.time() - float(self.qs.value("update_checked_at", 0) or 0) < 20 * 3600:
                return

        def work():
            try:
                res = updates.check()
            except Exception as e:  # shown (or, when quiet, ignored)
                res = e
            try:
                self._update_sig.done.emit(quiet, res)
            except RuntimeError:  # the window was closed
                pass

        threading.Thread(target=work, daemon=True).start()

    def _update_answered(self, quiet: bool, res):
        if isinstance(res, Exception):
            if not quiet:
                QMessageBox.information(self, "Updates", str(res))
            return
        self.qs.setValue("update_checked_at", time.time())
        if res is None:
            if not quiet:
                QMessageBox.information(self, "Updates", f"You have the latest version of {APP} ({__version__}).")
            return
        if quiet and self.qs.value("update_skip", "") == res.version:
            return
        dlg = UpdateDialog(self, res, self._install_update)
        dlg.exec()
        if dlg.skipped:
            self.qs.setValue("update_skip", res.version)

    def _install_update(self, path: Path):
        try:
            self._relaunch = updates.install(path)
        except updates.UpdateError as e:
            QMessageBox.warning(self, "Updates", str(e))
            return
        if not self.close():  # they chose to keep working (unsaved changes)
            self._relaunch = None
            QMessageBox.information(self, "Updates", "The update is installed. It starts the next time you open "
                                    f"{APP}.")

    def _cleanup_update(self, tries: int = 6):
        # the copy this update replaced may still be closing for a few seconds
        if not updates.cleanup() and tries > 1:
            QTimer.singleShot(5000, lambda: self._cleanup_update(tries - 1))

    def show_tips(self):
        QMessageBox.information(self, "Tips for great results", (
            "• Simple artwork with flat colours (logos, clip art, cartoons) works best.\n"
            "• Use the biggest size your hoop allows – small text under ~5 mm won't be readable.\n"
            "• Fewer colours = fewer thread changes and a cleaner result.\n"
            "• Pick the right fabric: knits need more underlay and pull compensation.\n"
            "• Check the Density tab: red areas can break needles.\n"
            "• Big flat areas can be sewn as appliqué (⋯ next to a thread) – faster and softer.\n"
            "• Always use stabilizer, and stitch a test on scrap fabric first.\n"
            "• Photos turn into posterised art; try 'Bold' detail and 4–8 colours."))


def apply_light_theme(app: QApplication, name: str | None = None):
    """Apply the saved (or given) theme; it pins a light palette so Windows dark mode can't clash."""
    theme.apply(app, name or app_settings().value("theme", theme.DEFAULT_THEME))


def selftest(src: str, dst: str) -> int:
    """Headless check used to verify a packaged build: picture in, embroidery file out."""
    try:
        from shapely import box

        s = Settings(texts=[TextItem("Test", height_mm=8, arc=60), TextItem("AB", kind="monogram", frame="circle")],
                     border="satin", text_fixes=[TextFix("Test", (0.3, 0.8, 0.7, 0.95))])
        d = digitize(load_image(src), s)
        match_threads(d, "Brother", snap=False)
        export(d, dst)
        fills = sum(len(r) for r in pat.cross_runs(box(0, 0, 20, 20), 2.2, None)
                    + pat.motif_runs(box(0, 0, 30, 30), "hearts", 5.0, None))
        d.blocks[0].fabric = [np.array([[0, 0], [20, 0], [10, 15]], float)]
        cutfiles.write(d, dst + ".svg")
        import anthropic  # noqa: F401  (packaged for AI assist, which imports it on first use)
        import openai  # noqa: F401  (packaged for "Create with AI")
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey  # noqa: F401  (updates)

        assert "embroidery" in imagegen.build_prompt("a frog")
        Path(dst + ".txt").write_text(f"ok {d.stitch_count} stitches {len(d.blocks)} colours, "
                                      f"{fills} pattern points, cut file, image AI\n")
        return 0
    except Exception:
        Path(dst + ".txt").write_text(traceback.format_exc())
        return 1


class App(QApplication):
    """Also opens files that macOS hands over as events (dropped on the Dock icon, Finder ▸ Open With)."""

    def __init__(self, argv):
        super().__init__(argv)
        self.window: MainWindow | None = None
        self.pending_file: str | None = None

    def event(self, e):
        if e.type() == QEvent.Type.FileOpen and e.file():
            if self.window is not None:
                self.window.open_path(e.file())
            else:  # arrives before the window exists when the app is launched by opening a file
                self.pending_file = e.file()
            return True
        return super().event(e)


def main():
    if len(sys.argv) == 4 and sys.argv[1] == "--selftest":
        sys.exit(selftest(sys.argv[2], sys.argv[3]))
    app = App(sys.argv)
    app.setApplicationName(APP)
    app.setWindowIcon(app_icon())
    apply_light_theme(app)
    w = MainWindow()
    app.window = w
    w.show()
    w.check_recovery()
    w._cleanup_update()
    QTimer.singleShot(3000, lambda: w.check_updates(quiet=True))
    # macOS adds a -psn_… argument when launched from Finder on older systems: ignore anything not a file
    args = [a for a in sys.argv[1:] if not a.startswith("-psn_") and Path(a).exists()]
    if app.pending_file:
        w.open_path(app.pending_file)
    elif args:
        w.open_path(args[0])
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
