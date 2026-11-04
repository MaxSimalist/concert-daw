"""
gui_board.py — Элементы табло: полоса прогресса, список треков, плитки каналов пульта.
Всё рисуется само и масштабируется под размер экрана (телевизор, монитор, ноутбук).
"""
from __future__ import annotations

import math
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
        f_num = font(self.px * 0.8, False)
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
            p.setPen(QColor(color))
            name_rect = QRectF(num_w + self.px * 0.4, y, w - num_w - dur_w - self.px, row_h - 3)
            name = str(sg.get("name", ""))
            # название — ЦЕЛИКОМ: длинное — шрифт поменьше (до 60 %), а если и так не влезает — в 2 строки
            f = font(self.px, True)
            while f.pixelSize() > self.px * 0.6 and QFontMetrics(f).horizontalAdvance(name) > name_rect.width():
                f.setPixelSize(f.pixelSize() - 1)
            p.setFont(f)
            if QFontMetrics(f).horizontalAdvance(name) <= name_rect.width():
                p.drawText(name_rect, Qt.AlignVCenter | Qt.AlignLeft, name)
            else:
                p.drawText(name_rect, Qt.AlignVCenter | Qt.AlignLeft | Qt.TextWordWrap, name)
            p.setFont(f_num)
            dur = float(sg.get("duration", 0.0) or 0.0)
            p.drawText(QRectF(w - dur_w - self.px * 0.4, y, dur_w, row_h - 3), Qt.AlignVCenter | Qt.AlignRight,
                       fmt_time(dur) if dur else "")
            y += row_h
            if y > h:
                break
        p.end()


METER_MARKS = (0, -6, -12, -20, -30, -40, -60)     # деления шкалы, dBFS


class ChannelTile(QWidget):
    """Канал пульта (только показ): название, ВЕРТИКАЛЬНЫЙ индикатор уровня со шкалой в dB,
    уровень числом, фейдер и лампа состояния: зелёная «В ЭФИРЕ» / красная «ОТКЛЮЧЕН» (MUTE)."""

    def __init__(self, key: str, parent=None):
        super().__init__(parent)
        self.key = key
        self.name = key
        self.levels: list[float] = []
        self.holds: list[float] = []
        self.peak_db: Optional[float] = None
        self.muted: Optional[bool] = None
        self.fader_text = ""
        self.px = 16.0
        self.online = False
        self.setMinimumSize(48, 90)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

    def set_state(self, name: str, levels: list, muted: Optional[bool], fader: Optional[float],
                  online: bool) -> None:
        fr = [meter_fraction(v) for v in levels]
        if len(self.holds) != len(fr):
            self.holds = list(fr)
        self.holds = [max(f, h - 0.012) for f, h in zip(fr, self.holds)]   # «пик» медленно опускается
        loud = max(levels) if levels else 0.0
        db = None if not levels else (20 * math.log10(loud) if loud > 1e-6 else -120.0)
        text = format_db(fader) if fader is not None else ""
        db_shown = None if db is None else max(-60, round(db))
        state = (name, [round(f, 3) for f in fr], [round(h, 3) for h in self.holds], muted, text, online,
                 db_shown)
        if state != getattr(self, "_state", None):
            self._state = state
            self.name, self.levels, self.muted, self.fader_text, self.online = name, fr, muted, text, online
            self.peak_db = db
            self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(PANEL))
        p.drawRoundedRect(QRectF(0, 0, w, h), 8, 8)
        pad = max(3.0, self.px * 0.3)
        # название (сверху, по центру; длинное — сокращается)
        p.setFont(font(self.px * 0.95, True))
        fm = QFontMetrics(p.font())
        name_h = fm.height() + 2
        p.setPen(QColor(TEXT if self.online else DIM))
        p.drawText(QRectF(pad, pad * 0.6, w - 2 * pad, name_h), Qt.AlignHCenter | Qt.AlignVCenter,
                   fm.elidedText(self.name, Qt.ElideRight, int(w - 2 * pad)))
        # уровень числом
        small = font(self.px * 0.8, True)
        p.setFont(small)
        sm = QFontMetrics(small)
        y = pad * 0.6 + name_h
        db_txt = "" if self.peak_db is None or not self.online else (
            "−∞" if self.peak_db <= -60 else f"{self.peak_db:.0f} dB".replace("-", "−"))
        p.setPen(QColor(ALERT if self.peak_db is not None and self.peak_db > -1 else TEXT))
        p.drawText(QRectF(pad, y, w - 2 * pad, sm.height()), Qt.AlignHCenter | Qt.AlignVCenter, db_txt)
        y += sm.height() + 2
        # низ: фейдер и лампа состояния
        lamp_font = font(self.px * 0.72, True)
        lm = QFontMetrics(lamp_font)
        lamp_h = lm.height() + pad * 1.2
        fader_h = sm.height() if self.fader_text else 0
        bottom = h - pad - lamp_h - fader_h - 2
        # вертикальный индикатор + шкала dB слева от него
        tiny = font(max(8.0, self.px * 0.62), False)
        tm = QFontMetrics(tiny)
        scale_w = tm.horizontalAdvance("−60") + 3
        bar_h = max(10.0, bottom - y - 2)
        n = max(1, len(self.levels))
        gap = 2.0
        bars_w = min(w - 2 * pad - scale_w, max(10.0, self.px * 0.9) * n + gap * (n - 1))
        x0 = pad + (w - 2 * pad - scale_w - bars_w) / 2 + scale_w
        p.setFont(tiny)
        for mark in METER_MARKS:
            fy = y + bar_h * (1 - meter_fraction(10 ** (mark / 20)))
            p.setPen(QColor(FAINT))
            p.drawLine(int(x0 - 3), int(fy), int(x0 + bars_w), int(fy))
            if bar_h > tm.height() * 5 or mark in (0, -20, -60):
                p.setPen(QColor(DIM))
                p.drawText(QRectF(x0 - scale_w - 2, fy - tm.height() / 2, scale_w - 2, tm.height()),
                           Qt.AlignRight | Qt.AlignVCenter, str(mark).replace("-", "−"))
        bw = (bars_w - gap * (n - 1)) / n
        p.setPen(Qt.NoPen)
        for i in range(n):
            x = x0 + i * (bw + gap)
            p.setBrush(QColor("#06080a"))
            p.drawRoundedRect(QRectF(x, y, bw, bar_h), 3, 3)
            if i < len(self.levels) and self.online:
                f = self.levels[i]
                # зелёный → жёлтый (выше −12) → красный (выше −3): рисуем снизу вверх зонами
                for lo, hi, color in ((0.0, 0.8, ACCENT), (0.8, 0.95, WARN), (0.95, 1.0, ALERT)):
                    if f > lo:
                        top = min(f, hi)
                        c = QColor(color)
                        if self.muted:
                            c = QColor("#55606a")          # канал выключен — сигнал есть, но серый
                        p.setBrush(c)
                        p.drawRect(QRectF(x, y + bar_h * (1 - top), bw, bar_h * (top - lo)))
                if i < len(self.holds) and self.holds[i] > 0.01:
                    p.setBrush(QColor(TEXT))
                    p.drawRect(QRectF(x, y + bar_h * (1 - self.holds[i]), bw, 2))
        # фейдер
        yb = h - pad - lamp_h
        if self.fader_text:
            p.setFont(small)
            p.setPen(QColor(DIM))
            p.drawText(QRectF(pad, yb - fader_h - 2, w - 2 * pad, fader_h), Qt.AlignHCenter | Qt.AlignVCenter,
                       f"F {self.fader_text}")
        # лампа: В ЭФИРЕ / ОТКЛЮЧЕН
        if self.muted is None or not self.online:
            color, text, tcolor = FAINT, ("…" if self.online else "нет связи"), DIM
        elif self.muted:
            color, text, tcolor = ALERT, "ОТКЛЮЧЕН", "#ffffff"
        else:
            color, text, tcolor = ACCENT, "В ЭФИРЕ", "#06210f"
        lamp = QRectF(pad, yb, w - 2 * pad, lamp_h)
        p.setBrush(QColor(color))
        p.drawRoundedRect(lamp, lamp_h / 3, lamp_h / 3)
        f = QFont(lamp_font)
        while f.pixelSize() > 7 and QFontMetrics(f).horizontalAdvance(text) > lamp.width() - 4:
            f.setPixelSize(f.pixelSize() - 1)                      # узкая плитка — буквы мельче
        p.setFont(f)
        p.setPen(QColor(tcolor))
        p.drawText(lamp, Qt.AlignCenter, text)
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
        self.max_rows = 2                          # на вертикальном экране — до 4 рядов

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
        tile_w = max(64.0, self.px * 5.2)          # узкие плитки с вертикальными индикаторами
        cols = max(1, min(n, int(max(1, self.width()) // tile_w)))
        rows = -(-n // cols)
        if rows > self.max_rows:                   # рядов не больше max_rows: плитки становятся уже
            rows = self.max_rows
            cols = -(-n // rows)
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
