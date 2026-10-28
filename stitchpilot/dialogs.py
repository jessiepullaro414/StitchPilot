"""Dialogs: lettering editor and the printable worksheet."""
from __future__ import annotations

import threading
from datetime import date

import numpy as np
from PySide6.QtCore import QMarginsF, QObject, QRectF, QSize, Qt, QTimer, QUrl, Signal
from PySide6.QtGui import (
    QColor, QDesktopServices, QFont, QIcon, QImage, QPageLayout, QPageSize, QPainter, QPdfWriter, QPixmap,
)
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFontComboBox, QFormLayout, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPlainTextEdit, QProgressBar, QPushButton, QSpinBox, QToolButton, QVBoxLayout,
)

from . import __version__, account, imagegen, updates
from .lettering import FRAMES, render_text
from .settings import TextFix, TextItem

POSITIONS = {"Below the picture": "below", "Above the picture": "above", "On top of the picture": "over"}


class SuggestionsDialog(QDialog):
    """AI suggestions for shapes: tick the ones to use. Selecting a row shows its shape (on_pick(i))."""

    def __init__(self, parent, summary: str, rows: list[tuple[str, str]], on_pick=None):
        super().__init__(parent)
        self.setWindowTitle("AI suggestions for your shapes")
        self.setMinimumSize(560, 380)
        lay = QVBoxLayout(self)
        intro = QLabel(summary or "Here's what could stitch better.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self.list = QListWidget()
        for text, tip in rows:
            it = QListWidgetItem(text)
            it.setToolTip(tip)
            it.setFlags(it.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            it.setCheckState(Qt.CheckState.Checked)
            self.list.addItem(it)
        self.list.setWordWrap(True)
        if on_pick is not None:
            self.list.currentRowChanged.connect(on_pick)
        lay.addWidget(self.list, 1)
        hint = QLabel("Click a suggestion to see its shape. Untick any you don't want.")
        hint.setObjectName("muted")
        lay.addWidget(hint)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Use ticked suggestions")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def chosen(self) -> list[int]:
        return [i for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.CheckState.Checked]


class _Done(QObject):
    done = Signal(object)  # list of RGBA arrays, or the exception


def _thumb(rgba: np.ndarray, size: int = 150) -> QPixmap:
    """A picture on a light checkerboard (so transparent parts show as such)."""
    import cv2

    h, w = rgba.shape[:2]
    k = size / max(h, w)
    small = cv2.resize(rgba, (max(1, round(w * k)), max(1, round(h * k))), interpolation=cv2.INTER_AREA)
    sh, sw = small.shape[:2]
    yy, xx = np.mgrid[0:sh, 0:sw]
    board = np.where(((yy // 10) + (xx // 10)) % 2 == 0, 255, 232).astype(np.float32)[..., None].repeat(3, 2)
    a = small[..., 3:4].astype(np.float32) / 255
    rgb = np.ascontiguousarray((small[..., :3] * a + board * (1 - a)).astype(np.uint8))
    return QPixmap.fromImage(QImage(rgb.data, sw, sh, 3 * sw, QImage.Format.Format_RGB888).copy())


class ImageGenDialog(QDialog):
    """Create a picture with AI: describe it, get a few versions drawn for embroidery, pick one."""

    def __init__(self, parent, get_client, generate=None):
        """get_client() returns the client to draw with (connecting the account first if needed), or None."""
        super().__init__(parent)
        self.setWindowTitle("Create a picture with AI")
        self.setMinimumWidth(680)
        self.get_client = get_client
        self._generate = generate or imagegen.generate
        self.images: list[np.ndarray] = []
        self.running = False
        lay = QVBoxLayout(self)
        intro = QLabel("Describe what you'd like to stitch. The picture is drawn to embroider well: flat colours, "
                       "bold shapes and no tiny details. Each version uses one AI picture from your allowance "
                       "(StitchPilot Plus, or your free tries).")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        self.idea = QPlainTextEdit()
        self.idea.setPlaceholderText("e.g. a smiling sloth hugging a coffee cup  ·  a fox in a flower crown  ·  "
                                     "a cactus wearing sunglasses")
        self.idea.setFixedHeight(64)
        lay.addWidget(self.idea)
        row = QHBoxLayout()
        self.style = QComboBox()
        self.style.addItems(list(imagegen.STYLES))
        self.colours = QSpinBox()
        self.colours.setRange(2, 10)
        self.colours.setValue(5)
        self.colours.setToolTip("Each colour is a thread change. 3–6 is plenty for most designs.")
        self.quality = QComboBox()
        self.quality.addItems(list(imagegen.QUALITY))
        self.shape = QComboBox()
        self.shape.addItems(list(imagegen.SIZES))
        self.count = QSpinBox()
        self.count.setRange(1, 4)
        self.count.setValue(2)
        self.count.setToolTip("How many versions to draw at once (each one counts as an AI picture)")
        for w in (self.style, self.quality, self.shape):
            w.setSizeAdjustPolicy(QComboBox.SizeAdjustPolicy.AdjustToContents)
        for w in (self.colours, self.count):
            w.setFixedWidth(64)
        for label, w in (("Style", self.style), ("Quality", self.quality), ("Shape", self.shape)):
            row.addWidget(QLabel(label, objectName="muted"))
            row.addWidget(w)
        row.addStretch(1)
        lay.addLayout(row)
        row = QHBoxLayout()
        for label, w in (("Colours", self.colours), ("Versions", self.count)):
            row.addWidget(QLabel(label, objectName="muted"))
            row.addWidget(w)
        self.transparent = QCheckBox("Transparent background")
        self.transparent.setChecked(True)
        self.transparent.setToolTip("No background to remove: only the design itself gets stitched")
        row.addWidget(self.transparent)
        row.addStretch(1)
        self.go = QPushButton("✨ Create", objectName="ai")
        self.go.clicked.connect(self.create)
        row.addWidget(self.go)
        lay.addLayout(row)
        self.status = QLabel("", objectName="muted")
        self.status.setWordWrap(True)
        self.status.setOpenExternalLinks(True)
        lay.addWidget(self.status)
        self.grid = QGridLayout()
        self.grid.setSpacing(8)
        lay.addLayout(self.grid)
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        self.group.idClicked.connect(lambda _: self.ok.setEnabled(True))
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        self.ok = bb.button(QDialogButtonBox.StandardButton.Ok)
        self.ok.setText("Use this picture")
        self.ok.setEnabled(False)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._sig = _Done()
        self._sig.done.connect(self._finished)

    def params(self) -> dict:
        return dict(style=self.style.currentText(), colours=self.colours.value(), count=self.count.value(),
                    quality=imagegen.QUALITY[self.quality.currentText()], transparent=self.transparent.isChecked(),
                    size=imagegen.SIZES[self.shape.currentText()])

    def create(self):
        idea = self.idea.toPlainText().strip()
        if not idea:
            self.status.setText("Describe the picture you'd like first.")
            return
        client = self.get_client()
        if client is None:
            return
        self.running = True
        self.go.setEnabled(False)
        self.go.setText("✨ Drawing…")
        n = self.count.value()
        self.status.setText(f"Drawing {n} version{'s' if n > 1 else ''}… this usually takes 20–60 seconds.")
        kw = dict(self.params(), client=client)

        def work():
            try:
                res = self._generate(idea, **kw)
            except Exception as e:  # shown in the dialog
                res = e
            try:
                self._sig.done.emit(res)
            except RuntimeError:  # the dialog was closed meanwhile
                pass

        threading.Thread(target=work, daemon=True).start()

    def _finished(self, res):
        self.running = False
        self.go.setEnabled(True)
        self.go.setText("✨ Create more")
        if isinstance(res, account.OutOfAllowance):
            more = "" if res.plan == "plus" else (
                f" <a href='{account.SHOP_URL}'>Get StitchPilot Plus</a> for plenty more every month.")
            self.status.setText(f"<span style='color:#c0392b'>{res}</span>{more}")
            return
        if isinstance(res, Exception):
            self.status.setText(f"<span style='color:#c0392b'>{res}</span>")
            return
        self.images = (res + self.images)[:8]  # newest first; earlier ones stay to compare
        while self.grid.count():
            w = self.grid.takeAt(0).widget()
            if w:
                self.group.removeButton(w)
                w.deleteLater()
        for i, img in enumerate(self.images):
            b = QToolButton()
            b.setCheckable(True)
            b.setIcon(QIcon(_thumb(img)))
            b.setIconSize(QSize(150, 150))
            b.setToolTip("Click to choose this one")
            self.group.addButton(b, i)
            self.grid.addWidget(b, i // 4, i % 4)
        self.group.button(0).setChecked(True)
        self.ok.setEnabled(True)
        self.status.setText("Pick the one you like, then “Use this picture”. Not quite right? Change the "
                            "description and create more.")

    def chosen(self) -> np.ndarray | None:
        i = self.group.checkedId()
        return self.images[i] if 0 <= i < len(self.images) else None

    def reject(self):
        if self.running:
            self.status.setText("Still drawing… you can close once it's done (or close anyway to stop waiting).")
            self.running = False  # a second Cancel closes
            return
        super().reject()


class _Result(QObject):
    done = Signal(object, object)  # (what was asked, the answer or the exception)


class AccountDialog(QDialog):
    """StitchPilot Plus & account: connect this computer, see the plan and AI allowance, subscribe."""

    def __init__(self, parent, get_token, set_token, reason: str = ""):
        super().__init__(parent)
        self.setWindowTitle("StitchPilot Plus & account")
        self.setMinimumWidth(500)
        self.get_token, self.set_token = get_token, set_token
        self.info: dict | None = None
        self.link: dict | None = None
        self._busy = False
        lay = QVBoxLayout(self)
        self.reason = QLabel(reason, objectName="info")
        self.reason.setWordWrap(True)
        self.reason.setVisible(bool(reason))
        lay.addWidget(self.reason)
        self.text = QLabel()
        self.text.setWordWrap(True)
        self.text.setTextFormat(Qt.TextFormat.RichText)
        self.text.setOpenExternalLinks(True)
        lay.addWidget(self.text)
        self.code = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self.code.setStyleSheet("font-size: 26px; font-weight: 700; letter-spacing: 4px;")
        self.code.hide()
        lay.addWidget(self.code)
        row = QHBoxLayout()
        self.connect_btn = QPushButton("Connect with your account", objectName="ai")
        self.connect_btn.clicked.connect(self.start_link)
        self.plus_btn = QPushButton("✨ Get Plus")
        self.plus_btn.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(account.SHOP_URL)))
        self.refresh_btn = QPushButton("Refresh")
        self.refresh_btn.clicked.connect(self.refresh)
        self.disconnect_btn = QPushButton("Disconnect this computer")
        self.disconnect_btn.clicked.connect(self.disconnect)
        for b in (self.connect_btn, self.plus_btn, self.refresh_btn, self.disconnect_btn):
            row.addWidget(b)
        row.addStretch(1)
        lay.addLayout(row)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self._sig = _Result()
        self._sig.done.connect(self._answered)
        self.poll = QTimer(self)
        self.poll.setInterval(3000)
        self.poll.timeout.connect(self._poll)
        self.show_state()
        if self.get_token():
            self.refresh()

    # every network call runs off the UI thread, then comes back through _answered
    def _ask(self, what: str, fn, *args):
        if self._busy:
            return
        self._busy = True

        def work():
            try:
                res = fn(*args)
            except Exception as e:  # shown in the dialog
                res = e
            try:
                self._sig.done.emit(what, res)
            except RuntimeError:  # closed meanwhile
                pass

        threading.Thread(target=work, daemon=True).start()

    def _answered(self, what: str, res):
        self._busy = False
        if isinstance(res, account.NeedsAccount):  # disconnected on the website: forget the token
            self.set_token(None)
            self.info = None
            self.show_state(str(res))
            return
        if isinstance(res, Exception):
            self.poll.stop() if what == "start" else None
            self.show_state(f"<span style='color:#c0392b'>{res}</span>")
            return
        if what == "me":
            self.info = res
            self.reason.hide()  # "connect first" no longer applies
            self.show_state()
        elif what == "start":
            self.link = res
            self.code.setText(res["code"])
            self.code.show()
            QDesktopServices.openUrl(QUrl(res["url"]))
            self.poll.start()
            self.show_state()
        elif what == "poll":
            if res.get("status") == "linked":
                self.poll.stop()
                self.link = None
                self.code.hide()
                self.set_token(res["token"])
                self.refresh()
            elif res.get("status") == "expired":
                self.poll.stop()
                self.link = None
                self.code.hide()
                self.show_state("That code expired. Click Connect to try again.")

    def start_link(self):
        self._ask("start", account.start_link)

    def _poll(self):
        if self.link and not self._busy:
            self._ask("poll", account.poll_link, self.link["code"], self.link["pollSecret"])

    def refresh(self):
        token = self.get_token()
        if token:
            self._ask("me", account.me, token)

    def disconnect(self):
        self.set_token(None)
        self.info = None
        self.show_state("This computer is disconnected. (To remove it from your account's list too, use "
                        f"<a href='{account.SHOP_URL}'>stitchpilot.jessiescars.com</a>.)")

    def show_state(self, note: str = ""):
        token = self.get_token()
        connected = bool(token)
        i = self.info
        if self.link:
            body = ("Your browser opened stitchpilot.jessiescars.com. Sign in (or create a free account), check "
                    "the code matches the one below and click <b>Connect</b>. This window updates by itself.")
        elif not connected:
            body = ("<b>StitchPilot Plus</b> adds AI: create pictures from an idea, let AI choose settings, "
                    "AI stitch suggestions and text clean-up. Connect this computer to a free account to try it "
                    "(a few free AI uses), or get Plus for plenty every month.")
        elif i is None:
            body = "Connected. Checking your account…"
        else:
            u = i.get("usage", {})
            img, act = u.get("images", {}), u.get("actions", {})
            plan = i.get("plan")
            if plan == "plus":
                head = "<b>StitchPilot Plus</b> ✨" + (
                    " <span style='color:#c0392b'>(payment failed: update your card on the website)</span>"
                    if i.get("status") == "past_due" else "")
                left = "this month"
            else:
                price = (i.get("plus") or {}).get("price", "")
                head = f"<b>Free</b> · StitchPilot Plus is {price}"
                left = "free tries"
            body = (f"Connected as <b>{i.get('email', '')}</b><br>{head}<br><br>"
                    f"AI pictures: {img.get('used', 0)} of {img.get('limit', 0)} used ({left})<br>"
                    f"Other AI: {act.get('used', 0)} of {act.get('limit', 0)} used ({left})")
        self.text.setText(body + (f"<br><br>{note}" if note else ""))
        plus = bool(i and i.get("plan") == "plus")
        self.connect_btn.setVisible(not connected and not self.link)
        self.plus_btn.setText("Manage subscription" if plus else "✨ Get Plus")
        self.refresh_btn.setVisible(connected)
        self.disconnect_btn.setVisible(connected)


class TextFixDialog(QDialog):
    """AI text clean-up: the text found in the picture, with the matched font. Tick what to redraw;
    the words and font can be corrected first."""

    def __init__(self, parent, rows: list[tuple[str, TextFix | None, float, str]]):
        """rows: (text found, proposed fix or None if it can't be redrawn, font match 0..1, note)."""
        super().__init__(parent)
        self.setWindowTitle("Clean up text")
        self.setMinimumWidth(620)
        lay = QVBoxLayout(self)
        intro = QLabel("StitchPilot can redraw this text as clean lettering before stitching, so the "
                       "letters come out crisp. Check the words and the font, then tick what to redraw.")
        intro.setWordWrap(True)
        lay.addWidget(intro)
        grid = QGridLayout()
        for c, h in enumerate(("", "Text", "Font", "", "")):
            if h:
                grid.addWidget(QLabel(f"<b>{h}</b>"), 0, c)
        self.rows = []
        for r, (found, fix, score, note) in enumerate(rows, 1):
            tick = QCheckBox()
            grid.addWidget(tick, r, 0)
            if fix is None:
                lab = QLabel(f"“{found}” – {note}")
                lab.setObjectName("muted")
                lab.setWordWrap(True)
                grid.addWidget(lab, r, 1, 1, 4)
                tick.setEnabled(False)
                continue
            tick.setChecked(score >= 0.35)
            text = QLineEdit(fix.text)
            font = QFontComboBox()
            font.setCurrentFont(QFont(fix.font))
            bold = QCheckBox("Bold")
            bold.setChecked(fix.bold)
            match = QLabel(f"{score:.0%} match")
            match.setObjectName("muted")
            match.setToolTip("How closely the font's letters overlap the letters in the picture. "
                             "Under about 50%, try another font.")
            for c, w in enumerate((text, font, bold, match), 1):
                grid.addWidget(w, r, c)
            self.rows.append((tick, text, font, bold, fix))
        grid.setColumnStretch(1, 1)
        lay.addLayout(grid)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.button(QDialogButtonBox.StandardButton.Ok).setText("Redraw ticked text")
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def chosen(self) -> list[TextFix]:
        return [TextFix(text.text().strip(), fix.box, font.currentFont().family(), bold.isChecked(), fix.italic)
                for tick, text, font, bold, fix in self.rows if tick.isChecked() and text.text().strip()]


class TextDialog(QDialog):
    """Add or edit one line (or block) of lettering, curved text or a monogram, with a live preview."""

    def __init__(self, parent, item: TextItem | None = None):
        super().__init__(parent)
        self.setWindowTitle("Lettering")
        self.setMinimumWidth(440)
        item = item or TextItem("")
        self.rgb = item.rgb
        lay = QVBoxLayout(self)
        f = QFormLayout()
        self.kind = QComboBox()
        self.kind.addItem("Text", "text")
        self.kind.addItem("Monogram (1–3 initials)", "monogram")
        self.kind.setCurrentIndex(max(0, self.kind.findData(item.kind)))
        f.addRow("Style", self.kind)
        self.text = QPlainTextEdit(item.text)
        self.text.setFixedHeight(70)
        f.addRow("Text", self.text)
        self.font = QFontComboBox()
        self.font.setCurrentFont(QFont(item.font))
        f.addRow("Font", self.font)
        style = QHBoxLayout()
        self.bold = QCheckBox("Bold")
        self.bold.setChecked(item.bold)
        self.italic = QCheckBox("Italic")
        self.italic.setChecked(item.italic)
        style.addWidget(self.bold)
        style.addWidget(self.italic)
        style.addStretch(1)
        f.addRow("", style)
        self.arc = QSpinBox()
        self.arc.setRange(-300, 300)
        self.arc.setSingleStep(15)
        self.arc.setSuffix("°")
        self.arc.setSpecialValueText("Straight")
        self.arc.setValue(int(round(item.arc)))
        self.arc.setToolTip("Bend the line round a circle: + arches over like a rainbow, − curves under like a "
                            "smile. 180° is a half circle. A curved line is always one line.")
        self.arc_lbl = QLabel("Curve")
        f.addRow(self.arc_lbl, self.arc)
        self.frame = QComboBox()
        for k, v in FRAMES.items():
            self.frame.addItem(v, k)
        self.frame.setCurrentIndex(max(0, self.frame.findData(item.frame)))
        self.frame_lbl = QLabel("Frame")
        f.addRow(self.frame_lbl, self.frame)
        self.height = QDoubleSpinBox()
        self.height.setRange(4, 150)
        self.height.setDecimals(1)
        self.height.setSuffix(" mm")
        self.height.setValue(item.height_mm)
        self.height_lbl = QLabel("Letter height")
        f.addRow(self.height_lbl, self.height)
        self.color = QPushButton()
        self.color.clicked.connect(self._pick)
        self._paint()
        f.addRow("Thread colour", self.color)
        self.pos = QComboBox()
        self.pos.addItems(list(POSITIONS))
        self.pos.setCurrentIndex(list(POSITIONS.values()).index(item.position))
        f.addRow("Place", self.pos)
        lay.addLayout(f)
        self.preview = QLabel(alignment=Qt.AlignmentFlag.AlignCenter)
        self.preview.setFixedHeight(130)
        self.preview.setObjectName("preview")
        self.preview.setStyleSheet("background: white; border-radius: 8px;")
        lay.addWidget(self.preview)
        bb = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.kind.currentIndexChanged.connect(self._kind_changed)
        for sig in (self.text.textChanged, self.font.currentFontChanged, self.bold.toggled, self.italic.toggled,
                    self.arc.valueChanged, self.frame.currentIndexChanged):
            sig.connect(self._update_preview)
        self._kind_changed()

    def _kind_changed(self):
        mono = self.kind.currentData() == "monogram"
        for w in (self.frame, self.frame_lbl):
            w.setVisible(mono)
        for w in (self.arc, self.arc_lbl):
            w.setVisible(not mono)
        self.text.setPlaceholderText("Initials, e.g. JMP (the middle letter is the biggest)" if mono
                                     else "Type a name, initials or a slogan…")
        self.height_lbl.setText("Big letter height" if mono else "Letter height")
        self.height.setToolTip("Height of the main capital letter. A monogram looks good at 25–60 mm." if mono
                               else "Height of capital letters. Under 5 mm gets hard to read in thread.")
        self._update_preview()

    def _update_preview(self):
        it = self.item()
        if not it.text:
            self.preview.clear()
            return
        arr = render_text(it, 48)
        h, w = arr.shape[:2]
        a = arr[..., 3:4].astype(float) / 255  # on white, like the preview card
        rgb = (arr[..., :3] * a + 255 * (1 - a)).astype("uint8").copy()
        img = QImage(rgb.data, w, h, 3 * w, QImage.Format.Format_RGB888).copy()
        pm = QPixmap.fromImage(img)
        box = self.preview.size()
        if w > box.width() - 16 or h > box.height() - 16:
            pm = pm.scaled(box.width() - 16, box.height() - 16, Qt.AspectRatioMode.KeepAspectRatio,
                           Qt.TransformationMode.SmoothTransformation)
        self.preview.setPixmap(pm)

    def _paint(self):
        r, g, b = self.rgb
        fg = "white" if (r * 0.3 + g * 0.59 + b * 0.11) < 140 else "#2b2840"
        self.color.setText("#%02X%02X%02X" % self.rgb)
        self.color.setStyleSheet(f"background: rgb({r},{g},{b}); color: {fg};")

    def _pick(self):
        c = QColorDialog.getColor(QColor(*self.rgb), self, "Thread colour")
        if c.isValid():
            self.rgb = (c.red(), c.green(), c.blue())
            self._paint()
            self._update_preview()

    def item(self) -> TextItem:
        mono = self.kind.currentData() == "monogram"
        text = self.text.toPlainText().strip()
        if mono:
            text = "".join(text.split())[:3]
        return TextItem(text, self.font.currentFont().family(), self.bold.isChecked(), self.italic.isChecked(),
                        self.height.value(), self.rgb, POSITIONS[self.pos.currentText()],
                        0.0 if mono else float(self.arc.value()), "monogram" if mono else "text",
                        self.frame.currentData() if mono else "none")


def write_worksheet(path: str, design, preview_qimage, title: str, fmt_name: str) -> None:
    """One-page PDF: preview, size, stitch count and the thread colour sequence."""
    pdf = QPdfWriter(path)
    pdf.setPageLayout(QPageLayout(QPageSize(QPageSize.PageSizeId.Letter), QPageLayout.Orientation.Portrait,
                                  QMarginsF(15, 15, 15, 15), QPageLayout.Unit.Millimeter))
    pdf.setResolution(150)
    pdf.setTitle(title)
    p = QPainter(pdf)
    W = pdf.width()
    dpmm = 150 / 25.4
    y = 0.0

    def text(s, size=10, bold=False, color="#2b2840", x=0.0, w=None):
        nonlocal y
        f = QFont(QApplication.font().family(), size)  # Segoe UI on Windows, the system font on a Mac
        f.setBold(bold)
        p.setFont(f)
        p.setPen(QColor(color))
        h = size * 2.4
        p.drawText(QRectF(x, y, w or W, h), Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter, s)
        return h

    y += text(title, 18, True)
    y += text(f"StitchPilot worksheet · {date.today():%d %b %Y} · {fmt_name}", 9, color="#6d6a80")
    y += 10
    x0, y0, x1, y1 = design.bounds()
    stats = [("Size", f"{x1 - x0:.0f} × {y1 - y0:.0f} mm  ({(x1 - x0) / 25.4:.2f} × {(y1 - y0) / 25.4:.2f} in)"),
             ("Stitches", f"{design.stitch_count:,}"),
             ("Colours", f"{len(design.active)}  ({max(0, len(design.active) - 1)} changes)"),
             ("Trims / stops", f"{design.trim_count} / {design.stop_count}"),
             ("Sew time", f"≈ {design.est_minutes():.0f} min at 650 stitches/min")]
    for k, v in stats:
        text(k, 10, color="#6d6a80", w=W * 0.2)
        y += text(v, 10, True, x=W * 0.2)
    y += 10
    # preview, scaled to fit the remaining width / half the page
    img = preview_qimage
    box_h = pdf.height() * 0.42
    s = min(W / img.width(), box_h / img.height())
    p.drawImage(QRectF((W - img.width() * s) / 2, y, img.width() * s, img.height() * s), img)
    y += img.height() * s + 20
    y += text("Thread sequence", 12, True)
    for i, b in enumerate(design.active):
        r = 6 * dpmm
        p.setBrush(QColor(*b.rgb))
        p.setPen(QColor("#00000060"))
        p.drawEllipse(QRectF(0, y + 4, r, r))
        name = b.thread or "#%02X%02X%02X" % b.rgb
        extra = "  (appliqué: 2 stops)" if b.kinds.get("appliqué") else ""
        text(f"{i + 1}.  {name}{extra}", 10, x=r + 12, w=W * 0.6)
        y += text(f"{b.stitch_count:,} stitches", 10, color="#6d6a80", x=W * 0.7, w=W * 0.3)
        if y > pdf.height() - 60 and i < len(design.active) - 1:
            pdf.newPage()
            y = 0
    p.end()


class _Progress(QObject):
    moved = Signal(float)  # 0..1


class UpdateDialog(QDialog):
    """A newer StitchPilot is out: update and restart (portable Windows exe), or download it by hand.
    on_ready(path) is called with the checked download; it puts it in place and restarts."""

    def __init__(self, parent, rel: updates.Release, on_ready):
        super().__init__(parent)
        self.setWindowTitle("StitchPilot update")
        self.setMinimumWidth(460)
        self.rel, self.on_ready = rel, on_ready
        self.skipped = False
        self._cancel = False
        self._thread: threading.Thread | None = None
        lay = QVBoxLayout(self)
        self.text = QLabel(f"<b>StitchPilot {rel.version}</b> is available. You have {__version__}.")
        self.text.setWordWrap(True)
        lay.addWidget(self.text)
        if rel.notes:
            notes = QPlainTextEdit(rel.notes, readOnly=True)
            notes.setMaximumHeight(160)
            lay.addWidget(notes)
        self.bar = QProgressBar(maximum=1000, textVisible=False)
        self.bar.hide()
        lay.addWidget(self.bar)
        self.installable = updates.can_install() and bool(rel.url)
        bb = QDialogButtonBox()
        self.go = bb.addButton("Update and restart" if self.installable else "Download…",
                               QDialogButtonBox.ButtonRole.AcceptRole)
        self.go.setObjectName("ai")
        self.skip = bb.addButton("Skip this version", QDialogButtonBox.ButtonRole.DestructiveRole)
        self.later = bb.addButton("Later", QDialogButtonBox.ButtonRole.RejectRole)
        self.go.clicked.connect(self.start)
        self.skip.clicked.connect(self._skip)
        self.later.clicked.connect(self.reject)
        lay.addWidget(bb)
        self._sig = _Result()
        self._sig.done.connect(self._finished)
        self._prog = _Progress()
        self._prog.moved.connect(lambda f: self.bar.setValue(int(f * 1000)))

    def _skip(self):
        self.skipped = True
        self.reject()

    def start(self):
        if not self.installable:
            QDesktopServices.openUrl(QUrl(self.rel.url or self.rel.page))
            self.accept()
            return
        self.go.setEnabled(False)
        self.skip.setEnabled(False)
        self.later.setText("Cancel")
        self.bar.setValue(0)
        self.bar.show()
        self.text.setText(f"Downloading StitchPilot {self.rel.version}…")

        def work():
            try:
                res = updates.download(self.rel, updates.staging_path(), lambda d, t: self._prog.moved.emit(d / max(t, 1)),
                                       lambda: self._cancel)
            except Exception as e:  # shown in the dialog
                res = e
            try:
                self._sig.done.emit("download", res)
            except RuntimeError:  # the dialog is gone
                pass

        self._thread = threading.Thread(target=work, daemon=True)
        self._thread.start()

    def _finished(self, _what, res):
        self._thread = None
        if self._cancel:
            return
        if isinstance(res, Exception):
            self.bar.hide()
            self.text.setText(f"<b>The update didn't download.</b> {res}")
            self.go.setEnabled(True)
            self.go.setText("Try again")
            self.skip.setEnabled(True)
            self.later.setText("Later")
            return
        self.text.setText(f"StitchPilot {self.rel.version} is ready. Restarting…")
        self.accept()
        QTimer.singleShot(0, lambda: self.on_ready(res))  # once this dialog has closed

    def reject(self):
        self._cancel = self._thread is not None
        super().reject()
