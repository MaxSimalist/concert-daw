"""Общие виджеты: шина событий, XY-пэд для pan/tilt, слайдер атрибута."""

from PySide6.QtCore import QObject, QPointF, QRect, Qt, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import QHBoxLayout, QLabel, QSlider, QSpinBox, QWidget


class Hub(QObject):
    """Шина событий между вкладками."""
    changed = Signal(str)               # "patch", "scenes", "chases", "effects", "songs", "all"
    action = Signal(str, str, float)    # действие с пульта (для записи кьюшек): action, target, fade


class XYPad(QWidget):
    moved = Signal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumSize(220, 220)
        self._x = 128.0
        self._y = 128.0

    def set_values(self, x, y):
        self._x, self._y = float(x), float(y)
        self.update()

    def _area(self):
        side = min(self.width(), self.height()) - 2
        return QRect(1, 1, side, side)

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = self._area()
        p.fillRect(r, QColor(28, 28, 34))
        p.setPen(QPen(QColor(85, 85, 95)))
        p.drawRect(r)
        p.drawLine(r.center().x(), r.top(), r.center().x(), r.bottom())
        p.drawLine(r.left(), r.center().y(), r.right(), r.center().y())
        cx = r.left() + self._x / 255.0 * r.width()
        cy = r.top() + (1 - self._y / 255.0) * r.height()
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(255, 170, 0))
        p.drawEllipse(QPointF(cx, cy), 7, 7)
        p.setPen(QColor(210, 210, 210))
        p.drawText(r.adjusted(5, 3, 0, 0), Qt.AlignLeft | Qt.AlignTop,
                   f"Pan {self._x:.0f}   Tilt {self._y:.0f}")

    def _set_from(self, pos):
        r = self._area()
        x = (pos.x() - r.left()) / max(1, r.width()) * 255.0
        y = (1 - (pos.y() - r.top()) / max(1, r.height())) * 255.0
        self._x = max(0.0, min(255.0, x))
        self._y = max(0.0, min(255.0, y))
        self.update()
        self.moved.emit(self._x, self._y)

    def mousePressEvent(self, e):
        self._set_from(e.position())

    def mouseMoveEvent(self, e):
        if e.buttons() & Qt.LeftButton:
            self._set_from(e.position())


class AttrSlider(QWidget):
    changed = Signal(str, float)

    def __init__(self, attr, label, value=0.0, parent=None):
        super().__init__(parent)
        self.attr = attr
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lbl = QLabel(label)
        lbl.setFixedWidth(110)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 255)
        self.spin = QSpinBox()
        self.spin.setRange(0, 255)
        self.spin.setFixedWidth(64)
        lay.addWidget(lbl)
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.spin)
        self.set_value(value)
        self.slider.valueChanged.connect(self._from_slider)
        self.spin.valueChanged.connect(self._from_spin)

    def set_value(self, v):
        v = int(round(max(0, min(255, v))))
        for w in (self.slider, self.spin):
            w.blockSignals(True)
            w.setValue(v)
            w.blockSignals(False)

    def _from_slider(self, v):
        self.spin.blockSignals(True)
        self.spin.setValue(v)
        self.spin.blockSignals(False)
        self.changed.emit(self.attr, float(v))

    def _from_spin(self, v):
        self.slider.blockSignals(True)
        self.slider.setValue(v)
        self.slider.blockSignals(False)
        self.changed.emit(self.attr, float(v))


def clear_layout(layout):
    while layout.count():
        item = layout.takeAt(0)
        w = item.widget()
        if w is not None:
            w.deleteLater()
        elif item.layout() is not None:
            clear_layout(item.layout())
