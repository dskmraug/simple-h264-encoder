#!/usr/bin/env python3
"""Simple H.264 video editor — crop, trim, and re-encode."""

import sys
import subprocess
import shutil
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QLabel, QComboBox, QSpinBox, QDoubleSpinBox, QFileDialog,
    QGraphicsView, QGraphicsScene, QGraphicsEllipseItem, QGraphicsRectItem,
    QGraphicsLineItem, QGraphicsSimpleTextItem, QSizePolicy, QProgressDialog,
    QMessageBox, QFrame, QGridLayout, QGroupBox, QGraphicsItem,
)
from PySide6.QtCore import (
    Qt, QTimer, QUrl, QRectF, QPointF, QSizeF, Signal, QThread,
)
from PySide6.QtGui import (
    QColor, QPainter, QPen, QBrush, QFont, QKeySequence, QShortcut,
    QPainterPath, QPalette,
)
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem


RESOLUTIONS = {
    "Original":         None,
    "4K  3840×2160":    (3840, 2160),
    "2K  2560×1440":    (2560, 1440),
    "1080p 1920×1080":  (1920, 1080),
    "720p  1280×720":   (1280, 720),
    "480p   854×480":   (854,  480),
    "Custom":           "custom",
}


def ms_to_ts(ms: int) -> str:
    s, frac = divmod(ms, 1000)
    h, s = divmod(s, 3600)
    m, s = divmod(s, 60)
    if h:
        return f"{h}:{m:02d}:{s:02d}.{frac:03d}"
    return f"{m:02d}:{s:02d}.{frac:03d}"


def atempo_chain(speed: float) -> list[str]:
    """Build an atempo filter list for any speed in [0.1, 3.0].

    Each atempo stage is clamped to [0.5, 2.0] per FFmpeg's requirement,
    so extreme values are achieved by chaining multiple stages.
    """
    if abs(speed - 1.0) < 0.001:
        return []
    stages: list[str] = []
    remaining = speed
    while remaining > 2.0 + 1e-9:
        stages.append("atempo=2.0")
        remaining /= 2.0
    while remaining < 0.5 - 1e-9:
        stages.append("atempo=0.5")
        remaining /= 0.5
    if abs(remaining - 1.0) > 0.001:
        stages.append(f"atempo={remaining:.6f}")
    return stages


# ── Crop handle (corner square) ───────────────────────────────────────────────

class CropHandle(QGraphicsRectItem):
    TL, TR, BL, BR = 0, 1, 2, 3
    _DEFAULT_R = 6  # half-size in scene coords

    def __init__(self, corner: int, overlay: "CropOverlay"):
        r = self._DEFAULT_R
        super().__init__(-r, -r, r * 2, r * 2)
        self.corner = corner
        self.overlay = overlay
        self.setZValue(20)
        self.setFlag(QGraphicsItem.ItemIsMovable, True)
        self.setFlag(QGraphicsItem.ItemSendsGeometryChanges, True)
        self.setBrush(QBrush(QColor(255, 255, 255, 230)))
        self.setPen(QPen(QColor(0, 140, 255), 1.5))
        _CURSORS = {
            self.TL: Qt.SizeFDiagCursor, self.TR: Qt.SizeBDiagCursor,
            self.BL: Qt.SizeBDiagCursor, self.BR: Qt.SizeFDiagCursor,
        }
        self.setCursor(_CURSORS[corner])

    def set_radius(self, r: int):
        self.setRect(-r, -r, r * 2, r * 2)

    def itemChange(self, change, value):
        if change == QGraphicsItem.ItemPositionChange and not self.overlay.updating:
            constrained = self.overlay.on_handle_drag(self.corner, value)
            return constrained
        return super().itemChange(change, value)


# ── Crop overlay (dim + border + handles) ─────────────────────────────────────

class CropOverlay(QWidget):  # inherits QWidget just to get Qt.NoPen etc; actually QObject
    pass


class CropOverlay:
    """Manages crop rectangle overlay drawn over the video in the scene."""

    def __init__(self, scene: QGraphicsScene, video_rect: QRectF):
        self._scene = scene
        self.video_rect = QRectF(video_rect)
        self.rect = QRectF(video_rect)
        self.updating = False
        self._changed_cb = None

        dim = QBrush(QColor(0, 0, 0, 140))
        self._dims = [scene.addRect(QRectF(), Qt.NoPen, dim) for _ in range(4)]
        for d in self._dims:
            d.setZValue(10)

        bp = QPen(QColor(255, 255, 255, 200), 1.0)
        bp.setCosmetic(True)
        self._border = scene.addRect(QRectF(), bp)
        self._border.setZValue(11)

        tp = QPen(QColor(255, 255, 255, 60), 0.5)
        tp.setCosmetic(True)
        self._thirds = [scene.addLine(0, 0, 0, 0, tp) for _ in range(4)]
        for t in self._thirds:
            t.setZValue(12)

        self.handles: list[CropHandle] = []
        for c in (CropHandle.TL, CropHandle.TR, CropHandle.BL, CropHandle.BR):
            h = CropHandle(c, self)
            scene.addItem(h)
            self.handles.append(h)

        self._refresh()

    def set_changed_callback(self, cb):
        self._changed_cb = cb

    def on_handle_drag(self, corner: int, new_pos: QPointF) -> QPointF:
        vr = self.video_rect
        MIN = 20
        x = max(vr.left(), min(new_pos.x(), vr.right()))
        y = max(vr.top(), min(new_pos.y(), vr.bottom()))

        r = QRectF(self.rect)
        if corner == CropHandle.TL:
            r.setLeft(min(x, r.right() - MIN)); r.setTop(min(y, r.bottom() - MIN))
        elif corner == CropHandle.TR:
            r.setRight(max(x, r.left() + MIN)); r.setTop(min(y, r.bottom() - MIN))
        elif corner == CropHandle.BL:
            r.setLeft(min(x, r.right() - MIN)); r.setBottom(max(y, r.top() + MIN))
        elif corner == CropHandle.BR:
            r.setRight(max(x, r.left() + MIN)); r.setBottom(max(y, r.top() + MIN))

        self.set_rect(r)
        return self._handle_positions()[corner]

    def _handle_positions(self) -> list[QPointF]:
        r = self.rect
        return [
            QPointF(r.left(), r.top()),
            QPointF(r.right(), r.top()),
            QPointF(r.left(), r.bottom()),
            QPointF(r.right(), r.bottom()),
        ]

    def set_rect(self, rect: QRectF, notify=True):
        self.rect = rect.normalized()
        self.updating = True
        self._refresh()
        self.updating = False
        if notify and self._changed_cb:
            self._changed_cb(self.rect)

    def reset(self):
        self.set_rect(QRectF(self.video_rect))

    def update_handle_size(self, r: int):
        for h in self.handles:
            h.set_radius(r)

    def _refresh(self):
        r, vr = self.rect, self.video_rect

        self._dims[0].setRect(QRectF(vr.left(), vr.top(),    vr.width(), r.top() - vr.top()))
        self._dims[1].setRect(QRectF(vr.left(), r.bottom(),  vr.width(), vr.bottom() - r.bottom()))
        self._dims[2].setRect(QRectF(vr.left(), r.top(),     r.left() - vr.left(), r.height()))
        self._dims[3].setRect(QRectF(r.right(), r.top(),     vr.right() - r.right(), r.height()))
        self._border.setRect(r)

        tx1, tx2 = r.left() + r.width()/3, r.left() + r.width()*2/3
        ty1, ty2 = r.top() + r.height()/3, r.top() + r.height()*2/3
        self._thirds[0].setLine(tx1, r.top(), tx1, r.bottom())
        self._thirds[1].setLine(tx2, r.top(), tx2, r.bottom())
        self._thirds[2].setLine(r.left(), ty1, r.right(), ty1)
        self._thirds[3].setLine(r.left(), ty2, r.right(), ty2)

        for h, pos in zip(self.handles, self._handle_positions()):
            h.setPos(pos)

    def remove(self):
        for item in self._dims + self._thirds + [self._border] + self.handles:
            self._scene.removeItem(item)


# ── Timeline ──────────────────────────────────────────────────────────────────

class TimelineWidget(QWidget):
    seek_requested = Signal(int)
    in_changed     = Signal(int)
    out_changed    = Signal(int)

    _BAR_H    = 10
    _HANDLE_W = 7

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedHeight(56)
        self._dur  = 0
        self._pos  = 0
        self._in   = 0
        self._out  = 0
        self._drag = None
        self.setMouseTracking(True)

    # ── public setters ──
    def set_duration(self, ms): self._dur = ms; self._out = ms; self.update()
    def set_position(self, ms): self._pos = ms; self.update()
    def set_in(self, ms):       self._in  = ms; self.update()
    def set_out(self, ms):      self._out = ms; self.update()

    # ── coordinate helpers ──
    def _bar(self) -> QRectF:
        m = 28
        return QRectF(m, (self.height() - self._BAR_H) / 2, self.width() - m*2, self._BAR_H)

    def _x(self, ms) -> float:
        b = self._bar()
        return b.left() + (ms / self._dur) * b.width() if self._dur else b.left()

    def _ms(self, x) -> int:
        b = self._bar()
        t = (x - b.left()) / b.width() if b.width() else 0
        return int(max(0.0, min(1.0, t)) * self._dur)

    def _hit(self, x) -> str | None:
        if not self._dur:
            return None
        if abs(x - self._x(self._in))  < 10: return "in"
        if abs(x - self._x(self._out)) < 10: return "out"
        return "play"

    # ── painting ──
    def paintEvent(self, _):
        if not self._dur:
            return
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        b = self._bar()

        # track
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(55, 55, 60))
        p.drawRoundedRect(b, 3, 3)

        # selected range
        xi, xo = self._x(self._in), self._x(self._out)
        p.setBrush(QColor(0, 110, 240, 170))
        p.drawRoundedRect(QRectF(xi, b.top(), xo - xi, b.height()), 3, 3)

        # in / out handles
        hw, hh = self._HANDLE_W, 22
        hy = b.center().y() - hh / 2
        p.setBrush(QColor(60, 200, 80))
        p.drawRect(QRectF(xi, hy, hw, hh))
        p.setBrush(QColor(220, 70, 70))
        p.drawRect(QRectF(xo - hw, hy, hw, hh))

        # playhead
        xp = self._x(self._pos)
        p.setPen(QPen(QColor(255, 220, 0), 2))
        p.drawLine(QPointF(xp, b.top() - 8), QPointF(xp, b.bottom() + 4))
        tri = QPainterPath()
        tri.moveTo(xp, b.top() - 8)
        tri.lineTo(xp - 5, b.top() - 14)
        tri.lineTo(xp + 5, b.top() - 14)
        tri.closeSubpath()
        p.setPen(Qt.NoPen)
        p.fillPath(tri, QColor(255, 220, 0))

        p.end()

    # ── mouse ──
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton and self._dur:
            self._drag = self._hit(e.position().x())
            self._apply_drag(e.position().x())

    def mouseMoveEvent(self, e):
        if (e.buttons() & Qt.LeftButton) and self._drag:
            self._apply_drag(e.position().x())
        else:
            self.setCursor(Qt.SizeHorCursor if self._hit(e.position().x()) in ("in", "out")
                           else Qt.PointingHandCursor)

    def mouseReleaseEvent(self, _): self._drag = None

    def _apply_drag(self, x):
        ms = self._ms(x)
        if self._drag == "in":
            ms = max(0, min(ms, self._out - 100))
            self._in = ms; self.in_changed.emit(ms)
        elif self._drag == "out":
            ms = max(self._in + 100, min(ms, self._dur))
            self._out = ms; self.out_changed.emit(ms)
        else:
            self._pos = ms; self.seek_requested.emit(ms)
        self.update()


# ── Video view ────────────────────────────────────────────────────────────────

class VideoView(QGraphicsView):
    file_dropped = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self._gscene = QGraphicsScene(self)
        self.setScene(self._gscene)
        self.setBackgroundBrush(QBrush(QColor(18, 18, 20)))
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setAcceptDrops(True)

        self._hint = self._gscene.addSimpleText("ここにビデオファイルをドロップ、または「開く」を押してください")
        font = QFont(); font.setPointSize(13)
        self._hint.setFont(font)
        self._hint.setBrush(QBrush(QColor(100, 100, 110)))
        self._hint.setZValue(30)

        self.video_item = QGraphicsVideoItem()
        self._gscene.addItem(self.video_item)
        self.video_item.hide()

        self.crop: CropOverlay | None = None
        self._native: QSizeF = QSizeF()

    # ── drag & drop ──
    def dragEnterEvent(self, e): e.acceptProposedAction() if e.mimeData().hasUrls() else None
    def dragMoveEvent(self,  e): e.acceptProposedAction()
    def dropEvent(self, e):
        for url in e.mimeData().urls():
            p = url.toLocalFile()
            if p: self.file_dropped.emit(p); break

    # ── setup after video loads ──
    def setup_video(self, size: QSizeF):
        self._native = size
        w, h = size.width(), size.height()
        self._gscene.setSceneRect(0, 0, w, h)
        self.video_item.setSize(size)
        self.video_item.setPos(0, 0)
        self.video_item.show()
        self._hint.hide()

        if self.crop:
            self.crop.remove()
        self.crop = CropOverlay(self._gscene, QRectF(0, 0, w, h))

        self._fit_and_resize_handles()

    def _fit_and_resize_handles(self):
        self.fitInView(self._gscene.sceneRect(), Qt.KeepAspectRatio)
        if self.crop and self._native.isValid():
            scale = abs(self.transform().m11())
            r = max(4, int(9 / scale)) if scale > 0 else 6
            self.crop.update_handle_size(r)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        if self._native.isValid():
            self._fit_and_resize_handles()
        else:
            br = self._hint.boundingRect()
            self._hint.setPos((self.width() - br.width()) / 2,
                              (self.height() - br.height()) / 2)

    def get_crop(self) -> QRectF:
        return self.crop.rect if self.crop else QRectF()

    def reset_crop(self):
        if self.crop: self.crop.reset()

    def is_full_crop(self) -> bool:
        if not self.crop or not self._native.isValid():
            return True
        r = self.crop.rect
        return (int(r.x()) == 0 and int(r.y()) == 0
                and int(r.width()) == int(self._native.width())
                and int(r.height()) == int(self._native.height()))


# ── FFmpeg export thread ───────────────────────────────────────────────────────

class ExportThread(QThread):
    log     = Signal(str)
    done    = Signal(bool, str)

    def __init__(self, cmd: list[str]):
        super().__init__()
        self.cmd = cmd
        self._proc = None

    def run(self):
        try:
            self._proc = subprocess.Popen(
                self.cmd, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True,
            )
            for line in self._proc.stdout:
                self.log.emit(line.rstrip())
            self._proc.wait()
            ok = self._proc.returncode == 0
            self.done.emit(ok, "" if ok else f"return code {self._proc.returncode}")
        except Exception as ex:
            self.done.emit(False, str(ex))

    def cancel(self):
        if self._proc:
            self._proc.terminate()


# ── Main window ───────────────────────────────────────────────────────────────

class VideoEditor(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Simple H.264 Editor")
        self.resize(1140, 760)

        self._src: Path | None = None
        self._dur = 0
        self._in  = 0
        self._out = 0
        self._vw = self._vh = 0
        self._spinbox_updating = False

        self._player = QMediaPlayer(self)
        self._audio  = QAudioOutput(self)
        self._player.setAudioOutput(self._audio)
        self._audio.setVolume(0.8)

        self._build_ui()
        self._wire()
        self._shortcuts()

        self._ticker = QTimer(self)
        self._ticker.setInterval(80)
        self._ticker.timeout.connect(self._tick)
        self._ticker.start()

        if not shutil.which("ffmpeg"):
            QMessageBox.warning(self, "ffmpeg not found",
                                "ffmpeg が見つかりません。エクスポートは動作しません。\n"
                                "brew install ffmpeg でインストールしてください。")

    # ── UI construction ──────────────────────────────────────────────────────

    def _build_ui(self):
        root_w = QWidget()
        self.setCentralWidget(root_w)
        vbox = QVBoxLayout(root_w)
        vbox.setContentsMargins(8, 8, 8, 8)
        vbox.setSpacing(5)

        # video view
        self.view = VideoView()
        self.view.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self._player.setVideoOutput(self.view.video_item)
        vbox.addWidget(self.view, stretch=1)

        # timeline
        self.tl = TimelineWidget()
        vbox.addWidget(self.tl)

        # transport bar
        tb = QHBoxLayout()
        self.btn_open  = QPushButton("開く")
        self.btn_open.setFixedWidth(64)
        self.btn_play  = QPushButton("▶ 再生")
        self.btn_play.setFixedWidth(84)
        self.btn_play.setEnabled(False)
        self.lbl_time  = QLabel("--:--.--- / --:--.---")
        self.lbl_time.setStyleSheet("color:#aaa; font-family:'Menlo','Courier New',monospace; font-size:12px;")
        self.btn_in    = QPushButton("[ In  (I)")
        self.btn_out   = QPushButton("Out ] (O)")
        self.btn_trim_reset = QPushButton("トリムリセット")
        for b in (self.btn_in, self.btn_out, self.btn_trim_reset):
            b.setEnabled(False)
            b.setFixedWidth(96)

        # speed control
        self.lbl_speed = QLabel("速度:")
        self.lbl_speed.setStyleSheet("color:#aaa;")
        self.sp_speed = QDoubleSpinBox()
        self.sp_speed.setRange(0.1, 3.0)
        self.sp_speed.setSingleStep(0.1)
        self.sp_speed.setDecimals(1)
        self.sp_speed.setValue(1.0)
        self.sp_speed.setSuffix(" x")
        self.sp_speed.setFixedWidth(72)
        self.sp_speed.setToolTip("再生速度 (0.1x〜3.0x)。エクスポートにも反映されます。")
        self.btn_speed_reset = QPushButton("1x")
        self.btn_speed_reset.setFixedWidth(32)
        self.btn_speed_reset.setToolTip("速度を 1.0x にリセット")

        tb.addWidget(self.btn_open)
        tb.addWidget(self.btn_play)
        tb.addWidget(self.lbl_time)
        tb.addStretch()
        tb.addWidget(self.lbl_speed)
        tb.addWidget(self.sp_speed)
        tb.addWidget(self.btn_speed_reset)
        tb.addSpacing(16)
        tb.addWidget(self.btn_in)
        tb.addWidget(self.btn_out)
        tb.addWidget(self.btn_trim_reset)
        vbox.addLayout(tb)

        # settings bar
        sb = QHBoxLayout()
        sb.setSpacing(10)

        # crop group
        cg = QGroupBox("クロップ（ピクセル）")
        cgl = QGridLayout(cg)
        cgl.setSpacing(4)
        self.sp_cx = QSpinBox(); self.sp_cx.setRange(0, 9999); self.sp_cx.setPrefix("X: ")
        self.sp_cy = QSpinBox(); self.sp_cy.setRange(0, 9999); self.sp_cy.setPrefix("Y: ")
        self.sp_cw = QSpinBox(); self.sp_cw.setRange(2, 9999); self.sp_cw.setPrefix("W: ")
        self.sp_ch = QSpinBox(); self.sp_ch.setRange(2, 9999); self.sp_ch.setPrefix("H: ")
        self.btn_crop_reset = QPushButton("クロップリセット")
        cgl.addWidget(self.sp_cx, 0, 0); cgl.addWidget(self.sp_cy, 0, 1)
        cgl.addWidget(self.sp_cw, 1, 0); cgl.addWidget(self.sp_ch, 1, 1)
        cgl.addWidget(self.btn_crop_reset, 2, 0, 1, 2)
        sb.addWidget(cg)

        # resolution group
        rg = QGroupBox("出力解像度")
        rgl = QVBoxLayout(rg)
        self.combo_res = QComboBox()
        for k in RESOLUTIONS:
            self.combo_res.addItem(k)
        self.custom_w = QWidget()
        cwl = QHBoxLayout(self.custom_w)
        cwl.setContentsMargins(0, 0, 0, 0)
        self.sp_ow = QSpinBox(); self.sp_ow.setRange(2, 7680); self.sp_ow.setValue(1920); self.sp_ow.setPrefix("W: ")
        self.sp_oh = QSpinBox(); self.sp_oh.setRange(2, 4320); self.sp_oh.setValue(1080); self.sp_oh.setPrefix("H: ")
        cwl.addWidget(self.sp_ow); cwl.addWidget(self.sp_oh)
        self.custom_w.hide()
        rgl.addWidget(self.combo_res)
        rgl.addWidget(self.custom_w)
        sb.addWidget(rg)

        # export button
        self.btn_export = QPushButton("エクスポート H.264")
        self.btn_export.setFixedHeight(60)
        self.btn_export.setEnabled(False)
        self.btn_export.setStyleSheet(
            "QPushButton{background:#0055cc;color:white;font-size:14px;"
            "font-weight:bold;border-radius:7px;}"
            "QPushButton:hover{background:#0070ff;}"
            "QPushButton:disabled{background:#3a3a40;color:#666;}"
        )
        sb.addWidget(self.btn_export)

        vbox.addLayout(sb)

    # ── signal wiring ────────────────────────────────────────────────────────

    def _wire(self):
        self.btn_open.clicked.connect(self._open)
        self.btn_play.clicked.connect(self._toggle_play)
        self.btn_in.clicked.connect(lambda: self._set_in(self._player.position()))
        self.btn_out.clicked.connect(lambda: self._set_out(self._player.position()))
        self.btn_trim_reset.clicked.connect(self._reset_trim)
        self.btn_crop_reset.clicked.connect(self.view.reset_crop)
        self.btn_export.clicked.connect(self._export)

        self.view.file_dropped.connect(self._load)
        self.view.video_item.nativeSizeChanged.connect(self._on_native_size)
        self._player.durationChanged.connect(self._on_duration)
        self._player.playbackStateChanged.connect(self._on_state)

        self.tl.seek_requested.connect(self._player.setPosition)
        self.tl.in_changed.connect(self._on_in_changed)
        self.tl.out_changed.connect(self._on_out_changed)

        self.combo_res.currentTextChanged.connect(
            lambda t: self.custom_w.setVisible(t == "Custom"))

        self.sp_speed.valueChanged.connect(self._on_speed_changed)
        self.btn_speed_reset.clicked.connect(lambda: self.sp_speed.setValue(1.0))

        for sp in (self.sp_cx, self.sp_cy, self.sp_cw, self.sp_ch):
            sp.valueChanged.connect(self._spinbox_to_crop)

    def _shortcuts(self):
        QShortcut(QKeySequence(Qt.Key_Space),      self, self._toggle_play)
        QShortcut(QKeySequence(Qt.Key_Left),        self, lambda: self._step(-500))
        QShortcut(QKeySequence(Qt.Key_Right),       self, lambda: self._step(500))
        QShortcut(QKeySequence("Shift+Left"),       self, lambda: self._step(-100))
        QShortcut(QKeySequence("Shift+Right"),      self, lambda: self._step(100))
        QShortcut(QKeySequence(Qt.Key_I),           self, lambda: self._set_in(self._player.position()))
        QShortcut(QKeySequence(Qt.Key_O),           self, lambda: self._set_out(self._player.position()))

    # ── file loading ─────────────────────────────────────────────────────────

    def _open(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "ビデオを開く", "",
            "Video (*.mp4 *.mov *.m4v *.mkv *.avi *.h264 *.ts);;All (*)"
        )
        if path:
            self._load(path)

    def _load(self, path: str):
        self._src = Path(path)
        self._player.setSource(QUrl.fromLocalFile(str(path)))
        self._player.pause()
        self.setWindowTitle(f"Simple H.264 Editor — {self._src.name}")

    # ── player events ────────────────────────────────────────────────────────

    def _on_native_size(self, size: QSizeF):
        if size.width() < 1 or size.height() < 1:
            return
        self._vw = int(size.width())
        self._vh = int(size.height())
        self.view.setup_video(size)
        if self.view.crop:
            self.view.crop.set_changed_callback(self._on_crop_changed)
        self._reset_crop_spinboxes()
        for w in (self.btn_play, self.btn_in, self.btn_out,
                  self.btn_trim_reset, self.btn_export):
            w.setEnabled(True)

    def _on_duration(self, ms: int):
        self._dur = ms
        self._out = ms
        self.tl.set_duration(ms)

    def _on_state(self, state):
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.btn_play.setText("⏸ 一時停止" if playing else "▶ 再生")

    def _tick(self):
        pos = self._player.position()
        dur = self._player.duration()
        self.tl.set_position(pos)
        self.lbl_time.setText(f"{ms_to_ts(pos)} / {ms_to_ts(dur)}")
        # loop within in/out region
        if (self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState
                and self._dur > 0 and pos >= self._out):
            self._player.setPosition(self._in)

    # ── transport ────────────────────────────────────────────────────────────

    def _toggle_play(self):
        if self._player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self._player.pause()
        else:
            if self._player.position() >= self._out:
                self._player.setPosition(self._in)
            self._player.play()

    def _step(self, ms: int):
        pos = max(0, min(self._player.position() + ms, self._dur))
        self._player.setPosition(pos)

    def _set_in(self, ms: int):
        self._in = max(0, min(ms, self._out - 100))
        self.tl.set_in(self._in)

    def _set_out(self, ms: int):
        self._out = max(self._in + 100, min(ms, self._dur))
        self.tl.set_out(self._out)

    def _on_in_changed(self, ms):  self._in  = ms
    def _on_out_changed(self, ms): self._out = ms

    def _reset_trim(self):
        self._in = 0; self._out = self._dur
        self.tl.set_in(0); self.tl.set_out(self._dur)

    def _on_speed_changed(self, speed: float):
        self._player.setPlaybackRate(speed)
        # highlight when not 1.0x
        style = ("color:#ffcc44; font-weight:bold;" if abs(speed - 1.0) > 0.05
                 else "color:#aaa;")
        self.lbl_speed.setStyleSheet(style)

    # ── crop sync ────────────────────────────────────────────────────────────

    def _on_crop_changed(self, rect: QRectF):
        self._spinbox_updating = True
        self.sp_cx.setValue(int(rect.x()))
        self.sp_cy.setValue(int(rect.y()))
        self.sp_cw.setValue(int(rect.width()))
        self.sp_ch.setValue(int(rect.height()))
        self._spinbox_updating = False

    def _spinbox_to_crop(self):
        if self._spinbox_updating or not self.view.crop:
            return
        r = QRectF(self.sp_cx.value(), self.sp_cy.value(),
                   self.sp_cw.value(), self.sp_ch.value())
        cb = self.view.crop._changed_cb
        self.view.crop.set_changed_callback(None)  # suppress feedback
        self.view.crop.set_rect(r, notify=False)
        self.view.crop.set_changed_callback(cb)

    def _reset_crop_spinboxes(self):
        self._spinbox_updating = True
        self.sp_cx.setMaximum(self._vw); self.sp_cy.setMaximum(self._vh)
        self.sp_cw.setMaximum(self._vw); self.sp_ch.setMaximum(self._vh)
        self.sp_cx.setValue(0); self.sp_cy.setValue(0)
        self.sp_cw.setValue(self._vw); self.sp_ch.setValue(self._vh)
        self._spinbox_updating = False

    # ── export ───────────────────────────────────────────────────────────────

    def _output_res(self) -> tuple[int, int] | None:
        t = self.combo_res.currentText()
        v = RESOLUTIONS.get(t)
        if v == "custom":
            return (self.sp_ow.value(), self.sp_oh.value())
        return v  # None or (w, h)

    def _build_cmd(self, out: Path) -> list[str]:
        cmd = ["ffmpeg", "-y"]
        speed = round(self.sp_speed.value(), 1)
        in_s  = self._in  / 1000
        dur_s = (self._out - self._in) / 1000
        if in_s > 0:
            cmd += ["-ss", f"{in_s:.3f}"]
        cmd += ["-i", str(self._src)]
        cmd += ["-t", f"{dur_s:.3f}"]

        # ── video filters ──
        vf: list[str] = []
        if not self.view.is_full_crop():
            cr = self.view.get_crop()
            x, y = int(cr.x()), int(cr.y())
            w = int(cr.width())  & ~1   # ensure even for libx264
            h = int(cr.height()) & ~1
            vf.append(f"crop={w}:{h}:{x}:{y}")
        res = self._output_res()
        if res:
            vf.append(f"scale={res[0]}:{res[1]}")
        if abs(speed - 1.0) > 0.001:
            # setpts compresses/stretches timestamps: PTS/speed → faster when speed>1
            vf.append(f"setpts=PTS/{speed:.6f}")
        if vf:
            cmd += ["-vf", ",".join(vf)]

        # ── audio filters ──
        af = atempo_chain(speed)
        if af:
            cmd += ["-af", ",".join(af)]

        cmd += ["-c:v", "libx264", "-crf", "23", "-preset", "medium"]
        cmd += ["-map", "0:v:0", "-map", "0:a:0?"]
        cmd += ["-c:a", "aac", "-b:a", "128k"]
        cmd += [str(out)]
        return cmd

    def _export(self):
        if not self._src:
            return
        default = str(self._src.parent / (self._src.stem + "_edited.mp4"))
        out, _ = QFileDialog.getSaveFileName(
            self, "エクスポート先", default, "MP4 (*.mp4);;All (*)")
        if not out:
            return

        cmd = self._build_cmd(Path(out))
        self._prog = QProgressDialog("エクスポート中...", "キャンセル", 0, 0, self)
        self._prog.setWindowTitle("エクスポート")
        self._prog.setWindowModality(Qt.WindowModal)
        self._prog.show()

        self._exp = ExportThread(cmd)
        self._exp.log.connect(lambda s: self._prog.setLabelText(s[-100:]))
        self._exp.done.connect(self._on_export_done)
        self._prog.canceled.connect(self._exp.cancel)
        self._exp.start()

    def _on_export_done(self, ok: bool, err: str):
        self._prog.close()
        if ok:
            QMessageBox.information(self, "完了", "エクスポートが完了しました。")
        else:
            QMessageBox.critical(self, "エラー", f"FFmpeg エラー:\n{err}")


# ── entry point ───────────────────────────────────────────────────────────────

def _dark_palette(app: QApplication) -> QPalette:
    p = app.palette()
    C = QColor
    p.setColor(QPalette.Window,          C(28, 28, 32))
    p.setColor(QPalette.WindowText,      C(220, 220, 225))
    p.setColor(QPalette.Base,            C(20, 20, 24))
    p.setColor(QPalette.AlternateBase,   C(36, 36, 40))
    p.setColor(QPalette.Text,            C(220, 220, 225))
    p.setColor(QPalette.Button,          C(40, 40, 45))
    p.setColor(QPalette.ButtonText,      C(220, 220, 225))
    p.setColor(QPalette.Highlight,       C(0,  100, 210))
    p.setColor(QPalette.HighlightedText, C(255, 255, 255))
    p.setColor(QPalette.Mid,             C(50,  50,  55))
    p.setColor(QPalette.Dark,            C(22,  22,  26))
    p.setColor(QPalette.Shadow,          C(10,  10,  12))
    return p


def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    app.setPalette(_dark_palette(app))
    win = VideoEditor()
    win.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
