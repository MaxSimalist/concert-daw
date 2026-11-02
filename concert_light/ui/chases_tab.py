"""Вкладка «Чейзы»: последовательности сцен с шагом, фейдом и tap tempo."""

import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel,
    QListWidget, QMessageBox, QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from .scenes_tab import rename_refs


class ChasesTab(QWidget):
    def __init__(self, show, engine, hub):
        super().__init__()
        self.show_, self.engine, self.hub = show, engine, hub
        self._taps = []
        lay = QHBoxLayout(self)

        left = QVBoxLayout()
        self.list = QListWidget()
        self.list.currentTextChanged.connect(self._load)
        left.addWidget(self.list, 1)
        row = QHBoxLayout()
        for text, slot in (("Новый", self._new), ("Удалить", self._delete)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            row.addWidget(b)
        left.addLayout(row)
        lay.addLayout(left, 1)

        self.editor = QWidget()
        ed = QVBoxLayout(self.editor)
        steps_box = QGroupBox("Шаги (сцены по порядку)")
        sb = QVBoxLayout(steps_box)
        self.steps = QListWidget()
        sb.addWidget(self.steps)
        srow = QHBoxLayout()
        self.scene_combo = QComboBox()
        srow.addWidget(self.scene_combo, 1)
        for text, slot in (("+ Шаг", self._add_step), ("Удалить", self._del_step), ("↑", lambda: self._move(-1)),
                           ("↓", lambda: self._move(1))):
            b = QPushButton(text)
            b.clicked.connect(slot)
            srow.addWidget(b)
        sb.addLayout(srow)
        ed.addWidget(steps_box, 1)

        tbox = QGroupBox("Время")
        tf = QFormLayout(tbox)
        self.step_time = QDoubleSpinBox()
        self.step_time.setRange(0.05, 60)
        self.step_time.setDecimals(2)
        self.step_time.setSuffix(" с")
        self.step_time.valueChanged.connect(self._params_changed)
        self.bpm_label = QLabel()
        self.fade = QSpinBox()
        self.fade.setRange(0, 100)
        self.fade.setSuffix(" % шага")
        self.fade.valueChanged.connect(self._params_changed)
        self.tap = QPushButton("TAP (стучите в такт)")
        self.tap.clicked.connect(self._tap)
        tf.addRow("Длина шага", self.step_time)
        tf.addRow("Темп", self.bpm_label)
        tf.addRow("Фейд", self.fade)
        tf.addRow(self.tap)
        ed.addWidget(tbox)

        prow = QHBoxLayout()
        self.b_start = QPushButton("▶ Старт")
        self.b_start.clicked.connect(self._start)
        self.b_stop = QPushButton("■ Стоп")
        self.b_stop.clicked.connect(self._stop)
        self.status = QLabel()
        prow.addWidget(self.b_start)
        prow.addWidget(self.b_stop)
        prow.addWidget(self.status, 1)
        ed.addLayout(prow)
        lay.addWidget(self.editor, 2)

        hub.changed.connect(self._on_changed)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._update_status)
        self.timer.start(250)
        self.refresh()

    def _on_changed(self, kind):
        if kind in ("chases", "all"):
            self.refresh()
        elif kind == "scenes":
            self._fill_scene_combo()

    def _fill_scene_combo(self):
        self.scene_combo.clear()
        self.scene_combo.addItems(list(self.show_.scenes))

    def refresh(self):
        cur = self.name()
        self.list.blockSignals(True)
        self.list.clear()
        self.list.addItems(list(self.show_.chases))
        items = self.list.findItems(cur or "", Qt.MatchExactly)
        if items:
            self.list.setCurrentItem(items[0])
        elif self.list.count():
            self.list.setCurrentRow(0)
        self.list.blockSignals(False)
        self._fill_scene_combo()
        self._load()

    def name(self):
        it = self.list.currentItem()
        return it.text() if it else None

    def data(self):
        return self.show_.chases.get(self.name())

    def _load(self, *_):
        ch = self.data()
        self.editor.setEnabled(ch is not None)
        self.steps.clear()
        if ch is None:
            return
        self.steps.addItems(ch["steps"])
        for w, v in ((self.step_time, ch.get("step_time", 1.0)), (self.fade, int(ch.get("fade", 0) * 100))):
            w.blockSignals(True)
            w.setValue(v)
            w.blockSignals(False)
        self._update_bpm()

    def _save_steps(self):
        ch = self.data()
        if ch is not None:
            ch["steps"] = [self.steps.item(i).text() for i in range(self.steps.count())]

    def _params_changed(self, *_):
        ch = self.data()
        if ch is None:
            return
        ch["step_time"] = self.step_time.value()
        ch["fade"] = self.fade.value() / 100.0
        self.engine.set_chase_step_time(self.name(), ch["step_time"])
        ec = self.engine.chase
        if ec and ec["name"] == self.name():
            ec["fade"] = ch["fade"]
        self._update_bpm()

    def _update_bpm(self):
        self.bpm_label.setText(f"{60.0 / self.step_time.value():.1f} BPM (1 шаг = 1 доля)")

    def _new(self):
        name, ok = QInputDialog.getText(self, "Новый чейз", "Название:")
        name = name.strip()
        if not ok or not name:
            return
        if name in self.show_.chases:
            QMessageBox.warning(self, "Ошибка", "Такое имя уже есть")
            return
        self.show_.chases[name] = {"steps": [], "step_time": 0.5, "fade": 0.0}
        self.hub.changed.emit("chases")
        items = self.list.findItems(name, Qt.MatchExactly)
        if items:
            self.list.setCurrentItem(items[0])

    def _delete(self):
        name = self.name()
        if not name or QMessageBox.question(self, "Удалить", f"Удалить чейз «{name}»?") != QMessageBox.Yes:
            return
        if self.engine.chase and self.engine.chase["name"] == name:
            self.engine.stop_chase()
        del self.show_.chases[name]
        rename_refs(self.show_, "chases", name, None)
        self.hub.changed.emit("chases")

    def _add_step(self):
        s = self.scene_combo.currentText()
        if s:
            self.steps.addItem(s)
            self._save_steps()

    def _del_step(self):
        row = self.steps.currentRow()
        if row >= 0:
            self.steps.takeItem(row)
            self._save_steps()

    def _move(self, d):
        row = self.steps.currentRow()
        new = row + d
        if row < 0 or not 0 <= new < self.steps.count():
            return
        it = self.steps.takeItem(row)
        self.steps.insertItem(new, it)
        self.steps.setCurrentRow(new)
        self._save_steps()

    def _tap(self):
        now = time.monotonic()
        if self._taps and now - self._taps[-1] > 2.0:
            self._taps = []
        self._taps.append(now)
        self._taps = self._taps[-8:]
        if len(self._taps) >= 2:
            intervals = [b - a for a, b in zip(self._taps, self._taps[1:])]
            self.step_time.setValue(sum(intervals) / len(intervals))

    def _start(self):
        name = self.name()
        if name and not self.engine.start_chase(name):
            QMessageBox.warning(self, "Чейз", "Добавьте в чейз хотя бы один шаг")
            return
        self.hub.action.emit("chase_start", name or "", 0.0)

    def _stop(self):
        self.engine.stop_chase()
        self.hub.action.emit("chase_stop", "", 0.0)

    def _update_status(self):
        ch = self.engine.chase
        self.status.setText(f"Идёт: {ch['name']} (шаг {ch['index'] + 1}/{len(ch['steps'])})" if ch else "Остановлен")
