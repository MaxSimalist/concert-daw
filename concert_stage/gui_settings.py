"""
gui_settings.py — Настройки Concert Stage: табло, Concert DAW, пульт X32, обновления и отзывы.
"""
from __future__ import annotations

from copy import deepcopy

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
                               QPushButton, QSpinBox, QTabWidget, QVBoxLayout, QWidget)

import sync_client
import x32_osc
from stage_model import StageSettings, app_data_dir


class SettingsDialog(QDialog):
    def __init__(self, settings: StageSettings, mirror: x32_osc.ConsoleMirror, update_button=None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки табло")
        self.s = deepcopy(settings)
        self.mirror = mirror
        lay = QVBoxLayout(self)
        tabs = QTabWidget()
        lay.addWidget(tabs, 1)

        # ── табло ──────────────────────────────────────────────────────────
        w = QWidget()
        f = QFormLayout(w)
        self.cb = {}
        for key, text in (("show_setlist", "Очередь треков (плейлист) справа"),
                          ("show_marker", "Часть песни по маркерам (Куплет, Припев…)"),
                          ("show_timer", "Секундомер концерта (запускается в Concert DAW)"),
                          ("show_clock", "Часы"),
                          ("show_channels", "Каналы пульта X32 внизу")):
            c = QCheckBox(text)
            c.setChecked(bool(getattr(self.s, key)))
            f.addRow(c)
            self.cb[key] = c
        self.warn = QSpinBox()
        self.warn.setRange(0, 600)
        self.warn.setSuffix(" с")
        self.warn.setValue(self.s.warn_seconds)
        self.alert = QSpinBox()
        self.alert.setRange(0, 600)
        self.alert.setSuffix(" с")
        self.alert.setValue(self.s.alert_seconds)
        f.addRow("«Осталось» жёлтым, когда меньше:", self.warn)
        f.addRow("…красным и мигает, когда меньше:", self.alert)
        self.scale = QDoubleSpinBox()
        self.scale.setRange(0.5, 3.0)
        self.scale.setSingleStep(0.1)
        self.scale.setValue(self.s.font_scale)
        self.scale.setSuffix(" ×")
        f.addRow("Размер надписей:", self.scale)
        self.screen = QComboBox()
        self.screen.addItem("где открыто окно", -1)
        for i, sc in enumerate(QGuiApplication.screens()):
            g = sc.geometry()
            self.screen.addItem(f"монитор {i + 1}: {sc.name()} ({g.width()}×{g.height()})", i)
        self.screen.setCurrentIndex(max(0, self.screen.findData(self.s.screen)))
        f.addRow("На весь экран (F11) — на:", self.screen)
        self.start_full = QCheckBox("Открывать сразу на весь экран")
        self.start_full.setChecked(self.s.start_fullscreen)
        f.addRow(self.start_full)
        tabs.addTab(w, "Табло")

        # ── Concert DAW ────────────────────────────────────────────────────
        w = QWidget()
        f = QFormLayout(w)
        f.addRow(QLabel("Concert DAW находится в сети сам. В Concert DAW должна стоять галочка\n"
                        "Настройки → Видео → «Разрешить подключение видео-программы и табло (мастер-часы)»\n"
                        "(она включена по умолчанию)."))
        self.daw_ip = QLineEdit(self.s.daw_ip)
        self.daw_ip.setPlaceholderText("пусто — искать в сети")
        self.daw_port = QSpinBox()
        self.daw_port.setRange(1024, 65535)
        self.daw_port.setValue(self.s.daw_port)
        f.addRow("IP компьютера с Concert DAW:", self.daw_ip)
        f.addRow("Порт:", self.daw_port)
        row = QHBoxLayout()
        find = QPushButton("Найти в сети")
        find.clicked.connect(self._find_daw)
        self.daw_found = QComboBox()
        self.daw_found.activated.connect(self._pick_daw)
        row.addWidget(find)
        row.addWidget(self.daw_found, 1)
        f.addRow(row)
        tabs.addTab(w, "Concert DAW")

        # ── пульт ──────────────────────────────────────────────────────────
        w = QWidget()
        v = QVBoxLayout(w)
        self.x32_on = QCheckBox("Показывать каналы пульта Behringer X32 (только смотреть — табло ничего "
                                "не меняет на пульте)")
        self.x32_on.setChecked(self.s.x32_on)
        v.addWidget(self.x32_on)
        row = QHBoxLayout()
        row.addWidget(QLabel("IP пульта:"))
        self.x32_ip = QLineEdit(self.s.x32_ip)
        self.x32_ip.setPlaceholderText("пусто — искать в сети")
        row.addWidget(self.x32_ip, 1)
        findx = QPushButton("Найти пульт")
        findx.clicked.connect(self._find_x32)
        row.addWidget(findx)
        v.addLayout(row)
        self.x32_note = QLabel("")
        self.x32_note.setObjectName("caption")
        v.addWidget(self.x32_note)
        v.addWidget(QLabel("Какие каналы показывать (ничего не отмечено — каналы 1–32 с названиями на "
                           "пульте + MAIN LR):"))
        self.ch_list = QListWidget()
        chosen = set(self.s.channels)
        for group, keys in x32_osc.CONSOLE_GROUPS:
            head = QListWidgetItem(f"— {group} —")
            head.setFlags(Qt.NoItemFlags)
            self.ch_list.addItem(head)
            for k in keys:
                it = QListWidgetItem(f"{x32_osc.default_name(k)}   {self._console_name(k)}")
                it.setData(Qt.UserRole, k)
                it.setFlags(Qt.ItemIsEnabled | Qt.ItemIsUserCheckable)
                it.setCheckState(Qt.Checked if k in chosen else Qt.Unchecked)
                self.ch_list.addItem(it)
        v.addWidget(self.ch_list, 1)
        row = QHBoxLayout()
        for text, fn in (("Авто (снять все)", lambda: self._check(lambda k: False)),
                         ("Каналы 1–16", lambda: self._check(lambda k: k in x32_osc.ALL_KEYS[2:18])),
                         ("С названиями", lambda: self._check(
                             lambda k: bool(self.mirror.names.get(k)) and
                             self.mirror.names.get(k) != x32_osc.default_name(k)))):
            b = QPushButton(text)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        tabs.addTab(w, "Пульт X32")

        # ── обновления и отзывы ────────────────────────────────────────────
        from gui_feedback import build_updates_tab
        upd, self.update_auto = build_updates_tab(self, update_button, self.s.update_auto,
                                                  app_data_dir() / "error.log")
        tabs.addTab(upd, "Обновления и отзывы")

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self.resize(640, 560)

    def _console_name(self, key: str) -> str:
        n = self.mirror.names.get(key, "")
        return f"«{n}»" if n and n != x32_osc.default_name(key) else ""

    def _check(self, pred) -> None:
        for i in range(self.ch_list.count()):
            it = self.ch_list.item(i)
            k = it.data(Qt.UserRole)
            if k:
                it.setCheckState(Qt.Checked if pred(k) else Qt.Unchecked)

    def _find_daw(self) -> None:
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            found = sync_client.discover(1.5)
        finally:
            QApplication.restoreOverrideCursor()
        self.daw_found.clear()
        for d in found:
            self.daw_found.addItem(f"{d['name']} — {d['ip']}:{d['port']}", d)
        if not found:
            self.daw_found.addItem("не найден — запустите Concert DAW (галочка «мастер-часы» в его настройках)", None)
        elif len(found) == 1:
            self._pick_daw(0)

    def _pick_daw(self, i: int) -> None:
        d = self.daw_found.itemData(i)
        if d:
            self.daw_ip.setText(d["ip"])
            self.daw_port.setValue(int(d["port"]))
            self.s.daw_name = d["name"]

    def _find_x32(self) -> None:
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            found = x32_osc.discover(1.5)
        finally:
            QApplication.restoreOverrideCursor()
        if found:
            self.x32_ip.setText(found[0]["ip"])
            self.x32_note.setText("Найдено: " + ", ".join(f"{d['name']} ({d['ip']})" for d in found))
        else:
            self.x32_note.setText("Пульт не найден. Проверьте сеть или впишите IP (на пульте: Setup → Network).")

    def result_settings(self) -> StageSettings:
        s = self.s
        for key, c in self.cb.items():
            setattr(s, key, c.isChecked())
        s.alert_seconds = self.alert.value()
        s.warn_seconds = max(self.warn.value(), s.alert_seconds)
        s.font_scale = float(self.scale.value())
        s.screen = int(self.screen.currentData())
        s.start_fullscreen = self.start_full.isChecked()
        new_ip = self.daw_ip.text().strip()
        if new_ip != s.daw_ip and not self.daw_found.count():
            s.daw_name = ""                       # IP вписали руками — имя прежнего компьютера не держим
        s.daw_ip, s.daw_port = new_ip, int(self.daw_port.value())
        s.x32_on = self.x32_on.isChecked()
        s.x32_ip = self.x32_ip.text().strip()
        s.channels = [self.ch_list.item(i).data(Qt.UserRole) for i in range(self.ch_list.count())
                      if self.ch_list.item(i).data(Qt.UserRole)
                      and self.ch_list.item(i).checkState() == Qt.Checked]
        s.update_auto = self.update_auto.isChecked()
        return s
