"""Вкладка «Сцены»: кнопки-пресеты. Щелчок по сцене = GO."""

import copy

from PySide6.QtCore import QSize, Qt, QTimer
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QListView,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)


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


class ScenesTab(QWidget):
    def __init__(self, show, engine, hub):
        super().__init__()
        self.show_, self.engine, self.hub = show, engine, hub
        lay = QHBoxLayout(self)

        self.list = QListWidget()
        self.list.setViewMode(QListView.IconMode)
        self.list.setGridSize(QSize(150, 74))
        self.list.setMovement(QListView.Static)
        self.list.setResizeMode(QListView.Adjust)
        self.list.setWordWrap(True)
        self.list.setSpacing(4)
        f = QFont()
        f.setPointSize(11)
        f.setBold(True)
        self.list.setFont(f)
        self.list.itemClicked.connect(self._go)
        lay.addWidget(self.list, 1)

        side = QVBoxLayout()
        box = QGroupBox("Выбранная сцена")
        form = QFormLayout(box)
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
        side.addWidget(box)

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
        side.addWidget(QLabel("Щелчок по сцене — включить,\nещё раз — выключить.\n"
                              "Правка сцены: «В программер» →\nизменить → записать под тем же именем."))
        side.addStretch()
        lay.addLayout(side)

        self.list.currentItemChanged.connect(self._selected)
        hub.changed.connect(lambda k: k in ("scenes", "all") and self.refresh())
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._highlight)
        self.timer.start(250)
        self.refresh()

    def refresh(self):
        cur = self._current_name()
        self.list.clear()
        for name in self.show_.scenes:
            it = QListWidgetItem(name)
            it.setData(Qt.UserRole, name)
            it.setTextAlignment(Qt.AlignCenter)
            it.setSizeHint(QSize(144, 68))
            self.list.addItem(it)
            if name == cur:
                self.list.setCurrentItem(it)

    def _current_name(self):
        it = self.list.currentItem()
        return it.data(Qt.UserRole) if it else None

    def _highlight(self):
        active = self.engine.current_scene
        for i in range(self.list.count()):
            it = self.list.item(i)
            on = it.data(Qt.UserRole) == active
            it.setBackground(QColor(200, 120, 0) if on else QColor(55, 55, 65))
            it.setForeground(QColor(0, 0, 0) if on else QColor(230, 230, 230))

    def _go(self, item):
        name = item.data(Qt.UserRole)
        fade = float(self.show_.scenes[name].get("fade", 0))
        self.engine.stop_chase()
        if self.engine.current_scene == name:          # повторное нажатие — выключить сцену
            self.engine.release(fade)
            self.hub.action.emit("release", "", fade)
        else:
            self.engine.go_scene(name)
            self.hub.action.emit("scene", name, fade)
        self.hub.changed.emit("programmer")             # сцена могла убрать значения из программера

    def _selected(self, *_):
        name = self._current_name()
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
