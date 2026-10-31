"""
schedule.py — Расписание: видео и сцены включаются сами в заданное время
(например, 18:45 — заставка, 19:00 — видео-открытие, 19:05 — сцена «Ведущий»).

Без Qt — логика проверяется тестами. Главное окно раз в кадр зовёт Scheduler.due(),
получает пункты, время которых наступило, и выводит их в эфир.

Правила
-------
* Пункт срабатывает, когда часы компьютера ПЕРЕШЛИ его время (между прошлой и
  текущей проверкой) — ровно один раз в день.
* Программу открыли позже времени пункта: опоздание до GRACE секунд (2 мин) —
  пункт ещё выполнится; больше — пропускается (не включать «Открытие» в середине концерта).
* Дни: каждый день / выбранные дни недели / одна дата.
* Ночь: время 00:10 при проверке в 23:59 → 00:00 тоже переходит (смена суток учтена).
"""
from __future__ import annotations

import datetime as dt
from dataclasses import dataclass, field
from typing import Optional

from vmodel import _from_dict, new_id

GRACE = 120.0                  # сек: насколько можно опоздать (программа была закрыта / зависла)
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]
AFTER_ACTIONS = {"keep": "Оставить последний кадр", "standby": "Показать заставку",
                 "black": "Затемнить", "scene": "Перейти на сцену…"}


@dataclass
class ScheduleItem:
    time: str = "19:00:00"            # ЧЧ:ММ:СС
    scene_id: str = ""                # какую сцену вывести в эфир
    name: str = ""                    # подпись в списке (обычно имя видео или сцены)
    id: str = field(default_factory=new_id)
    enabled: bool = True
    mode: str = "daily"               # daily — каждый день, weekdays — по дням, date — один раз
    weekdays: list = field(default_factory=lambda: [0, 1, 2, 3, 4, 5, 6])   # 0 = понедельник
    date: str = ""                    # ГГГГ-ММ-ДД для mode="date"
    restart: bool = True              # видео в сцене — с начала
    after: str = "keep"               # когда видео сцены закончилось
    after_scene: str = ""
    fired: str = ""                   # дата последнего срабатывания (чтобы не повторить)

    @classmethod
    def from_dict(cls, d: dict) -> "ScheduleItem":
        it = _from_dict(cls, d)
        it.weekdays = [int(x) for x in (it.weekdays or [])]
        return it

    # ── время ──────────────────────────────────────────────────────────────
    def clock(self) -> dt.time:
        return parse_time(self.time) or dt.time(0, 0)

    def runs_on(self, day: dt.date) -> bool:
        if self.mode == "date":
            return self.date == day.isoformat()
        if self.mode == "weekdays":
            return day.weekday() in self.weekdays
        return True

    def moment(self, day: dt.date) -> dt.datetime:
        return dt.datetime.combine(day, self.clock())

    def next_run(self, now: dt.datetime) -> Optional[dt.datetime]:
        """Ближайший будущий запуск (для «Следующее: … через …»)."""
        if not self.enabled:
            return None
        for k in range(0, 8 if self.mode != "date" else 400):
            day = now.date() + dt.timedelta(days=k)
            m = self.moment(day)
            if m > now and self.runs_on(day) and self.fired != day.isoformat():
                return m
            if self.mode == "date" and day.isoformat() > self.date:
                return None
        return None

    def days_text(self) -> str:
        if self.mode == "date":
            try:
                return dt.date.fromisoformat(self.date).strftime("%d.%m.%Y")
            except ValueError:
                return "дата?"
        if self.mode == "weekdays":
            days = sorted(set(self.weekdays))
            if days == [0, 1, 2, 3, 4]:
                return "будни"
            if days == [5, 6]:
                return "выходные"
            if len(days) == 7:
                return "каждый день"
            return ", ".join(WEEKDAYS[d] for d in days) or "никогда"
        return "каждый день"


def parse_time(text: str) -> Optional[dt.time]:
    """«19:00», «19:00:30», «7:5» → time; ерунда → None."""
    try:
        parts = [int(p) for p in str(text).strip().split(":")]
    except ValueError:
        return None
    if not 2 <= len(parts) <= 3:
        return None
    h, m, s = (parts + [0])[:3]
    if not (0 <= h < 24 and 0 <= m < 60 and 0 <= s < 60):
        return None
    return dt.time(h, m, s)


def fmt_left(seconds: float) -> str:
    s = int(max(0, seconds))
    if s >= 86400:
        return f"{s // 86400} дн {s % 86400 // 3600} ч"
    return f"{s // 3600:02d}:{s // 60 % 60:02d}:{s % 60:02d}"


class Scheduler:
    def __init__(self) -> None:
        self.last: Optional[dt.datetime] = None

    def due(self, items: list, now: dt.datetime) -> list:
        """Пункты, время которых наступило с прошлой проверки (по порядку времени).
        Помечает их выполненными на сегодня."""
        prev = self.last
        self.last = now
        if prev is None:                          # первый вызов после запуска: «опоздавшие» в пределах GRACE
            prev = now - dt.timedelta(seconds=GRACE)
        elif now < prev or (now - prev).total_seconds() > GRACE:
            # часы перевели назад или программа «спала» дольше GRACE — пропущенное не наверстываем,
            # кроме недавнего (в пределах GRACE)
            prev = now - dt.timedelta(seconds=GRACE) if now >= prev else now
        hits = []
        for it in items:
            if not it.enabled:
                continue
            for day in {prev.date(), now.date()}:
                m = it.moment(day)
                if prev < m <= now and it.runs_on(day) and it.fired != day.isoformat():
                    it.fired = day.isoformat()
                    hits.append((m, it))
        hits.sort(key=lambda x: x[0])
        return [it for _m, it in hits]

    @staticmethod
    def upcoming(items: list, now: dt.datetime) -> Optional[tuple[dt.datetime, "ScheduleItem"]]:
        best = None
        for it in items:
            m = it.next_run(now)
            if m is not None and (best is None or m < best[0]):
                best = (m, it)
        return best
