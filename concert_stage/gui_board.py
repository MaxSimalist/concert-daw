"""
gui_board.py — Элементы табло: полоса прогресса, список треков, плитки каналов пульта.
Всё рисуется само и масштабируется под размер экрана (телевизор, монитор, ноутбук).
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QFont, QFontMetrics, QPainter, QPen
from PySide6.QtWidgets import QGridLayout, QSizePolicy, QWidget

from stage_model import fmt_time, is_muted, meter_fraction
from x32_osc import ConsoleMirror, format_db

BG = "#0b0d10"
PANEL = "#14181d"
TEXT = "#eef2f5"
DIM = "#7f8b97"
FAINT = "#3a434c"
ACCENT = "#36d399"      # зелёный — играет / текущий
WARN = "#f7c948"        # жёлтый
ALERT = "#ef4b55"       # красный
BLUE = "#4aa8ff"


def font(px: float, bold: bool = True) -> QFont:
    f = QFont()
    f.setPixelSize(max(8, int(px)))
    f.setBold(bold)
    return f


class ProgressStrip(QWidget):
    """Полоса «сколько сыграно» с отметками маркеров."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.value = 0.0
        self.color = ACCENT
        self.ticks: list[float] = []
        self.setMinimumHeight(10)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)

    def set_state(self, value: float, color: str, ticks: list) -> None:
        value = min(1.0, max(0.0, value))
        if abs(value - self.value) > 0.0005 or color != self.color or ticks != self.ticks:
            self.value, self.color, self.ticks = value, color, list(ticks)
            self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(0, 0, self.width(), self.height())
        rad = r.height() / 2
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(FAINT))
        p.drawRoundedRect(r, rad, rad)
        if self.value > 0:
            p.setBrush(QColor(self.color))
            p.drawRoundedRect(QRectF(0, 0, max(r.height(), r.width() * self.value), r.height()), rad, rad)
        p.setPen(QPen(QColor(BG), max(2.0, r.height() * 0.15)))
        for t in self.ticks:
            x = r.width() * t
            if 2 < x < r.width() - 2:
                p.drawLine(int(x), 0, int(x), int(r.height()))
        p.end()


class SetlistView(QWidget):
    """Очередь треков: сыгранные — тусклые, текущий — подсвечен, дальше — обычные.
    Список сам прокручивается так, чтобы текущий трек был ближе к верху."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.songs: list = []
        self.index = -1
        self.next_id = ""
        self.px = 22.0
        self.playing = False
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_data(self, songs: list, index: int, next_id: str, playing: bool) -> None:
        key = ([s.get("id") for s in songs], [s.get("name") for s in songs], index, next_id, playing)
        if key != getattr(self, "_key", None):
            self._key = key
            self.songs, self.index, self.next_id, self.playing = songs, index, next_id, playing
            self.update()

    def set_font_px(self, px: float) -> None:
        if abs(px - self.px) > 0.5:
            self.px = px
            self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        row_h = self.px * 1.75
        if not self.songs:
            p.setPen(QColor(DIM))
            p.setFont(font(self.px * 0.9, False))
            p.drawText(QRectF(0, 0, w, h), Qt.AlignCenter | Qt.TextWordWrap, "Плейлист появится, когда\n"
                                                                              "подключится Concert DAW")
            p.end()
            return
        visible = max(1, int(h // row_h))
        first = 0
        if self.index >= 0 and len(self.songs) > visible:
            first = min(max(0, self.index - 1), len(self.songs) - visible)   # один сыгранный сверху
        f_name, f_num = font(self.px, True), font(self.px * 0.8, False)
        fm = QFontMetrics(f_name)
        num_w = QFontMetrics(f_num).horizontalAdvance("00") + self.px * 0.6
        dur_w = QFontMetrics(f_num).horizontalAdvance("00:00") + self.px * 0.4
        y = 0.0
        for i in range(first, min(len(self.songs), first + visible + 1)):
            sg = self.songs[i]
            rect = QRectF(0, y, w, row_h - 3)
            cur = i == self.index
            if cur:
                p.setPen(Qt.NoPen)
                p.setBrush(QColor(ACCENT if self.playing else BLUE))
                p.drawRoundedRect(rect, 6, 6)
            elif sg.get("id") == self.next_id:
                p.setPen(QPen(QColor(WARN), 2))
                p.setBrush(Qt.NoBrush)
                p.drawRoundedRect(rect.adjusted(1, 1, -1, -1), 6, 6)
            color = "#0b0d10" if cur else (FAINT if 0 <= i < self.index else TEXT)
            p.setPen(QColor(color if cur or i >= self.index else DIM))
            p.setFont(f_num)
            p.drawText(QRectF(self.px * 0.4, y, num_w, row_h - 3), Qt.AlignVCenter | Qt.AlignLeft, f"{i + 1}")
            p.setFont(f_name)
            p.setPen(QColor(color))
            name_rect = QRectF(num_w + self.px * 0.4, y, w - num_w - dur_w - self.px, row_h - 3)
            p.drawText(name_rect, Qt.AlignVCenter | Qt.AlignLeft,
                       fm.elidedText(str(sg.get("name", "")), Qt.ElideRight, int(name_rect.width())))
            p.setFont(f_num)
            dur = float(sg.get("duration", 0.0) or 0.0)
            p.drawText(QRectF(w - dur_w - self.px * 0.4, y, dur_w, row_h - 3), Qt.AlignVCenter | Qt.AlignRight,
                       fmt_time(dur) if dur else "")
            y += row_h
            if y > h:
                break
        p.end()


class ChannelTile(QWidget):
    """Канал пульта: название, уровень сигнала, положение фейдера, MUTE (только показ)."""

    def __init__(self, key: str, parent=None):
        super().__init__(parent)
        self.key = key
        self.name = key
        self.levels: list[float] = []
        self.holds: list[float] = []
        self.muted: Optional[bool] = None
        self.fader_text = ""
        self.px = 16.0
        self.online = False
        self.setMinimumSize(60, 40)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_state(self, name: str, levels: list, muted: Optional[bool], fader: Optional[float],
                  online: bool) -> None:
        fr = [meter_fraction(v) for v in levels]
        if len(self.holds) != len(fr):
            self.holds = list(fr)
        self.holds = [max(f, h - 0.012) for f, h in zip(fr, self.holds)]   # «пик» медленно опускается
        text = format_db(fader) if fader is not None else ""
        state = (name, [round(f, 3) for f in fr], [round(h, 3) for h in self.holds], muted, text, online)
        if state != getattr(self, "_state", None):
            self._state = state
            self.name, self.levels, self.muted, self.fader_text, self.online = name, fr, muted, text, online
            self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        muted = self.muted is True
        p.setPen(Qt.NoPen)
        p.setBrush(QColor("#5a1218" if muted else PANEL))
        p.drawRoundedRect(QRectF(0, 0, w, h), 8, 8)
        pad = max(4.0, self.px * 0.35)
        name_h = self.px * 1.3
        p.setFont(font(self.px, True))
        fm = QFontMetrics(p.font())
        p.setPen(QColor(TEXT if self.online else DIM))
        p.drawText(QRectF(pad, pad * 0.6, w - 2 * pad, name_h), Qt.AlignLeft | Qt.AlignVCenter,
                   fm.elidedText(self.name, Qt.ElideRight, int(w - 2 * pad)))
        # индикатор уровня
        bar_top = pad * 0.6 + name_h + pad * 0.4
        bottom_h = self.px * 1.25
        bar_h = max(6.0, h - bar_top - bottom_h - pad)
        n = max(1, len(self.levels))
        gap = 3.0
        bw = (w - 2 * pad - gap * (n - 1)) / n
        for i in range(n):
            x = pad + i * (bw + gap)
            p.setBrush(QColor("#0b0d10"))
            p.drawRoundedRect(QRectF(x, bar_top, bw, bar_h), 3, 3)
            if i < len(self.levels) and self.online:
                f = self.levels[i]
                color = ALERT if f > 0.95 else (WARN if f > 0.8 else ACCENT)
                if muted:
                    color = "#7b5a5e"
                p.setBrush(QColor(color))
                p.drawRoundedRect(QRectF(x, bar_top, bw * f, bar_h), 3, 3)
                if i < len(self.holds) and self.holds[i] > 0.01:
                    p.setBrush(QColor(TEXT))
                    hx = x + bw * self.holds[i]
                    p.drawRect(QRectF(min(hx, x + bw - 2), bar_top, 2, bar_h))
        # низ: фейдер и MUTE
        y = h - bottom_h - pad * 0.4
        p.setFont(font(self.px * 0.85, False))
        p.setPen(QColor(DIM))
        if self.fader_text:
            p.drawText(QRectF(pad, y, w - 2 * pad, bottom_h), Qt.AlignLeft | Qt.AlignVCenter,
                       f"фейдер {self.fader_text} dB")
        if muted:
            p.setFont(font(self.px * 0.95, True))
            p.setPen(QColor("#ffd5d8"))
            p.drawText(QRectF(pad, y, w - 2 * pad, bottom_h), Qt.AlignRight | Qt.AlignVCenter, "MUTE")
        elif self.muted is None and self.online:
            p.drawText(QRectF(pad, y, w - 2 * pad, bottom_h), Qt.AlignRight | Qt.AlignVCenter, "…")
        p.end()


class ChannelsPanel(QWidget):
    """Сетка плиток каналов. Число столбцов подбирается под ширину."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.grid = QGridLayout(self)
        self.grid.setContentsMargins(0, 0, 0, 0)
        self.grid.setSpacing(6)
        self.tiles: list[ChannelTile] = []
        self.keys: list[str] = []
        self.px = 16.0
        self._cols = 0

    def set_keys(self, keys: list) -> None:
        if list(keys) == self.keys:
            return
        self.keys = list(keys)
        for t in self.tiles:
            t.setParent(None)
            t.deleteLater()
        self.tiles = [ChannelTile(k, self) for k in self.keys]
        self._cols = 0
        self._relayout()

    def _relayout(self) -> None:
        n = len(self.tiles)
        if not n:
            return
        tile_w = max(110.0, self.px * 9)
        cols = max(1, min(n, int(max(1, self.width()) // tile_w)))
        rows = -(-n // cols)
        if rows > 2:                               # не больше двух рядов: плитки становятся уже
            rows = 2
            cols = -(-n // 2)
        if cols == self._cols:
            return
        self._cols = cols
        for i, t in enumerate(self.tiles):
            self.grid.removeWidget(t)
            self.grid.addWidget(t, i // cols, i % cols)

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._relayout()

    def set_font_px(self, px: float) -> None:
        self.px = px
        for t in self.tiles:
            t.px = px
            t.update()
        self._cols = 0
        self._relayout()

    def tick(self, mirror: ConsoleMirror, online: bool) -> None:
        for t in self.tiles:
            t.set_state(mirror.name(t.key), mirror.levels(t.key) if online else [], is_muted(mirror, t.key),
                        mirror.fader.get(t.key), online)
