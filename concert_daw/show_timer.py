"""
show_timer.py — Секундомер концерта («время пошло»).

* Запускается сам, когда начинает звучать выбранный трек (после отсчёта, если он включён):
    - "first"  — первый трек плейлиста (по умолчанию);
    - "song"   — указанный трек (правый клик по секундомеру → «Запускать с текущего трека»);
    - "manual" — только кнопкой «Запустить сейчас».
* Идёт, пока его не сбросят (пауза и стоп между треками его не останавливают).
* Время старта хранится в настройках (по часам компьютера): если программу перезапустили
  посреди концерта, секундомер продолжит с того же места.
* Concert DAW передаёт его по сети — в Concert Video, Concert Light и Concert Stage идёт то же время.
"""
from __future__ import annotations

import time
from typing import Optional

MODES = {"first": "с первого трека плейлиста", "song": "с указанного трека", "manual": "только вручную"}


def fmt_clock(seconds: float) -> str:
    """1:02:03 или 02:03 (часы — только если есть)."""
    s = int(max(0.0, seconds))
    h, m, sec = s // 3600, (s // 60) % 60, s % 60
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m:02d}:{sec:02d}"


class ShowTimer:
    def __init__(self, mode: str = "first", song_id: str = "", started_at: float = 0.0) -> None:
        self.mode = mode if mode in MODES else "first"
        self.song_id = song_id or ""
        self.started_at = float(started_at or 0.0)       # time.time() старта; 0 — не идёт

    @property
    def running(self) -> bool:
        return self.started_at > 0

    def elapsed(self, now: Optional[float] = None) -> float:
        if not self.running:
            return 0.0
        return max(0.0, (time.time() if now is None else now) - self.started_at)

    def should_start(self, song_id: str, index_in_playlist: int) -> bool:
        """Начал звучать трек song_id (номер в своём плейлисте index_in_playlist, с 0)."""
        if self.running or not song_id:
            return False
        if self.mode == "first":
            return index_in_playlist == 0
        if self.mode == "song":
            return song_id == self.song_id
        return False

    def on_track_started(self, song_id: str, index_in_playlist: int, now: Optional[float] = None) -> bool:
        if self.should_start(song_id, index_in_playlist):
            self.start(now)
            return True
        return False

    def start(self, now: Optional[float] = None) -> None:
        self.started_at = time.time() if now is None else now

    def reset(self) -> None:
        self.started_at = 0.0

    def describe(self, song_name: str = "") -> str:
        if self.mode == "song":
            return f"запуск: с трека «{song_name or '?'}»"
        return "запуск: " + MODES[self.mode]
