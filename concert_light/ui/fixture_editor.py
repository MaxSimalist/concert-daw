"""Ручной патч прибора: каждому DMX-каналу назначается функция.

Ползунок «Проверка» сразу шлёт значение на этот канал uDMX (поверх всего остального) —
так по инструкции или «на глаз» находится, какой канал что делает у китайского прибора.
"""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMessageBox, QPushButton, QSlider, QSpinBox, QTableWidget, QTableWidgetItem,
    QVBoxLayout,
)

from core.fixtures import FixtureType, attr_label, is_raw

RAW = "raw"

# порядок функций в выпадающем списке
FUNCTIONS = [
    (RAW, "— без функции —"),
    ("dimmer", None),
    ("red", None), ("green", None), ("blue", None), ("white", None), ("amber", None), ("uv", None),
    ("cto", None), ("color_wheel", None), ("color_macro", None),
    ("pan", None), ("pan_fine", "Pan точно (16 бит)"), ("tilt", None), ("tilt_fine", "Tilt точно (16 бит)"),
    ("pt_speed", None),
    ("shutter", None), ("strobe", None), ("gobo", None), ("gobo_rotation", None), ("prism", None),
    ("focus", None), ("zoom", None), ("iris", None), ("frost", None),
    ("mode", None), ("speed", None), ("macro", None), ("reset", None),
]
CENTER_DEFAULT = {"pan": 128, "tilt": 128}


def func_label(attr, label=None):
    return label or attr_label(attr)


class FixtureEditorDialog(QDialog):
    COLS = ["№", "DMX", "Функция", "По умолч.", "Проверка", ""]

    def __init__(self, engine, show, ftype=None, fixture=None, parent=None):
        super().__init__(parent)
        self.engine, self.show_ = engine, show
        self.orig = ftype
        self.fixture = fixture
        self.result_type = None
        self.add = None
        self.setWindowTitle("Изменить каналы прибора" if ftype else "Свой прибор — каналы вручную")
        self.resize(760, 680)
        lay = QVBoxLayout(self)

        form = QFormLayout()
        self.name = QLineEdit(ftype.name if ftype else "")
        self.name.setPlaceholderText("например: PAR 18x3W RGB 7ch")
        self.manuf = QLineEdit(ftype.manufacturer if ftype else "")
        self.count = QSpinBox()
        self.count.setRange(1, 64)
        self.address = QSpinBox()
        self.address.setRange(1, 512)
        if fixture:
            self.address.setValue(fixture.address)
        else:
            self.address.setValue(show.next_free_address(ftype.channel_count if ftype else 1))
        self.template = QComboBox()
        self.template.addItem("— пустой —", "")
        for n in sorted(show.all_types()):
            self.template.addItem(n, n)
        row = QHBoxLayout()
        row.addWidget(self.template, 1)
        b_tpl = QPushButton("Взять каналы")
        b_tpl.clicked.connect(self._from_template)
        row.addWidget(b_tpl)
        form.addRow("Название:", self.name)
        form.addRow("Производитель:", self.manuf)
        form.addRow("Каналов (режим прибора):", self.count)
        form.addRow("DMX-адрес прибора:", self.address)
        if not ftype:
            form.addRow("Начать с готового типа:", row)
        lay.addLayout(form)

        if ftype:
            n = sum(1 for f in show.patch if f.type.name == ftype.name)
            note = QLabel(f"Изменения коснутся всех приборов этого типа в патче ({n} шт.).")
            note.setStyleSheet("color: #f0b040;")
            lay.addWidget(note)
        hint = QLabel("Выставьте на приборе тот же адрес и режим (число каналов), что здесь. "
                      "Двигайте «Проверку» — увидите, что делает канал, и выберите ему функцию.")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        h = self.table.horizontalHeader()
        for c, mode in enumerate((QHeaderView.ResizeToContents, QHeaderView.ResizeToContents,
                                  QHeaderView.Stretch, QHeaderView.ResizeToContents, QHeaderView.Stretch,
                                  QHeaderView.ResizeToContents)):
            h.setSectionResizeMode(c, mode)
        self.table.verticalHeader().setVisible(False)
        lay.addWidget(self.table, 1)

        trow = QHBoxLayout()
        b_off = QPushButton("Погасить проверку")
        b_off.clicked.connect(self._test_off)
        b_def = QPushButton("Проверка = значения по умолчанию")
        b_def.clicked.connect(self._test_defaults)
        trow.addWidget(b_off)
        trow.addWidget(b_def)
        trow.addStretch(1)
        lay.addLayout(trow)

        self.add_box = None
        if not ftype:
            self.add_box = QGroupBox("Сразу добавить в патч")
            self.add_box.setCheckable(True)
            self.add_box.setChecked(True)
            af = QFormLayout(self.add_box)
            self.add_count = QSpinBox()
            self.add_count.setRange(1, 64)
            self.add_name = QLineEdit("PAR")
            af.addRow("Сколько приборов:", self.add_count)
            af.addRow("Имя:", self.add_name)
            af.addRow(QLabel("Адреса пойдут подряд, начиная с «DMX-адрес прибора»."))
            lay.addWidget(self.add_box)

        bb = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._save)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

        self.rows = []      # [(combo, default_spin, slider, value_label)]
        channels = ftype.channels if ftype else [{"attr": "dimmer", "default": 0}]
        self.count.setValue(len(channels))
        self._set_rows(channels)
        self.count.valueChanged.connect(self._count_changed)
        self.address.valueChanged.connect(self._address_changed)

    # ------------------------------------------------------------------
    # строки каналов
    # ------------------------------------------------------------------

    def _make_combo(self, attr):
        cb = QComboBox()
        known = []
        for a, label in FUNCTIONS:
            cb.addItem(func_label(a, label), a)
            known.append(a)
        key = RAW if is_raw(attr) else attr
        if key not in known:                      # неизвестная функция из файла — сохраняем
            cb.addItem(attr, attr)
        cb.setCurrentIndex(cb.findData(key))
        return cb

    def _set_rows(self, channels):
        self._test_off()
        self.table.setRowCount(0)
        self.rows = []
        for ch in channels:
            self._append_row(ch.get("attr", RAW), int(ch.get("default", 0)))
        self._address_changed()

    def _append_row(self, attr, default):
        i = len(self.rows)
        self.table.insertRow(i)
        num = QTableWidgetItem(str(i + 1))
        num.setFlags(Qt.ItemIsEnabled)
        self.table.setItem(i, 0, num)
        dmx = QTableWidgetItem()
        dmx.setFlags(Qt.ItemIsEnabled)
        self.table.setItem(i, 1, dmx)
        combo = self._make_combo(attr)
        spin = QSpinBox()
        spin.setRange(0, 255)
        spin.setValue(default)
        slider = QSlider(Qt.Horizontal)
        slider.setRange(0, 255)
        value = QLabel("—")
        value.setMinimumWidth(32)
        combo.currentIndexChanged.connect(lambda _=0, r=i: self._func_changed(r))
        slider.valueChanged.connect(lambda v, r=i: self._test(r, v))
        self.table.setCellWidget(i, 2, combo)
        self.table.setCellWidget(i, 3, spin)
        self.table.setCellWidget(i, 4, slider)
        self.table.setCellWidget(i, 5, value)
        self.rows.append((combo, spin, slider, value))

    def _count_changed(self, n):
        while len(self.rows) < n:
            self._append_row(RAW, 0)
        while len(self.rows) > n:
            r = len(self.rows) - 1
            self.engine.test_channels.pop(self.address.value() + r, None)
            self.table.removeRow(r)
            self.rows.pop()
        self._address_changed()

    def _address_changed(self, *_):
        base = self.address.value()
        self.engine.test_channels.clear()           # адрес сменился — проверка переезжает
        for i, (_c, _s, slider, value) in enumerate(self.rows):
            addr = base + i
            it = self.table.item(i, 1)
            it.setText(str(addr) if addr <= 512 else "> 512!")
            it.setForeground(QColor(255, 90, 90) if addr > 512 else QColor(220, 220, 220))
            if value.text() != "—" and addr <= 512:
                self.engine.test_channels[addr] = slider.value()

    def _func_changed(self, r):
        combo, spin, *_ = self.rows[r]
        a = combo.currentData()
        if a in CENTER_DEFAULT and spin.value() == 0:
            spin.setValue(CENTER_DEFAULT[a])

    # ------------------------------------------------------------------
    # проверка каналов
    # ------------------------------------------------------------------

    def _test(self, r, v):
        addr = self.address.value() + r
        if addr > 512:
            return
        self.engine.test_channels[addr] = v
        self.rows[r][3].setText(str(v))
        for i in range(self.table.columnCount()):
            item = self.table.item(r, i)
            if item:
                item.setBackground(QColor(90, 60, 10))

    def _test_off(self):
        self.engine.test_channels.clear()
        for r, (_c, _s, slider, value) in enumerate(self.rows):
            slider.blockSignals(True)
            slider.setValue(0)
            slider.blockSignals(False)
            value.setText("—")
            for i in (0, 1):
                item = self.table.item(r, i)
                if item:
                    item.setBackground(QColor(0, 0, 0, 0))

    def _test_defaults(self):
        for r, (_c, spin, slider, _v) in enumerate(self.rows):
            slider.setValue(spin.value())
            self._test(r, spin.value())

    def _from_template(self):
        name = self.template.currentData()
        ft = self.show_.get_type(name) if name else None
        if not ft:
            return
        self.count.blockSignals(True)
        self.count.setValue(ft.channel_count)
        self.count.blockSignals(False)
        self._set_rows(ft.channels)
        if not self.name.text().strip():
            self.name.setText(ft.name + " (мой)")
        self.manuf.setText(self.manuf.text() or ft.manufacturer)

    # ------------------------------------------------------------------
    # сохранение
    # ------------------------------------------------------------------

    def channels(self):
        out = []
        for i, (combo, spin, _s, _v) in enumerate(self.rows):
            a = combo.currentData()
            out.append({"attr": f"raw{i + 1}" if a == RAW else a, "default": spin.value()})
        return out

    def _save(self):
        name = self.name.text().strip()
        if not name:
            QMessageBox.warning(self, "Прибор", "Введите название прибора")
            return
        chans = self.channels()
        seen, dup = set(), []
        for c in chans:
            a = c["attr"]
            if a in seen and not is_raw(a):
                dup.append(attr_label(a))
            seen.add(a)
        if dup:
            QMessageBox.warning(self, "Прибор", "Одна функция назначена на несколько каналов: "
                                + ", ".join(sorted(set(dup))) + ".\nОставьте её на одном канале.")
            return
        for fine, coarse in (("pan_fine", "pan"), ("tilt_fine", "tilt")):
            if fine in seen and coarse not in seen:
                QMessageBox.warning(self, "Прибор", f"«{attr_label(fine)}» работает только вместе с «{coarse}».")
                return
        renamed = self.orig is None or name != self.orig.name
        if renamed and self.show_.get_type(name) and QMessageBox.question(
                self, "Прибор", f"Тип «{name}» уже есть. Заменить его?") != QMessageBox.Yes:
            return
        last = self.address.value() + len(chans) - 1
        if self.add_box is not None and self.add_box.isChecked():
            last = self.address.value() + len(chans) * self.add_count.value() - 1
            self.add = (self.add_count.value(), self.add_name.text().strip() or name, self.address.value())
        if last > 512:
            QMessageBox.warning(self, "Прибор", f"Не помещается в 512 каналов (последний — {last}).")
            self.add = None
            return
        self.result_type = FixtureType({"name": name, "manufacturer": self.manuf.text().strip(),
                                        "channels": chans})
        self.accept()

    def done(self, r):
        self.engine.test_channels.clear()           # проверка не должна остаться гореть
        super().done(r)
