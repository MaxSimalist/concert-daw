"""
gui_feedback.py — Окно «Предложить идею / Сообщить об ошибке» и вкладка настроек
«Обновления и отзывы» (общие для Concert DAW и Concert Video).
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices, QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
                               QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget)

import feedback
from version import APP_NAME, REPO, VERSION


class FeedbackDialog(QDialog):
    """Человек пишет сам, видит весь отчёт и сам выбирает, куда отправить."""

    def __init__(self, kind: str = "idea", log_path: Optional[Path] = None, error_text: str = "", parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Отзыв о программе — {APP_NAME} {VERSION}")
        self.log_path = log_path
        lay = QVBoxLayout(self)
        form = QFormLayout()
        self.kind = QComboBox()
        self.kind.addItem("Предложить идею", "idea")
        self.kind.addItem("Сообщить об ошибке", "bug")
        self.kind.setCurrentIndex(max(0, self.kind.findData(kind)))
        self.kind.currentIndexChanged.connect(self._kind_changed)
        form.addRow("Что:", self.kind)
        self.title = QLineEdit()
        self.title.setPlaceholderText("Коротко: «Добавить кнопку…» / «Не открывается проектор»")
        form.addRow("Заголовок:", self.title)
        lay.addLayout(form)
        self.text = QPlainTextEdit()
        lay.addWidget(self.text, 1)
        self.sys_cb = QCheckBox("Добавить версию программы и Windows")
        self.sys_cb.setChecked(True)
        self.log_cb = QCheckBox("Добавить последние ошибки из журнала программы")
        has_log = bool(error_text) or bool(log_path and feedback.recent_errors(log_path))
        self.log_cb.setEnabled(has_log)
        self.error_text = error_text
        lay.addWidget(self.sys_cb)
        lay.addWidget(self.log_cb)
        note = QLabel("Программа ничего не отправляет сама. Перед отправкой вы увидите весь текст; "
                      "имя пользователя, компьютера и пути к вашим папкам из него убираются.")
        note.setWordWrap(True)
        note.setObjectName("caption")
        lay.addWidget(note)
        row = QHBoxLayout()
        preview = QPushButton("Показать, что уйдёт")
        preview.clicked.connect(self._preview)
        row.addWidget(preview)
        row.addStretch(1)
        gh = QPushButton(" Отправить через GitHub")
        gh.setToolTip(f"Откроется страница нового обращения в github.com/{REPO} с готовым текстом "
                      "(нужен бесплатный аккаунт GitHub)")
        gh.clicked.connect(self._github)
        row.addWidget(gh)
        if feedback.FEEDBACK_EMAIL:
            mail = QPushButton(" По почте")
            mail.setToolTip(f"Письмо на {feedback.FEEDBACK_EMAIL} в вашей почтовой программе")
            mail.clicked.connect(self._mail)
            row.addWidget(mail)
        copy = QPushButton("Копировать текст")
        copy.clicked.connect(self._copy)
        row.addWidget(copy)
        lay.addLayout(row)
        self.resize(620, 520)
        if error_text:
            self.log_cb.setChecked(True)
        self._kind_changed()

    def _kind_changed(self, *_a) -> None:
        bug = self.kind.currentData() == "bug"
        self.text.setPlaceholderText(
            "Что произошло? Что вы делали перед этим (какие кнопки нажимали)? Что ожидали увидеть?"
            if bug else "Опишите идею: что хотелось бы добавить или изменить и зачем это нужно на концерте.")
        if bug and self.log_cb.isEnabled():
            self.log_cb.setChecked(True)

    def report(self) -> tuple[str, str]:
        log = ""
        if self.log_cb.isChecked():
            log = self.error_text or (feedback.recent_errors(self.log_path) if self.log_path else "")
        return feedback.build_report(self.kind.currentData(), self.title.text(), self.text.toPlainText(),
                                     self.sys_cb.isChecked(), log)

    def _ready(self) -> bool:
        if not self.text.toPlainText().strip() and not self.title.text().strip():
            QMessageBox.information(self, "Отзыв", "Напишите хотя бы пару слов.")
            return False
        return True

    def _preview(self) -> None:
        title, body = self.report()
        box = QMessageBox(self)
        box.setWindowTitle("Что будет отправлено")
        box.setText(title)
        box.setDetailedText(body)
        box.setInformativeText("Полный текст — кнопка «Show Details…» / «Подробнее».")
        box.exec()

    def _copy(self) -> None:
        title, body = self.report()
        QGuiApplication.clipboard().setText(f"{title}\n\n{body}")
        QMessageBox.information(self, "Отзыв", "Текст скопирован — вставьте его в письмо или сообщение.")

    def _github(self) -> None:
        if not self._ready():
            return
        title, body = self.report()
        QGuiApplication.clipboard().setText(f"{title}\n\n{body}")     # на случай, если адрес обрежется
        QDesktopServices.openUrl(QUrl(feedback.issue_url(title, body)))
        self.accept()

    def _mail(self) -> None:
        if not self._ready():
            return
        title, body = self.report()
        QGuiApplication.clipboard().setText(f"{title}\n\n{body}")
        QDesktopServices.openUrl(QUrl(feedback.mailto_url(title, body)))
        self.accept()


def build_updates_tab(parent: QWidget, update_button, auto_on: bool, log_path: Path) -> tuple[QWidget, QCheckBox]:
    """Вкладка настроек «Обновления и отзывы». Возвращает (вкладка, галочка «проверять сами»)."""
    w = QWidget()
    lay = QVBoxLayout(w)
    box = QGroupBox("Обновления")
    f = QFormLayout(box)
    f.addRow("Установлена версия:", QLabel(f"<b>{VERSION}</b>"))
    link = QLabel(f'<a href="https://github.com/{REPO}">github.com/{REPO}</a>')
    link.setOpenExternalLinks(True)
    f.addRow("Откуда обновления:", link)
    auto = QCheckBox("Проверять новую версию при запуске (раз в сутки)")
    auto.setChecked(auto_on)
    f.addRow(auto)
    check = QPushButton(" Проверить сейчас")
    check.setToolTip("Спросить GitHub, нет ли новой версии; если есть — показать, что нового, и обновить")

    def do_check():
        from gui_update import UpdateDialog
        if update_button is not None:
            UpdateDialog(update_button, parent).exec()
    check.clicked.connect(do_check)
    f.addRow(check)
    hint = QLabel("Обновить можно и вручную: закройте программу и запустите update.bat в её папке.")
    hint.setWordWrap(True)
    hint.setObjectName("caption")
    f.addRow(hint)
    lay.addWidget(box)

    box2 = QGroupBox("Идеи и ошибки")
    v = QVBoxLayout(box2)
    info = QLabel("Есть идея, чего не хватает на концерте, или что-то работает не так? Напишите — "
                  "текст можно отправить через GitHub или скопировать. Программа ничего не отправляет "
                  "сама и не собирает данные в фоне.")
    info.setWordWrap(True)
    v.addWidget(info)
    row = QHBoxLayout()
    idea = QPushButton(" Предложить идею")
    idea.clicked.connect(lambda: FeedbackDialog("idea", log_path, parent=parent).exec())
    bug = QPushButton(" Сообщить об ошибке")
    bug.clicked.connect(lambda: FeedbackDialog("bug", log_path, parent=parent).exec())
    row.addWidget(idea)
    row.addWidget(bug)
    row.addStretch(1)
    v.addLayout(row)
    lay.addWidget(box2)
    lay.addStretch(1)
    return w, auto
