"""Логика таймлайна песни: срабатывание кьюшек и восстановление состояния при перемотке.

Не зависит от Qt: позицию ему сообщает плеер (своё аудио или Concert DAW).
"""

import threading


class TimelineLogic:
    JUMP_BACK = 0.15   # откат назад больше этого = перемотка
    JUMP_FWD = 1.0     # скачок вперёд больше этого = перемотка

    def __init__(self, show, engine):
        self.show = show
        self.engine = engine
        self.song = None
        self._last = None
        self.lock = threading.Lock()

    def cues(self):
        s = self.show.songs.get(self.song)
        return s["cues"] if s else []

    def set_song(self, name):
        with self.lock:
            self.song = name if name in self.show.songs else None
            self._last = None

    def reset(self):
        with self.lock:
            self._last = None

    def fire(self, cue):
        e = self.engine
        a, target = cue.get("action"), cue.get("target")
        if a == "scene":
            e.go_scene(target, fade=cue.get("fade"))
        elif a == "chase_start":
            e.start_chase(target)
        elif a == "chase_stop":
            e.stop_chase()
        elif a == "effect_on":
            e.start_effect(target)
        elif a == "effect_off":
            e.stop_effect(target)
        elif a == "release":
            e.release(cue.get("fade") or 1.0)
            e.stop_all_effects()
        elif a == "blackout_on":
            e.blackout = True
        elif a == "blackout_off":
            e.blackout = False

    def apply_state_at(self, pos):
        """Выставить свет так, как он был бы в момент pos, если бы песня шла с начала."""
        scene = None
        chase = None
        effects = set()
        released = False
        blackout = False
        for c in self.cues():
            if c["t"] > pos + 1e-6:
                break
            a = c["action"]
            if a == "scene":
                scene, released = c, False
            elif a == "release":
                scene, chase, released = None, None, True
                effects.clear()
            elif a == "chase_start":
                chase = c["target"]
            elif a == "chase_stop":
                chase = None
            elif a == "effect_on":
                effects.add(c["target"])
            elif a == "effect_off":
                effects.discard(c["target"])
            elif a == "blackout_on":
                blackout = True
            elif a == "blackout_off":
                blackout = False
        e = self.engine
        e.stop_all_effects()
        for name in effects:
            e.start_effect(name)
        e.blackout = blackout
        if chase:
            e.start_chase(chase)
        else:
            e.stop_chase()
            if scene:
                e.go_scene(scene["target"], fade=0)
            elif released:
                e.release(0)

    def update(self, pos, playing):
        """Вызывать регулярно (каждые ~20 мс). Возвращает список сработавших кьюшек."""
        with self.lock:
            if not self.song:
                self._last = pos
                return []
            if not playing:
                self._last = pos
                return []
            fired = []
            if self._last is None or pos < self._last - self.JUMP_BACK or pos > self._last + self.JUMP_FWD:
                self.apply_state_at(pos)
                self._last = pos
                return fired
            for c in self.cues():
                if self._last < c["t"] <= pos:
                    self.fire(c)
                    fired.append(c)
            # небольшой откат назад (дрожание синхры) не должен повторно запускать кьюшки
            self._last = max(pos, self._last)
            return fired


def match_daw_song(songs, song_id, song_name):
    """Песня шоу для трека DAW: сначала по привязке (daw_id), потом по названию."""
    if song_id:
        for name, s in songs.items():
            if s.get("daw_id") == song_id:
                return name
    if song_name in songs:
        return song_name
    low = (song_name or "").strip().lower()
    for name in songs:
        if name.strip().lower() == low:
            return name
    return None
