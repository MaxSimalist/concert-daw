"""
gui_mixer.py — Микшер звука видео (как в OBS): у каждой строки индикатор уровня
(L/R, шкала dB, удержание пика, лампа перегруза) и фейдер в dB.

Фейдер: −60…0 dB (0 dB — громкость файла как есть; громче Qt не умеет).
* у нуля «прилипает» (зона ±1.5 dB) — легко вернуть ровно 0;
* нажатие колёсиком или двойной клик — сразу 0 dB;
* колесо мыши — по 1 dB, Shift — по 0.1 dB;
* крайнее левое положение — «−∞» (тишина).

Строки: «Выход» (все видео сразу), «Сцена …» (громкость запоминается для каждой
сцены отдельно) и источники сцены в эфире (громкость — у каждого источника).
"""
from __future__ import annotations

import math
from typing import Callable

from PySide6.QtCore import QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QLinearGradient, QPainter
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout, QWidget

from gui_layout import ElidedLabel

DB_MIN = -60.0
SNAP_DB = 1.5
METER_LO, METER_HI = -60.0, 0.0


def db_to_gain(db: float) -> float:
    return 0.0 if db <= DB_MIN else 10 ** (db / 20.0)


def gain_to_db(g: float) -> float:
    return DB_MIN if g <= 1e-6 else max(DB_MIN, min(0.0, 20 * math.log10(g)))


def fmt_db(db: float) -> str:
    return "−∞" if db <= DB_MIN else ("0.0 dB" if abs(db) < 0.05 else f"{db:.1f} dB")


class DbFader(QSlider):
    """Горизонтальный фейдер в десятых долях dB (−600…0)."""

    def __init__(self, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self.setRange(int(DB_MIN * 10), 0)
        self.setSingleStep(10)
        self.setPageStep(60)
        self.setFocusPolicy(Qt.NoFocus)
        self.setToolTip("Громкость. Колёсико мыши (нажать) или двойной клик — 0 dB; у нуля прилипает.\n"
                        "Колесо — по 1 dB, с Shift — по 0.1 dB")
        self.valueChanged.connect(self._snap)

    def db(self) -> float:
        return self.value() / 10.0

    def set_db(self, db: float) -> None:
        self.blockSignals(True)
        self.setValue(int(round(max(DB_MIN, min(0.0, db)) * 10)))
        self.blockSignals(False)

    def _snap(self, v: int) -> None:
        if v != 0 and abs(v) <= SNAP_DB * 10 and self.isSliderDown():
            self.setValue(0)                    # прилипание к нулю, пока тянут мышью

    def mousePressEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.MiddleButton:
            self.setValue(0)
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseDoubleClickEvent(self, _e) -> None:  # noqa: N802
        self.setValue(0)

    def wheelEvent(self, e) -> None:  # noqa: N802
        step = 1 if e.modifiers() & Qt.ShiftModifier else 10
        d = step if e.angleDelta().y() > 0 else -step
        v = self.value() + d
        if self.value() < 0 < v or (self.value() < 0 and v > -SNAP_DB * 10 and step == 10):
            v = 0
        self.setValue(max(self.minimum(), min(0, v)))
        e.accept()


class HMeter(QWidget):
    """Горизонтальный индикатор: 1–2 полоски (L/R), шкала dB, пик-холд, перегруз."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.levels: list[float] = []          # dB по каналам
        self.holds: list[float] = []
        self.hold_t: list[int] = []
        self.clip = False
        self._drawn = None
        self.dim = False                       # канал без звука (M) — уровень серым
        self.setMinimumHeight(18)
        self.setMaximumHeight(22)
        self.setToolTip("Уровень звука после громкости (dBFS). Красная лампа — перегруз (клик — сбросить)")

    def push(self, peaks: list) -> None:
        if not peaks:
            peaks = [0.0] * max(1, len(self.levels) or 2)
        n = min(2, len(peaks)) if len(peaks) <= 2 else 2
        if len(peaks) > 2:                      # многоканальный звук — показываем L и R
            peaks = [max(peaks[0::2]), max(peaks[1::2])]
        if len(self.levels) != n:
            self.levels = [METER_LO] * n
            self.holds = [METER_LO] * n
            self.hold_t = [0] * n
        for i, p in enumerate(peaks[:n]):
            if p >= 0.999:
                self.clip = True
            db = 20 * math.log10(p) if p > 1e-6 else METER_LO
            self.levels[i] = db if db > self.levels[i] else max(METER_LO, self.levels[i] - 1.5)
            if db >= self.holds[i]:
                self.holds[i], self.hold_t[i] = db, 40
            elif self.hold_t[i] > 0:
                self.hold_t[i] -= 1
            else:
                self.holds[i] = max(METER_LO, self.holds[i] - 0.7)
        state = (tuple(round(v, 1) for v in self.levels), tuple(round(v, 1) for v in self.holds), self.clip)
        if state != self._drawn:     # тишина — без перерисовок
            self._drawn = state
            self.update()

    def mousePressEvent(self, _e) -> None:  # noqa: N802
        self.clip = False
        self.update()

    def _x(self, db: float, w: float) -> float:
        return w * (min(METER_HI, max(METER_LO, db)) - METER_LO) / (METER_HI - METER_LO)

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        W = self.width() - 10
        H = self.height()
        n = max(1, len(self.levels))
        bar_h = (H - 7) / n
        grad = QLinearGradient(0, 0, W, 0)
        grad.setColorAt(0.0, QColor("#1f7a58"))
        grad.setColorAt(self._x(-20, W) / W, QColor("#36d399"))
        grad.setColorAt(self._x(-9, W) / W, QColor("#f7c948"))
        grad.setColorAt(self._x(-3, W) / W, QColor("#ef4b55"))
        grad.setColorAt(1.0, QColor("#ef4b55"))
        for i in range(n):
            y = i * (bar_h + 1)
            p.fillRect(QRectF(0, y, W, bar_h), QColor("#0b0d10"))
            if self.levels:
                lw = self._x(self.levels[i], W)
                if lw > 0.5:
                    p.fillRect(QRectF(0, y, lw, bar_h), QColor("#4a535c") if self.dim else QBrush(grad))
                hx = self._x(self.holds[i], W)
                if self.holds[i] > METER_LO:
                    p.fillRect(QRectF(hx - 1, y, 2, bar_h), QColor("#e6eaee"))
        p.fillRect(QRectF(W + 3, 0, 7, H - 6), QColor("#ef4b55" if self.clip else "#3a1519"))
        # шкала
        f = QFont(self.font())
        f.setPixelSize(8)
        p.setFont(f)
        p.setPen(QColor("#56616c"))
        for db in (-50, -40, -30, -20, -10, -5, 0):
            x = self._x(db, W)
            p.drawLine(int(x), H - 6, int(x), H - 4)
        p.end()


class MixerStrip(QWidget):
    """Одна строка микшера: название, индикатор, фейдер, dB, кнопка M."""

    changed = Signal(float, bool)           # dB, muted

    def __init__(self, title: str, db: float, muted: bool, accent: str = "", parent=None):
        super().__init__(parent)
        v = QVBoxLayout(self)
        v.setContentsMargins(2, 3, 2, 3)
        v.setSpacing(1)
        top = QHBoxLayout()
        self.name = ElidedLabel(title)
        if accent:
            self.name.setStyleSheet(f"font-weight:800;color:{accent};background:transparent")
        top.addWidget(self.name, 1)
        self.db_label = QLabel(fmt_db(db))
        self.db_label.setStyleSheet("font-family:Consolas,monospace;color:#a3adb7;background:transparent")
        top.addWidget(self.db_label)
        v.addLayout(top)
        self.meter = HMeter()
        v.addWidget(self.meter)
        row = QHBoxLayout()
        self.fader = DbFader()
        self.fader.set_db(db)
        self.fader.valueChanged.connect(self._emit)
        row.addWidget(self.fader, 1)
        self.mute = QPushButton("M")
        self.mute.setObjectName("muteBtn")
        self.mute.setCheckable(True)
        self.mute.setChecked(muted)
        self.mute.setFixedWidth(34)
        self.mute.setFocusPolicy(Qt.NoFocus)
        self.mute.setToolTip("Без звука")
        self.mute.toggled.connect(self._emit)
        self.meter.dim = muted
        row.addWidget(self.mute)
        v.addLayout(row)

    def _emit(self, *_a) -> None:
        db = self.fader.db()
        self.meter.dim = self.mute.isChecked()
        self.db_label.setText(fmt_db(db))
        self.changed.emit(db, self.mute.isChecked())


def connect_strip(strip: MixerStrip, fn: Callable[[float, bool], None]) -> MixerStrip:
    strip.changed.connect(fn)
    return strip
