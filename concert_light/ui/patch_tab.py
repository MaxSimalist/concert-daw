"""Вкладка «Патч»: приборы, DMX-адреса, создание своих типов приборов."""

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMessageBox, QPushButton, QSpinBox, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

from core.fixtures import save_type_to_library

from .fixture_editor import FixtureEditorDialog


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


class PatchTab(QWidget):
    COLS = ["ID", "Имя", "Тип", "Адрес", "Каналы"]

    def __init__(self, show, engine, hub, lib_folder):
        super().__init__()
        self.show_, self.engine, self.hub, self.lib_folder = show, engine, hub, lib_folder
        lay = QVBoxLayout(self)
        btns = QHBoxLayout()
        for text, slot in (("Добавить из библиотеки…", self._add),
                           ("Свой прибор (каналы вручную)…", self._new_type),
                           ("Изменить каналы прибора…", self._edit_type),
                           ("Удалить выбранные", self._remove)):
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
        self.table.cellDoubleClicked.connect(self._double_clicked)
        lay.addWidget(self.table)
        lay.addWidget(QLabel("Имя и адрес — двойной щелчок по ячейке. Двойной щелчок по типу — "
                             "назначить каналы вручную. Красным — пересечение адресов."))
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
        d = FixtureEditorDialog(self.engine, self.show_, parent=self)
        if not d.exec() or d.result_type is None:
            return
        ft = d.result_type
        save_type_to_library(ft, self.lib_folder)
        with self.engine.lock:
            self.show_.replace_type(ft.name, ft)          # тип с таким именем мог уже стоять в патче
            if d.add:
                count, base, addr = d.add
                for i in range(count):
                    name = f"{base} {i + 1}" if count > 1 else base
                    self.show_.add_fixture(ft.name, name, addr + i * ft.channel_count)
        self.engine.rebuild_patch()
        self.refresh()
        self.hub.changed.emit("patch")

    def _double_clicked(self, row, col):
        if col == 2:
            self._edit_type()

    def _edit_type(self):
        ids = self._selected_ids()
        if not ids:
            QMessageBox.information(self, "Каналы", "Выберите прибор в таблице")
            return
        fx = self.show_.fixture(ids[0])
        d = FixtureEditorDialog(self.engine, self.show_, ftype=fx.type, fixture=fx, parent=self)
        if not d.exec() or d.result_type is None:
            return
        ft = d.result_type
        save_type_to_library(ft, self.lib_folder)
        with self.engine.lock:
            self.show_.replace_type(fx.type.name, ft)
            if d.address.value() + ft.channel_count - 1 <= 512:
                fx.address = d.address.value()
        self.engine.rebuild_patch()
        self.refresh()
        self.hub.changed.emit("patch")
