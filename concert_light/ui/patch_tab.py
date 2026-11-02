"""Вкладка «Патч»: приборы, DMX-адреса, создание своих типов приборов."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QSpinBox, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from core.fixtures import ATTR_LABELS, FixtureType, save_type_to_library


class AddFixtureDialog(QDialog):
    def __init__(self, show, parent=None):
        super().__init__(parent)
        self.show_ = show
        self.setWindowTitle("Добавить приборы")
        form = QFormLayout(self)
        self.type_combo = QComboBox()
        for name in sorted(show.all_types()):
            self.type_combo.addItem(name)
        self.name_edit = QLineEdit("PAR")
        self.count = QSpinBox()
        self.count.setRange(1, 64)
        self.address = QSpinBox()
        self.address.setRange(1, 512)
        self.info = QLabel()
        form.addRow("Тип", self.type_combo)
        form.addRow("Имя", self.name_edit)
        form.addRow("Количество", self.count)
        form.addRow("Стартовый адрес", self.address)
        form.addRow("", self.info)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        form.addRow(bb)
        self.type_combo.currentTextChanged.connect(self._update)
        self.count.valueChanged.connect(self._update)
        self.address.valueChanged.connect(self._update_info)
        self._update()

    def channels(self):
        ft = self.show_.get_type(self.type_combo.currentText())
        return ft.channel_count if ft else 1

    def _update(self):
        ch = self.channels()
        self.address.blockSignals(True)
        self.address.setValue(self.show_.next_free_address(ch * self.count.value()))
        self.address.blockSignals(False)
        self._update_info()

    def _update_info(self):
        ch = self.channels()
        last = self.address.value() + ch * self.count.value() - 1
        self.info.setText(f"{ch} кан. на прибор, займут адреса {self.address.value()}–{last}"
                          + ("  ⚠ больше 512!" if last > 512 else ""))


class NewTypeDialog(QDialog):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Новый тип прибора")
        self.resize(460, 520)
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.name = QLineEdit()
        self.manuf = QLineEdit()
        form.addRow("Название", self.name)
        form.addRow("Производитель", self.manuf)
        lay.addLayout(form)
        lay.addWidget(QLabel(
            "Каналы по порядку, по одному в строке, как в инструкции к прибору.\n"
            "Можно указать значение по умолчанию: pan=128\n"
            "Младший байт 16-бит: pan_fine, tilt_fine.\n"
            "Известные атрибуты: " + ", ".join(ATTR_LABELS)))
        self.text = QPlainTextEdit("dimmer\nred\ngreen\nblue\nstrobe\nmode\nspeed")
        lay.addWidget(self.text, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self._ok)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.result_type = None

    def _ok(self):
        name = self.name.text().strip()
        if not name:
            QMessageBox.warning(self, "Ошибка", "Введите название")
            return
        chans = []
        for line in self.text.toPlainText().splitlines():
            line = line.strip()
            if not line:
                continue
            attr, _, dv = line.partition("=")
            try:
                default = float(dv) if dv else 0.0
            except ValueError:
                QMessageBox.warning(self, "Ошибка", f"Плохое значение: {line}")
                return
            chans.append({"attr": attr.strip().lower(), "default": default})
        if not chans:
            QMessageBox.warning(self, "Ошибка", "Нет каналов")
            return
        self.result_type = FixtureType({"name": name, "manufacturer": self.manuf.text(), "channels": chans})
        self.accept()


class PatchTab(QWidget):
    COLS = ["ID", "Имя", "Тип", "Адрес", "Каналы"]

    def __init__(self, show, engine, hub, lib_folder):
        super().__init__()
        self.show_, self.engine, self.hub, self.lib_folder = show, engine, hub, lib_folder
        lay = QVBoxLayout(self)
        btns = QHBoxLayout()
        for text, slot in (("Добавить приборы…", self._add), ("Удалить выбранные", self._remove),
                           ("Новый тип прибора…", self._new_type)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            btns.addWidget(b)
        btns.addStretch()
        lay.addLayout(btns)
        self.table = QTableWidget(0, len(self.COLS))
        self.table.setHorizontalHeaderLabels(self.COLS)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.cellChanged.connect(self._cell_changed)
        lay.addWidget(self.table)
        lay.addWidget(QLabel("Имя и адрес можно править двойным щелчком. Красным — пересечение адресов."))
        hub.changed.connect(lambda k: k in ("patch", "all") and self.refresh())
        self.refresh()

    def refresh(self):
        bad = self.show_.conflicts()
        t = self.table
        t.blockSignals(True)
        t.setRowCount(len(self.show_.patch))
        for row, fx in enumerate(sorted(self.show_.patch, key=lambda f: f.address)):
            vals = [str(fx.id), fx.name, fx.type.name, str(fx.address), f"{fx.address}–{fx.last_channel}"]
            for col, v in enumerate(vals):
                it = QTableWidgetItem(v)
                it.setData(Qt.UserRole, fx.id)
                if col not in (1, 3):
                    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                if fx.id in bad:
                    it.setBackground(QColor(120, 30, 30))
                t.setItem(row, col, it)
        t.blockSignals(False)

    def _selected_ids(self):
        return sorted({it.data(Qt.UserRole) for it in self.table.selectedItems()})

    def _cell_changed(self, row, col):
        it = self.table.item(row, col)
        fx = self.show_.fixture(it.data(Qt.UserRole))
        if fx is None:
            return
        if col == 1:
            fx.name = it.text().strip() or fx.name
        elif col == 3:
            try:
                addr = int(it.text())
                if not 1 <= addr <= 513 - fx.type.channel_count:
                    raise ValueError
                fx.address = addr
            except ValueError:
                QMessageBox.warning(self, "Адрес", "Неверный адрес")
        self.refresh()
        self.hub.changed.emit("patch")

    def _add(self):
        if not self.show_.all_types():
            QMessageBox.warning(self, "Нет типов", "Библиотека приборов пуста. Создайте тип прибора.")
            return
        d = AddFixtureDialog(self.show_, self)
        if not d.exec():
            return
        tname = d.type_combo.currentText()
        ch = d.channels()
        base = d.name_edit.text().strip() or "Прибор"
        n = d.count.value()
        try:
            with self.engine.lock:
                for i in range(n):
                    name = f"{base} {i + 1}" if n > 1 else base
                    self.show_.add_fixture(tname, name, d.address.value() + i * ch)
        except Exception as e:
            QMessageBox.warning(self, "Ошибка", str(e))
        self.engine.rebuild_patch()
        self.refresh()
        self.hub.changed.emit("patch")

    def _remove(self):
        ids = self._selected_ids()
        if not ids:
            return
        if QMessageBox.question(self, "Удалить", f"Удалить приборов: {len(ids)}?") != QMessageBox.Yes:
            return
        with self.engine.lock:
            for fid in ids:
                self.show_.remove_fixture(fid)
        self.engine.rebuild_patch()
        self.refresh()
        self.hub.changed.emit("patch")

    def _new_type(self):
        d = NewTypeDialog(self)
        if not d.exec() or d.result_type is None:
            return
        ft = d.result_type
        path = save_type_to_library(ft, self.lib_folder)
        self.show_.library[ft.name] = ft
        QMessageBox.information(self, "Готово", f"Тип «{ft.name}» сохранён:\n{path}")
