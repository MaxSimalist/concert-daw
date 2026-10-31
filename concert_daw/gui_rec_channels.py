"""
gui_rec_channels.py — Выбор входов для записи (какие из 32 каналов писать).

* Сетка 1…32: галочка «писать» + имя канала. Имя попадает в название файла
  (Ch05 Вокал.wav) и в подпись дорожки на таймлайне.
* Быстрые кнопки: все / ничего / 1–8 / 1–16 / 17–32 / только с именами.
* «Имена с пульта» — подставить названия каналов X32 (Ch 1–32), если пульт
  на связи. По умолчанию карта X-USB отдаёт в компьютер каналы 1–32 как есть
  (Routing → Card Out); если роутинг другой — подпишите входы вручную.
* Внизу — сколько каналов выбрано и сколько места на диске займёт час записи.
"""
from __future__ import annotations

from typing import Callable, Optional

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QGridLayout, QHBoxLayout, QLabel,
                               QLineEdit, QPushButton, QVBoxLayout, QWidget)

from gui_icons import set_icon
from gui_layout import fit_to_screen, scrollable
from recorder import format_channels, parse_channels

MAX_INPUTS = 32
COLUMNS = 4


class RecChannelsDialog(QDialog):
    """selected — строка вида «1-8, 17»; names — {"5": "Вокал"}; available — сколько входов
    есть у устройства записи; console_names() — {номер: имя} с пульта (или пусто)."""

    def __init__(self, selected: str, names: dict, available: int = MAX_INPUTS,
                 samplerate: int = 48000, subtype: str = "PCM_24",
                 console_names: Optional[Callable[[], dict]] = None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Входы для записи")
        self.available = max(1, min(MAX_INPUTS, available or MAX_INPUTS))
        self.samplerate = samplerate
        self.bytes_per_sample = {"PCM_16": 2, "PCM_24": 3, "FLOAT": 4}.get(subtype, 3)
        self.console_names = console_names
        chosen = {c + 1 for c in parse_channels(selected, self.available)}

        root = QVBoxLayout(self)
        info = QLabel("Отметьте каналы, которые нужно записывать. Имя канала попадёт в название файла "
                      "(Ch05 Вокал.wav) и на таймлайн. Номера — входы карты X-USB "
                      "(на пульте: Routing → Card Out).")
        info.setWordWrap(True)
        root.addWidget(info)

        presets = QHBoxLayout()
        for text, fn in (("Все", lambda: self._set(range(1, MAX_INPUTS + 1))),
                         ("Ничего", lambda: self._set([])),
                         ("1–8", lambda: self._set(range(1, 9))),
                         ("1–16", lambda: self._set(range(1, 17))),
                         ("17–32", lambda: self._set(range(17, 33))),
                         ("Только с именами", self._only_named)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            presets.addWidget(b)
        self.console_btn = QPushButton(" Имена с пульта")
        set_icon(self.console_btn, "plug", 15)
        self.console_btn.setToolTip("Подставить названия каналов 1–32 с пульта X32 (пульт должен быть на связи)")
        self.console_btn.clicked.connect(self._from_console)
        presets.addWidget(self.console_btn)
        presets.addStretch(1)
        root.addLayout(presets)

        grid_host = QWidget()
        grid = QGridLayout(grid_host)
        grid.setHorizontalSpacing(14)
        grid.setVerticalSpacing(4)
        self.checks: dict[int, QCheckBox] = {}
        self.edits: dict[int, QLineEdit] = {}
        rows = (MAX_INPUTS + COLUMNS - 1) // COLUMNS
        for n in range(1, MAX_INPUTS + 1):
            col, row = (n - 1) // rows, (n - 1) % rows
            cb = QCheckBox(str(n))
            cb.setMinimumWidth(46)
            cb.setChecked(n in chosen)
            edit = QLineEdit(str(names.get(str(n), "")))
            edit.setPlaceholderText(f"Вход {n}")
            edit.setMinimumWidth(90)
            edit.setMaxLength(32)
            if n > self.available:
                cb.setEnabled(False)
                cb.setChecked(False)
                edit.setEnabled(False)
                cb.setToolTip(f"У устройства записи только {self.available} входов")
            cb.toggled.connect(self._update_summary)
            # ввели имя — канал, скорее всего, нужен: отмечаем сами
            edit.textEdited.connect(lambda t, c=cb: c.setChecked(True) if t.strip() and c.isEnabled() else None)
            grid.addWidget(cb, row, col * 2)
            grid.addWidget(edit, row, col * 2 + 1)
            self.checks[n], self.edits[n] = cb, edit
        root.addWidget(scrollable(grid_host), 1)

        self.summary = QLabel()
        self.summary.setObjectName("caption")
        root.addWidget(self.summary)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._accept)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self._update_summary()
        fit_to_screen(self, 860, 560)

    # ── быстрые действия ────────────────────────────────────────────────────
    def _set(self, numbers) -> None:
        wanted = set(numbers)
        for n, cb in self.checks.items():
            if cb.isEnabled():
                cb.setChecked(n in wanted)

    def _only_named(self) -> None:
        self._set([n for n, e in self.edits.items() if e.text().strip()])

    def _from_console(self) -> None:
        got = self.console_names() if self.console_names else {}
        if not got:
            self.summary.setText("Пульт не на связи или названия ещё не пришли — подключите пульт "
                                 "(зона «Пульт X32») и нажмите ещё раз.")
            return
        for n, name in got.items():
            if n in self.edits and name and self.edits[n].isEnabled():
                self.edits[n].setText(name)
        self.summary.setText(f"Имена с пульта подставлены ({len(got)}). Отметьте нужные каналы.")

    # ── итог ───────────────────────────────────────────────────────────────
    def selected(self) -> list[int]:
        return [n for n, cb in self.checks.items() if cb.isChecked()]

    def channels_spec(self) -> str:
        return format_channels(self.selected())

    def names(self) -> dict:
        return {str(n): e.text().strip() for n, e in self.edits.items() if e.text().strip()}

    def _update_summary(self, *_args) -> None:
        n = len(self.selected())
        gb_hour = n * self.samplerate * self.bytes_per_sample * 3600 / 1e9
        text = (f"Выбрано каналов: {n} из {self.available}" +
                (f"  ·  час записи ≈ {gb_hour:.1f} ГБ" if n else "  ·  ничего не выбрано"))
        self.summary.setText(text)

    def _accept(self) -> None:
        if not self.selected():
            self.summary.setText("Отметьте хотя бы один канал.")
            return
        self.accept()


def summary_text(spec: str, names: dict, available: int = MAX_INPUTS) -> str:
    """Короткое описание выбора для настроек и подсказки REC: «6 кан.: 1 Вокал, 2 Гитара, 5–8»."""
    chans = [c + 1 for c in parse_channels(spec, available)]
    if not chans:
        return "ничего не выбрано"
    named = [f"{n} {names[str(n)]}" for n in chans if names.get(str(n))]
    head = f"{len(chans)} кан. ({format_channels(chans)})"
    if named:
        more = "…" if len(named) > 6 else ""
        head += ": " + ", ".join(named[:6]) + more
    return head
