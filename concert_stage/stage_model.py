"""
stage_model.py — Настройки и логика табло Concert Stage (без Qt — проверяется тестами).

* StageSettings / SettingsStore — настройки в %APPDATA%\\ConcertStage\\settings.json.
* Что показывать: текущий трек, сколько осталось, очередь, текущий маркер, секундомер.
* RelayMirror — каналы пульта, которые собирает и присылает Concert DAW. Табло к пульту
  не подключается вообще: только Concert DAW держит связь с X32.
"""
from __future__ import annotations

import json
import math
import os
import sys
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Optional

from x32_osc import ALL_KEYS, ConsoleMirror, default_name  # noqa: F401


def app_data_dir() -> Path:
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        root = Path(os.environ["APPDATA"])
    else:
        root = Path.home() / ".config"
    return root / "ConcertStage"


# ══════════════════════════════════════════════════════════════════════════════
# Настройки
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class StageSettings:
    # Concert DAW
    daw_ip: str = ""                 # пусто — искать в сети автоматически
    daw_port: int = 47810
    daw_name: str = ""               # имя найденного компьютера с Concert DAW (чтобы найти снова)
    # пульт X32 (только смотреть)
    x32_on: bool = True
    x32_ip: str = ""                 # пусто — искать в сети автоматически
    channels: list = field(default_factory=list)   # ["ch/01", "ch/05", "main/st"]; пусто — авто
    # табло
    show_clock: bool = True
    show_setlist: bool = True
    show_channels: bool = True
    show_timer: bool = True
    show_marker: bool = True
    warn_seconds: int = 30           # жёлтым «осталось» — когда меньше стольких секунд
    alert_seconds: int = 10          # красным и мигает
    font_scale: float = 1.0
    orientation: str = "auto"        # "auto" (по экрану) / "landscape" / "portrait" (вертикальный монитор)
    start_fullscreen: bool = False
    screen: int = -1                 # на каком экране на весь экран (-1 — где окно)
    # обновления
    update_auto: bool = True
    update_checked: float = 0.0

    @classmethod
    def from_dict(cls, d: dict) -> "StageSettings":
        known = {f.name: f for f in fields(cls)}
        s = cls()
        for k, v in (d or {}).items():
            if k not in known:
                continue
            default = getattr(s, k)
            try:
                if isinstance(default, bool):
                    v = bool(v)
                elif isinstance(default, int):
                    v = int(v)
                elif isinstance(default, float):
                    v = float(v)
                elif isinstance(default, list):
                    v = [str(x) for x in v if str(x) in ALL_KEYS]
                else:
                    v = str(v)
            except (TypeError, ValueError):
                continue
            setattr(s, k, v)
        s.font_scale = min(3.0, max(0.5, s.font_scale))
        if s.orientation not in ("auto", "landscape", "portrait"):
            s.orientation = "auto"
        s.alert_seconds = max(0, s.alert_seconds)
        s.warn_seconds = max(s.alert_seconds, s.warn_seconds)
        return s


class SettingsStore:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else app_data_dir() / "settings.json"

    def load(self) -> StageSettings:
        for p in (self.path, self.path.with_suffix(".json.bak")):
            try:
                return StageSettings.from_dict(json.loads(p.read_text("utf-8")))
            except (OSError, ValueError):
                continue
        return StageSettings()

    def save(self, s: StageSettings) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(asdict(s), ensure_ascii=False, indent=1), "utf-8")
        if self.path.exists():
            try:
                os.replace(self.path, self.path.with_suffix(".json.bak"))
            except OSError:
                pass
        os.replace(tmp, self.path)


# ══════════════════════════════════════════════════════════════════════════════
# Время
# ══════════════════════════════════════════════════════════════════════════════
def fmt_time(seconds: float, hours: bool = False) -> str:
    """02:31 / 1:02:31. Округление вниз, как на часах."""
    s = int(math.floor(max(0.0, seconds) + 1e-6))
    h, m, sec = s // 3600, (s // 60) % 60, s % 60
    if h or hours:
        return f"{h}:{m:02d}:{sec:02d}"
    return f"{m:02d}:{sec:02d}"


def fmt_remaining(seconds: float) -> str:
    """Остаток «−02:31»: округление вверх (на 0.4 с до конца ещё показано −00:01)."""
    s = int(math.ceil(max(0.0, seconds) - 1e-6))
    return "−" + fmt_time(s)


def remaining_level(remaining: float, warn: int, alert: int, playing: bool) -> str:
    """"normal" / "warn" (жёлтым) / "alert" (красным, мигает) — только пока трек играет."""
    if not playing:
        return "normal"
    if remaining <= alert:
        return "alert"
    if remaining <= warn:
        return "warn"
    return "normal"


# ══════════════════════════════════════════════════════════════════════════════
# Плейлист и маркеры
# ══════════════════════════════════════════════════════════════════════════════
@dataclass
class Setlist:
    name: str = ""
    songs: list = field(default_factory=list)      # [{"id","name","duration","markers"}]
    index: int = -1                                # текущий трек в songs (-1 — не найден)

    @property
    def current(self) -> Optional[dict]:
        return self.songs[self.index] if 0 <= self.index < len(self.songs) else None

    def left_after_current(self) -> float:
        """Сколько идут треки ПОСЛЕ текущего (до конца плейлиста), сек."""
        if self.index < 0:
            return 0.0
        return sum(float(s.get("duration", 0.0) or 0.0) for s in self.songs[self.index + 1:])


def find_setlist(playlists: list, current_id: str, playlist_name: str = "") -> Setlist:
    """Плейлист, в котором текущий трек (сначала — с тем названием, что прислал Concert DAW)."""
    candidates = [pl for pl in playlists if pl.get("name") == playlist_name] + list(playlists)
    for pl in candidates:
        songs = pl.get("songs") or []
        for i, sg in enumerate(songs):
            if sg.get("id") == current_id:
                return Setlist(pl.get("name", ""), songs, i)
    for pl in candidates:                        # трека нет (не выбран) — показываем плейлист из DAW
        if pl.get("name") == playlist_name:
            return Setlist(pl.get("name", ""), pl.get("songs") or [], -1)
    return Setlist()


def current_marker(markers: list, pos: float) -> str:
    """Название маркера, в котором сейчас позиция (Куплет, Припев…); "" — до первого."""
    name = ""
    for m in sorted(markers or [], key=lambda x: float(x.get("t", 0.0))):
        if float(m.get("t", 0.0)) <= pos + 0.05:
            name = str(m.get("name", ""))
        else:
            break
    return name


def next_marker(markers: list, pos: float) -> Optional[tuple[str, float]]:
    """(название, через сколько секунд) — следующий маркер."""
    for m in sorted(markers or [], key=lambda x: float(x.get("t", 0.0))):
        t = float(m.get("t", 0.0))
        if t > pos + 0.05:
            return str(m.get("name", "")), t - pos
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Пульт X32 — только чтение
# ══════════════════════════════════════════════════════════════════════════════
class RelayMirror:
    """Каналы пульта, которые прислал Concert DAW (табло к пульту само не подключается).
    Интерфейс как у x32_osc.ConsoleMirror: name(), levels(), on, fader, names."""

    STALE_S = 2.5

    def __init__(self) -> None:
        self.names: dict = {}
        self.on: dict = {}
        self.fader: dict = {}
        self._levels: dict = {}
        self.auto: list = []
        self.order: list = []          # каналы в том порядке, как прислал DAW
        self.online = False            # Concert DAW на связи с пультом
        self.ip = ""
        self.faders_on = True
        self.at = 0.0                  # время последнего сообщения (местное), 0 — ещё не было

    def update(self, msg: dict, now: float) -> None:
        self.online = bool(msg.get("online"))
        self.ip = str(msg.get("ip", ""))
        self.faders_on = bool(msg.get("faders_on", True))
        self.auto = [str(k) for k in msg.get("auto") or []]
        if isinstance(msg.get("names"), dict):
            self.names.update({str(k): str(v) for k, v in msg["names"].items()})
        self.order = []
        for item in msg.get("ch") or []:
            k = str(item.get("k", ""))
            if not k:
                continue
            self.order.append(k)
            if item.get("n"):
                self.names[k] = str(item["n"])
            if "on" in item:
                self.on[k] = int(item["on"])
            else:
                self.on.pop(k, None)
            if "f" in item:
                self.fader[k] = float(item["f"])
            self._levels[k] = [float(v) for v in item.get("lv") or []]
        self.at = now

    def fresh(self, now: float) -> bool:
        return self.at > 0 and now - self.at < self.STALE_S

    def name(self, key: str) -> str:
        return self.names.get(key) or default_name(key)

    def levels(self, key: str) -> list:
        return list(self._levels.get(key, [])) if self.online else []

    def keys_for(self, chosen: list) -> list:
        """Какие плитки показывать: выбранные в настройках или «авто» от DAW."""
        return list(chosen) if chosen else list(self.auto or self.order)


def console_caption(connected: bool, relay: "RelayMirror", n: int, now: float) -> tuple[str, bool]:
    """(подпись над каналами, есть ли живые данные)."""
    if not connected:
        return "ПУЛЬТ X32 · нет связи с Concert DAW", False
    if not relay.fresh(now):
        return "ПУЛЬТ X32 · Concert DAW не присылает данные пульта (обновите Concert DAW до 1.4)", False
    if not relay.ip:
        return "ПУЛЬТ X32 · Concert DAW не подключён к пульту (в DAW: зона «Пульт X32» → Подключить)", False
    if not relay.faders_on:
        return "ПУЛЬТ X32 · в Concert DAW выключена синхронизация с пультом (Настройки → Пульт X32)", False
    if not relay.online:
        return f"ПУЛЬТ X32 · у Concert DAW нет связи с пультом ({relay.ip})", False
    return f"ПУЛЬТ X32 (через Concert DAW) · {n} кан.", True


def level_db(lin: float) -> float:
    return 20 * math.log10(lin) if lin > 1e-6 else -120.0


def meter_fraction(lin: float, floor_db: float = -60.0) -> float:
    """Длина полоски индикатора 0…1 (−60…0 dBFS)."""
    db = level_db(lin)
    return min(1.0, max(0.0, (db - floor_db) / -floor_db))


def is_muted(mirror, key: str) -> Optional[bool]:
    """True — канал выключен (MUTE), False — включён, None — пульт ещё не ответил."""
    on = mirror.on.get(key)
    return None if on is None else on == 0
