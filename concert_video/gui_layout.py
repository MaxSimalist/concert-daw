"""
gui_layout.py — Элементы, благодаря которым окно помещается на любом экране.

* ElidedLabel — надпись, которая не растягивает окно: длинный текст сокращается
  с «…», полный текст — во всплывающей подсказке. Обычный QLabel требует от окна
  ширину всего своего текста, поэтому длинное название трека или имя звуковой
  карты в строке состояния выталкивали интерфейс за край экрана ноутбука.
* FlowLayout — ряд кнопок, который переносится на следующую строку, если не
  помещается по ширине (как слова в тексте), вместо того чтобы расширять окно.
"""
from __future__ import annotations

from PySide6.QtCore import QByteArray, QEvent, QMimeData, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QColor, QDrag, QPainter
from PySide6.QtWidgets import QApplication, QLabel, QLayout, QSizePolicy, QWidget


class ElidedLabel(QLabel):
    MIN_WIDTH = 24

    def __init__(self, text: str = "", parent=None, full_tooltip: bool = False):
        super().__init__(parent)
        self._full = ""
        self._full_tooltip = full_tooltip
        self.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        self.setText(text)

    # ── текст ───────────────────────────────────────────────────────────────
    def setText(self, text: str) -> None:  # noqa: N802 (имя метода Qt)
        self._full = text or ""
        if self._full_tooltip:
            self.setToolTip(self._full)
        self._refresh()
        self.updateGeometry()

    def text(self) -> str:
        return self._full

    def _refresh(self) -> None:
        width = max(self.MIN_WIDTH, self.contentsRect().width())
        shown = self.fontMetrics().elidedText(self._full, Qt.ElideRight, width)
        if QLabel.text(self) != shown:
            QLabel.setText(self, shown)

    # ── размеры ─────────────────────────────────────────────────────────────
    def minimumSizeHint(self) -> QSize:  # noqa: N802
        h = super().minimumSizeHint().height()
        return QSize(self.MIN_WIDTH, h)

    def sizeHint(self) -> QSize:  # noqa: N802
        fm = self.fontMetrics()
        m = self.contentsMargins()
        return QSize(fm.horizontalAdvance(self._full) + m.left() + m.right() + 4,
                     super().sizeHint().height())

    def resizeEvent(self, e) -> None:  # noqa: N802
        super().resizeEvent(e)
        self._refresh()

    def changeEvent(self, e) -> None:  # noqa: N802
        super().changeEvent(e)
        if e.type() in (QEvent.FontChange, QEvent.StyleChange):
            self._refresh()
            self.updateGeometry()


class FlowLayout(QLayout):
    """Раскладка «как текст»: элементы слева направо, при нехватке места — новая строка."""

    def __init__(self, parent=None, hspacing: int = 6, vspacing: int = 4):
        super().__init__(parent)
        self._items = []
        self._h = hspacing
        self._v = vspacing
        self.setContentsMargins(0, 0, 0, 0)

    def __del__(self):
        item = self.takeAt(0)
        while item:
            item = self.takeAt(0)

    def addItem(self, item) -> None:  # noqa: N802
        self._items.append(item)

    def count(self) -> int:
        return len(self._items)

    def itemAt(self, index: int):  # noqa: N802
        return self._items[index] if 0 <= index < len(self._items) else None

    def takeAt(self, index: int):  # noqa: N802
        return self._items.pop(index) if 0 <= index < len(self._items) else None

    def expandingDirections(self):  # noqa: N802
        return Qt.Orientation(0)

    def hasHeightForWidth(self) -> bool:  # noqa: N802
        return True

    def heightForWidth(self, width: int) -> int:  # noqa: N802
        return self._do_layout(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect: QRect) -> None:  # noqa: N802
        super().setGeometry(rect)
        self._do_layout(rect, False)

    def _visible(self):
        return [it for it in self._items if not it.isEmpty()]

    def sizeHint(self) -> QSize:  # noqa: N802
        """Желаемый размер — всё в одну строку."""
        items = self._visible()
        m = self.contentsMargins()
        w = sum(it.sizeHint().width() for it in items) + self._h * max(0, len(items) - 1)
        h = max((it.sizeHint().height() for it in items), default=0)
        return QSize(w + m.left() + m.right(), h + m.top() + m.bottom())

    def minimumSize(self) -> QSize:  # noqa: N802
        """Минимум — самый широкий элемент: остальное перенесётся на новые строки."""
        size = QSize()
        for it in self._visible():
            size = size.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        return size + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _do_layout(self, rect: QRect, test_only: bool) -> int:
        m = self.contentsMargins()
        eff = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line_h = eff.x(), eff.y(), 0
        for it in self._visible():
            hint = it.sizeHint()
            if x > eff.x() and x + hint.width() > eff.right() + 1:
                x = eff.x()
                y += line_h + self._v
                line_h = 0
            if not test_only:
                w = min(hint.width(), eff.width()) if eff.width() > 0 else hint.width()
                it.setGeometry(QRect(QPoint(x, y), QSize(w, hint.height())))
            x += hint.width() + self._h
            line_h = max(line_h, hint.height())
        return y + line_h - rect.y() + m.bottom()


def scrollable(widget):
    """Обернуть вкладку/панель в прокрутку: на невысоком экране — полоса прокрутки,
    а не обрезанные кнопки."""
    from PySide6.QtWidgets import QFrame, QScrollArea
    area = QScrollArea()
    area.setWidget(widget)
    area.setWidgetResizable(True)
    area.setFrameShape(QFrame.NoFrame)
    return area


def fit_to_screen(window, width: int, height: int) -> None:
    """Желаемый размер окна/диалога, но не больше доступной области экрана (минус панель задач)."""
    screen = window.screen() if hasattr(window, "screen") and window.screen() else None
    if screen is None:
        from PySide6.QtGui import QGuiApplication
        screen = QGuiApplication.primaryScreen()
    if screen is None:
        window.resize(width, height)
        return
    avail = screen.availableGeometry()
    window.resize(min(width, avail.width() - 20), min(height, avail.height() - 40))


# ══════════════════════════════════════════════════════════════════════════════
# Перестановка фейдеров мышью
# ══════════════════════════════════════════════════════════════════════════════
def _mime(group: str) -> str:
    return f"application/x-concertdaw-{group}"


class DragStripMixin:
    """Для стрипа (QFrame): потяните мышью за название / пустое место — стрип можно
    перенести на другое место. Фейдер, кнопки и списки работают как обычно
    (они сами забирают нажатия мыши, до стрипа они не доходят)."""
    drag_group = "strip"

    def drag_key(self) -> str:
        raise NotImplementedError

    def mousePressEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.LeftButton:
            self._drag_start = e.position().toPoint()
        super().mousePressEvent(e)

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        start = getattr(self, "_drag_start", None)
        if start is None or not (e.buttons() & Qt.LeftButton):
            return super().mouseMoveEvent(e)
        if (e.position().toPoint() - start).manhattanLength() < QApplication.startDragDistance():
            return
        self._drag_start = None
        mime = QMimeData()
        mime.setData(_mime(self.drag_group), QByteArray(self.drag_key().encode("utf-8")))
        drag = QDrag(self)
        drag.setMimeData(mime)
        pix = self.grab()
        drag.setPixmap(pix.scaledToHeight(min(160, pix.height())))
        drag.exec(Qt.MoveAction)

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        self._drag_start = None
        super().mouseReleaseEvent(e)


class ReorderHost(QWidget):
    """Контейнер ряда стрипов: принимает перетаскиваемый стрип своей группы,
    рисует жёлтую черту в месте вставки и сообщает moved(ключ, индекс вставки)."""
    moved = Signal(str, int)

    def __init__(self, group: str, parent=None):
        super().__init__(parent)
        self.group = group
        self.setAcceptDrops(True)
        self._line_x: int | None = None

    def _strips(self) -> list:
        lay = self.layout()
        out = []
        for i in range(lay.count() if lay else 0):
            w = lay.itemAt(i).widget()
            if w is not None and isinstance(w, DragStripMixin) and w.isVisible():
                out.append(w)
        return out

    def _index_at(self, x: int) -> tuple[int, int]:
        strips = self._strips()
        for i, w in enumerate(strips):
            g = w.geometry()
            if x < g.center().x():
                return i, g.left() - 3
        if strips:
            return len(strips), strips[-1].geometry().right() + 3
        return 0, 2

    def _accepts(self, e) -> bool:
        return e.mimeData().hasFormat(_mime(self.group))

    def dragEnterEvent(self, e) -> None:  # noqa: N802
        if self._accepts(e):
            e.acceptProposedAction()
        else:
            e.ignore()   # файлы и пр. — родителю (например, зоне микшера)

    def dragMoveEvent(self, e) -> None:  # noqa: N802
        if not self._accepts(e):
            e.ignore()
            return
        e.acceptProposedAction()
        _, x = self._index_at(int(e.position().x()))
        if x != self._line_x:
            self._line_x = x
            self.update()

    def dragLeaveEvent(self, e) -> None:  # noqa: N802
        self._line_x = None
        self.update()

    def dropEvent(self, e) -> None:  # noqa: N802
        if not self._accepts(e):
            e.ignore()
            return
        key = bytes(e.mimeData().data(_mime(self.group))).decode("utf-8")
        idx, _ = self._index_at(int(e.position().x()))
        self._line_x = None
        self.update()
        e.acceptProposedAction()
        self.moved.emit(key, idx)

    def paintEvent(self, e) -> None:  # noqa: N802
        super().paintEvent(e)
        if self._line_x is not None:
            p = QPainter(self)
            p.fillRect(self._line_x - 2, 0, 4, self.height(), QColor("#f7c948"))
            p.end()


def move_in_list(items: list, from_index: int, insert_index: int) -> list:
    """Новый список: элемент from_index перенесён на место вставки insert_index
    (индекс вставки — как в ряду ДО переноса)."""
    items = list(items)
    if not (0 <= from_index < len(items)):
        return items
    item = items.pop(from_index)
    if insert_index > from_index:
        insert_index -= 1
    items.insert(max(0, min(insert_index, len(items))), item)
    return items
