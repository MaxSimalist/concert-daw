"""Вкладка «Сцены»: кнопки-пресеты. Щелчок — включить (кнопка загорается), ещё раз — выключить."""

import copy

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox, QDoubleSpinBox, QFormLayout, QGridLayout, QGroupBox, QHBoxLayout, QInputDialog,
    QLabel, QListWidget, QMessageBox, QPushButton, QScrollArea, QSizePolicy, QVBoxLayout, QWidget,
)

from core.show import scene_rgb, scene_steps, set_scene_steps


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
        self._label = name
        self.setMinimumSize(140, 64)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setFocusPolicy(Qt.NoFocus)
        self.set_state(False, False)

    def set_label(self, text):
        if text != self._label:
            self._label = text
            self.setText(text)

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
        bl = QVBoxLayout(self.box)
        bl.addWidget(QLabel("Шаги сцены:"))
        self.steps = QListWidget()
        self.steps.setMaximumHeight(170)
        self.steps.currentRowChanged.connect(self._step_selected)
        self.steps.itemDoubleClicked.connect(lambda _i: self._show_step())
        bl.addWidget(self.steps)
        tf = QFormLayout()
        self.fade = QDoubleSpinBox()
        self.fade.setRange(0, 60)
        self.fade.setSuffix(" с")
        self.fade.valueChanged.connect(self._timing_changed)
        self.hold = QDoubleSpinBox()
        self.hold.setRange(0, 600)
        self.hold.setSuffix(" с")
        self.hold.setToolTip("Сколько стоять на шаге после фейда, прежде чем перейти к следующему")
        self.hold.valueChanged.connect(self._timing_changed)
        tf.addRow("Фейд шага", self.fade)
        tf.addRow("Пауза шага", self.hold)
        bl.addLayout(tf)
        self.loop = QCheckBox("Шаги по кругу (выкл — один раз и стоп на последнем)")
        self.loop.toggled.connect(self._loop_changed)
        bl.addWidget(self.loop)
        grid = QGridLayout()
        for i, (text, slot) in enumerate((
                ("▶ Показать шаг", self._show_step), ("Шаг в программер", self._to_programmer),
                ("Заменить из программера", self._replace_step), ("Дублировать шаг", self._dup_step),
                ("↑ Выше", lambda: self._move_step(-1)), ("↓ Ниже", lambda: self._move_step(1)),
                ("Удалить шаг", self._del_step))):
            b = QPushButton(text)
            b.clicked.connect(slot)
            grid.addWidget(b, i // 2, i % 2)
        bl.addLayout(grid)
        srow = QHBoxLayout()
        for text, slot in (("Переименовать", self._rename), ("Дублировать сцену", self._duplicate),
                           ("Удалить сцену", self._delete)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            srow.addWidget(b)
        bl.addLayout(srow)
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
        side.addWidget(QLabel("Щелчок по сцене — включить (кнопка загорается),\nещё раз — выключить. "
                              "Белая рамка — сцена\nдля правки. Шаги записываются на вкладке\n"
                              "«Программер» кнопкой «+ Добавить шаг в сцену»."))
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
        prog = self.engine.scene_progress() if active else None
        for btn in self.buttons:
            on = btn.name == active
            sc = self.show_.scenes.get(btn.name)
            n = len(scene_steps(sc)) if sc else 1
            if on and prog and prog[1] > 1:
                btn.set_label(f"{btn.name}\n▶ шаг {prog[0]}/{prog[1]}")
            elif n > 1:
                btn.set_label(f"{btn.name}\nшагов: {n}")
            else:
                btn.set_label(btn.name)
            btn.set_state(on, btn.name == self.selected)
        if active and active == self.selected and prog and self.engine.seq:
            row = prog[0] - 1
            if self.steps.currentRow() != row and not self.steps.hasFocus():
                self.steps.blockSignals(True)
                self.steps.setCurrentRow(row)
                self.steps.blockSignals(False)
                self._step_selected(row)

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

    def _selected(self, keep_row=None):
        name = self.selected
        self.box.setTitle(f"Сцена «{name}»" if name else "Выбранная сцена")
        self.box.setEnabled(bool(name))
        self.steps.blockSignals(True)
        self.steps.clear()
        sc = self.show_.scenes.get(name)
        if sc:
            steps = scene_steps(sc)
            for i, st in enumerate(steps):
                n = len(st["values"])
                self.steps.addItem(f"{i + 1}.  фейд {float(st.get('fade', 0)):.1f} с,  "
                                   f"пауза {float(st.get('hold', 0)):.1f} с   ({n} приб.)")
            self.steps.setCurrentRow(min(keep_row or 0, len(steps) - 1))
            self.loop.blockSignals(True)
            self.loop.setChecked(bool(sc.get("loop", True)))
            self.loop.setEnabled(len(steps) > 1)
            self.loop.blockSignals(False)
        self.steps.blockSignals(False)
        self._step_selected(self.steps.currentRow())

    # --- шаги -----------------------------------------------------------

    def _scene(self):
        return self.show_.scenes.get(self.selected)

    def _step_list(self):
        sc = self._scene()
        return [dict(s) for s in scene_steps(sc)] if sc else []

    def _save_steps(self, steps, row=None):
        sc = self._scene()
        if sc is None or not steps:
            return
        set_scene_steps(sc, steps)
        self._selected(keep_row=row if row is not None else self.steps.currentRow())
        self._highlight()

    def _step_selected(self, row):
        steps = self._step_list()
        ok = 0 <= row < len(steps)
        for w, key in ((self.fade, "fade"), (self.hold, "hold")):
            w.blockSignals(True)
            w.setValue(float(steps[row].get(key, 0)) if ok else 0)
            w.blockSignals(False)
        self.hold.setEnabled(len(steps) > 1)

    def _timing_changed(self, *_):
        row = self.steps.currentRow()
        steps = self._step_list()
        if not 0 <= row < len(steps):
            return
        steps[row]["fade"] = self.fade.value()
        steps[row]["hold"] = self.hold.value()
        self._save_steps(steps, row)

    def _loop_changed(self, on):
        sc = self._scene()
        if sc is not None:
            sc["loop"] = bool(on)

    def _show_step(self):
        row = self.steps.currentRow()
        if self.selected and row >= 0:
            self.engine.stop_chase()
            self.engine.go_scene(self.selected, sequence=False, step=row)
            self._highlight()

    def _replace_step(self):
        row = self.steps.currentRow()
        steps = self._step_list()
        if not self.selected or not 0 <= row < len(steps):
            return
        st = steps[row]
        if not self.engine.record_step(self.selected, st.get("fade", 0), st.get("hold", 0), index=row):
            QMessageBox.warning(self, "Шаг", "Программер пуст — сначала выставьте значения на вкладке «Программер»")
            return
        self._selected(keep_row=row)

    def _dup_step(self):
        row = self.steps.currentRow()
        steps = self._step_list()
        if 0 <= row < len(steps):
            steps.insert(row + 1, copy.deepcopy(steps[row]))
            self._save_steps(steps, row + 1)

    def _move_step(self, d):
        row = self.steps.currentRow()
        steps = self._step_list()
        new = row + d
        if 0 <= row < len(steps) and 0 <= new < len(steps):
            steps[row], steps[new] = steps[new], steps[row]
            self._save_steps(steps, new)

    def _del_step(self):
        row = self.steps.currentRow()
        steps = self._step_list()
        if not 0 <= row < len(steps):
            return
        if len(steps) == 1:
            QMessageBox.information(self, "Шаг", "Это единственный шаг — удалите сцену целиком.")
            return
        del steps[row]
        self._save_steps(steps, max(0, row - 1))

    def _to_programmer(self):
        name = self._current_name()
        row = max(0, self.steps.currentRow())
        if name:
            self.engine.load_scene_to_programmer(name, step=row)
            self.hub.changed.emit("programmer")
            QMessageBox.information(self, "Программер",
                                    f"Шаг {row + 1} сцены «{name}» загружен в программер.\n"
                                    "Измените его и нажмите здесь «Заменить из программера».")

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
