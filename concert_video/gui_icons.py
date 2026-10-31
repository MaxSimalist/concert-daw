"""
gui_icons.py — Единый набор значков программы, нарисованных кодом.

Раньше в кнопках были цветные эмодзи Windows (🔁 📂 🗑 💾 🔌 🔍) вперемешку с символами
разных шрифтов (⚙ ✎ ✕ ⟳ ✂ ♩), и на каждом компьютере это выглядело по-своему.
Теперь все значки рисуются здесь одним стилем:
* линейные значки — сетка 24×24, линия 2 px, скруглённые концы, один цвет;
* значки транспорта (play/pause/stop/…) — сплошные фигуры.
"""
from __future__ import annotations

import math
from functools import lru_cache

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

TEXT = "#e6eaee"
DIM = "#56616c"
_PX = 48            # рисуем крупно, Qt уменьшает без потери чёткости


# ══════════════════════════════════════════════════════════════════════════════
# Линейные значки (24×24)
# ══════════════════════════════════════════════════════════════════════════════
def _line_paths(name: str) -> list[QPainterPath]:
    p = QPainterPath()
    extra: list[QPainterPath] = []

    def line(x1, y1, x2, y2):
        p.moveTo(x1, y1)
        p.lineTo(x2, y2)

    def poly(*pts, close=False):
        p.moveTo(*pts[0])
        for pt in pts[1:]:
            p.lineTo(*pt)
        if close:
            p.closeSubpath()

    if name == "plus":
        line(12, 5, 12, 19); line(5, 12, 19, 12)
    elif name == "close":
        line(6, 6, 18, 18); line(18, 6, 6, 18)
    elif name == "edit":                       # карандаш
        poly((4, 20), (5, 15.5), (15.5, 5), (19, 8.5), (8.5, 19), close=True)
        line(13, 7.5, 16.5, 11)
    elif name == "settings":                   # шестерёнка
        p.addEllipse(QPointF(12, 12), 3, 3)
        pts = []
        for i in range(8):                     # 8 зубцов: впадина — зубец — зубец — впадина
            c = i * 45
            for da, r in ((-17, 6.8), (-9, 9.6), (9, 9.6), (17, 6.8)):
                a = math.radians(c + da)
                pts.append((12 + r * math.cos(a), 12 + r * math.sin(a)))
        poly(*pts, close=True)
    elif name == "timeline":                   # дорожки со «звуком»
        line(3, 6, 21, 6); line(3, 12, 21, 12); line(3, 18, 21, 18)
        line(8, 4, 8, 8); line(14, 10, 14, 14); line(10, 16, 10, 20)
    elif name == "loop":
        poly((4, 13), (4, 10), (7, 7), (20, 7))
        poly((17, 4), (20, 7), (17, 10))
        poly((20, 11), (20, 14), (17, 17), (4, 17))
        poly((7, 14), (4, 17), (7, 20))
    elif name == "refresh":
        r = QRectF(5, 5, 14, 14)
        p.arcMoveTo(r, 50)
        p.arcTo(r, 50, 290)
        poly((14, 2.5), (16.5, 6.2), (12.6, 8))
    elif name == "reset":                      # отменить (стрелка назад по кругу)
        r = QRectF(5, 6, 14, 14)
        p.arcMoveTo(r, 135)
        p.arcTo(r, 135, -270)
        poly((4, 6), (7.2, 8.6), (9, 4.8))
    elif name == "folder":
        poly((3, 18), (3, 6), (4, 5), (9, 5), (11, 7), (20, 7), (21, 8), (21, 18), (20, 19), (4, 19), close=True)
    elif name == "trash":
        line(4, 7, 20, 7)
        poly((9, 7), (9, 4), (15, 4), (15, 7))
        poly((6, 7), (7, 20), (17, 20), (18, 7))
        line(10, 11, 10, 16); line(14, 11, 14, 16)
    elif name == "save":
        poly((4, 4), (17, 4), (20, 7), (20, 20), (4, 20), close=True)
        poly((8, 4), (8, 9), (15, 9), (15, 4))
        poly((7, 20), (7, 13), (17, 13), (17, 20))
    elif name == "scissors":
        p.addEllipse(QPointF(6, 6), 3, 3)
        p.addEllipse(QPointF(6, 18), 3, 3)
        line(8.4, 7.8, 20, 18); line(8.4, 16.2, 20, 6)
    elif name == "plug":
        line(9, 2, 9, 7); line(15, 2, 15, 7)
        poly((6, 7), (18, 7), (18, 11), (14, 16), (10, 16), (6, 11), close=True)
        line(12, 16, 12, 22)
    elif name == "search":
        p.addEllipse(QPointF(10.5, 10.5), 6, 6)
        line(15, 15, 20.5, 20.5)
    elif name == "metronome":
        poly((7, 20), (17, 20), (14, 4), (10, 4), close=True)
        line(12, 16, 18, 7)
    elif name == "marker":                     # флажок
        line(6, 21, 6, 3)
        poly((6, 4), (18, 4), (15, 8), (18, 12), (6, 12))
    elif name == "layers":                     # мультитрек
        poly((12, 3.5), (20.5, 8), (12, 12.5), (3.5, 8), close=True)
        poly((3.5, 12), (12, 16.5), (20.5, 12))
        poly((3.5, 16), (12, 20.5), (20.5, 16))
    elif name == "music":                      # трек
        line(9, 18, 9, 5); line(9, 5, 19, 3); line(19, 3, 19, 16)
        p.addEllipse(QPointF(6.5, 18), 2.5, 2.5)
        p.addEllipse(QPointF(16.5, 16), 2.5, 2.5)
    elif name == "list":                       # плейлист / сет
        line(9, 6, 20, 6); line(9, 12, 20, 12); line(9, 18, 20, 18)
        p.addEllipse(QPointF(5, 6), 1, 1); p.addEllipse(QPointF(5, 12), 1, 1); p.addEllipse(QPointF(5, 18), 1, 1)
    elif name == "check":
        poly((5, 12.5), (10, 17.5), (19, 7))
    elif name == "fit":                        # «по размеру»
        poly((4, 9), (4, 4), (9, 4)); poly((15, 4), (20, 4), (20, 9))
        poly((20, 15), (20, 20), (15, 20)); poly((9, 20), (4, 20), (4, 15))
    elif name == "wave":                       # тишина / звук
        line(3, 12, 6, 12); line(6, 12, 8, 7); line(8, 7, 10, 17); line(10, 17, 12, 9)
        line(12, 9, 14, 15); line(14, 15, 16, 12); line(16, 12, 21, 12)
    elif name == "lowshelf":                   # полка НЧ
        poly((3, 8), (8, 8), (16, 16), (21, 16))
    elif name == "highshelf":                  # полка ВЧ
        poly((3, 16), (8, 16), (16, 8), (21, 8))
    elif name == "peak":                       # колокол
        p.moveTo(3, 16)
        p.cubicTo(9, 16, 9, 6, 12, 6)
        p.cubicTo(15, 6, 15, 16, 21, 16)
    elif name == "power":
        r = QRectF(4.5, 5, 15, 15)
        p.arcMoveTo(r, 125)
        p.arcTo(r, 125, 290)
        line(12, 3, 12, 11)
    elif name == "camera":
        p.addRoundedRect(QRectF(3, 7, 13, 10), 2, 2)
        poly((16, 10.5), (21, 7.5), (21, 16.5), (16, 13.5))
    elif name == "video":                      # кадр плёнки с треугольником
        p.addRoundedRect(QRectF(3, 5, 18, 14), 2, 2)
        poly((10, 9), (15, 12), (10, 15), close=True)
    elif name == "image":
        p.addRoundedRect(QRectF(3, 5, 18, 14), 2, 2)
        poly((3, 17), (9, 11), (13, 15), (16, 12), (21, 17))
        p.addEllipse(QPointF(16, 9), 1.5, 1.5)
    elif name == "text":
        line(5, 6, 19, 6); line(12, 6, 12, 19); line(9, 19, 15, 19)
    elif name == "color":                      # капля
        p.moveTo(12, 3.5)
        p.cubicTo(16, 9, 18.5, 12, 18.5, 14.5)
        p.cubicTo(18.5, 18.5, 15.5, 20.5, 12, 20.5)
        p.cubicTo(8.5, 20.5, 5.5, 18.5, 5.5, 14.5)
        p.cubicTo(5.5, 12, 8, 9, 12, 3.5)
    elif name == "monitor":
        p.addRoundedRect(QRectF(3, 4, 18, 12), 1.5, 1.5)
        line(12, 16, 12, 20); line(8, 20, 16, 20)
    elif name == "window":
        p.addRoundedRect(QRectF(3, 4, 18, 16), 1.5, 1.5)
        line(3, 8, 21, 8)
    elif name == "slides":
        p.addRoundedRect(QRectF(6, 7, 15, 12), 1.5, 1.5)
        poly((3, 15), (3, 4), (17, 4))
    elif name == "eye":
        p.moveTo(2.5, 12)
        p.cubicTo(6, 6, 18, 6, 21.5, 12)
        p.cubicTo(18, 18, 6, 18, 2.5, 12)
        p.addEllipse(QPointF(12, 12), 2.8, 2.8)
    elif name == "eye_off":
        p.moveTo(2.5, 12)
        p.cubicTo(6, 6, 18, 6, 21.5, 12)
        p.cubicTo(18, 18, 6, 18, 2.5, 12)
        line(4, 20, 20, 4)
    elif name == "lock":
        p.addRoundedRect(QRectF(5, 11, 14, 10), 2, 2)
        p.moveTo(8, 11); p.lineTo(8, 8)
        p.arcTo(QRectF(8, 4, 8, 8), 180, -180)
        p.lineTo(16, 11)
    elif name == "up":
        poly((6, 14), (12, 8), (18, 14))
    elif name == "down":
        poly((6, 10), (12, 16), (18, 10))
    elif name == "copy":
        p.addRoundedRect(QRectF(8, 8, 12, 12), 2, 2)
        poly((16, 8), (16, 4), (4, 4), (4, 16), (8, 16))
    elif name == "broadcast":                  # трансляция: точка и волны
        p.addEllipse(QPointF(12, 12), 2, 2)
        for r in (5.5, 9):
            rr = QRectF(12 - r, 12 - r, 2 * r, 2 * r)
            p.arcMoveTo(rr, 40); p.arcTo(rr, 40, -80)
            p.arcMoveTo(rr, 140); p.arcTo(rr, 140, 80)
    elif name == "link":
        p.addRoundedRect(QRectF(3, 9, 10, 6), 3, 3)
        p.addRoundedRect(QRectF(11, 9, 10, 6), 3, 3)
    elif name == "arrow_right":
        line(4, 12, 19, 12); poly((13, 6), (19, 12), (13, 18))
    else:
        line(6, 6, 18, 18)
    return [p] + extra


def _draw_line_icon(name: str, color: str) -> QPixmap:
    pix = QPixmap(_PX, _PX)
    pix.fill(Qt.transparent)
    painter = QPainter(pix)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.scale(_PX / 24.0, _PX / 24.0)
    pen = QPen(QColor(color), 2.0)
    pen.setCapStyle(Qt.RoundCap)
    pen.setJoinStyle(Qt.RoundJoin)
    painter.setPen(pen)
    painter.setBrush(Qt.NoBrush)
    for path in _line_paths(name):
        painter.drawPath(path)
    painter.end()
    return pix


# ══════════════════════════════════════════════════════════════════════════════
# Значки транспорта (сплошные)
# ══════════════════════════════════════════════════════════════════════════════
def _draw_transport(kind: str, color: str) -> QPixmap:
    S = 64
    pix = QPixmap(S, S)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(Qt.NoPen)
    p.setBrush(QColor(color))
    m = 10.0
    s = S - 2 * m
    path = QPainterPath()
    bar = 7.0

    def triangle(x0: float, x1: float, right: bool) -> None:
        top, bottom, mid = m + 4, m + s - 4, S / 2
        pts = [(x0, top), (x1, mid), (x0, bottom)] if right else [(x1, top), (x0, mid), (x1, bottom)]
        path.moveTo(QPointF(*pts[0]))
        for pt in pts[1:]:
            path.lineTo(QPointF(*pt))
        path.closeSubpath()

    if kind == "play":
        triangle(m + 6, m + s - 2, right=True)
    elif kind == "pause":
        path.addRoundedRect(QRectF(m + 7, m + 4, 10, s - 8), 2.5, 2.5)
        path.addRoundedRect(QRectF(m + s - 17, m + 4, 10, s - 8), 2.5, 2.5)
    elif kind == "stop":
        path.addRoundedRect(QRectF(m + 4, m + 4, s - 8, s - 8), 4, 4)
    elif kind == "prev":
        path.addRoundedRect(QRectF(m + 2, m + 4, bar, s - 8), 2, 2)
        triangle(m + 2 + bar + 2, m + s - 2, right=False)
    elif kind == "next":
        triangle(m + 2, m + s - 2 - bar - 2, right=True)
        path.addRoundedRect(QRectF(m + s - 2 - bar, m + 4, bar, s - 8), 2, 2)
    elif kind == "rec":
        path.addEllipse(QRectF(m + 6, m + 6, s - 12, s - 12))
    p.drawPath(path)
    p.end()
    return pix


TRANSPORT = {"play", "pause", "stop", "prev", "next", "rec"}


@lru_cache(maxsize=256)
def icon(name: str, color: str = TEXT, on_color: str = "") -> QIcon:
    """Значок по имени. on_color — цвет во «включённом» состоянии кнопки (checked)."""
    draw = _draw_transport if name in TRANSPORT else _draw_line_icon
    ic = QIcon()
    ic.addPixmap(draw(name, color), QIcon.Normal, QIcon.Off)
    ic.addPixmap(draw(name, on_color or color), QIcon.Normal, QIcon.On)
    ic.addPixmap(draw(name, on_color or color), QIcon.Active, QIcon.On)
    ic.addPixmap(draw(name, DIM), QIcon.Disabled, QIcon.Off)
    ic.addPixmap(draw(name, DIM), QIcon.Disabled, QIcon.On)
    return ic


def transport_icon(kind: str, color: str = TEXT, checked_color: str = "") -> QIcon:
    return icon(kind, color, checked_color)


def set_icon(button, name: str, size: int = 16, color: str = TEXT, on_color: str = "") -> None:
    """Поставить значок на кнопку (QPushButton/QToolButton)."""
    button.setIcon(icon(name, color, on_color))
    button.setIconSize(QSize(size, size))


def pixmap(name: str, size: int = 16, color: str = TEXT) -> QPixmap:
    return icon(name, color).pixmap(QSize(size, size))
