"""
gui_fx.py — Окна встроенных плагинов дорожки в стиле студийных плагинов
(по мотивам FabFilter Pro-Q / Pro-MB, Waves C4, канальных линеек SSL):

* «CD Pro EQ» — параметрический эквалайзер: большой график с анализатором
  спектра (серый — до обработки, цветной — после), точки полос тянутся мышью,
  под графиком — колонки полос с крутилками Частота / Усиление / Ширина (Q)
  и выбором формы (полка НЧ / колокол / полка ВЧ), срез низа, выходная громкость,
  индикаторы входа и выхода.
* «CD Multiband» — 3-полосный компрессор: график полос с анализатором,
  разделы полос перетаскиваются мышью, ослабление каждой полосы видно прямо
  на графике; у полосы — крутилки Порог / Степень / Атака / Восстановление /
  Усиление, кнопки «вкл» и S (послушать полосу отдельно); Mix (параллельная
  компрессия), выход, индикаторы.
* Вверху как у настоящих плагинов: кнопка питания, сравнение A/B, пресеты.

Крутилка: тянуть вверх-вниз (с Shift — точно), колесо мыши, двойной клик — значение
по умолчанию, правый клик — ввести число.
Сама обработка — dsp.py; окна только меняют настройки и рисуют.
"""
from __future__ import annotations

import copy
import math
import sys
from pathlib import Path
from typing import Callable, Optional

import numpy as np
from PySide6.QtCore import QObject, QPointF, QProcess, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QLinearGradient, QPainter, QPainterPath, QPen, QRadialGradient
from PySide6.QtWidgets import (QButtonGroup, QComboBox, QDialog, QFrame, QGridLayout, QHBoxLayout,
                               QInputDialog, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget)

import dsp
from gui_icons import set_icon
from gui_layout import fit_to_screen

F_MIN, F_MAX = 20.0, 20000.0
EQ_RANGE = 18.0                       # шкала усиления эквалайзера, ±dB
SPEC_TOP, SPEC_BOTTOM = 6.0, -84.0    # шкала анализатора, dB (с наклоном 4.5 dB/окт, как в Pro-Q)
SPEC_TILT = 4.5
BAND_COLORS = ["#ff9f43", "#f7c948", "#36d399", "#50a0ff"]
MB_COLORS = ["#ff7a59", "#f7c948", "#50a0ff"]
HPF_COLOR = "#c3cbd3"
TEXT, DIM, DEEP = "#e6eaee", "#7f8b97", "#0b0d10"

PLUGIN_STYLE = """
QDialog#plugin { background: #15191d; }
#pluginHeader { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #252c33, stop:1 #1a1f24);
                border-bottom: 1px solid #2c333b; }
#pluginHeader QLabel { background: transparent; }
QLabel#pluginName { font-size: 17px; font-weight: 800; letter-spacing: 2px; color: #e6eaee; }
QLabel#pluginTrack { color: #7f8b97; font-size: 13px; }
#pluginPanel { background: qlineargradient(x1:0, y1:0, x2:0, y2:1, stop:0 #1c2126, stop:1 #131619); }
#bandBox { background: #191e23; border: 1px solid #262d34; border-radius: 10px; }
#bandBox[selected="true"] { border: 1px solid #5b6672; background: #1d2329; }
#bandBox QLabel, #pluginPanel QLabel { background: transparent; }
QLabel#bandTitle { font-weight: 800; font-size: 13px; letter-spacing: 1px; }
QLabel#bandSub { color: #7f8b97; font-size: 11px; }
QPushButton#powerBtn { border-radius: 15px; padding: 0px; min-width: 30px; max-width: 30px;
                       min-height: 30px; max-height: 30px; background: #2a1316; border: 1px solid #5a2228; }
QPushButton#powerBtn:checked { background: #173a2e; border: 1px solid #36d399; }
QPushButton#abBtn { min-width: 28px; padding: 4px 6px; font-weight: 800; }
QPushButton#shapeBtn { padding: 0px; min-width: 26px; max-width: 26px; min-height: 22px; border-radius: 5px;
                       background: #12161a; }
QPushButton#shapeBtn:checked { background: #22303a; border: 1px solid #5b6672; }
QPushButton#tinyBtn { padding: 1px 6px; min-height: 18px; font-size: 11px; font-weight: 800; border-radius: 5px;
                      background: #12161a; color: #7f8b97; }
QPushButton#tinyBtn:checked { background: #173a2e; color: #36d399; border: 1px solid #36d399; }
QPushButton#soloBtnFx:checked { background: #f7c948; color: #1b1705; border: 1px solid #f7c948; }
"""


def _fmt_hz(v: float) -> str:
    return f"{v / 1000:.2f} кГц" if v >= 1000 else f"{v:.0f} Гц"


def _fmt_axis(f: float) -> str:
    return f"{f / 1000:g}k" if f >= 1000 else f"{f:g}"


# ══════════════════════════════════════════════════════════════════════════════
# Крутилка
# ══════════════════════════════════════════════════════════════════════════════
class Knob(QWidget):
    """Поворотный регулятор. value — в единицах параметра; log — логарифмическая шкала
    (частота, время); bipolar — дуга от середины (усиление ±)."""

    valueChanged = Signal(float)

    def __init__(self, name: str, lo: float, hi: float, default: float, fmt: Callable[[float], str],
                 log: bool = False, bipolar: bool = False, step: float = 0.0, color: str = "#36d399",
                 size: int = 50, parent=None):
        super().__init__(parent)
        self.name, self.lo, self.hi, self.default = name, lo, hi, default
        self.fmt, self.log, self.bipolar, self.step = fmt, log, bipolar, step
        self.color = QColor(color)
        self.knob = size
        self.value = default
        self._drag: Optional[tuple[float, float]] = None
        self.setFixedSize(max(66, size + 18), size + 36)
        self.setCursor(Qt.SizeVerCursor)
        self.setToolTip(f"{name}: тяните вверх-вниз (Shift — точно), колесо мыши.\n"
                        "Двойной клик — по умолчанию, правый клик — ввести число")

    # ── шкала ──────────────────────────────────────────────────────────────
    def _norm(self, v: float) -> float:
        if self.log:
            return math.log(max(v, self.lo) / self.lo) / math.log(self.hi / self.lo)
        return (v - self.lo) / (self.hi - self.lo)

    def _from_norm(self, n: float) -> float:
        n = min(1.0, max(0.0, n))
        v = self.lo * (self.hi / self.lo) ** n if self.log else self.lo + n * (self.hi - self.lo)
        if self.step:
            v = round(v / self.step) * self.step
        return min(self.hi, max(self.lo, v))

    def set_value(self, v: float, emit: bool = False) -> None:
        v = min(self.hi, max(self.lo, float(v)))
        if v != self.value:
            self.value = v
            self.update()
            if emit:
                self.valueChanged.emit(v)

    # ── мышь ───────────────────────────────────────────────────────────────
    def mousePressEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.LeftButton and self.isEnabled():
            self._drag = (e.position().y(), self._norm(self.value))
        elif e.button() == Qt.RightButton and self.isEnabled():
            dec = 0 if (self.step or 0) >= 1 else 2
            v, ok = QInputDialog.getDouble(self, self.name, f"{self.name}:", self.value, self.lo, self.hi, dec)
            if ok:
                self.set_value(v, emit=True)

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        if self._drag is None:
            return
        y0, n0 = self._drag
        sens = 0.0008 if e.modifiers() & Qt.ShiftModifier else 0.005
        self.set_value(self._from_norm(n0 + (y0 - e.position().y()) * sens), emit=True)

    def mouseReleaseEvent(self, _e) -> None:  # noqa: N802
        self._drag = None

    def mouseDoubleClickEvent(self, _e) -> None:  # noqa: N802
        if self.isEnabled():
            self.set_value(self.default, emit=True)

    def wheelEvent(self, e) -> None:  # noqa: N802
        if not self.isEnabled():
            return
        d = 1 if e.angleDelta().y() > 0 else -1
        k = 0.004 if e.modifiers() & Qt.ShiftModifier else 0.02
        n = self._norm(self.value) + d * k
        v = self._from_norm(n)
        if v == self.value and self.step:          # шаг крупнее сдвига — всё равно сдвинуться
            v = min(self.hi, max(self.lo, self.value + d * self.step))
        self.set_value(v, emit=True)
        e.accept()

    # ── рисование ──────────────────────────────────────────────────────────
    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w = self.width()
        enabled = self.isEnabled()
        small = QFont(self.font())
        small.setPixelSize(11)
        small.setBold(True)
        p.setFont(small)
        p.setPen(QColor(DIM))
        p.drawText(QRectF(0, 0, w, 14), Qt.AlignCenter, self.name.upper())

        r = self.knob / 2.0
        cx, cy = w / 2.0, 16 + r
        arc = QRectF(cx - r + 3, cy - r + 3, 2 * r - 6, 2 * r - 6)
        p.setPen(QPen(QColor("#262d35"), 4, Qt.SolidLine, Qt.RoundCap))
        p.drawArc(arc, 225 * 16, -270 * 16)
        n = self._norm(self.value)
        n0 = self._norm(0.0) if self.bipolar and self.lo < 0 < self.hi else 0.0
        a0, a1 = 225 - 270 * n0, 225 - 270 * n
        color = QColor(self.color) if enabled else QColor("#3a434d")
        if abs(a1 - a0) > 0.5:
            p.setPen(QPen(color, 4, Qt.SolidLine, Qt.RoundCap))
            p.drawArc(arc, int(a0 * 16), int((a1 - a0) * 16))
        # «шляпка» крутилки
        rb = r - 9
        grad = QRadialGradient(QPointF(cx - rb * 0.4, cy - rb * 0.5), rb * 1.6)
        grad.setColorAt(0.0, QColor("#46505b"))
        grad.setColorAt(0.6, QColor("#242a31"))
        grad.setColorAt(1.0, QColor("#15191d"))
        p.setBrush(grad)
        p.setPen(QPen(QColor("#07090b"), 1.2))
        p.drawEllipse(QPointF(cx, cy), rb, rb)
        ang = math.radians(a1)
        p.setPen(QPen(QColor(TEXT) if enabled else QColor("#56616c"), 2.4, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(cx + math.cos(ang) * rb * 0.3, cy - math.sin(ang) * rb * 0.3),
                   QPointF(cx + math.cos(ang) * rb * 0.88, cy - math.sin(ang) * rb * 0.88))
        val = QFont(self.font())
        val.setPixelSize(12)
        p.setFont(val)
        p.setPen(QColor(TEXT) if enabled else QColor("#56616c"))
        p.drawText(QRectF(0, cy + r + 1, w, 16), Qt.AlignCenter, self.fmt(self.value))
        p.end()


# ══════════════════════════════════════════════════════════════════════════════
# Индикаторы
# ══════════════════════════════════════════════════════════════════════════════
class PeakMeter(QWidget):
    """Вертикальный пиковый индикатор -48…+6 dB с удержанием пика."""

    LO, HI = -48.0, 6.0

    def __init__(self, label: str, parent=None):
        super().__init__(parent)
        self.label = label
        self.db = self.LO
        self.hold = self.LO
        self._hold_t = 0
        self.setFixedWidth(22)
        self.setMinimumHeight(90)

    def push(self, peak: float) -> None:
        db = 20 * math.log10(peak) if peak > 1e-6 else self.LO
        self.db = db if db > self.db else max(self.LO, self.db - 1.2)
        if db >= self.hold:
            self.hold, self._hold_t = db, 30
        elif self._hold_t > 0:
            self._hold_t -= 1
        else:
            self.hold = max(self.LO, self.hold - 0.8)
        self.update()

    def _y(self, db: float, top: float, h: float) -> float:
        return top + h * (self.HI - min(self.HI, max(self.LO, db))) / (self.HI - self.LO)

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        top, h = 2.0, self.height() - 18.0
        bar = QRectF(6, top, 10, h)
        p.fillRect(bar, QColor(DEEP))
        y = self._y(self.db, top, h)
        if y < top + h:
            g = QLinearGradient(0, top, 0, top + h)
            g.setColorAt(0.0, QColor("#ef4b55"))
            g.setColorAt(self._frac(0.0), QColor("#ef4b55"))
            g.setColorAt(self._frac(-6.0), QColor("#f7c948"))
            g.setColorAt(self._frac(-18.0), QColor("#36d399"))
            g.setColorAt(1.0, QColor("#1f7a58"))
            p.fillRect(QRectF(bar.left(), y, bar.width(), top + h - y), QBrush(g))
        hy = self._y(self.hold, top, h)
        p.fillRect(QRectF(bar.left(), hy - 1, bar.width(), 2), QColor(TEXT if self.hold < 0 else "#ef4b55"))
        p.setPen(QColor("#56616c"))
        zero = self._y(0.0, top, h)
        p.drawLine(QPointF(2, zero), QPointF(5, zero))
        f = QFont(self.font())
        f.setPixelSize(10)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(DIM))
        p.drawText(QRectF(0, self.height() - 15, self.width(), 14), Qt.AlignCenter, self.label)
        p.end()

    def _frac(self, db: float) -> float:
        return (self.HI - db) / (self.HI - self.LO)


class GrMeter(QWidget):
    """Вертикальный индикатор ослабления полосы компрессора (0…24 dB, растёт вниз)."""

    MAX_DB = 24.0

    def __init__(self, color: str, parent=None):
        super().__init__(parent)
        self.color = QColor(color)
        self.value = 0.0
        self.setFixedWidth(16)
        self.setMinimumHeight(90)
        self.setToolTip("Ослабление полосы (GR), dB")

    def set_value(self, db: float) -> None:
        self.value = db if db > self.value else self.value * 0.8 + db * 0.2
        self.update()

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        h = self.height() - 16
        p.fillRect(QRectF(3, 0, 10, h), QColor(DEEP))
        fh = h * min(1.0, self.value / self.MAX_DB)
        if fh > 0.5:
            p.fillRect(QRectF(3, 0, 10, fh), self.color)
        p.setPen(QColor("#2c333b"))
        for db in (3, 6, 12, 18):
            y = h * db / self.MAX_DB
            p.drawLine(QPointF(0, y), QPointF(2, y))
        f = QFont(self.font())
        f.setPixelSize(10)
        f.setBold(True)
        p.setFont(f)
        p.setPen(QColor(DIM))
        p.drawText(QRectF(-6, h + 1, self.width() + 12, 14), Qt.AlignCenter, "GR")
        p.end()


# ══════════════════════════════════════════════════════════════════════════════
# Анализатор спектра
# ══════════════════════════════════════════════════════════════════════════════
class Spectrum:
    """Сглаженный спектр для рисования: FFT 4096 с окном Ханна, наклон 4.5 dB/окт,
    «падение» 1.2 dB за кадр — как у анализаторов в студийных эквалайзерах."""

    def __init__(self, points: int = 160):
        self.freqs = F_MIN * (F_MAX / F_MIN) ** np.linspace(0, 1, points)
        self.pre = np.full(points, SPEC_BOTTOM)
        self.post = np.full(points, SPEC_BOTTOM)
        self._win = np.hanning(4096).astype(np.float32)

    def _calc(self, x: np.ndarray, sr: float) -> np.ndarray:
        n = len(x)
        win = self._win if n == len(self._win) else np.hanning(n).astype(np.float32)
        mag = np.abs(np.fft.rfft(x * win)) * 2.0 / win.sum()
        db = 20 * np.log10(mag + 1e-9)
        bins = np.arange(len(db)) * sr / n
        f = self.freqs
        edges = np.searchsorted(bins, f * 2 ** (-1 / 24))
        edges = np.clip(edges, 0, len(db) - 1)
        peak = np.maximum.reduceat(db, edges)
        nxt = np.append(edges[1:], len(db))
        sparse = (nxt - edges) <= 1
        out = np.where(sparse, np.interp(f, bins, db), peak)
        return out + SPEC_TILT * np.log2(f / 1000.0)

    def update(self, fx, sr: float) -> None:
        if fx is None:
            return
        pre, post = fx.recent(4096)
        for attr, x in (("pre", pre), ("post", post)):
            new = self._calc(x, sr)
            old = getattr(self, attr)
            setattr(self, attr, np.maximum(new, old - 1.2))


# ══════════════════════════════════════════════════════════════════════════════
# Общая рамка плагина
# ══════════════════════════════════════════════════════════════════════════════
class PluginWindow(QDialog):
    """Окно плагина: шапка (питание, название, A/B, пресеты) + тело + таймер индикаторов."""

    PART = ""                 # "eq" | "comp"
    TITLE = ""
    PRESETS: dict = {}

    def __init__(self, track: str, settings: dict, samplerate: float, apply: Callable[[dict], None],
                 fx_getter: Callable[[], object], parent=None):
        super().__init__(parent)
        self.setObjectName("plugin")
        self.setStyleSheet(PLUGIN_STYLE)
        self.setWindowTitle(f"{self.TITLE} — {track}")
        self.sr = float(samplerate or 48000)
        self.apply = apply
        self.fx_getter = fx_getter
        self.part = dsp.normalize(settings)[self.PART]
        self.slots = {"A": None, "B": None}
        self.slot = "A"
        self.spectrum = Spectrum()

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        header = QFrame()
        header.setObjectName("pluginHeader")
        hl = QHBoxLayout(header)
        hl.setContentsMargins(10, 6, 10, 6)
        self.power = QPushButton()
        self.power.setObjectName("powerBtn")
        self.power.setCheckable(True)
        set_icon(self.power, "power", 16, color="#ef4b55", on_color="#36d399")
        self.power.setToolTip("Включить / выключить плагин")
        self.power.toggled.connect(self._on_power)
        hl.addWidget(self.power)
        name = QLabel(self.TITLE.upper())
        name.setObjectName("pluginName")
        hl.addWidget(name)
        sub = QLabel(f"·  {track}")
        sub.setObjectName("pluginTrack")
        hl.addWidget(sub, 1)
        self.ab = {}
        group = QButtonGroup(self)
        for key in ("A", "B"):
            b = QPushButton(key)
            b.setObjectName("abBtn")
            b.setCheckable(True)
            b.setToolTip("Сравнение A/B: две версии настроек, переключайтесь и слушайте")
            group.addButton(b)
            b.clicked.connect(lambda _=False, k=key: self._switch_slot(k))
            hl.addWidget(b)
            self.ab[key] = b
        self.ab["A"].setChecked(True)
        copy_btn = QPushButton("A→B")
        copy_btn.setObjectName("abBtn")
        copy_btn.setToolTip("Скопировать текущие настройки во вторую ячейку")
        copy_btn.clicked.connect(self._copy_slot)
        hl.addWidget(copy_btn)
        hl.addSpacing(12)
        self.preset = QComboBox()
        self.preset.addItem("Пресеты…", "")
        for n in self.PRESETS:
            self.preset.addItem(n, n)
        self.preset.activated.connect(self._apply_preset)
        hl.addWidget(self.preset)
        root.addWidget(header)

        # нет scipy — обработка не работает: объясняем и даём установить прямо отсюда
        self.banner = QFrame()
        self.banner.setStyleSheet("QFrame{background:#3a2a10;border-bottom:1px solid #6b5a1c}"
                                  "QLabel{background:transparent;color:#f7c948;font-weight:700}")
        bl = QHBoxLayout(self.banner)
        bl.setContentsMargins(12, 6, 12, 6)
        self.banner_text = QLabel()
        self.banner_text.setWordWrap(True)
        bl.addWidget(self.banner_text, 1)
        self.install_btn = QPushButton("Установить")
        self.install_btn.clicked.connect(self._install)
        bl.addWidget(self.install_btn)
        root.addWidget(self.banner)
        INSTALLER.finished.connect(self._on_installed)
        self._update_banner()

        self.body = QVBoxLayout()
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(0)
        root.addLayout(self.body, 1)

        self._timer = QTimer(self)
        self._timer.setInterval(33)
        self._timer.timeout.connect(self._tick)

    # ── настройки ─────────────────────────────────────────────────────────
    def changed(self) -> None:
        """Пользователь что-то покрутил → обработка и конфиг дорожки."""
        self.apply(copy.deepcopy(self.part))

    def set_settings(self, settings: dict) -> None:
        fresh = dsp.normalize(settings)[self.PART]
        self.part.clear()
        self.part.update(fresh)
        self.refresh()

    def refresh(self) -> None:
        """Показать self.part на всех регуляторах (переопределяется)."""
        self.power.blockSignals(True)
        self.power.setChecked(bool(self.part.get("on")))
        self.power.blockSignals(False)

    def _on_power(self, on: bool) -> None:
        self.part["on"] = on
        self.changed()
        if hasattr(self, "graph"):
            self.graph.update()

    def _replace(self, new: dict) -> None:
        self.part.clear()
        self.part.update(copy.deepcopy(new))
        self.refresh()
        self.changed()

    def _apply_preset(self, _i: int) -> None:
        name = self.preset.currentData()
        self.preset.setCurrentIndex(0)
        if name:
            new = copy.deepcopy(self.PRESETS[name])
            new["on"] = True
            self._replace(new)

    def _switch_slot(self, key: str) -> None:
        if key == self.slot:
            return
        self.slots[self.slot] = copy.deepcopy(self.part)
        self.slot = key
        other = self.slots.get(key)
        if other is not None:
            self._replace(other)

    def _copy_slot(self) -> None:
        other = "B" if self.slot == "A" else "A"
        self.slots[other] = copy.deepcopy(self.part)

    # ── установка scipy ──────────────────────────────────────────────────
    def _update_banner(self) -> None:
        if dsp.DSP_AVAILABLE:
            self.banner.hide()
            return
        self.banner.show()
        if INSTALLER.running:
            self.banner_text.setText("Устанавливаю библиотеку scipy… (1–2 минуты, нужен интернет)")
            self.install_btn.setEnabled(False)
        else:
            self.banner_text.setText("Звук пока НЕ обрабатывается: не установлена библиотека scipy. "
                                     "Нажмите «Установить» (нужен интернет, ~40 МБ) — перезапуск не нужен. "
                                     "Настройки можно крутить уже сейчас, они сохранятся.")
            self.install_btn.setEnabled(True)

    def _install(self) -> None:
        INSTALLER.start()
        self._update_banner()

    def _on_installed(self, ok: bool, message: str) -> None:
        if ok:
            fx = self.fx()
            if fx is not None:
                fx.update(fx.settings)
            self.banner.hide()
        else:
            self.banner.show()
            self.banner_text.setText(f"Не получилось установить scipy: {message}\n"
                                     "Закройте программу и запустите install.bat.")
            self.install_btn.setEnabled(True)

    # ── индикаторы / анализатор ──────────────────────────────────────────
    def fx(self):
        return self.fx_getter()

    def showEvent(self, e) -> None:  # noqa: N802
        self._update_banner()
        fx = self.fx()
        if fx is not None:
            fx.analyze = True
        self._timer.start()
        super().showEvent(e)

    def closeEvent(self, e) -> None:  # noqa: N802
        self._timer.stop()
        fx = self.fx()
        if fx is not None:
            fx.analyze = False
        super().closeEvent(e)

    def hideEvent(self, e) -> None:  # noqa: N802
        self._timer.stop()
        fx = self.fx()
        if fx is not None:
            fx.analyze = False
        super().hideEvent(e)

    def _tick(self) -> None:
        fx = self.fx()
        if fx is not None:
            fx.analyze = True
            self.spectrum.update(fx, self.sr)
            pin, pout = fx.take_peaks()
            self.meter_in.push(pin)
            self.meter_out.push(pout)
        self.tick_extra(fx)
        self.graph.update()

    def tick_extra(self, fx) -> None:
        pass

    def _meters(self) -> QWidget:
        w = QWidget()
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        self.meter_in = PeakMeter("IN")
        self.meter_out = PeakMeter("OUT")
        lay.addWidget(self.meter_in)
        lay.addWidget(self.meter_out)
        return w


class _Installer(QObject):
    """Установка scipy в то же окружение Python, из которого запущена программа.
    Один процесс на всю программу (сколько бы окон плагинов ни было открыто)."""

    finished = Signal(bool, str)

    def __init__(self):
        super().__init__()
        self.proc: Optional[QProcess] = None

    @property
    def running(self) -> bool:
        return self.proc is not None

    @staticmethod
    def python_exe() -> str:
        exe = Path(sys.executable)
        console = exe.with_name("python.exe")      # программа запущена через pythonw.exe
        return str(console) if exe.name.lower() == "pythonw.exe" and console.exists() else str(exe)

    def start(self) -> None:
        if self.proc is not None or dsp.DSP_AVAILABLE:
            return
        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.MergedChannels)
        self.proc.finished.connect(self._done)
        self.proc.errorOccurred.connect(lambda _e: self._done(-1, None))
        self.proc.start(self.python_exe(), ["-m", "pip", "install", "scipy>=1.10",
                                            "--disable-pip-version-check"])

    def _done(self, code, _status) -> None:
        proc, self.proc = self.proc, None
        if proc is None:
            return
        out = bytes(proc.readAll()).decode("utf-8", "replace").strip()
        ok = code == 0 and dsp.try_enable()
        tail = out.splitlines()[-1] if out else (proc.errorString() if code == -1 else f"код {code}")
        self.finished.emit(ok, "" if ok else (dsp.DSP_ERROR if code == 0 else tail))


INSTALLER = _Installer()


def _box(title: str, color: str, sub: str = "") -> tuple[QFrame, QVBoxLayout, QLabel]:
    box = QFrame()
    box.setObjectName("bandBox")
    v = QVBoxLayout(box)
    v.setContentsMargins(8, 6, 8, 6)
    v.setSpacing(2)
    head = QLabel(title)
    head.setObjectName("bandTitle")
    head.setStyleSheet(f"color:{color}")
    head.setAlignment(Qt.AlignCenter)
    v.addWidget(head)
    sub_lbl = QLabel(sub)
    sub_lbl.setObjectName("bandSub")
    sub_lbl.setAlignment(Qt.AlignCenter)
    v.addWidget(sub_lbl)
    return box, v, sub_lbl


def _repolish(w) -> None:
    w.style().unpolish(w)
    w.style().polish(w)


# ══════════════════════════════════════════════════════════════════════════════
# Эквалайзер
# ══════════════════════════════════════════════════════════════════════════════
class EqGraph(QWidget):
    """АЧХ эквалайзера поверх анализатора спектра; точки полос тянутся мышью."""

    selected = Signal(object)          # 0..3 или "hpf"
    edited = Signal()
    R = 9

    def __init__(self, win: "EqDialog"):
        super().__init__(win)
        self.win = win
        self._drag = None
        self.sel = None
        self.setMinimumHeight(220)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)

    @property
    def eq(self) -> dict:
        return self.win.part

    def _plot(self) -> QRectF:
        return QRectF(36, 10, max(10, self.width() - 72), max(10, self.height() - 30))

    def _x(self, f: float) -> float:
        r = self._plot()
        return r.left() + r.width() * math.log(max(f, F_MIN) / F_MIN) / math.log(F_MAX / F_MIN)

    def _f(self, x: float) -> float:
        r = self._plot()
        return F_MIN * (F_MAX / F_MIN) ** min(1.0, max(0.0, (x - r.left()) / r.width()))

    def _y(self, g: float) -> float:
        r = self._plot()
        return r.center().y() - r.height() / 2 * max(-1.15, min(1.15, g / EQ_RANGE))

    def _g(self, y: float) -> float:
        r = self._plot()
        return max(-EQ_RANGE, min(EQ_RANGE, (r.center().y() - y) / (r.height() / 2) * EQ_RANGE))

    def _sy(self, db: np.ndarray) -> np.ndarray:
        r = self._plot()
        return r.top() + r.height() * (SPEC_TOP - np.clip(db, SPEC_BOTTOM, SPEC_TOP)) / (SPEC_TOP - SPEC_BOTTOM)

    def handles(self) -> list:
        sr = self.win.sr
        hp_y = float(dsp.eq_response(self.eq, np.array([self.eq["hpf"]]), sr)[0])
        pts = [("hpf", QPointF(self._x(self.eq["hpf"]), self._y(max(-EQ_RANGE, hp_y))))]
        for i, b in enumerate(self.eq["bands"]):
            pts.append((i, QPointF(self._x(b["freq"]), self._y(b["gain"]))))
        return pts

    def _hit(self, pos, radius: float = 14.0):
        best, dist = None, radius
        for key, pt in self.handles():
            d = math.hypot(pt.x() - pos.x(), pt.y() - pos.y())
            if d <= dist:
                best, dist = key, d
        return best

    def mousePressEvent(self, e) -> None:  # noqa: N802
        key = self._hit(e.position())
        if key is not None:
            self.sel = key
            self.selected.emit(key)
            if e.button() == Qt.LeftButton:
                self._drag = key
        self.update()

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        pos = e.position()
        if self._drag is None:
            self.setCursor(Qt.PointingHandCursor if self._hit(pos) is not None else Qt.ArrowCursor)
            return
        f = float(round(self._f(pos.x())))
        if self._drag == "hpf":
            self.eq["hpf"] = min(500.0, max(20.0, f))
            self.eq["hpf_on"] = True
        else:
            b = self.eq["bands"][self._drag]
            b["freq"] = f
            b["gain"] = round(self._g(pos.y()) * 2) / 2
        self.edited.emit()

    def mouseReleaseEvent(self, _e) -> None:  # noqa: N802
        self._drag = None

    def mouseDoubleClickEvent(self, e) -> None:  # noqa: N802
        key = self._hit(e.position())
        if key == "hpf":
            self.eq["hpf_on"] = not self.eq["hpf_on"]
        elif key is not None:
            self.eq["bands"][key]["gain"] = 0.0
        else:
            return
        self.edited.emit()

    def wheelEvent(self, e) -> None:  # noqa: N802
        key = self._hit(e.position(), 40.0)
        if key is None or key == "hpf":
            return
        b = self.eq["bands"][key]
        b["q"] = round(min(10.0, max(0.2, b["q"] * (1.12 if e.angleDelta().y() > 0 else 1 / 1.12))), 2)
        self.edited.emit()

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self._plot()
        bg = QLinearGradient(0, 0, 0, self.height())
        bg.setColorAt(0, QColor("#11151a"))
        bg.setColorAt(1, QColor("#0a0c0f"))
        p.fillRect(self.rect(), QBrush(bg))
        f = QFont(self.font())
        f.setPixelSize(10)
        p.setFont(f)
        # сетка
        for fr in (30, 50, 100, 200, 300, 500, 1000, 2000, 3000, 5000, 10000, 20000):
            x = self._x(fr)
            major = fr in (100, 1000, 10000)
            p.setPen(QPen(QColor("#222a32" if major else "#181d23"), 1))
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
            p.setPen(QColor("#56616c"))
            p.drawText(QRectF(x - 20, r.bottom() + 3, 40, 14), Qt.AlignCenter, _fmt_axis(fr))
        for g in (-12, -6, 0, 6, 12):
            y = self._y(g)
            p.setPen(QPen(QColor("#2c333b" if g == 0 else "#181d23"), 1))
            p.drawLine(QPointF(r.left(), y), QPointF(r.right(), y))
            p.setPen(QColor("#56616c"))
            p.drawText(QRectF(0, y - 7, 32, 14), Qt.AlignRight | Qt.AlignVCenter, f"{g:+d}" if g else "0")
        for db in (-12, -24, -36, -48, -60, -72):
            y = float(self._sy(np.array([db]))[0])
            p.drawText(QRectF(r.right() + 4, y - 7, 34, 14), Qt.AlignLeft | Qt.AlignVCenter, f"{db}")
        # анализатор: до обработки — серая заливка, после — цветная линия
        sp = self.win.spectrum
        xs = np.array([self._x(fq) for fq in sp.freqs])
        for data, fill, line in ((sp.pre, QColor(120, 132, 145, 45), None),
                                 (sp.post, QColor(54, 211, 153, 28), QColor(54, 211, 153, 140))):
            ys = self._sy(data)
            path = QPainterPath(QPointF(xs[0], r.bottom()))
            for x, y in zip(xs, ys):
                path.lineTo(QPointF(x, y))
            path.lineTo(QPointF(xs[-1], r.bottom()))
            path.closeSubpath()
            p.fillPath(path, QBrush(fill))
            if line is not None:
                ln = QPainterPath(QPointF(xs[0], ys[0]))
                for x, y in zip(xs[1:], ys[1:]):
                    ln.lineTo(QPointF(x, y))
                p.setPen(QPen(line, 1))
                p.setBrush(Qt.NoBrush)
                p.drawPath(ln)
        # вклад выбранной полосы — подсветка
        on = bool(self.eq.get("on"))
        freqs = F_MIN * (F_MAX / F_MIN) ** np.linspace(0, 1, max(80, int(r.width() / 2)))
        cx = np.array([self._x(fq) for fq in freqs])
        if isinstance(self.sel, int):
            b = self.eq["bands"][self.sel]
            single = dsp.sos_response(np.array([dsp.biquad(b["type"], b["freq"], self.win.sr, b["gain"], b["q"])]),
                                      freqs, self.win.sr)
            path = QPainterPath(QPointF(cx[0], self._y(0)))
            for x, g in zip(cx, single):
                path.lineTo(QPointF(x, self._y(float(g))))
            path.lineTo(QPointF(cx[-1], self._y(0)))
            path.closeSubpath()
            c = QColor(BAND_COLORS[self.sel])
            c.setAlpha(55)
            p.fillPath(path, QBrush(c))
        # общая АЧХ
        resp = dsp.eq_response(self.eq, freqs, self.win.sr)
        curve = QPainterPath(QPointF(cx[0], self._y(float(resp[0]))))
        for x, g in zip(cx[1:], resp[1:]):
            curve.lineTo(QPointF(x, max(r.top() - 6, min(r.bottom() + 6, self._y(float(g))))))
        p.setPen(QPen(QColor("#f2f5f7") if on else QColor("#56616c"), 2.2))
        p.setBrush(Qt.NoBrush)
        p.drawPath(curve)
        # точки
        fb = QFont(self.font())
        fb.setPixelSize(11)
        fb.setBold(True)
        p.setFont(fb)
        for key, pt in self.handles():
            color = QColor(HPF_COLOR if key == "hpf" else BAND_COLORS[key])
            active = self.eq["hpf_on"] if key == "hpf" else True
            if key == self.sel:
                halo = QColor(color)
                halo.setAlpha(60)
                p.setPen(Qt.NoPen)
                p.setBrush(halo)
                p.drawEllipse(pt, self.R + 6, self.R + 6)
            fill = QColor(color)
            fill.setAlpha(230 if active else 40)
            p.setBrush(fill)
            p.setPen(QPen(color.darker(160), 1.5))
            p.drawEllipse(pt, self.R, self.R)
            p.setPen(QColor(DEEP) if active else color)
            p.drawText(QRectF(pt.x() - 10, pt.y() - 8, 20, 16), Qt.AlignCenter,
                       "HP" if key == "hpf" else str(key + 1))
        if not on:
            p.setPen(QColor("#ef4b55"))
            p.drawText(r.adjusted(0, 4, -8, 0), Qt.AlignTop | Qt.AlignRight, "BYPASS — плагин выключен")
        p.end()


class EqDialog(PluginWindow):
    PART = "eq"
    TITLE = "CD Pro EQ"
    PRESETS = dsp.EQ_PRESETS

    def __init__(self, track: str, settings: dict, samplerate: float, apply, fx_getter, parent=None):
        super().__init__(track, settings, samplerate, apply, fx_getter, parent)
        self.graph = EqGraph(self)
        self.graph.edited.connect(self._from_graph)
        self.graph.selected.connect(self._select)
        self.body.addWidget(self.graph, 1)

        panel = QFrame()
        panel.setObjectName("pluginPanel")
        pl = QHBoxLayout(panel)
        pl.setContentsMargins(10, 8, 10, 8)
        pl.setSpacing(8)

        # срез низа
        box, v, _ = _box("LOW CUT", HPF_COLOR, "24 dB/окт")
        self.hpf_on = QPushButton("ВКЛ")
        self.hpf_on.setObjectName("tinyBtn")
        self.hpf_on.setCheckable(True)
        self.hpf_on.toggled.connect(self._from_controls)
        v.addWidget(self.hpf_on, 0, Qt.AlignCenter)
        self.hpf_knob = Knob("Частота", 20, 500, 80, _fmt_hz, log=True, step=1, color=HPF_COLOR)
        self.hpf_knob.valueChanged.connect(self._from_controls)
        v.addWidget(self.hpf_knob, 0, Qt.AlignCenter)
        v.addStretch(1)
        pl.addWidget(box)
        self.boxes = {"hpf": box}

        # 4 полосы
        self.bands = []
        for i in range(4):
            box, v, _ = _box(f"ПОЛОСА {i + 1}", BAND_COLORS[i])
            shapes = QHBoxLayout()
            shapes.setSpacing(2)
            group = QButtonGroup(box)
            shape_btns = {}
            for kind, tip in (("lowshelf", "Полка НЧ"), ("peak", "Колокол"), ("highshelf", "Полка ВЧ")):
                b = QPushButton()
                b.setObjectName("shapeBtn")
                b.setCheckable(True)
                b.setToolTip(tip)
                set_icon(b, kind, 18, color=DIM, on_color=BAND_COLORS[i])
                group.addButton(b)
                b.clicked.connect(lambda _=False, k=i, t=kind: self._set_shape(k, t))
                shapes.addWidget(b)
                shape_btns[kind] = b
            v.addLayout(shapes)
            knobs = QGridLayout()
            knobs.setSpacing(0)
            freq = Knob("Частота", 20, 20000, dsp.default_eq()["bands"][i]["freq"], _fmt_hz, log=True,
                        step=1, color=BAND_COLORS[i])
            gain = Knob("Усиление", -EQ_RANGE, EQ_RANGE, 0.0, lambda x: f"{x:+.1f} dB", bipolar=True,
                        step=0.1, color=BAND_COLORS[i])
            q = Knob("Q", 0.2, 10.0, 1.0, lambda x: f"{x:.2f}", log=True, step=0.01, color=BAND_COLORS[i],
                     size=40)
            for k in (freq, gain, q):
                k.valueChanged.connect(self._from_controls)
            knobs.addWidget(freq, 0, 0)
            knobs.addWidget(gain, 0, 1)
            knobs.addWidget(q, 1, 0, 1, 2, Qt.AlignCenter)
            v.addLayout(knobs)
            pl.addWidget(box)
            self.boxes[i] = box
            self.bands.append((shape_btns, freq, gain, q))

        # выход
        box, v, _ = _box("ВЫХОД", TEXT)
        self.out_knob = Knob("Громкость", -18, 18, 0.0, lambda x: f"{x:+.1f} dB", bipolar=True, step=0.1,
                             color="#e6eaee")
        self.out_knob.valueChanged.connect(self._from_controls)
        v.addWidget(self.out_knob, 0, Qt.AlignCenter)
        v.addWidget(self._meters(), 1, Qt.AlignCenter)
        pl.addWidget(box)
        self.body.addWidget(panel)

        self.refresh()
        fit_to_screen(self, 1040, 680)

    # ── связь регуляторов и настроек ───────────────────────────────────────
    def refresh(self) -> None:
        super().refresh()
        e = self.part
        self.hpf_on.blockSignals(True)
        self.hpf_on.setChecked(bool(e["hpf_on"]))
        self.hpf_on.blockSignals(False)
        self.hpf_knob.set_value(e["hpf"])
        self.hpf_knob.setEnabled(bool(e["hpf_on"]))
        for (shapes, freq, gain, q), b in zip(self.bands, e["bands"]):
            for kind, btn in shapes.items():
                btn.blockSignals(True)
                btn.setChecked(kind == b["type"])
                btn.blockSignals(False)
            freq.set_value(b["freq"])
            gain.set_value(b["gain"])
            q.set_value(b["q"])
        self.out_knob.set_value(e.get("out", 0.0))
        self.graph.update()

    def _from_controls(self, *_a) -> None:
        e = self.part
        e["hpf_on"] = self.hpf_on.isChecked()
        e["hpf"] = self.hpf_knob.value
        self.hpf_knob.setEnabled(e["hpf_on"])
        for (_shapes, freq, gain, q), b in zip(self.bands, e["bands"]):
            b.update(freq=freq.value, gain=gain.value, q=q.value)
        e["out"] = self.out_knob.value
        self._auto_on()
        self.graph.update()
        self.changed()

    def _from_graph(self) -> None:
        self._auto_on()
        self.refresh()
        self.changed()

    def _auto_on(self) -> None:
        """Покрутили — значит, хотят слышать: плагин включается сам."""
        if not self.part.get("on"):
            self.part["on"] = True
            super().refresh()

    def _set_shape(self, band: int, kind: str) -> None:
        self.part["bands"][band]["type"] = kind
        self._select(band)
        self._auto_on()
        self.graph.update()
        self.changed()

    def _select(self, key) -> None:
        self.graph.sel = key
        for k, box in self.boxes.items():
            box.setProperty("selected", k == key)
            _repolish(box)
        self.graph.update()


# ══════════════════════════════════════════════════════════════════════════════
# Многополосный компрессор
# ══════════════════════════════════════════════════════════════════════════════
class MbGraph(QWidget):
    """Полосы компрессора по частоте: анализатор, разделы (тянутся мышью), ослабление полос."""

    edited = Signal()

    def __init__(self, win: "CompDialog"):
        super().__init__(win)
        self.win = win
        self.gr = [0.0, 0.0, 0.0]
        self._drag: Optional[int] = None
        self.setMinimumHeight(200)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMouseTracking(True)

    def _plot(self) -> QRectF:
        return QRectF(36, 10, max(10, self.width() - 46), max(10, self.height() - 30))

    def _x(self, f: float) -> float:
        r = self._plot()
        return r.left() + r.width() * math.log(max(f, F_MIN) / F_MIN) / math.log(F_MAX / F_MIN)

    def _f(self, x: float) -> float:
        r = self._plot()
        return F_MIN * (F_MAX / F_MIN) ** min(1.0, max(0.0, (x - r.left()) / r.width()))

    def _sy(self, db) -> np.ndarray:
        r = self._plot()
        return r.top() + r.height() * (SPEC_TOP - np.clip(db, SPEC_BOTTOM, SPEC_TOP)) / (SPEC_TOP - SPEC_BOTTOM)

    def _hit(self, x: float) -> Optional[int]:
        for i, f in enumerate(self.win.part["xover"]):
            if abs(self._x(f) - x) < 8:
                return i
        return None

    def mousePressEvent(self, e) -> None:  # noqa: N802
        self._drag = self._hit(e.position().x())

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        x = e.position().x()
        if self._drag is None:
            self.setCursor(Qt.SizeHorCursor if self._hit(x) is not None else Qt.ArrowCursor)
            return
        xo = self.win.part["xover"]
        f = float(round(self._f(x)))
        if self._drag == 0:
            xo[0] = min(max(40.0, f), xo[1] / 1.5)
        else:
            xo[1] = max(min(16000.0, f), xo[0] * 1.5)
        self.edited.emit()

    def mouseReleaseEvent(self, _e) -> None:  # noqa: N802
        self._drag = None

    def paintEvent(self, _e) -> None:  # noqa: N802
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self._plot()
        bg = QLinearGradient(0, 0, 0, self.height())
        bg.setColorAt(0, QColor("#11151a"))
        bg.setColorAt(1, QColor("#0a0c0f"))
        p.fillRect(self.rect(), QBrush(bg))
        f = QFont(self.font())
        f.setPixelSize(10)
        p.setFont(f)
        comp = self.win.part
        on = bool(comp.get("on"))
        edges = [r.left(), self._x(comp["xover"][0]), self._x(comp["xover"][1]), r.right()]
        bands = comp["bands"]
        any_solo = any(b.get("solo") for b in bands)
        # полосы: фон и ослабление (сверху вниз, шкала 0…24 dB на верхнюю половину)
        for i in range(3):
            c = QColor(MB_COLORS[i])
            audible = bands[i].get("solo") or not any_solo
            c.setAlpha(26 if audible else 8)
            p.fillRect(QRectF(edges[i], r.top(), edges[i + 1] - edges[i], r.height()), c)
            if on and bands[i]["on"]:
                gr = self.gr[i]
                h = r.height() * 0.5 * min(1.0, gr / 24.0)
                cg = QColor(MB_COLORS[i])
                cg.setAlpha(120)
                p.fillRect(QRectF(edges[i] + 1, r.top(), edges[i + 1] - edges[i] - 2, h), cg)
            p.setPen(QColor(MB_COLORS[i]))
            label = ("НЧ", "СЧ", "ВЧ")[i]
            text = f"{label}   −{self.gr[i]:.1f} dB" if on and bands[i]["on"] and self.gr[i] >= 0.1 else label
            p.drawText(QRectF(edges[i], r.bottom() - 18, edges[i + 1] - edges[i], 16), Qt.AlignCenter, text)
        # сетка частот
        for fr in (50, 100, 200, 500, 1000, 2000, 5000, 10000):
            x = self._x(fr)
            p.setPen(QPen(QColor("#1b2027"), 1))
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
            p.setPen(QColor("#56616c"))
            p.drawText(QRectF(x - 20, r.bottom() + 3, 40, 14), Qt.AlignCenter, _fmt_axis(fr))
        for db in (0, -12, -24, -36, -48, -60, -72):
            y = float(self._sy(np.array([db]))[0])
            p.setPen(QColor("#56616c"))
            p.drawText(QRectF(0, y - 7, 32, 14), Qt.AlignRight | Qt.AlignVCenter, f"{db}")
        # анализатор
        sp = self.win.spectrum
        xs = np.array([self._x(fq) for fq in sp.freqs])
        for data, fill, line in ((sp.pre, QColor(120, 132, 145, 40), None),
                                 (sp.post, QColor(230, 234, 238, 20), QColor(230, 234, 238, 150))):
            ys = self._sy(data)
            path = QPainterPath(QPointF(xs[0], r.bottom()))
            for x, y in zip(xs, ys):
                path.lineTo(QPointF(x, y))
            path.lineTo(QPointF(xs[-1], r.bottom()))
            path.closeSubpath()
            p.fillPath(path, QBrush(fill))
            if line is not None:
                ln = QPainterPath(QPointF(xs[0], ys[0]))
                for x, y in zip(xs[1:], ys[1:]):
                    ln.lineTo(QPointF(x, y))
                p.setPen(QPen(line, 1))
                p.setBrush(Qt.NoBrush)
                p.drawPath(ln)
        # разделы
        for i, fx in enumerate(comp["xover"]):
            x = self._x(fx)
            p.setPen(QPen(QColor("#e6eaee"), 2))
            p.drawLine(QPointF(x, r.top()), QPointF(x, r.bottom()))
            p.setBrush(QColor("#e6eaee"))
            p.setPen(Qt.NoPen)
            p.drawRoundedRect(QRectF(x - 5, r.top() + r.height() / 2 - 14, 10, 28), 4, 4)
            p.setPen(QColor("#e6eaee"))
            p.drawText(QRectF(x - 40, r.top() + 2, 80, 14), Qt.AlignCenter, _fmt_hz(fx))
        if not on:
            p.setPen(QColor("#ef4b55"))
            p.drawText(r.adjusted(0, 18, -8, 0), Qt.AlignTop | Qt.AlignRight, "BYPASS — плагин выключен")
        p.end()


class CompDialog(PluginWindow):
    PART = "comp"
    TITLE = "CD Multiband"
    PRESETS = dsp.COMP_PRESETS
    NAMES = ("НЧ", "СЧ", "ВЧ")

    def __init__(self, track: str, settings: dict, samplerate: float, apply, fx_getter, parent=None):
        super().__init__(track, settings, samplerate, apply, fx_getter, parent)
        self.graph = MbGraph(self)
        self.graph.edited.connect(self._from_graph)
        self.body.addWidget(self.graph, 1)

        panel = QFrame()
        panel.setObjectName("pluginPanel")
        pl = QHBoxLayout(panel)
        pl.setContentsMargins(10, 8, 10, 8)
        pl.setSpacing(8)
        self.bands = []
        for i in range(3):
            color = MB_COLORS[i]
            box, v, sub = _box(f"{self.NAMES[i]} ПОЛОСА", color)
            btns = QHBoxLayout()
            btns.setSpacing(3)
            on = QPushButton("СЖАТИЕ")
            on.setObjectName("tinyBtn")
            on.setCheckable(True)
            on.setToolTip("Сжимать эту полосу (выключено — полоса проходит как есть)")
            solo = QPushButton("S")
            solo.setObjectName("tinyBtn")
            solo.setCheckable(True)
            solo.setToolTip("Послушать только эту полосу")
            solo.setStyleSheet("QPushButton:checked{background:#f7c948;color:#1b1705;border:1px solid #f7c948}")
            on.toggled.connect(self._from_controls)
            solo.toggled.connect(self._from_controls)
            btns.addStretch(1)
            btns.addWidget(on)
            btns.addWidget(solo)
            btns.addStretch(1)
            v.addLayout(btns)
            row = QHBoxLayout()
            row.setSpacing(0)
            grid = QGridLayout()
            grid.setSpacing(0)
            thr = Knob("Порог", -60, 0, -20, lambda x: f"{x:.1f} dB", step=0.5, color=color)
            ratio = Knob("Степень", 1, 20, 2.5, lambda x: f"{x:.1f}:1", log=True, step=0.1, color=color)
            att = Knob("Атака", 0.1, 200, 15, lambda x: f"{x:.1f} мс" if x < 10 else f"{x:.0f} мс", log=True,
                       step=0.1, color=color, size=42)
            rel = Knob("Восст.", 10, 2000, 150, lambda x: f"{x:.0f} мс", log=True, step=1, color=color, size=42)
            gain = Knob("Усиление", -12, 24, 0, lambda x: f"{x:+.1f} dB", bipolar=True, step=0.1, color=color,
                        size=42)
            for k in (thr, ratio, att, rel, gain):
                k.valueChanged.connect(self._from_controls)
            grid.addWidget(thr, 0, 0)
            grid.addWidget(ratio, 0, 1)
            grid.addWidget(gain, 0, 2)
            grid.addWidget(att, 1, 0)
            grid.addWidget(rel, 1, 1)
            row.addLayout(grid)
            meter = GrMeter(color)
            row.addWidget(meter)
            v.addLayout(row)
            pl.addWidget(box)
            self.bands.append((on, solo, thr, ratio, att, rel, gain, meter, sub))

        box, v, _ = _box("ОБЩЕЕ", TEXT)
        grid = QGridLayout()
        grid.setSpacing(0)
        self.x1 = Knob("НЧ | СЧ", 40, 2000, 250, _fmt_hz, log=True, step=1, color="#e6eaee", size=42)
        self.x2 = Knob("СЧ | ВЧ", 500, 16000, 3000, _fmt_hz, log=True, step=10, color="#e6eaee", size=42)
        self.mix = Knob("Mix", 0, 100, 100, lambda x: f"{x:.0f} %", step=1, color="#e6eaee", size=42)
        self.mix.setToolTip("Параллельная компрессия: 100 % — только обработанный сигнал, "
                            "меньше — подмешивается исходный (звук плотнее, но живой)")
        self.out = Knob("Выход", -24, 24, 0, lambda x: f"{x:+.1f} dB", bipolar=True, step=0.1, color="#e6eaee",
                        size=42)
        for k in (self.x1, self.x2, self.mix, self.out):
            k.valueChanged.connect(self._from_controls)
        grid.addWidget(self.x1, 0, 0)
        grid.addWidget(self.x2, 0, 1)
        grid.addWidget(self.mix, 1, 0)
        grid.addWidget(self.out, 1, 1)
        row = QHBoxLayout()
        row.addLayout(grid)
        row.addWidget(self._meters())
        v.addLayout(row)
        pl.addWidget(box)
        self.body.addWidget(panel)

        self.refresh()
        fit_to_screen(self, 1080, 660)

    def refresh(self) -> None:
        super().refresh()
        c = self.part
        for (on, solo, thr, ratio, att, rel, gain, _m, sub), b, i in zip(self.bands, c["bands"], range(3)):
            for btn, val in ((on, b["on"]), (solo, b.get("solo", False))):
                btn.blockSignals(True)
                btn.setChecked(bool(val))
                btn.blockSignals(False)
            thr.set_value(b["thr"])
            ratio.set_value(b["ratio"])
            att.set_value(b["att"])
            rel.set_value(b["rel"])
            gain.set_value(b["gain"])
            for k in (thr, ratio, att, rel):
                k.setEnabled(bool(b["on"]))
        self.x1.set_value(c["xover"][0])
        self.x2.set_value(c["xover"][1])
        self.mix.set_value(c.get("mix", 100.0))
        self.out.set_value(c.get("out", 0.0))
        self._update_ranges()
        self.graph.update()

    def _update_ranges(self) -> None:
        f1, f2 = self.part["xover"]
        texts = (f"до {_fmt_hz(f1)}", f"{_fmt_hz(f1)} – {_fmt_hz(f2)}", f"от {_fmt_hz(f2)}")
        for row, t in zip(self.bands, texts):
            row[-1].setText(t)

    def _from_controls(self, *_a) -> None:
        c = self.part
        for (on, solo, thr, ratio, att, rel, gain, _m, _s), b in zip(self.bands, c["bands"]):
            b.update(on=on.isChecked(), solo=solo.isChecked(), thr=thr.value, ratio=ratio.value,
                     att=att.value, rel=rel.value, gain=gain.value)
            for k in (thr, ratio, att, rel):
                k.setEnabled(on.isChecked())
        x1, x2 = self.x1.value, self.x2.value
        if x2 < x1 * 1.5:                       # разделы не пересекаются
            x2 = min(16000.0, x1 * 1.5)
            self.x2.set_value(x2)
        c["xover"] = [x1, x2]
        c["mix"] = self.mix.value
        c["out"] = self.out.value
        if not c.get("on"):
            c["on"] = True
            super().refresh()
        self._update_ranges()
        self.graph.update()
        self.changed()

    def _from_graph(self) -> None:
        self.refresh()
        self.changed()

    def tick_extra(self, fx) -> None:
        values = fx.take_gr() if fx is not None and self.part.get("on") else [0.0, 0.0, 0.0]
        for row, v in zip(self.bands, values):
            row[7].set_value(v)
        self.graph.gr = [row[7].value for row in self.bands]
