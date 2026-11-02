"""Главное окно Concert Light."""

import os
import threading
import time

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (
    QFileDialog, QLabel, QMainWindow, QMessageBox, QPushButton, QSlider, QTabWidget, QToolBar,
    QWidget, QSizePolicy,
)

from core.appdata import app_data_dir, shows_dir
from core.engine import Engine
from core.output import Output
from core.show import Show

from .chases_tab import ChasesTab
from .effects_tab import EffectsTab
from .patch_tab import PatchTab
from .programmer_tab import ProgrammerTab
from .scenes_tab import ScenesTab
from .settings_dialog import SettingsDialog
from .timeline_tab import TimelineTab
from .widgets import Hub
from gui_update import UpdateButton
from sync_client import SyncClient, discover
from version import VERSION

APP_TITLE = "Concert Light"


class MainWindow(QMainWindow):
    def __init__(self, library, lib_folder, settings):
        super().__init__()
        self.settings = settings
        self.client = SyncClient()
        self._found = None
        self._searching = False
        self._last_search = 0.0
        self.show_ = Show(library)
        self.output = Output(rate=int(settings.udmx_rate or 20), log_path=app_data_dir() / "udmx.log")
        self.engine = Engine(self.show_, self.output)
        self.hub = Hub()

        self.tabs = QTabWidget()
        self.patch_tab = PatchTab(self.show_, self.engine, self.hub, lib_folder)
        self.prog_tab = ProgrammerTab(self.show_, self.engine, self.hub)
        self.scenes_tab = ScenesTab(self.show_, self.engine, self.hub)
        self.chases_tab = ChasesTab(self.show_, self.engine, self.hub)
        self.effects_tab = EffectsTab(self.show_, self.engine, self.hub)
        self.timeline_tab = TimelineTab(self.show_, self.engine, self.hub, self.client)
        for w, name in ((self.patch_tab, "Патч"), (self.prog_tab, "Программер"), (self.scenes_tab, "Сцены"),
                        (self.chases_tab, "Чейзы"), (self.effects_tab, "Эффекты"),
                        (self.timeline_tab, "Таймлайн")):
            self.tabs.addTab(w, name)
        self.setCentralWidget(self.tabs)

        self._build_toolbar()
        self.daw_label = QLabel("")
        self.statusBar().addPermanentWidget(self.daw_label)
        self.update_btn = UpdateButton(
            self, self._update_blocker, lambda: float(self.settings.update_checked or 0),
            lambda t: self.settings.set(update_checked=t), lambda: bool(self.settings.update_auto))
        self.statusBar().addPermanentWidget(self.update_btn)
        self.status_timer = QTimer(self)
        self.status_timer.timeout.connect(self._update_status)
        self.status_timer.start(400)

        self.engine.start()
        last = self.settings.last_show
        if last and os.path.exists(last):
            self._load(last)
        self._update_title()
        self._start_daw_link()

    # ------------------------------------------------------------------

    def _build_toolbar(self):
        tb = QToolBar("Главная")
        tb.setMovable(False)
        self.addToolBar(tb)
        for text, key, slot in (("Новое шоу", QKeySequence.New, self._new),
                                ("Открыть…", QKeySequence.Open, self._open),
                                ("Сохранить", QKeySequence.Save, self._save),
                                ("Сохранить как…", QKeySequence.SaveAs, self._save_as)):
            a = QAction(text, self)
            a.setShortcut(QKeySequence(key))
            a.triggered.connect(slot)
            tb.addAction(a)
        a = QAction("Настройки…", self)
        a.triggered.connect(self.open_settings)
        tb.addAction(a)
        tb.addSeparator()

        tb.addWidget(QLabel(" Мастер "))
        self.gm = QSlider(Qt.Horizontal)
        self.gm.setRange(0, 100)
        self.gm.setValue(100)
        self.gm.setFixedWidth(160)
        self.gm.valueChanged.connect(self._gm_changed)
        tb.addWidget(self.gm)
        self.gm_label = QLabel("100% ")
        tb.addWidget(self.gm_label)

        self.bo = QPushButton("BLACKOUT")
        self.bo.setCheckable(True)
        self.bo.setStyleSheet("QPushButton { padding: 4px 14px; font-weight: bold; }"
                              "QPushButton:checked { background: #c01818; color: white; }")
        self.bo.toggled.connect(self._blackout)
        tb.addWidget(self.bo)

        rel = QPushButton("Сброс всего")
        rel.setToolTip("Остановить чейз и эффекты, очистить программер, погасить сцены")
        rel.clicked.connect(self._release_all)
        tb.addWidget(rel)

        spacer = QWidget()
        spacer.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        tb.addWidget(spacer)
        self.out_label = QLabel()
        tb.addWidget(self.out_label)

    def _gm_changed(self, v):
        self.engine.gm = v / 100.0
        self.gm_label.setText(f"{v}% ")

    def _blackout(self, on):
        self.engine.blackout = on
        self.hub.action.emit("blackout_on" if on else "blackout_off", "", 0.0)

    def _release_all(self):
        self.engine.release_all(1.0)
        self.hub.changed.emit("patch")  # обновить программер
        self.hub.action.emit("release", "", 1.0)

    def _update_status(self):
        connected = self.output.connected
        color = "#3c3" if connected else "#e44"
        self.out_label.setText(f"<span style='color:{color}'>●</span> {self.output.status} ")
        e = self.engine
        parts = [f"Сцена: {e.current_scene or '—'}"]
        if e.chase:
            parts.append(f"Чейз: {e.chase['name']}")
        if e.effects:
            parts.append("Эффекты: " + ", ".join(e.effects))
        if e.programmer:
            parts.append(f"Программер: {len(e.programmer)} приб.")
        self.statusBar().showMessage("   |   ".join(parts))
        self._check_search()
        c = self.client
        if c.connected:
            self.daw_label.setText(f"<span style='color:#3c3'>●</span> DAW: {c.server_name or c.ip} ")
        elif c.ip:
            self.daw_label.setText(f"<span style='color:#e44'>●</span> DAW: нет связи ")
        else:
            self.daw_label.setText("")
        if self.bo.isChecked() != e.blackout:  # blackout могла переключить кьюшка
            self.bo.blockSignals(True)
            self.bo.setChecked(e.blackout)
            self.bo.blockSignals(False)

    # ------------------------------------------------------------------
    # Concert DAW, настройки, обновления
    # ------------------------------------------------------------------

    def _start_daw_link(self):
        s = self.settings
        if s.auto_connect and s.daw_ip:
            self.client.connect(s.daw_ip, int(s.daw_port))
        elif s.auto_connect:
            self._search_daw()

    def _search_daw(self):
        """Поиск Concert DAW в сети в фоне; результат забирает _check_search."""
        if self._searching:
            return
        self._searching = True
        self._last_search = time.monotonic()

        def run():
            try:
                self._found = discover(1.5)
            except Exception:
                self._found = []
        threading.Thread(target=run, daemon=True, name="daw-discover").start()

    def _check_search(self):
        s = self.settings
        if self._searching and self._found is not None:
            found, self._found, self._searching = self._found, None, False
            pick = next((d for d in found if s.daw_name and d["name"] == s.daw_name), None)
            if pick is None and not s.daw_ip and found:
                pick = found[0]                      # первый запуск — берём найденный
            if pick and ((pick["ip"], pick["port"]) != (self.client.ip, self.client.port)
                         or not self.client.connected):
                s.set(daw_ip=pick["ip"], daw_port=int(pick["port"]), daw_name=pick["name"])
                self.client.connect(pick["ip"], int(pick["port"]))
                self.statusBar().showMessage(f"Найден Concert DAW «{pick['name']}» — подключаюсь", 4000)
            return
        # связь пропала надолго (например, у компьютера с DAW сменился IP) — ищем его по имени
        if (s.auto_connect and s.daw_name and self.client.ip and not self.client.connected
                and not self._searching and time.monotonic() - self._last_search > 15):
            self._search_daw()

    def open_settings(self):
        dlg = SettingsDialog(self.settings, self.client, self.output, self.update_btn, self)
        if dlg.exec():
            if dlg.apply():
                if self.settings.daw_ip:
                    self.client.connect(self.settings.daw_ip, int(self.settings.daw_port))
                else:
                    self.client.disconnect()

    def _update_blocker(self) -> str:
        e = self.engine
        if self.timeline_tab.is_playing():
            return "Сначала остановите песню — обновление перезапускает программу."
        if e.current_scene or e.chase or e.effects or e.programmer:
            return ("Обновление перезапускает программу — свет на время погаснет. "
                    "Сделайте это вне концерта: нажмите «Сброс всего» и обновите.")
        if self._has_content() and not self.show_.path:
            return "Сначала сохраните шоу (Сохранить как…) — после обновления оно откроется само."
        return ""

    # ------------------------------------------------------------------
    # файлы
    # ------------------------------------------------------------------

    def _update_title(self):
        name = os.path.basename(self.show_.path) if self.show_.path else "без имени"
        self.setWindowTitle(f"{APP_TITLE} {VERSION} — {name}")

    def _replace_show(self, new):
        with self.engine.lock:
            self.engine.release_all(0)
            self.engine.blackout = False
            self.show_.__dict__.update(new.__dict__)  # вкладки держат ссылку на тот же объект
        self.engine.rebuild_patch()
        self.timeline_tab.stop()
        self.hub.changed.emit("all")
        self._update_title()

    def _new(self):
        if not self._confirm_discard():
            return
        self._replace_show(Show(self.show_.library))

    def _open(self):
        if not self._confirm_discard():
            return
        path, _ = QFileDialog.getOpenFileName(self, "Открыть шоу", self._dir(), "Шоу (*.clshow *.json)")
        if path:
            self._load(path)

    def _load(self, path):
        try:
            self._replace_show(Show.load(path, self.show_.library))
            self.settings.set(last_show=path)
        except Exception as e:
            QMessageBox.warning(self, "Ошибка", f"Не удалось открыть шоу:\n{e}")

    def _save(self):
        if not self.show_.path:
            return self._save_as()
        try:
            self.show_.save()
            self.settings.set(last_show=self.show_.path)
            self.statusBar().showMessage("Сохранено", 2000)
            return True
        except Exception as e:
            QMessageBox.warning(self, "Ошибка", f"Не удалось сохранить:\n{e}")
            return False

    def _save_as(self):
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить шоу", self._dir(), "Шоу (*.clshow)")
        if not path:
            return False
        if not os.path.splitext(path)[1]:
            path += ".clshow"
        self.show_.path = path
        ok = self._save()
        self._update_title()
        return ok

    def _dir(self):
        if self.show_.path:
            return os.path.dirname(self.show_.path)
        d = shows_dir()
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            return os.path.expanduser("~")
        return str(d)

    def _has_content(self):
        s = self.show_
        return bool(s.patch or s.scenes or s.chases or s.effects or s.songs)

    def _confirm_discard(self):
        if not self._has_content():
            return True
        r = QMessageBox.question(self, APP_TITLE, "Сохранить текущее шоу?",
                                 QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        if r == QMessageBox.Save:
            return bool(self._save())
        return r == QMessageBox.Discard

    def closeEvent(self, e):
        if not self._confirm_discard():
            e.ignore()
            return
        self.timeline_tab.shutdown()
        self.client.disconnect()
        self.engine.stop()
        e.accept()
