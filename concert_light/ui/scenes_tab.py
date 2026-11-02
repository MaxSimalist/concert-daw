"""Вкладка «Сцены»: кнопки-пресеты. Щелчок — включить (кнопка загорается), ещё раз — выключить."""

import copy

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QDoubleSpinBox, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel,
    QMessageBox, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from core.show import scene_rgb


def rename_refs(show, kind, old, new):
    """Обновить ссылки на сцену/чейз/эффект в чейзах и таймлайнах (new=None — удалить)."""
    actions = {"scenes": ("scene",), "chases": ("chase_start",), "effects": ("effect_on", "effect_off")}[kind]
    if kind == "scenes":
        for ch in show.chases.values():
            ch["steps"] = [new if s == old else s for s in ch["steps"] if not (s == old and new is None)]
    for song in show.songs.values():
        cues = []
        for c in song["cues"]:
            if c["action"] in actions and c.get("target") == old:
                if new is None:
                    continue
                c["target"] = new
            cues.append(c)
        song["cues"] = cues


def scene_color(scene):
    rgb = scene_rgb(scene)
    return QColor(*rgb) if rgb else None


class SceneButton(QPushButton):
    """Кнопка сцены: включена — горит (цветом сцены), выключена — тёмная."""

    def __init__(self, name, color):
        super().__init__(name)
        self.name = name
        self.color = color
        self.on = None
        self.selected = False
        self.setMinimumSize(140, 64)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFocusPolicy(Qt.NoFocus)
        self.set_state(False, False)

    def set_state(self, on, selected):
        if (on, selected) == (self.on, self.selected):
            return
        self.on, self.selected = on, selected
        border = "3px solid #ffffff" if selected else "3px solid transparent"
        if on:
            c = self.color or QColor(255, 170, 0)
            fg = "#000000" if c.lightness() > 110 else "#ffffff"
            css = (f"background:{c.name()}; color:{fg}; border:{border}; border-radius:8px;"
                   "font: bold 12pt; padding:4px;")
        else:
            bar = (self.color or QColor(110, 110, 120)).name()
            css = (f"background:#33333c; color:#b8b8c0; border:{border}; border-bottom:6px solid {bar};"
                   "border-radius:8px; font: bold 12pt; padding:4px;")
        self.setStyleSheet(f"QPushButton{{{css}}}")


class ScenesTab(QWidget):
    MIN_W = 150

    def __init__(self, show, engine, hub):
        super().__init__()
        self.show_, self.engine, self.hub = show, engine, hub
        self.buttons = []
        self.selected = None
        lay = QHBoxLayout(self)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.grid_host = QWidget()
        self.grid = QGridLayout(self.grid_host)
        self.grid.setSpacing(8)
        self.grid.setAlignment(Qt.AlignTop)
        self.scroll.setWidget(self.grid_host)
        self.empty = QLabel("Сцен пока нет. Запишите сцену на вкладке «Программер».")
        self.empty.setAlignment(Qt.AlignCenter)
        left = QVBoxLayout()
        left.addWidget(self.scroll, 1)
        left.addWidget(self.empty)
        lay.addLayout(left, 1)

        side = QVBoxLayout()
        self.box = QGroupBox("Выбранная сцена")
        form = QFormLayout(self.box)
        self.fade = QDoubleSpinBox()
        self.fade.setRange(0, 60)
        self.fade.setSuffix(" с")
        self.fade.valueChanged.connect(self._fade_changed)
        form.addRow("Фейд", self.fade)
        for text, slot in (("В программер (для правки)", self._to_programmer), ("Переименовать", self._rename),
                           ("Дублировать", self._duplicate), ("Удалить", self._delete)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            form.addRow(b)
        side.addWidget(self.box)

        rel = QGroupBox("Сброс")
        rf = QFormLayout(rel)
        self.rel_fade = QDoubleSpinBox()
        self.rel_fade.setRange(0, 60)
        self.rel_fade.setValue(2.0)
        self.rel_fade.setSuffix(" с")
        rf.addRow("Фейд", self.rel_fade)
        b = QPushButton("Погасить сцены (Release)")
        b.clicked.connect(self._release)
        rf.addRow(b)
        side.addWidget(rel)
        side.addWidget(QLabel("Щелчок по сцене — включить (кнопка\nзагорается), ещё раз — выключить.\n"
                              "Белая рамка — сцена, выбранная\nдля правки (настройки выше).\n"
                              "Правка сцены: «В программер» →\nизменить → записать под тем же именем."))
        side.addStretch()
        lay.addLayout(side)

        hub.changed.connect(lambda k: k in ("scenes", "all") and self.refresh())
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._highlight)
        self.timer.start(100)
        self.refresh()

    # --- кнопки ---------------------------------------------------------

    def refresh(self):
        if self.selected not in self.show_.scenes:
            self.selected = next(iter(self.show_.scenes), None)
        for btn in self.buttons:
            btn.deleteLater()
        self.buttons = []
        for name, sc in self.show_.scenes.items():
            btn = SceneButton(name, scene_color(sc))
            btn.clicked.connect(lambda _=False, n=name: self._go(n))
            self.buttons.append(btn)
        self._layout_buttons()
        self.empty.setVisible(not self.buttons)
        self._selected()
        self._highlight()

    def _layout_buttons(self):
        cols = max(1, self.scroll.viewport().width() // self.MIN_W)
        for i, btn in enumerate(self.buttons):
            self.grid.addWidget(btn, i // cols, i % cols)
        self._cols = cols

    def resizeEvent(self, e):
        super().resizeEvent(e)
        cols = max(1, self.scroll.viewport().width() // self.MIN_W)
        if cols != getattr(self, "_cols", None) and self.buttons:
            for btn in self.buttons:
                self.grid.removeWidget(btn)
            self._layout_buttons()

    def _current_name(self):
        return self.selected

    def _highlight(self):
        active = self.engine.current_scene
        for btn in self.buttons:
            btn.set_state(btn.name == active, btn.name == self.selected)

    def _go(self, name):
        if name not in self.show_.scenes:
            return
        self.selected = name
        self._selected()
        fade = float(self.show_.scenes[name].get("fade", 0))
        self.engine.stop_chase()
        if self.engine.current_scene == name:          # повторное нажатие — выключить сцену
            self.engine.release(fade)
            self.hub.action.emit("release", "", fade)
        else:
            self.engine.go_scene(name)
            self.hub.action.emit("scene", name, fade)
        self._highlight()                               # кнопка меняет цвет сразу, без задержки
        self.hub.changed.emit("programmer")             # сцена могла убрать значения из программера

    def _selected(self):
        name = self.selected
        self.box.setTitle(f"Сцена «{name}»" if name else "Выбранная сцена")
        self.box.setEnabled(bool(name))
        self.fade.blockSignals(True)
        self.fade.setValue(self.show_.scenes[name].get("fade", 0) if name in self.show_.scenes else 0)
        self.fade.blockSignals(False)

    def _fade_changed(self, v):
        name = self._current_name()
        if name in self.show_.scenes:
            self.show_.scenes[name]["fade"] = v

    def _to_programmer(self):
        name = self._current_name()
        if name:
            self.engine.load_scene_to_programmer(name)
            self.hub.changed.emit("patch")  # перестроит программер
            QMessageBox.information(self, "Программер", f"Сцена «{name}» загружена в программер.")

    def _rename(self):
        old = self._current_name()
        if not old:
            return
        new, ok = QInputDialog.getText(self, "Переименовать", "Новое имя:", text=old)
        new = new.strip()
        if not ok or not new or new == old:
            return
        if new in self.show_.scenes:
            QMessageBox.warning(self, "Ошибка", "Такое имя уже есть")
            return
        self.show_.scenes = {(new if k == old else k): v for k, v in self.show_.scenes.items()}
        self.selected = new
        rename_refs(self.show_, "scenes", old, new)
        if self.engine.current_scene == old:
            self.engine.current_scene = new
        self.hub.changed.emit("all")

    def _duplicate(self):
        name = self._current_name()
        if not name:
            return
        n = 2
        while f"{name} ({n})" in self.show_.scenes:
            n += 1
        self.show_.scenes[f"{name} ({n})"] = copy.deepcopy(self.show_.scenes[name])
        self.hub.changed.emit("scenes")

    def _delete(self):
        name = self._current_name()
        if not name:
            return
        if QMessageBox.question(self, "Удалить", f"Удалить сцену «{name}»?\nОна уберётся из чейзов и таймлайнов.") != QMessageBox.Yes:
            return
        del self.show_.scenes[name]
        rename_refs(self.show_, "scenes", name, None)
        self.hub.changed.emit("all")

    def _release(self):
        self.engine.release(self.rel_fade.value())
        self.hub.action.emit("release", "", self.rel_fade.value())
