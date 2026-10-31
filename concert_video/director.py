"""
director.py — «Режиссёр»: что показывать на экранах в каждый момент по состоянию
Concert DAW, и как держать видео в синхроне с треком. Без Qt — проверяется тестами.

Правила автоматики (Project.follow_daw):
* Трек играет или на паузе в середине → сцена трека в этот момент (с учётом смены
  сцен по времени — cues).
* Трек выбран, но стоит в начале (ещё не запускали / STOP) → по настройке привязки:
  оставить сцену трека с первым кадром, показать заставку или затемнить.
* Трек без привязки → ничего не трогать или заставка (Project.unbound_action).
* Связи с DAW нет → ничего не трогаем: оператор управляет вручную.

VideoSync — удержание видео в синхроне с часами мастера:
* расхождение > 0.30 с (старт, перемотка, сбой) — перескок на нужное место;
* 0.02…0.30 с — плавная подстройка скорости ±8 % (незаметно глазу), без рывков;
* < 0.02 с — обычная скорость.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

from vmodel import Project

PAUSE_EPS = 0.05              # позиция больше этого при остановке = пауза в середине трека


@dataclass
class Decision:
    scene_id: Optional[str]   # какую сцену вывести в эфир (None — не трогать)
    black: Optional[bool]     # затемнение (None — не трогать)
    media_pos: float          # позиция для видео, идущих за треком (с учётом сдвига)
    playing: bool
    song_id: str


def decide(project: Project, connected: bool, song_id: str, pos: float, playing: bool) -> Decision:
    media_pos = pos - project.video_delay_ms / 1000.0
    if not connected:
        return Decision(None, None, media_pos, False, "")
    if not project.follow_daw:
        return Decision(None, None, media_pos, playing, song_id)
    binding = project.bindings.get(song_id) if song_id else None
    has_scene = binding is not None and (binding.scene_id or binding.cues)
    if not has_scene:
        if project.unbound_action == "standby" and project.standby_scene:
            return Decision(project.standby_scene, False, media_pos, playing, song_id)
        return Decision(None, None, media_pos, playing, song_id)
    started = playing or pos > PAUSE_EPS
    scene = binding.scene_at(max(0.0, pos))
    if started:
        return Decision(scene or None, False, media_pos, playing, song_id)
    # трек выбран, стоит в начале
    if binding.on_stop == "standby" and project.standby_scene:
        return Decision(project.standby_scene, False, media_pos, False, song_id)
    if binding.on_stop == "black":
        return Decision(None, True, media_pos, False, song_id)
    return Decision(scene or None, False, media_pos, False, song_id)


class PlayerAdapter:
    """Интерфейс плеера для VideoSync (в программе — QMediaPlayer, в тестах — модель)."""

    def position(self) -> float: ...
    def duration(self) -> float: ...
    def is_playing(self) -> bool: ...
    def play(self) -> None: ...
    def pause(self) -> None: ...
    def seek(self, t: float) -> None: ...
    def set_rate(self, r: float) -> None: ...


class VideoSync:
    HARD = 0.30          # сек: больше — перескок
    SOFT = 0.02          # сек: меньше — всё хорошо
    MAX_RATE = 0.08      # ±8 % скорости при подстройке
    GAIN = 0.8           # насколько агрессивно догонять (доля расхождения за секунду)
    SEEK_LEAD = 0.06     # сек: прыгаем чуть вперёд — пока плеер «раскачается», трек уйдёт дальше
    SEEK_HOLDOFF = 0.8   # сек: после перескока даём плееру время, не дёргаем снова

    def __init__(self, player: PlayerAdapter, clock=time.monotonic):
        self.p = player
        self.clock = clock
        self.rate = 1.0
        self.last_seek = -10.0
        self.drift = 0.0

    def _seek(self, t: float) -> None:
        self.p.seek(max(0.0, t))
        self.last_seek = self.clock()

    def _set_rate(self, r: float) -> None:
        if abs(r - self.rate) > 0.004:
            self.rate = r
            self.p.set_rate(r)

    def update(self, target: float, playing: bool, loop: bool = False) -> str:
        """Вызывать ~30 раз в секунду. Возвращает, что сделано (для индикации/тестов)."""
        dur = self.p.duration()
        if loop and dur > 0.5:
            target %= dur
        now = self.clock()
        if target < 0:                                  # трек ещё не дошёл до начала видео
            if self.p.is_playing():
                self.p.pause()
            if self.p.position() > 0.05 and now - self.last_seek > 0.3:
                self._seek(0.0)
            self._set_rate(1.0)
            return "wait"
        if dur > 0 and target >= dur and not loop:      # видео кончилось раньше трека — стоп-кадр
            if self.p.is_playing():
                self.p.pause()
            return "end"
        cur = self.p.position()
        drift = cur - target
        self.drift = drift
        if not playing:
            if self.p.is_playing():
                self.p.pause()
            self._set_rate(1.0)
            if abs(drift) > 0.04 and now - self.last_seek > 0.2:
                self._seek(target)                      # пауза/перемотка на стопе — показать нужный кадр
                return "seek"
            return "paused"
        if not self.p.is_playing():
            self._seek(target + self.SEEK_LEAD)
            self.p.play()
            self._set_rate(1.0)
            return "start"
        if abs(drift) > self.HARD:
            if now - self.last_seek > self.SEEK_HOLDOFF:
                self._seek(target + self.SEEK_LEAD)
                self._set_rate(1.0)
                return "seek"
            return "settle"
        if abs(drift) > self.SOFT:
            r = 1.0 - max(-self.MAX_RATE, min(self.MAX_RATE, drift * self.GAIN))
            self._set_rate(r)
            return "adjust"
        self._set_rate(1.0)
        return "ok"
