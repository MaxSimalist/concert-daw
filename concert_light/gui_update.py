"""
gui_update.py — Кнопка версии в строке состояния и окно «Обновление программы».

* При запуске (через 10 с, в фоне, не чаще раза в сутки) программа спрашивает GitHub,
  нет ли новой версии. Есть — кнопка версии внизу окна подсвечивается: «⬆ Обновление 1.1.0».
* Клик по кнопке — окно: текущая и новая версия, что нового, «Обновить сейчас».
* «Обновить сейчас»: скачивание (с прогрессом) → программа закрывается → новые файлы
  ставятся отдельным процессом → программа запускается снова. Настройки сохраняются.
* Во время игры/записи/эфира обновление не предлагается навязчиво — только подсветка кнопки.
"""
from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (QApplication, QDialog, QHBoxLayout, QLabel, QMessageBox, QPlainTextEdit,
                               QProgressBar, QPushButton, QVBoxLayout)

import updater
from version import APP_NAME, REPO, VERSION


class UpdateChecker:
    """Фоновая проверка: результат забирает GUI-таймер (никаких вызовов Qt из потока)."""

    def __init__(self) -> None:
        self.info: Optional[updater.UpdateInfo] = None
        self.error = ""
        self.busy = False
        self.checked_at = 0.0

    def start(self) -> None:
        if self.busy:
            return
        self.busy = True

        def run():
            try:
                self.info, self.error = updater.check(), ""
            except Exception as exc:
                self.info, self.error = None, str(exc)
            self.checked_at = time.time()
            self.busy = False
        threading.Thread(target=run, daemon=True, name="update-check").start()


class UpdateButton(QPushButton):
    """Кнопка в строке состояния: «v1.0.0» или «⬆ Обновление 1.1.0»."""

    def __init__(self, parent_window, can_quit: Callable[[], str], last_check_get: Callable[[], float],
                 last_check_set: Callable[[float], None], auto_get: Callable[[], bool] = lambda: True):
        super().__init__(f"v{VERSION}")
        self.win = parent_window
        self.can_quit = can_quit
        self.last_get, self.last_set = last_check_get, last_check_set
        self.checker = UpdateChecker()
        self.setFlat(True)
        self.setFocusPolicy(Qt.NoFocus)
        self.setToolTip(f"{APP_NAME} {VERSION}. Клик — проверить обновления")
        self.setStyleSheet("QPushButton{padding:2px 8px;color:#7f8b97;border:none;background:transparent}"
                           "QPushButton:hover{color:#e6eaee}")
        self.clicked.connect(self.open_dialog)
        self._timer = QTimer(self)
        self._timer.setInterval(500)
        self._timer.timeout.connect(self._poll)
        if REPO and not REPO.startswith("OWNER/") and auto_get() and time.time() - self.last_get() > 20 * 3600:
            QTimer.singleShot(10000, self._auto_check)

    def _auto_check(self) -> None:
        self.checker.start()
        self._timer.start()

    def _poll(self) -> None:
        if self.checker.busy:
            return
        self._timer.stop()
        self.last_set(self.checker.checked_at)
        info = self.checker.info
        if info is not None and info.newer:
            self.setText(f"⬆ Обновление {info.version}")
            self.setToolTip(f"Доступна версия {info.version}. Клик — подробности и установка")
            self.setStyleSheet("QPushButton{padding:2px 10px;color:#1b1705;background:#f7c948;border:none;"
                               "border-radius:4px;font-weight:800}")

    def open_dialog(self) -> None:
        UpdateDialog(self, self.win).exec()


class UpdateDialog(QDialog):
    def __init__(self, button: UpdateButton, parent=None):
        super().__init__(parent)
        self.button = button
        self.setWindowTitle(f"Обновление — {APP_NAME}")
        lay = QVBoxLayout(self)
        self.head = QLabel(f"<b>{APP_NAME}</b> — установлена версия <b>{VERSION}</b>")
        lay.addWidget(self.head)
        self.state = QLabel("Проверяю GitHub…")
        self.state.setWordWrap(True)
        lay.addWidget(self.state)
        self.notes = QPlainTextEdit()
        self.notes.setReadOnly(True)
        self.notes.hide()
        lay.addWidget(self.notes, 1)
        self.bar = QProgressBar()
        self.bar.hide()
        lay.addWidget(self.bar)
        row = QHBoxLayout()
        self.page_btn = QPushButton("Страница на GitHub")
        self.page_btn.clicked.connect(self._open_page)
        row.addWidget(self.page_btn)
        row.addStretch(1)
        self.update_btn = QPushButton("Обновить сейчас")
        self.update_btn.setEnabled(False)
        self.update_btn.clicked.connect(self._update)
        row.addWidget(self.update_btn)
        close = QPushButton("Закрыть")
        close.clicked.connect(self.reject)
        row.addWidget(close)
        lay.addLayout(row)
        self.resize(560, 380)
        self._info: Optional[updater.UpdateInfo] = None
        self._dl: dict = {}
        self._timer = QTimer(self)
        self._timer.setInterval(150)
        self._timer.timeout.connect(self._poll)
        self.button.checker.start()
        self._timer.start()

    def _open_page(self) -> None:
        url = self._info.page if self._info and self._info.page else f"https://github.com/{REPO}/releases"
        QDesktopServices.openUrl(QUrl(url))

    def _poll(self) -> None:
        if self._dl:
            d = self._dl
            if d.get("total"):
                self.bar.setMaximum(d["total"])
                self.bar.setValue(d.get("done", 0))
            if "result" in d or "error" in d:
                self._timer.stop()
                self._finish_download()
            return
        ch = self.button.checker
        if ch.busy:
            return
        self._timer.stop()
        self.button._poll()
        info = ch.info
        if info is None:
            self.state.setText(f"Не удалось проверить: {ch.error}")
            return
        self._info = info
        if not info.newer:
            self.state.setText(f"У вас последняя версия ({VERSION}).")
            return
        self.state.setText(f"Доступна новая версия <b>{info.version}</b>. Настройки и ваши файлы сохранятся.")
        self.notes.setPlainText(info.notes or "(описание изменений не указано)")
        self.notes.show()
        self.update_btn.setEnabled(True)

    def _update(self) -> None:
        busy = self.button.can_quit()
        if busy:
            QMessageBox.information(self, "Обновление", busy)
            return
        self.update_btn.setEnabled(False)
        self.state.setText("Скачиваю обновление…")
        self.bar.setValue(0)
        self.bar.show()
        d = self._dl = {"done": 0, "total": 0}

        def progress(done, total):
            d["done"], d["total"] = done, total

        def run():
            try:
                d["result"] = updater.download(self._info, progress)
            except Exception as exc:
                d["error"] = str(exc)
        threading.Thread(target=run, daemon=True, name="update-download").start()
        self._timer.start()

    def _finish_download(self) -> None:
        d = self._dl
        if "error" in d:
            self.state.setText(f"Скачать не удалось: {d['error']}")
            self.update_btn.setEnabled(True)
            self._dl = {}
            return
        src: Path = d["result"]
        if QMessageBox.question(self, "Обновление", f"Версия {self._info.version} скачана.\n\n"
                                "Программа закроется, обновится и откроется снова (около минуты). "
                                "Продолжить?") != QMessageBox.StandardButton.Yes:
            self.state.setText("Обновление отложено — нажмите «Обновить сейчас», когда будет удобно.")
            self.update_btn.setEnabled(True)
            self._dl = {}
            return
        updater.launch_apply(src)
        self.accept()
        win = self.button.win
        win.close()                      # обычное закрытие: настройки сохраняются
        QApplication.quit()
