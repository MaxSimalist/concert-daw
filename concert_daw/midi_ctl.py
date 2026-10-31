"""
midi_ctl.py — MIDI-педаль / контроллер: управление плеером с ноги или с кнопок.

* Библиотека mido + python-rtmidi (pip install mido python-rtmidi). Без них
  программа работает как обычно, просто вкладка MIDI сообщит, что их нет.
* Сообщения приходят в отдельном потоке rtmidi → кладутся в очередь →
  главное окно забирает их таймером (30 раз в секунду). Никаких вызовов Qt
  из чужого потока.
* «Обучение»: нажимаете «Обучить» у действия и жмёте педаль — запоминается
  её сообщение (нота, CC или Program Change с номером канала).
* CC-педали: срабатывание на нажатие (значение ≥ 64). Для педалей с фиксацией,
  которые шлют 127/0 попеременно, есть настройка «срабатывать на любое значение».
"""
from __future__ import annotations

import queue
from typing import Optional

try:
    import mido
    MIDO_AVAILABLE = True
    MIDO_ERROR = ""
except Exception as exc:  # pragma: no cover
    mido = None
    MIDO_AVAILABLE = False
    MIDO_ERROR = str(exc)

ACTIONS: dict[str, str] = {
    "play_stop": "Play / Пауза",
    "play": "Play",
    "pause": "Пауза / продолжить",
    "stop": "Stop (в начало)",
    "next": "Следующий трек",
    "prev": "Предыдущий трек",
    "next_marker": "Следующий маркер",
    "prev_marker": "Предыдущий маркер",
    "loop": "Петля вкл/выкл",
    "metro": "Метроном вкл/выкл",
    "countin": "Отсчёт вкл/выкл",
    "rec": "Запись вкл/выкл",
}


def describe(sig: str) -> str:
    """note:0:60 → «Нота 60, канал 1»."""
    if not sig:
        return "—"
    kind, ch, num = sig.split(":")
    name = {"note": "Нота", "cc": "CC", "pc": "Program"}.get(kind, kind)
    return f"{name} {num}, канал {int(ch) + 1}"


class MidiController:
    def __init__(self) -> None:
        self.port = None
        self.port_name = ""
        self.cc_any = False
        self.learning = False   # пока окно настроек «учится», главное окно не забирает сообщения
        self.error = ""
        self._q: "queue.SimpleQueue[str]" = queue.SimpleQueue()
        self._last_cc: dict[str, int] = {}

    @staticmethod
    def list_ports() -> list[str]:
        if not MIDO_AVAILABLE:
            return []
        try:
            return list(mido.get_input_names())
        except Exception:
            return []

    def open(self, name: str) -> bool:
        self.close()
        if not name or not MIDO_AVAILABLE:
            return False
        try:
            self.port = mido.open_input(name, callback=self._on_message)
            self.port_name = name
            self.error = ""
            return True
        except Exception as exc:
            self.error = str(exc)
            self.port = None
            return False

    def close(self) -> None:
        if self.port is not None:
            try:
                self.port.close()
            except Exception:
                pass
        self.port = None
        self.port_name = ""

    def signature(self, msg) -> Optional[str]:
        t = getattr(msg, "type", "")
        if t == "note_on" and msg.velocity > 0:
            return f"note:{msg.channel}:{msg.note}"
        if t == "control_change":
            key = f"cc:{msg.channel}:{msg.control}"
            prev = self._last_cc.get(key)
            self._last_cc[key] = msg.value
            if self.cc_any:
                return key if prev != msg.value else None
            # нажатие: переход через середину вверх (или первое сообщение ≥ 64)
            if msg.value >= 64 and (prev is None or prev < 64):
                return key
            return None
        if t == "program_change":
            return f"pc:{msg.channel}:{msg.program}"
        return None

    def _on_message(self, msg) -> None:
        """Поток rtmidi: только очередь."""
        sig = self.signature(msg)
        if sig:
            self._q.put(sig)

    def poll(self) -> list[str]:
        out = []
        while True:
            try:
                out.append(self._q.get_nowait())
            except queue.Empty:
                return out
