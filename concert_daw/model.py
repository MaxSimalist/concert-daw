"""
model.py — Модель данных плейлистов (без зависимостей от GUI и аудио).

Иерархия:
    AppConfig
      ├── playlists: [Playlist]              «Тур 2026 - Москва», «Акустический сет»
      │     └── songs: [Song]                мультитрек = папка с WAV одной песни
      │           ├── edit: SongEdit         обрезка, темп, тон, фейды (общие для всех дорожек)
      │           └── tracks: [TrackConfig]  Клик, Гайд, Бас, ... + выход X32, гейн, VST
      ├── routing_defaults: {имя дорожки: выход}   — кэш роутинга между песнями
      └── настройки: аудио, интерфейс, плейлист

Всё хранится в одном JSON. Запись атомарная (tmp-файл + os.replace) и с
резервной копией .bak — внезапное отключение питания на площадке не убьёт конфиг.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sys
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Iterable, Optional

try:
    import soundfile as sf
except ImportError:  # модель можно импортировать и без soundfile
    sf = None

AUDIO_EXTENSIONS = {".wav", ".wave", ".aif", ".aiff", ".flac", ".mp3", ".ogg"}
MAX_OUTPUTS = 32  # X-USB / X-LIVE: 32 канала USB-аудио в каждую сторону

# Подсказки авто-роутинга по имени файла (если дорожку с таким именем
# ещё ни разу не роутили вручную). Регулярка → первый выход (1-based).
KEYWORD_HINTS: list[tuple[str, int]] = [
    (r"click|клик|metronom|метроном", 1),
    (r"guide|гайд|cue|подсказ|count", 2),
    (r"(playback|плейбек|pb|minus|минус|track).*(\bl\b|left|лев|_l$)", 3),
    (r"(playback|плейбек|pb|minus|минус|track).*(\br\b|right|прав|_r$)", 4),
    (r"bass|бас", 5),
    (r"guitar|гитар|gtr", 6),
]
# Клик и голосовые подсказки при смене тональности НЕ транспонируются
CLICK_LIKE = r"click|клик|metronom|метроном|guide|гайд|cue|подсказ|count"


def normalize_track_name(name: str) -> str:
    """«  Клик  » и «клик» — одна и та же дорожка для кэша роутинга."""
    return re.sub(r"\s+", " ", name.strip().lower())


def is_click_like(name: str) -> bool:
    return re.search(CLICK_LIKE, normalize_track_name(name)) is not None


def is_audio_file(path: str | Path) -> bool:
    p = Path(path)
    return p.is_file() and p.suffix.lower() in AUDIO_EXTENSIONS


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def _from_dict(cls, d: dict):
    """Создать dataclass из словаря, молча пропуская неизвестные ключи
    (конфиг от новой/старой версии программы не должен ломать запуск)."""
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in d.items() if k in names})


# ──────────────────────────────────────────────────────────────────────────────
# Дорожка / правка песни / песня / плейлист
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class PluginState:
    """Инсерт-плагин дорожки: путь к VST3 + сохранённое состояние (base64)."""
    path: str
    state_b64: str = ""
    bypass: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> "PluginState":
        return _from_dict(cls, d)


@dataclass
class TrackConfig:
    file: str                 # абсолютный путь к аудиофайлу
    name: str                 # отображаемое имя (по умолчанию — имя файла)
    output: int = 1           # ПЕРВЫЙ физический выход X32 (1..32); стерео занимает N и N+1
    channels: int = 1         # число каналов в файле (определяется при сканировании)
    gain_db: float = 0.0
    mute: bool = False
    solo: bool = False
    plugin: Optional[PluginState] = None
    transpose: Optional[bool] = None  # менять ли тон; None = авто (клик/гайд — нет, остальное — да)
    clip_start: float = 0.0   # обрезка дорожки в окне дорожек (сек исходника): до — тишина
    clip_end: float = 0.0     # после — тишина; 0 = до конца. Длина песни и синхронность не меняются
    fx: dict = field(default_factory=dict)  # встроенная обработка: {"eq": {...}, "comp": {...}} (см. dsp.py)

    @classmethod
    def from_dict(cls, d: dict) -> "TrackConfig":
        d = dict(d)
        plug = d.pop("plugin", None)
        tc = _from_dict(cls, d)
        tc.name = tc.name or Path(tc.file).stem
        tc.plugin = PluginState.from_dict(plug) if plug else None
        return tc

    @property
    def transposes(self) -> bool:
        return self.transpose if self.transpose is not None else not is_click_like(self.name)


@dataclass
class SongEdit:
    """Неразрушающая правка песни. Применяется ко ВСЕМ дорожкам одинаково
    (кроме тона — см. TrackConfig.transpose), поэтому синхронность сохраняется.
    Исходные файлы не меняются: результат считается при загрузке и кэшируется."""
    start: float = 0.0        # сек: точка входа в исходнике (обрезка начала)
    end: float = 0.0          # сек: точка выхода в исходнике, 0 = до конца
    speed: float = 1.0        # 1.0 = оригинал, 1.05 = на 5 % быстрее (тон не меняется)
    pitch: float = 0.0        # полутоны (+2 = на тон выше), темп не меняется
    fade_in: float = 0.0      # мс
    fade_out: float = 0.0     # мс
    original_bpm: float = 0.0  # для подсказки «новый темп = …»

    @classmethod
    def from_dict(cls, d: dict) -> "SongEdit":
        return _from_dict(cls, d or {})

    @property
    def is_identity(self) -> bool:
        return self == SongEdit(original_bpm=self.original_bpm)

    def output_duration(self, source_duration: float) -> float:
        end = self.end if 0 < self.end <= source_duration else source_duration
        return max(0.0, end - self.start) / self.speed

    def summary(self) -> str:
        parts = []
        if self.speed != 1.0:
            parts.append(f"темп {self.speed * 100:.0f}%")
        if self.pitch:
            parts.append(f"тон {self.pitch:+g}")
        if self.start or self.end:
            parts.append("обрезка")
        return ", ".join(parts)


@dataclass
class Song:
    """Мультитрек: логический контейнер (папка) с дорожками одной песни."""
    name: str
    folder: str
    tracks: list[TrackConfig] = field(default_factory=list)
    id: str = field(default_factory=new_id)
    edit: SongEdit = field(default_factory=SongEdit)
    duration: float = 0.0     # длина исходника в секундах (самая длинная дорожка)
    key: str = ""             # тональность исходника: "Am", "F#"; "" — ещё не определена, "?" — не удалось
    key_manual: bool = False  # указана вручную — не перезаписывать автоопределением
    end_action: str = "default"  # в конце песни: default (как в настройках) | stop | next
    end_gap: float = 0.0      # для "next": пауза перед следующей песней, сек
    bpm: float = 0.0          # темп исходника (автоопределение); 0 — ещё не определён, -1 — не удалось
    beat_offset: float = 0.0  # время первой доли в исходнике, сек — для привязки метронома
    markers: list = field(default_factory=list)  # [{"t": сек исходника, "name": "Припев"}]
    x32_scene: int = -1       # сцена пульта при выборе/старте трека; -1 — не менять
    x32_snippet: int = -1     # сниппет пульта; -1 — не менять
    lufs: float = 0.0         # измеренная громкость музыки (без клика); 0 — не измерена, -99 — не удалось
    peak: float = 0.0         # пиковый уровень микса музыки (для запаса при нормализации)
    norm_exclude: bool = False  # не нормализовать этот трек
    silence_lead: float = -1.0   # тишина в начале исходника, сек; -1 — ещё не проверено
    silence_trail: float = -1.0  # тишина в конце исходника, сек
    silence_ignore: bool = False  # «не предлагать обрезать тишину» для этого трека

    @classmethod
    def from_dict(cls, d: dict) -> "Song":
        return cls(name=d["name"], folder=d.get("folder", ""),
                   tracks=[TrackConfig.from_dict(t) for t in d.get("tracks", [])],
                   id=d.get("id") or new_id(),
                   edit=SongEdit.from_dict(d.get("edit") or {}),
                   duration=float(d.get("duration", 0.0)),
                   key=str(d.get("key", "")),
                   key_manual=bool(d.get("key_manual", False)),
                   end_action=str(d.get("end_action", "default")),
                   end_gap=float(d.get("end_gap", 0.0)),
                   bpm=float(d.get("bpm", 0.0)),
                   beat_offset=float(d.get("beat_offset", 0.0)),
                   markers=[dict(m) for m in d.get("markers", []) if "t" in m],
                   x32_scene=int(d.get("x32_scene", -1)),
                   x32_snippet=int(d.get("x32_snippet", -1)),
                   lufs=float(d.get("lufs", 0.0)),
                   peak=float(d.get("peak", 0.0)),
                   norm_exclude=bool(d.get("norm_exclude", False)),
                   silence_lead=float(d.get("silence_lead", -1.0)),
                   silence_trail=float(d.get("silence_trail", -1.0)),
                   silence_ignore=bool(d.get("silence_ignore", False)))

    def silence_offer(self) -> tuple[float, float]:
        """Сколько тишины можно срезать сейчас: (в начале, в конце), 0 — нечего.
        Предлагаем только там, где трек ещё не обрезан вручную."""
        if self.silence_ignore or self.silence_lead < 0:
            return 0.0, 0.0
        lead = self.silence_lead if self.edit.start == 0 and self.silence_lead >= 0.5 else 0.0
        trail = self.silence_trail if self.edit.end == 0 and self.silence_trail >= 1.0 else 0.0
        return lead, trail

    def trim_silence(self, lead: float, trail: float) -> None:
        """Обрезать тишину (неразрушающе — через правку трека) + короткие фейды от щелчков."""
        if lead > 0:
            self.edit.start = round(lead, 3)
            self.edit.fade_in = max(self.edit.fade_in, 10.0)
        if trail > 0 and self.duration > trail:
            self.edit.end = round(self.duration - trail, 3)
            self.edit.fade_out = max(self.edit.fade_out, 50.0)

    def out_time(self, src: float) -> float:
        """Время исходника → время воспроизведения (с учётом обрезки и скорости)."""
        return (src - self.edit.start) / self.edit.speed

    def src_time(self, out: float) -> float:
        return self.edit.start + out * self.edit.speed

    def markers_out(self) -> list[tuple[float, str]]:
        """Маркеры во времени воспроизведения, по порядку, только внутри трека."""
        dur = self.output_duration or float("inf")
        res = [(self.out_time(m["t"]), m.get("name", "")) for m in self.markers]
        return sorted((t, n) for t, n in res if 0 <= t <= dur)

    @property
    def source_bpm(self) -> float:
        """Темп исходника: указанный вручную в редакторе важнее автоопределения."""
        if self.edit.original_bpm > 0:
            return self.edit.original_bpm
        return self.bpm if self.bpm > 0 else 0.0

    @property
    def play_bpm(self) -> float:
        """Темп, который реально звучит (с учётом скорости из редактора)."""
        return self.source_bpm * self.edit.speed

    @property
    def output_duration(self) -> float:
        return self.edit.output_duration(self.duration)

    @classmethod
    def from_folder(cls, folder: str, routing_defaults: dict[str, int]) -> "Song":
        """Песня из папки: каждый аудиофайл → дорожка."""
        folder_path = Path(folder)
        files = sorted(p for p in folder_path.iterdir() if is_audio_file(p))
        if not files:
            raise ValueError(f"В папке нет аудиофайлов: {folder}")
        return cls.from_files(files, folder_path.name, routing_defaults)

    @classmethod
    def from_files(cls, files: Iterable[str | Path], name: str, routing_defaults: dict[str, int]) -> "Song":
        files = [Path(f) for f in files]
        song = cls(name=name, folder=str(files[0].parent.resolve()) if files else "")
        song.add_files(files, routing_defaults)
        if not song.tracks:
            raise ValueError("Нет подходящих аудиофайлов")
        return song

    def add_files(self, files: Iterable[str | Path], routing_defaults: dict[str, int]) -> int:
        """Добавить дорожки (перетаскивание в микшер). Выходы подбираются среди
        свободных: кэш роутинга → ключевые слова → первый свободный."""
        existing = {os.path.normcase(t.file) for t in self.tracks}
        probes = []
        for f in files:
            p = Path(f).resolve()
            if is_audio_file(p) and os.path.normcase(str(p)) not in existing:
                ch, dur = _probe(p)
                probes.append((p, ch))
                self.duration = max(self.duration, dur)
        used: set[int] = set()
        for t in self.tracks:
            used.update(range(t.output, t.output + max(1, t.channels)))
        assigned: dict[Path, int] = {}
        # 1-й проход: дорожки, для которых роутинг известен (кэш / ключевые слова)
        for p, ch in probes:
            out = _known_output(p.stem, routing_defaults)
            if out and out + ch - 1 <= MAX_OUTPUTS and not used.intersection(range(out, out + ch)):
                assigned[p] = out
                used.update(range(out, out + ch))
        # 2-й проход: остальным — первые свободные выходы подряд
        for p, ch in probes:
            if p not in assigned:
                out = _first_free(used, ch)
                assigned[p] = out
                used.update(range(out, out + ch))
        for p, ch in probes:
            self.tracks.append(TrackConfig(file=str(p), name=p.stem, output=assigned[p], channels=ch))
        self.tracks.sort(key=lambda t: t.output)  # стрипы в микшере идут по номерам выходов
        return len(probes)


@dataclass
class Playlist:
    name: str
    songs: list[Song] = field(default_factory=list)
    id: str = field(default_factory=new_id)

    @classmethod
    def from_dict(cls, d: dict) -> "Playlist":
        return cls(name=d["name"], songs=[Song.from_dict(s) for s in d.get("songs", [])],
                   id=d.get("id") or new_id())


@dataclass
class AppConfig:
    playlists: list[Playlist] = field(default_factory=list)
    routing_defaults: dict[str, int] = field(default_factory=dict)
    # ── звук ──
    device_name: str = ""        # выбранное устройство (ищется по имени, индексы плавают)
    samplerate: int = 48000      # должна совпадать с частотой пульта X32!
    blocksize: int = 256         # размер буфера в сэмплах
    output_mode: str = "auto"    # auto | routed (выходы пульта) | stereo (сведение без пульта)
    # ── интерфейс ──
    ui_scale: float = 1.0
    start_maximized: bool = True
    # ── плейлист ──
    auto_cue: bool = True
    auto_play_next: bool = False  # автостарт следующей песни после паузы
    auto_play_delay: float = 5.0
    preload_next: bool = True
    confirm_delete: bool = True
    songs_folder: str = ""        # папка по умолчанию для диалогов «+ Песня»
    switch_while_playing: str = "play"  # выбор другой песни во время игры: play — сразу играть её, cue — встать на начало
    last_playlist_id: str = ""
    ui_splitters: dict = field(default_factory=dict)  # размеры зон окна (сохраняются)
    # ── запись с пульта ──
    rec_folder: str = ""          # пусто = «Музыка/ConcertDAW Записи»
    rec_channels: str = "1-32"    # входы карты X-USB: "1-32", "1-8,17,18"
    rec_names: dict = field(default_factory=dict)  # имена входов: {"5": "Вокал"} → Ch05 Вокал.wav
    video_sync_on: bool = True    # мастер-часы для видео-программы Concert Video (по сети)
    video_sync_port: int = 47810
    update_auto: bool = True      # проверять обновления при запуске
    update_checked: float = 0.0   # когда последний раз проверяли обновления на GitHub (time.time())
    rec_to_song: bool = True      # после записи — каналы в трек фейдерами (выход на пульт, EQ, компрессор)
    rec_chosen: bool = False      # входы ещё ни разу не выбирали → первый REC сначала спросит
    rec_format: str = "PCM_24"    # PCM_24 | PCM_16 | FLOAT
    rec_split: bool = True        # отдельный моно-файл на канал (удобно для DAW)
    rec_mode: str = "manual"      # manual — по кнопке REC; auto — с каждым PLAY, папка на песню
    rec_input_device: str = ""    # без ASIO-пульта: устройство записи (пусто = по умолчанию)
    last_recording: str = ""      # папка последней записи — для окна дорожек
    # ── метроном ──
    metro_output: int = 0         # выход пульта (1..32); 0 = как у дорожки «Клик» (или Out 1)
    metro_volume_db: float = -6.0
    metro_bpm: float = 120.0      # темп, когда у песни темп неизвестен
    metro_accent: bool = True     # акцент на первую долю (такт 4/4)
    metro_beats: int = 4          # долей в такте (для акцента и отсчёта)
    # ── отсчёт перед стартом ──
    countin_on: bool = False
    countin_bars: int = 1
    # ── нормализация громкости ──
    norm_on: bool = False
    norm_target: float = -16.0   # LUFS
    norm_max_boost: float = 6.0  # dB: тихие треки поднимаются не больше чем на столько
    # ── пульт X32 по сети (OSC) ──
    osc_on: bool = False
    osc_ip: str = ""
    osc_when: str = "cue"        # cue — при выборе трека; play — при старте
    osc_faders: bool = True      # показывать фейдеры пульта и синхронизировать их
    console_strips: list = field(default_factory=lambda: ["main/st"])  # фейдеры пульта по порядку: "main/st", "ch/05", "dca/2"…
    console_faders: list = field(default_factory=list)  # устарело (версия 8): мастер + этот список
    # ── MIDI ──
    midi_port: str = ""
    midi_map: dict = field(default_factory=dict)  # действие → "note:0:60" / "cc:0:64" / "pc:0:3"
    midi_cc_any: bool = False
    # ── темп эффектов пульта ──
    fx_tempo: dict = field(default_factory=dict)   # слот FX "1".."4" → доля: "1/4", "1/8."…
    fx_time_scale: str = "lin"                     # как пульт кодирует время задержки (уточняется само)
    fx_auto: bool = True                           # авто: все Stereo Delay в FX 1–4 под темп трека
    fx_auto_div: str = "1/4"                       # доля для авто-режима
    silence_offer: bool = True                     # предлагать обрезать тишину в начале/конце

    def remember_routing(self, track_name: str, output: int) -> None:
        """Кэш роутинга: «Клик» однажды отправили в Out 1 → в новых песнях тоже Out 1."""
        self.routing_defaults[normalize_track_name(track_name)] = int(output)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "AppConfig":
        d = dict(d)
        playlists = [Playlist.from_dict(p) for p in d.pop("playlists", [])]
        routing = {str(k): int(v) for k, v in d.pop("routing_defaults", {}).items()}
        cfg = _from_dict(cls, d)
        cfg.playlists, cfg.routing_defaults = playlists, routing
        if "console_strips" not in d:   # конфиг версии 8: мастер был всегда, остальные — в console_faders
            cfg.console_strips = ["main/st"] + [k for k in cfg.console_faders if k != "main/st"]
        if "rec_chosen" not in d and cfg.rec_channels.replace(" ", "") not in ("", "1-32"):
            cfg.rec_chosen = True        # входы уже настраивали строкой в старой версии
        cfg.rec_names = {str(k): str(v) for k, v in (cfg.rec_names or {}).items()}
        return cfg


# ──────────────────────────────────────────────────────────────────────────────
# Разбор перетащенных путей
# ──────────────────────────────────────────────────────────────────────────────
def audio_title(path: str | Path) -> str:
    """Название трека: тег «Название» (MP3 ID3, WAV/FLAC INFO), иначе имя файла
    без номера в начале («01 - Кукушка.mp3» → «Кукушка»)."""
    p = Path(path)
    try:  # mutagen (опционально) читает ID3-теги MP3 и любые другие
        import mutagen
        f = mutagen.File(str(p), easy=True)
        if f is not None and f.tags:
            title = (f.tags.get("title") or [""])[0].strip()
            if title:
                return title
    except Exception:
        pass
    if sf is not None:
        try:
            with sf.SoundFile(str(p)) as f:
                title = (getattr(f, "title", "") or "").strip()
                if title:
                    return title
        except Exception:
            pass
    stem = re.sub(r"^\s*\d{1,3}\s*[-._)]*\s*", "", p.stem).replace("_", " ").strip()
    return stem or p.stem


def songs_from_files(files: Iterable[str | Path], routing_defaults: dict[str, int]) -> list[Song]:
    """Каждый аудиофайл (WAV, MP3, …) — отдельный трек с названием песни."""
    result = []
    for f in files:
        if is_audio_file(f):
            result.append(Song.from_files([f], audio_title(f), routing_defaults))
    return result


def is_multitrack_folder(files: list[Path]) -> bool:
    """Дорожки мультитрека почти одной длины (Клик, Бас, Плейбек…), а песни альбома — разной.
    Разброс длительностей до 2 секунд → мультитрек."""
    if len(files) < 2:
        return False
    durations = [_probe(Path(f))[1] for f in files]
    return max(durations) - min(durations) <= 2.0


def songs_from_folder_auto(folder: Path, routing_defaults: dict[str, int]) -> list[Song]:
    files = sorted(f for f in folder.iterdir() if is_audio_file(f))
    if not files:
        return []
    if is_multitrack_folder(files):
        return [Song.from_folder(str(folder), routing_defaults)]
    return songs_from_files(files, routing_defaults)


def songs_from_paths(paths: Iterable[str], routing_defaults: dict[str, int]) -> list[Song]:
    """Перетащили в список треков:
    * аудиофайлы → каждый файл отдельный трек (название из тегов);
    * папка с дорожками одной длины → мультитрек; с файлами разной длины → треки по файлам;
    * папка из подпапок → то же для каждой подпапки (целый сет)."""
    songs: list[Song] = []
    for raw in paths:
        p = Path(raw)
        if p.is_dir():
            found = songs_from_folder_auto(p, routing_defaults)
            if not found:
                for sub in sorted(x for x in p.iterdir() if x.is_dir()):
                    found += songs_from_folder_auto(sub, routing_defaults)
            songs += found
        elif is_audio_file(p):
            songs += songs_from_files([p], routing_defaults)
    return songs


# ──────────────────────────────────────────────────────────────────────────────
# Вспомогательные функции роутинга
# ──────────────────────────────────────────────────────────────────────────────
def _probe(path: Path) -> tuple[int, float]:
    """(каналы, длительность в секундах)"""
    if sf is None:
        return 1, 0.0
    try:
        info = sf.info(str(path))
        return max(1, int(info.channels)), float(info.frames) / float(info.samplerate)
    except Exception:
        return 1, 0.0


def _known_output(stem: str, routing_defaults: dict[str, int]) -> Optional[int]:
    key = normalize_track_name(stem)
    if key in routing_defaults:
        return routing_defaults[key]
    for pattern, out in KEYWORD_HINTS:
        if re.search(pattern, key):
            return out
    return None


def _first_free(used: set[int], channels: int) -> int:
    for first in range(1, MAX_OUTPUTS - channels + 2):
        if not used.intersection(range(first, first + channels)):
            return first
    return 1  # все 32 заняты — пользователь переназначит вручную


# ──────────────────────────────────────────────────────────────────────────────
# Хранилище JSON
# ──────────────────────────────────────────────────────────────────────────────
def app_data_dir() -> Path:
    if sys.platform == "win32" and os.environ.get("APPDATA"):
        root = Path(os.environ["APPDATA"])
    else:
        root = Path.home() / ".config"
    return root / "ConcertDAW"


def default_config_path() -> Path:
    return app_data_dir() / "config.json"


def _json_default(obj):
    """Числа numpy (float32, int64…) из анализа звука → обычные числа Python.
    Без этого JSON отказывается их сохранять («float32 is not JSON serializable»)."""
    item = getattr(obj, "item", None)
    if callable(item):
        try:
            return item()
        except (TypeError, ValueError):
            pass
    if isinstance(obj, (set, frozenset, tuple)):
        return list(obj)
    raise TypeError(f"Object of type {type(obj).__name__} is not JSON serializable")


class ConfigStore:
    def __init__(self, path: Optional[str | Path] = None):
        self.path = Path(path) if path else default_config_path()

    def load(self) -> AppConfig:
        if self.path.exists():
            try:
                with open(self.path, encoding="utf-8") as f:
                    return AppConfig.from_dict(json.load(f))
            except Exception:
                # Битый конфиг не должен мешать запуску на концерте: откладываем его в сторону
                shutil.copy2(self.path, self.path.with_suffix(".broken.json"))
        cfg = AppConfig()
        cfg.playlists.append(Playlist(name="Новый плейлист"))
        return cfg

    def save(self, cfg: AppConfig) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(cfg.to_dict(), f, ensure_ascii=False, indent=2, default=_json_default)
            f.flush()
            os.fsync(f.fileno())
        if self.path.exists():
            shutil.copy2(self.path, self.path.with_suffix(".bak"))
        os.replace(tmp, self.path)
