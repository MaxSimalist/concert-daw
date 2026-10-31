"""
plugin_host.py — Хостинг VST3-плагинов (на macOS ещё и AU).

Почему Spotify «pedalboard», а не python-vst / cython-vstwrap
-------------------------------------------------------------
python-vst и cython-vstwrap давно заброшены, поддерживают только VST2 (чей SDK
Steinberg отозвал) и не собираются под актуальные версии Python. pedalboard —
поддерживаемая обёртка над JUCE: грузит VST3, умеет показывать родное окно
плагина, отпускает GIL во время обработки и ставится одной командой из pip.
Ограничение: VST2 (.dll) не поддерживается — только VST3.

Потокобезопасность
------------------
* process() вызывается ИЗ АУДИО-КОЛЛБЭКА. Он не должен бросать исключения
  наружу: упавший плагин помечается failed и дальше просто байпасится —
  звук дорожки продолжает идти на пульт без обработки.
* Замена плагина = присваивание ссылки self.plugin (атомарно под GIL).
  Новый плагин полностью создаётся и «прогревается» ДО присваивания,
  поэтому аудиопоток никогда не видит наполовину инициализированный объект.
"""
from __future__ import annotations

import base64
import os
import sys
import threading
from pathlib import Path

import numpy as np

try:
    from pedalboard import load_plugin
    PEDALBOARD_AVAILABLE = True
    PEDALBOARD_ERROR = ""
except Exception as exc:  # pragma: no cover — зависит от окружения
    load_plugin = None
    PEDALBOARD_AVAILABLE = False
    PEDALBOARD_ERROR = str(exc)


def default_vst3_dir() -> str:
    if sys.platform == "win32":
        return os.path.join(os.environ.get("COMMONPROGRAMFILES", r"C:\Program Files\Common Files"), "VST3")
    if sys.platform == "darwin":
        return "/Library/Audio/Plug-Ins/VST3"
    return str(Path.home() / ".vst3")


class PluginSlot:
    """Один инсерт-слот на дорожке."""

    def __init__(self) -> None:
        self.plugin = None          # объект pedalboard (читается аудиопотоком)
        self.path = ""
        self.name = ""
        self.bypass = False
        self.failed = False         # плагин бросил исключение в process()
        self.load_error = ""        # не удалось загрузить при открытии песни
        self._editor_thread: threading.Thread | None = None
        self._close_event: threading.Event | None = None

    # ── загрузка / выгрузка (GUI-поток или поток загрузчика) ────────────────
    def load(self, path: str, state_b64: str = "", samplerate: float = 48000.0,
             channels: int = 2, blocksize: int = 256) -> None:
        if not PEDALBOARD_AVAILABLE:
            raise RuntimeError(f"pedalboard не установлен: {PEDALBOARD_ERROR}")
        plugin = load_plugin(path)
        if getattr(plugin, "is_instrument", False) and not getattr(plugin, "is_effect", True):
            raise ValueError("Это виртуальный инструмент, а в инсерт нужен эффект")
        if state_b64:
            try:
                plugin.raw_state = base64.b64decode(state_b64)
            except Exception:
                pass  # старая версия pedalboard / плагин не отдаёт состояние — грузим с дефолтами

        # «Прогрев»: первый process() заставляет плагин выделить память и
        # выполнить prepareToPlay. Делаем это ЗДЕСЬ, а не в аудио-коллбэке,
        # иначе первый блок после загрузки дал бы щелчок/дроп на выходе пульта.
        dummy = np.zeros((max(1, channels), blocksize), dtype=np.float32)
        plugin.process(dummy, float(samplerate), buffer_size=blocksize, reset=True)

        self.close_editor()
        self.plugin = plugin  # атомарная замена — аудиопоток подхватит со следующего блока
        self.path = path
        self.name = getattr(plugin, "name", "") or Path(path).stem
        self.failed = False
        self.load_error = ""

    def unload(self) -> None:
        self.close_editor()
        self.plugin = None
        self.path = self.name = ""
        self.failed = False

    def export_state(self) -> str:
        plugin = self.plugin
        if plugin is None:
            return ""
        try:
            return base64.b64encode(bytes(plugin.raw_state)).decode("ascii")
        except Exception:
            return ""

    def reset(self) -> None:
        """Сброс хвостов (реверб, дилей) — вызывать, когда поток не играет."""
        plugin = self.plugin
        if plugin is not None:
            try:
                plugin.reset()
            except Exception:
                pass

    # ── обработка (АУДИО-ПОТОК) ──────────────────────────────────────────────
    def process(self, block: np.ndarray, samplerate: float) -> np.ndarray:
        """block: (frames, channels) float32. Возвращает массив той же формы."""
        plugin = self.plugin               # локальная копия ссылки — защита от замены посреди блока
        if plugin is None or self.bypass or self.failed:
            return block
        frames = block.shape[0]
        try:
            # pedalboard ждёт (channels, frames); reset=False — сохраняем состояние
            # между блоками, иначе компрессор/эквалайзер «обнулялись» бы каждые 5 мс
            out = plugin.process(np.ascontiguousarray(block.T), samplerate,
                                 buffer_size=frames, reset=False)
        except Exception:
            self.failed = True             # дальше — байпас, поток не роняем
            return block
        out = out.T
        if out.shape != block.shape:       # плагин сменил число каналов (моно→стерео и т.п.)
            fixed = np.zeros_like(block)
            fr = min(out.shape[0], frames)
            ch = min(out.shape[1], block.shape[1])
            fixed[:fr, :ch] = out[:fr, :ch]
            return fixed
        return out

    # ── родное окно плагина ──────────────────────────────────────────────────
    def open_editor(self) -> bool:
        plugin = self.plugin
        if plugin is None:
            return False
        if self._editor_thread is not None and self._editor_thread.is_alive():
            return False  # уже открыто
        if sys.platform == "darwin":
            # macOS разрешает окна только из главного потока — GUI блокируется до закрытия окна
            plugin.show_editor()
            return True

        # Windows: show_editor() блокирует поток до закрытия окна, поэтому выносим его
        # в отдельный поток, чтобы транспорт (PLAY/STOP, Пробел) продолжал работать.
        self._close_event = threading.Event()
        close_event = self._close_event

        def run() -> None:
            try:
                plugin.show_editor(close_event)
            except TypeError:              # старые версии pedalboard без close_event
                plugin.show_editor()
            except Exception as exc:
                print(f"[VST] окно плагина не открылось: {exc}", file=sys.stderr)

        self._editor_thread = threading.Thread(target=run, daemon=True,
                                               name=f"vst-editor-{self.name}")
        self._editor_thread.start()
        return True

    def close_editor(self) -> None:
        if self._close_event is not None:
            self._close_event.set()
