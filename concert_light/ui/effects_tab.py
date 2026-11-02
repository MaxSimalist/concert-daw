"""Вкладка «Эффекты»: круг (pan/tilt), волна (яркость), радуга (RGB)."""

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox, QDoubleSpinBox, QFormLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel,
    QListWidget, QListWidgetItem, QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from core.show import EFFECT_KINDS

from .scenes_tab import rename_refs

HELP = {
    "circle": "Размер — радиус круга в единицах pan/tilt (0–127).\nРазброс — сдвиг фазы между приборами (1 = по кругу).",
    "wave": "Размер — глубина волны в % (100 = до нуля).\nДиммер берётся из сцены/программера — включите приборы.",
    "rainbow": "Размер не используется.\nРазброс 1 — все цвета радуги по приборам одновременно.",
}


class EffectsTab(QWidget):
    def __init__(self, show, engine, hub):
        super().__init__()
        self.show_, self.engine, self.hub = show, engine, hub
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
        b = QPushButton("Остановить все эффекты")
        b.clicked.connect(self._stop_all)
        left.addWidget(b)
        lay.addLayout(left, 1)

        self.editor = QWidget()
        ed = QVBoxLayout(self.editor)
        pbox = QGroupBox("Параметры")
        form = QFormLayout(pbox)
        self.kind = QComboBox()
        for k, label in EFFECT_KINDS.items():
            self.kind.addItem(label, k)
        self.speed = QDoubleSpinBox()
        self.speed.setRange(0.01, 10)
        self.speed.setDecimals(2)
        self.speed.setSuffix(" цикл/с")
        self.size = QDoubleSpinBox()
        self.size.setRange(0, 255)
        self.spread = QDoubleSpinBox()
        self.spread.setRange(0, 4)
        self.spread.setSingleStep(0.25)
        self.help = QLabel()
        for w in (self.kind,):
            w.currentIndexChanged.connect(self._params_changed)
        for w in (self.speed, self.size, self.spread):
            w.valueChanged.connect(self._params_changed)
        form.addRow("Тип", self.kind)
        form.addRow("Скорость", self.speed)
        form.addRow("Размер", self.size)
        form.addRow("Разброс фазы", self.spread)
        form.addRow(self.help)
        ed.addWidget(pbox)

        fbox = QGroupBox("Приборы (порядок = порядок волны)")
        fl = QVBoxLayout(fbox)
        self.fx_list = QListWidget()
        self.fx_list.itemChanged.connect(self._fixtures_changed)
        fl.addWidget(self.fx_list)
        ed.addWidget(fbox, 1)

        prow = QHBoxLayout()
        self.b_toggle = QPushButton()
        self.b_toggle.setCheckable(True)
        self.b_toggle.clicked.connect(self._toggle)
        prow.addWidget(self.b_toggle)
        ed.addLayout(prow)
        lay.addWidget(self.editor, 2)

        hub.changed.connect(self._on_changed)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._update_running)
        self.timer.start(300)
        self.refresh()

    def _on_changed(self, kind):
        if kind in ("effects", "all"):
            self.refresh()
        elif kind == "patch":
            self._load()

    def refresh(self):
        cur = self.name()
        self.list.blockSignals(True)
        self.list.clear()
        self.list.addItems(list(self.show_.effects))
        items = self.list.findItems(cur or "", Qt.MatchExactly)
        if items:
            self.list.setCurrentItem(items[0])
        elif self.list.count():
            self.list.setCurrentRow(0)
        self.list.blockSignals(False)
        self._load()

    def name(self):
        it = self.list.currentItem()
        return it.text() if it else None

    def data(self):
        return self.show_.effects.get(self.name())

    def _load(self, *_):
        ef = self.data()
        self.editor.setEnabled(ef is not None)
        self.fx_list.blockSignals(True)
        self.fx_list.clear()
        if ef is None:
            self.fx_list.blockSignals(False)
            return
        chosen = ef["fixtures"]
        # сначала выбранные (в их порядке), потом остальные
        fxs = [self.show_.fixture(f) for f in chosen if self.show_.fixture(f)]
        fxs += [f for f in sorted(self.show_.patch, key=lambda f: f.id) if f.id not in chosen]
        for fx in fxs:
            it = QListWidgetItem(f"{fx.id}. {fx.name}")
            it.setData(Qt.UserRole, fx.id)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            it.setCheckState(Qt.Checked if fx.id in chosen else Qt.Unchecked)
            self.fx_list.addItem(it)
        self.fx_list.blockSignals(False)
        for w, v in ((self.speed, ef.get("speed", 0.5)), (self.size, ef.get("size", 30)),
                     (self.spread, ef.get("spread", 1.0))):
            w.blockSignals(True)
            w.setValue(v)
            w.blockSignals(False)
        self.kind.blockSignals(True)
        self.kind.setCurrentIndex(max(0, self.kind.findData(ef.get("kind", "circle"))))
        self.kind.blockSignals(False)
        self.help.setText(HELP.get(ef.get("kind"), ""))
        self._update_running()

    def _restart_if_running(self):
        name = self.name()
        if name in self.engine.effects:
            self.engine.start_effect(name)  # перезапуск с новыми параметрами

    def _params_changed(self, *_):
        ef = self.data()
        if ef is None:
            return
        ef["kind"] = self.kind.currentData()
        ef["speed"] = self.speed.value()
        ef["size"] = self.size.value()
        ef["spread"] = self.spread.value()
        self.help.setText(HELP.get(ef["kind"], ""))
        self._restart_if_running()

    def _fixtures_changed(self, *_):
        ef = self.data()
        if ef is None:
            return
        ef["fixtures"] = [self.fx_list.item(i).data(Qt.UserRole) for i in range(self.fx_list.count())
                          if self.fx_list.item(i).checkState() == Qt.Checked]
        self._restart_if_running()

    def _new(self):
        labels = list(EFFECT_KINDS.values())
        label, ok = QInputDialog.getItem(self, "Новый эффект", "Тип:", labels, 0, False)
        if not ok:
            return
        kind = list(EFFECT_KINDS)[labels.index(label)]
        name, ok = QInputDialog.getText(self, "Новый эффект", "Название:", text=label.split(" (")[0])
        name = name.strip()
        if not ok or not name:
            return
        if name in self.show_.effects:
            QMessageBox.warning(self, "Ошибка", "Такое имя уже есть")
            return
        defaults = {"circle": (0.25, 40, 1.0), "wave": (0.5, 80, 1.0), "rainbow": (0.1, 0, 1.0)}[kind]
        self.show_.effects[name] = {"kind": kind, "fixtures": [], "speed": defaults[0],
                                    "size": defaults[1], "spread": defaults[2]}
        self.hub.changed.emit("effects")
        items = self.list.findItems(name, Qt.MatchExactly)
        if items:
            self.list.setCurrentItem(items[0])

    def _delete(self):
        name = self.name()
        if not name or QMessageBox.question(self, "Удалить", f"Удалить эффект «{name}»?") != QMessageBox.Yes:
            return
        self.engine.stop_effect(name)
        del self.show_.effects[name]
        rename_refs(self.show_, "effects", name, None)
        self.hub.changed.emit("effects")

    def _toggle(self):
        name = self.name()
        if not name:
            return
        if name in self.engine.effects:
            self.engine.stop_effect(name)
            self.hub.action.emit("effect_off", name, 0.0)
        else:
            if not self.data()["fixtures"]:
                QMessageBox.warning(self, "Эффект", "Отметьте приборы для эффекта")
                self.b_toggle.setChecked(False)
                return
            self.engine.start_effect(name)
            self.hub.action.emit("effect_on", name, 0.0)
        self._update_running()

    def _stop_all(self):
        for name in list(self.engine.effects):
            self.hub.action.emit("effect_off", name, 0.0)
        self.engine.stop_all_effects()

    def _update_running(self):
        running = set(self.engine.effects)
        for i in range(self.list.count()):
            it = self.list.item(i)
            it.setForeground(QColor(255, 170, 0) if it.text() in running else QColor(220, 220, 220))
        on = self.name() in running
        self.b_toggle.setChecked(on)
        self.b_toggle.setText("■ Остановить" if on else "▶ Запустить")
