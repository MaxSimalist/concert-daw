"""Окно «Настройки»: Concert DAW, uDMX, Обновления и отзывы."""

import threading

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit,
    QListWidget, QListWidgetItem, QPushButton, QSpinBox, QTabWidget, QVBoxLayout, QWidget,
)

from core.appdata import app_data_dir
from gui_feedback import build_updates_tab
from sync_client import DEFAULT_PORT, discover


class DawTab(QWidget):
    def __init__(self, settings, client):
        super().__init__()
        self.client = client
        lay = QVBoxLayout(self)
        info = QLabel("Concert Light берёт время трека у Concert DAW — так же, как Concert Video. "
                      "В Concert DAW включите: Настройки → Видео → «Разрешить подключение». "
                      "Свет и DAW могут быть на одном компьютере или в одной сети.")
        info.setWordWrap(True)
        lay.addWidget(info)
        self.state = QLabel()
        lay.addWidget(self.state)
        self.list = QListWidget()
        self.list.itemClicked.connect(self._take)
        lay.addWidget(self.list, 1)
        self.search = QPushButton(" Найти Concert DAW в сети")
        self.search.clicked.connect(self._search)
        lay.addWidget(self.search)
        form = QFormLayout()
        self.ip = QLineEdit(settings.daw_ip)
        self.ip.setPlaceholderText("127.0.0.1 — этот же компьютер, или например 192.168.1.20")
        self.port = QSpinBox()
        self.port.setRange(1024, 65000)
        self.port.setValue(int(settings.daw_port or DEFAULT_PORT))
        self.auto = QCheckBox("Подключаться при запуске (и искать DAW в сети, если IP не задан)")
        self.auto.setChecked(bool(settings.auto_connect))
        form.addRow("IP Concert DAW:", self.ip)
        form.addRow("Порт:", self.port)
        form.addRow(self.auto)
        lay.addLayout(form)
        self.name = settings.daw_name
        self._found = None
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._check)
        self._state_timer = QTimer(self)
        self._state_timer.timeout.connect(self._show_state)
        self._state_timer.start(500)
        self._show_state()

    def _show_state(self):
        c = self.client
        if c.connected:
            self.state.setText(f"<span style='color:#3c3'>●</span> Подключено: {c.server_name or c.ip}")
        elif c.ip:
            self.state.setText(f"<span style='color:#e44'>●</span> Нет связи с {c.ip}:{c.port}")
        else:
            self.state.setText("<span style='color:#888'>●</span> Не подключено")

    def _search(self):
        self.search.setEnabled(False)
        self.search.setText(" Ищу…")
        self.list.clear()
        self._found = None

        def run():
            try:
                self._found = discover(1.5)
            except Exception:
                self._found = []
        threading.Thread(target=run, daemon=True, name="daw-discover").start()
        self._timer.start()

    def _check(self):
        if self._found is None:
            return
        self._timer.stop()
        self.search.setEnabled(True)
        self.search.setText(" Найти Concert DAW в сети")
        for d in self._found:
            it = QListWidgetItem(f"{d['name']}  —  {d['ip']}:{d['port']}")
            it.setData(Qt.UserRole, d)
            self.list.addItem(it)
        if not self._found:
            self.list.addItem("Не найден. Проверьте, что Concert DAW запущен, подключение разрешено "
                              "и компьютеры в одной сети. Можно ввести IP вручную.")
        elif len(self._found) == 1:
            self.list.setCurrentRow(0)
            self._take(self.list.item(0))

    def _take(self, item):
        d = item.data(Qt.UserRole)
        if d:
            self.ip.setText(d["ip"])
            self.port.setValue(int(d["port"]))
            self.name = d["name"]


class UdmxTab(QWidget):
    def __init__(self, output, settings):
        super().__init__()
        lay = QVBoxLayout(self)
        self.state = QLabel()
        self.state.setWordWrap(True)
        lay.addWidget(self.state)

        box0 = QGroupBox("Связь")
        f = QFormLayout(box0)
        self.stats = QLabel()
        self.stats.setWordWrap(True)
        f.addRow(self.stats)
        self.rate = QSpinBox()
        self.rate.setRange(5, 44)
        self.rate.setSuffix(" раз/с")
        self.rate.setValue(int(settings.udmx_rate or 30))
        self.rate.setToolTip("Как часто слать изменения на uDMX. Если связь рвётся — уменьшите до 20–25.")
        self.rate.valueChanged.connect(lambda v: setattr(output, "rate", v))
        f.addRow("Частота отправки:", self.rate)
        row = QHBoxLayout()
        b_re = QPushButton("Переподключить")
        b_re.clicked.connect(output.reconnect)
        b_log = QPushButton("Открыть журнал связи")
        b_log.clicked.connect(self._open_log)
        row.addWidget(b_re)
        row.addWidget(b_log)
        row.addStretch(1)
        f.addRow(row)
        lay.addWidget(box0)
        box = QGroupBox("Если uDMX не находится")
        v = QVBoxLayout(box)
        steps = QLabel(
            "1. Подключите uDMX к USB.\n"
            "2. Откройте Zadig (кнопка ниже) → меню Options → «List All Devices».\n"
            "3. Выберите в списке uDMX (USB ID 16C0 05DC).\n"
            "4. Справа выберите драйвер libusb-win32 → «Install Driver» (или «Replace Driver»).\n"
            "5. Переподключите uDMX — Concert Light найдёт его сам в течение 2 секунд.")
        steps.setWordWrap(True)
        v.addWidget(steps)
        b = QPushButton(" Скачать Zadig (zadig.akeo.ie)")
        b.clicked.connect(lambda: QDesktopServices.openUrl(QUrl("https://zadig.akeo.ie/")))
        v.addWidget(b)
        lay.addWidget(box)
        lay.addStretch(1)
        self.output = output
        t = QTimer(self)
        t.timeout.connect(self._show)
        t.start(500)
        self._show()

    def _show(self):
        o = self.output
        color = "#3c3" if o.connected else "#e44"
        self.state.setText(f"<span style='color:{color}'>●</span> {o.status}")
        st = o.stats
        txt = (f"Каналов в работе: {o.limit}. Посылок: {st['sent']}, ошибок: {st['errors']}, "
               f"переподключений: {st['reconnects']}.")
        if st["last_error"]:
            txt += f"\nПоследняя ошибка: {st['last_error']}"
        self.stats.setText(txt)

    def _open_log(self):
        path = self.output.log_path
        if path and path.exists():
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))
        else:
            self.stats.setText(self.stats.text() + "\nЖурнал пока пуст — ошибок не было.")


class SettingsDialog(QDialog):
    def __init__(self, settings, client, output, update_button, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки — Concert Light")
        self.settings = settings
        lay = QVBoxLayout(self)
        tabs = QTabWidget()
        self.daw = DawTab(settings, client)
        tabs.addTab(self.daw, "Concert DAW")
        self.udmx = UdmxTab(output, settings)
        tabs.addTab(self.udmx, "uDMX")
        upd, self.update_auto = build_updates_tab(self, update_button, bool(settings.update_auto),
                                                  app_data_dir() / "error.log")
        tabs.addTab(upd, "Обновления и отзывы")
        lay.addWidget(tabs, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)
        self.resize(600, 560)

    def apply(self):
        """Сохранить. Возвращает True, если изменилось подключение к DAW."""
        s = self.settings
        ip, port = self.daw.ip.text().strip(), self.daw.port.value()
        changed = (ip, port) != (s.daw_ip, int(s.daw_port))
        s.set(daw_ip=ip, daw_port=port, daw_name=self.daw.name if ip else "",
              auto_connect=self.daw.auto.isChecked(), update_auto=self.update_auto.isChecked(),
              udmx_rate=self.udmx.rate.value())
        return changed
