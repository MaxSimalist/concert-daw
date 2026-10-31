"""
views.py — Окна, показывающие сцены:
* StageView — холст сцены, вписанный в окно с сохранением пропорций. В режиме
  редактирования источники можно двигать мышью, тянуть за уголок (размер),
  Ctrl+колесо — масштаб, прилипание к краям и центру холста.
* TransitionView — «эфир»: холст + слои переходов (растворение, через чёрное,
  затемнение). Используется и в главном окне, и в полноэкранных проекторах —
  переход везде одинаковый.
* ProjectorWindow — полноэкранный вывод на выбранный монитор / проектор
  (без рамки и курсора) или в обычном окне.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QEasingCurve, QPointF, QRectF, Qt, QVariantAnimation, Signal
from PySide6.QtGui import QBrush, QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QFrame, QGraphicsItem, QGraphicsOpacityEffect, QGraphicsView, QLabel, QMenu,
                               QWidget)

SNAP = 18.0            # px холста: на таком расстоянии источник прилипает к краю/центру
GPU = {"on": True}     # рисовать эфир и проекторы видеокартой (OpenGL) — задаётся из настроек


def _gl_viewport():
    """Поверхность OpenGL для QGraphicsView: масштабирование видео 1080p на весь экран
    делает видеокарта, а не процессор. Нет OpenGL — None (обычная отрисовка)."""
    if not GPU["on"]:
        return None
    try:
        from PySide6.QtOpenGLWidgets import QOpenGLWidget
        return QOpenGLWidget()
    except Exception:
        return None
HANDLE = 22.0          # px холста: «уголок» для изменения размера


class StageView(QGraphicsView):
    source_selected = Signal(str)              # id источника ("" — ничего)
    source_moved = Signal(str, float, float)   # id, x, y (после отпускания мыши)
    source_resized = Signal(str, float, float, float, float)   # id, x, y, w, h
    source_menu = Signal(str, object)          # id, глобальная позиция

    def __init__(self, editable: bool = False, parent=None):
        super().__init__(parent)
        self.editable = editable
        self.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setFrameShape(QFrame.NoFrame)
        self.setBackgroundBrush(QBrush(QColor("#0b0d10")))
        self.setViewportUpdateMode(QGraphicsView.FullViewportUpdate)
        if not editable:
            # эфир/проектор: без сглаживания линий (его там нет), без сохранения состояния кисти
            self.setRenderHints(QPainter.SmoothPixmapTransform)
            self.setOptimizationFlags(QGraphicsView.DontSavePainterState |
                                      QGraphicsView.DontAdjustForAntialiasing)
            gl = _gl_viewport()
            if gl is not None:
                self.setViewport(gl)
        self.setInteractive(editable)
        self.setFocusPolicy(Qt.ClickFocus if editable else Qt.NoFocus)
        self._resize: Optional[tuple] = None
        self._moving: Optional[str] = None
        self._suppress = False

    # ── вписывание в окно ──────────────────────────────────────────────────
    def setScene(self, scene) -> None:  # noqa: N802
        if self.scene() is not None and self.editable:
            try:
                self.scene().selectionChanged.disconnect(self._on_selection)
            except (RuntimeError, TypeError):
                pass
        super().setScene(scene)
        if scene is not None and self.editable:
            scene.selectionChanged.connect(self._on_selection)
        self.refit()

    def refit(self) -> None:
        if self.scene() is not None:
            self.fitInView(self.scene().sceneRect(), Qt.KeepAspectRatio)

    def resizeEvent(self, e) -> None:  # noqa: N802
        super().resizeEvent(e)
        self.refit()

    def showEvent(self, e) -> None:  # noqa: N802
        super().showEvent(e)
        self.refit()

    def wheelEvent(self, e) -> None:  # noqa: N802
        if not self.editable:
            return
        item = self._selected()
        if item is None or not (e.modifiers() & Qt.ControlModifier):
            return
        sid = item.data(0)
        r = self._item_rect(item)
        k = 1.05 if e.angleDelta().y() > 0 else 1 / 1.05
        w, h = r.width() * k, r.height() * k
        c = r.center()
        self.source_resized.emit(sid, c.x() - w / 2, c.y() - h / 2, w, h)
        e.accept()

    # ── выбор / перемещение / размер ───────────────────────────────────────
    def _selected(self) -> Optional[QGraphicsItem]:
        if self.scene() is None:
            return None
        items = self.scene().selectedItems()
        return items[0] if items else None

    @staticmethod
    def _item_rect(item: QGraphicsItem) -> QRectF:
        br = item.boundingRect()
        return QRectF(item.pos().x(), item.pos().y(), br.width(), br.height())

    def select_source(self, source_id: str) -> None:
        if self.scene() is None:
            return
        self._suppress = True
        for it in self.scene().items():
            it.setSelected(bool(source_id) and it.data(0) == source_id)
        self._suppress = False
        self.viewport().update()

    def _on_selection(self) -> None:
        if self._suppress:
            return
        item = self._selected()
        self.source_selected.emit(item.data(0) if item is not None else "")
        self.viewport().update()

    def mousePressEvent(self, e) -> None:  # noqa: N802
        if not self.editable or self.scene() is None:
            e.ignore()                       # эфир/проектор: клик уходит родительскому окну
            return
        pt = self.mapToScene(e.position().toPoint())
        item = self._selected()
        if item is not None and e.button() == Qt.LeftButton and item.flags() & QGraphicsItem.ItemIsMovable:
            r = self._item_rect(item)
            if abs(pt.x() - r.right()) < HANDLE * self._k() and abs(pt.y() - r.bottom()) < HANDLE * self._k():
                self._resize = (item, r, pt)
                e.accept()
                return
        if e.button() == Qt.RightButton:
            hit = next((it for it in self.scene().items(pt) if it.data(0)), None)
            if hit is not None:
                self.select_source(hit.data(0))
                self.source_selected.emit(hit.data(0))
                self.source_menu.emit(hit.data(0), e.globalPosition().toPoint())
            return
        super().mousePressEvent(e)
        item = self._selected()
        self._moving = item.data(0) if item is not None else None

    def _k(self) -> float:
        """Сколько пикселей холста в одном пикселе экрана (для удобного «уголка»)."""
        t = self.transform()
        return 1.0 / max(0.01, t.m11())

    def mouseMoveEvent(self, e) -> None:  # noqa: N802
        if self._resize is not None:
            item, r, start = self._resize
            pt = self.mapToScene(e.position().toPoint())
            w = max(20.0, r.width() + pt.x() - start.x())
            h = max(20.0, r.height() + pt.y() - start.y())
            if not (e.modifiers() & Qt.ShiftModifier) and r.height() > 0:
                h = w * r.height() / r.width()        # пропорционально (Shift — свободно)
            self.source_resized.emit(item.data(0), r.x(), r.y(), w, h)
            return
        if self.editable:
            pt = self.mapToScene(e.position().toPoint())
            item = self._selected()
            near = False
            if item is not None:
                r = self._item_rect(item)
                near = abs(pt.x() - r.right()) < HANDLE * self._k() and abs(pt.y() - r.bottom()) < HANDLE * self._k()
            self.viewport().setCursor(Qt.SizeFDiagCursor if near else Qt.ArrowCursor)
        super().mouseMoveEvent(e)

    def mouseReleaseEvent(self, e) -> None:  # noqa: N802
        if self._resize is not None:
            self._resize = None
            return
        super().mouseReleaseEvent(e)
        if self._moving:
            item = self._selected()
            if item is not None and item.data(0) == self._moving:
                x, y = self._snap(item)
                self.source_moved.emit(self._moving, x, y)
        self._moving = None

    def _snap(self, item: QGraphicsItem) -> tuple[float, float]:
        r = self._item_rect(item)
        cw, ch = self.scene().sceneRect().width(), self.scene().sceneRect().height()
        x, y = r.x(), r.y()
        for target in (0.0, cw - r.width(), (cw - r.width()) / 2):
            if abs(x - target) < SNAP:
                x = target
                break
        for target in (0.0, ch - r.height(), (ch - r.height()) / 2):
            if abs(y - target) < SNAP:
                y = target
                break
        return x, y

    def drawForeground(self, painter: QPainter, rect) -> None:  # noqa: N802
        if not self.editable or self.scene() is None:
            return
        canvas = self.scene().sceneRect()
        painter.setPen(QPen(QColor("#56616c"), 0))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(canvas)
        item = self._selected()
        if item is None:
            return
        r = self._item_rect(item)
        k = self._k()
        painter.setPen(QPen(QColor("#ff5050"), 2 * k))
        painter.drawRect(r)
        s = 10 * k
        painter.setBrush(QColor("#ff5050"))
        for pt in (r.topLeft(), r.topRight(), r.bottomLeft()):
            painter.drawRect(QRectF(pt.x() - s / 2, pt.y() - s / 2, s, s))
        painter.setBrush(QColor("#ffffff"))
        painter.drawRect(QRectF(r.right() - s, r.bottom() - s, s * 1.6, s * 1.6))


class TransitionView(QWidget):
    """Эфир: холст + слои перехода и затемнения поверх."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.view = StageView(False, self)
        self.overlay = QLabel(self)               # «снимок» прошлой сцены во время растворения
        self.overlay.setScaledContents(True)
        self.overlay_fx = QGraphicsOpacityEffect(self.overlay)
        self.overlay.setGraphicsEffect(self.overlay_fx)
        self.overlay.hide()
        self.black = QWidget(self)                # затемнение
        self.black.setStyleSheet("background:#000000")
        self.black_fx = QGraphicsOpacityEffect(self.black)
        self.black_fx.setOpacity(0.0)
        self.black.setGraphicsEffect(self.black_fx)
        self.black.hide()
        self.black_level = 0.0
        self.overlay_level = 0.0
        self.snapshot: Optional[QPixmap] = None
        self._anims: list = []
        self.setStyleSheet("background:#000000")

    def resizeEvent(self, e) -> None:  # noqa: N802
        super().resizeEvent(e)
        for w in (self.view, self.overlay, self.black):
            w.setGeometry(self.rect())

    def _animate(self, start: float, end: float, ms: int, on_value, on_done=None) -> None:
        anim = QVariantAnimation(self)
        anim.setStartValue(float(start))
        anim.setEndValue(float(end))
        anim.setDuration(max(1, int(ms)))
        anim.setEasingCurve(QEasingCurve.InOutQuad)
        anim.valueChanged.connect(lambda v: on_value(float(v)))

        def done():
            if anim in self._anims:
                self._anims.remove(anim)
            if on_done:
                on_done()
        anim.finished.connect(done)
        self._anims.append(anim)
        anim.start()

    def _set_overlay(self, v: float) -> None:
        self.overlay_level = v
        self.overlay_fx.setOpacity(v)
        if v <= 0.001:
            self.overlay.hide()
            self.snapshot = None

    def _set_black(self, v: float) -> None:
        self.black_level = v
        self.black_fx.setOpacity(v)
        self.black.setVisible(v > 0.001)

    def show_scene(self, canvas, kind: str = "cut", ms: int = 0) -> None:
        if canvas is self.view.scene():
            return
        if kind == "cut" or ms <= 0 or self.view.scene() is None or not self.isVisible():
            self.view.setScene(canvas)
            return
        if kind == "fade_black":
            base = self.black_level

            def switch():
                self.view.setScene(canvas)
                self._animate(1.0, base, ms // 2, self._set_black)
            self._animate(base, 1.0, ms // 2, self._set_black, switch)
            self.black.raise_()
            self.black.show()
            return
        # растворение: снимок старой сцены поверх новой, плавно исчезает
        self.snapshot = self._snapshot()
        self.overlay.setPixmap(self.snapshot)
        self.overlay.setGeometry(self.rect())
        self._set_overlay(1.0)
        self.overlay.show()
        self.overlay.raise_()
        self.black.raise_()
        self.view.setScene(canvas)
        self._animate(1.0, 0.0, ms, self._set_overlay)

    def _snapshot(self) -> QPixmap:
        """Кадр текущей сцены размером с окно — рисуем сам холст (работает и с OpenGL,
        где «снимок окна» мог бы получиться чёрным)."""
        size = self.view.viewport().size()
        pix = QPixmap(max(1, size.width()), max(1, size.height()))
        pix.fill(QColor("#000000"))
        canvas = self.view.scene()
        if canvas is not None:
            target = self.view.mapFromScene(canvas.sceneRect()).boundingRect()
            painter = QPainter(pix)
            painter.setRenderHint(QPainter.SmoothPixmapTransform)
            canvas.render(painter, QRectF(target), canvas.sceneRect(), Qt.KeepAspectRatio)
            painter.end()
        return pix

    def set_black(self, on: bool, ms: int = 400) -> None:
        self.black.raise_()
        self.black.show()
        self._animate(self.black_level, 1.0 if on else 0.0, ms, self._set_black)


class ProjectorWindow(QWidget):
    """Полноэкранный вывод эфира на монитор/проектор (или в окне)."""

    closed = Signal(object)

    def __init__(self, screen=None, windowed: bool = False):
        super().__init__(None, Qt.Window)
        self.screen_name = screen.name() if screen is not None else ""
        self.windowed = windowed
        self.setWindowTitle("Concert Video — проектор" + (f" ({self.screen_name})" if self.screen_name else ""))
        self.tv = TransitionView(self)
        self.setStyleSheet("background:#000000")
        if not windowed:
            self.setWindowFlags(Qt.Window | Qt.FramelessWindowHint)
            self.setCursor(Qt.BlankCursor)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.tv.view.setContextMenuPolicy(Qt.NoContextMenu)
        if screen is not None and not windowed:
            self.setScreen(screen)
            self.setGeometry(screen.geometry())
        else:
            self.resize(960, 540)

    def open(self) -> None:
        if self.windowed:
            self.show()
        else:
            self.showFullScreen()

    def resizeEvent(self, e) -> None:  # noqa: N802
        super().resizeEvent(e)
        self.tv.setGeometry(self.rect())

    def _menu(self, pos) -> None:
        self.setCursor(Qt.ArrowCursor)
        m = QMenu(self)
        a_close = m.addAction("Закрыть проектор")
        chosen = m.exec(self.mapToGlobal(pos))
        if not self.windowed:
            self.setCursor(Qt.BlankCursor)
        if chosen is a_close:
            self.close()

    def keyPressEvent(self, e) -> None:  # noqa: N802
        # Esc на проекторе ничего не делает — случайное нажатие не должно гасить экран на концерте
        e.ignore()

    def closeEvent(self, e) -> None:  # noqa: N802
        self.closed.emit(self)
        super().closeEvent(e)
