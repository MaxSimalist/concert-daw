"""
audio_engine.py — Аудио-движок: ASIO Behringer X32, коллбэк, роутинг.

Архитектура
-----------
1. ОДИН OutputStream на ВСЕ выходы ASIO-драйвера пульта (у X-USB / X-LIVE — 32).
   Поток открывается один раз при старте и НЕ закрывается между песнями:
   переинициализация ASIO-драйвера на концерте = тишина, щелчок и риск зависания.
   Когда ничего не играет, коллбэк просто отдаёт нули.

2. Все дорожки песни целиком загружаются в RAM (numpy float32) ЗАРАНЕЕ,
   в фоновом потоке, пока играет предыдущая песня. В аудио-коллбэке нет
   чтения с диска, декодирования, блокировок и вызовов Qt — только срезы
   массивов, VST-обработка и сложение.

3. Синхронность: все дорожки читаются по ОДНОМУ счётчику позиции self._pos.
   Каждый блок — это срез [pos : pos+frames] из каждой дорожки, поэтому
   рассинхрон между Кликом и Плейбеком невозможен в принципе.

4. Роутинг: дорожка с выходом N (1-based) складывается в столбец N-1 буфера
   outdata. Столбец k буфера = USB-выход k+1 карты X-USB = «Card k+1» в
   роутинге пульта. Стерео-файл занимает выходы N и N+1.

Память: 5-минутная моно-дорожка 48 кГц ≈ 58 МБ. Песня из 6 дорожек ≈ 350 МБ.
В RAM одновременно держатся только текущая и следующая песня.
"""
from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

# ╔══════════════════════════════════════════════════════════════════════════╗
# ║ ВКЛЮЧЕНИЕ ASIO В SOUNDDEVICE                                             ║
# ║ Колёса sounddevice для Windows содержат две сборки PortAudio: обычную и  ║
# ║ с поддержкой ASIO. ASIO-сборка выбирается переменной окружения           ║
# ║ SD_ENABLE_ASIO, которую нужно выставить ДО первого `import sounddevice`. ║
# ║ Без неё в списке хост-API будут только MME / DirectSound / WASAPI, и     ║
# ║ драйвер X-USB ASIO просто не появится.                                    ║
# ╚══════════════════════════════════════════════════════════════════════════╝
os.environ.setdefault("SD_ENABLE_ASIO", "1")

import numpy as np
import sounddevice as sd
import soundfile as sf

from audio_edit import render_heavy_parallel, render_track
from model import Song, TrackConfig, _probe
from plugin_host import PluginSlot
from dsp import TrackFX

# Подстроки, по которым узнаём официальный ASIO-драйвер пульта.
# Реальные имена: «X-USB ASIO Driver», «X-LIVE ASIO», у некоторых сборок «Behringer X32 ASIO».
X32_DRIVER_PATTERNS = ("x-usb", "xusb", "x-live", "xlive", "x32", "behringer")

FADER_MIN_DB = -60.0      # низ фейдера = -∞
STOP_FADE_SECONDS = 0.03  # мягкий STOP без щелчка в порталах
CENTER_GAIN = np.float32(0.7071)  # моно-дорожка в стерео: по центру, -3 dB на канал
LIMIT_CEIL = 0.966        # −0.3 dBFS: выше этого на пульт ничего не уходит
LIMIT_RELEASE = 0.25      # сек: как быстро ограничитель отпускает громкость
SOFT_KNEE = 0.89          # −1 dBFS: отсюда мягкое «закругление» вместо жёсткого среза


class SafetyLimiter:
    """Защита от перегруза БЕЗ треска.

    Раньше всё громче 0 dBFS просто срезалось (np.clip) — жёсткий срез вершин волны
    слышен как хрип/треск. Теперь:
    1) ограничитель «ведёт» громкость: если в блоке пик выше потолка, громкость плавно
       (за ~1 мс) уходит вниз ровно настолько, насколько нужно, и плавно (0.25 с) возвращается;
    2) то, что всё же проскочило (самое начало удара), не срезается, а мягко закругляется.
    Пока сигнал ниже −1 dBFS, ограничитель ничего не делает (звук бит-в-бит как в файле)."""

    def __init__(self) -> None:
        self.gain = 1.0
        self.active = False            # для индикации: ограничитель сейчас работает

    def process(self, buf: np.ndarray, peak: float, sr: float) -> None:
        """buf (frames, ch) — на месте. peak — максимум |buf| (уже посчитан для индикаторов)."""
        frames = buf.shape[0]
        need = LIMIT_CEIL / peak if peak > LIMIT_CEIL else 1.0
        g0 = self.gain
        if need < g0:
            g1 = need                                     # вниз — быстро
        else:
            k = min(1.0, frames / (LIMIT_RELEASE * sr))
            g1 = g0 + (min(1.0, need) - g0) * k           # вверх — плавно
        self.active = g1 < 0.999 or g0 < 0.999
        if self.active:
            if abs(g1 - g0) > 1e-6:
                attack = max(1, min(frames, int(sr * 0.001)))
                ramp = np.full(frames, g1, dtype=np.float32)
                if need < g0:
                    ramp[:attack] = np.linspace(g0, g1, attack, dtype=np.float32)
                else:
                    ramp[:] = np.linspace(g0, g1, frames, dtype=np.float32)
                buf *= ramp[:, None]
            else:
                buf *= np.float32(g1)
        self.gain = g1
        if peak * max(g0, g1) > SOFT_KNEE:
            soft_clip(buf)


def soft_clip(x: np.ndarray) -> None:
    """Мягкое закругление выше SOFT_KNEE (на месте). Ниже — без изменений; предел ±1.0."""
    a = np.abs(x)
    over = a > SOFT_KNEE
    if not over.any():
        return
    room = 1.0 - SOFT_KNEE
    a_over = a[over]
    x[over] = np.sign(x[over]) * (SOFT_KNEE + room * np.tanh((a_over - SOFT_KNEE) / room))


def db_to_gain(db: float) -> float:
    return 0.0 if db <= FADER_MIN_DB else float(10.0 ** (db / 20.0))


# ──────────────────────────────────────────────────────────────────────────────
# Поиск устройств
# ──────────────────────────────────────────────────────────────────────────────
@dataclass
class DeviceInfo:
    index: int
    name: str
    hostapi: str
    outputs: int
    default_samplerate: float
    inputs: int = 0   # входы (X-USB: 32) — для записи с пульта

    @property
    def is_asio(self) -> bool:
        return "asio" in self.hostapi.lower()

    @property
    def is_wasapi(self) -> bool:
        return "wasapi" in self.hostapi.lower()

    @property
    def is_x32(self) -> bool:
        return self.is_asio and any(p in self.name.lower() for p in X32_DRIVER_PATTERNS)

    @property
    def label(self) -> str:
        return f"[{self.hostapi}] {self.name} — {self.outputs} вых."


def asio_available() -> bool:
    return any("asio" in h["name"].lower() for h in sd.query_hostapis())


def list_output_devices() -> list[DeviceInfo]:
    """Все устройства с выходами: сначала X32, потом прочие ASIO, потом остальное."""
    hostapis = sd.query_hostapis()
    result = []
    for idx, dev in enumerate(sd.query_devices()):
        if dev["max_output_channels"] <= 0:
            continue
        result.append(DeviceInfo(idx, dev["name"], hostapis[dev["hostapi"]]["name"],
                                 int(dev["max_output_channels"]), float(dev["default_samplerate"]),
                                 int(dev.get("max_input_channels", 0))))
    result.sort(key=lambda d: (not d.is_x32, not d.is_asio, d.hostapi, d.name))
    return result


def list_input_devices() -> list[DeviceInfo]:
    """Устройства записи (для работы без ASIO-пульта): сначала WASAPI."""
    hostapis = sd.query_hostapis()
    result = []
    for idx, dev in enumerate(sd.query_devices()):
        n_in = int(dev.get("max_input_channels", 0))
        if n_in <= 0:
            continue
        result.append(DeviceInfo(idx, dev["name"], hostapis[dev["hostapi"]]["name"],
                                 int(dev["max_output_channels"]), float(dev["default_samplerate"]), n_in))
    result.sort(key=lambda d: (not d.is_wasapi, d.hostapi, d.name))
    return result


def default_input_device() -> Optional[DeviceInfo]:
    devices = list_input_devices()
    by_index = {d.index: d for d in devices}
    for h in sd.query_hostapis():
        if "wasapi" in h["name"].lower() and h.get("default_input_device", -1) in by_index:
            return by_index[h["default_input_device"]]
    try:
        if sd.default.device[0] in by_index:
            return by_index[sd.default.device[0]]
    except Exception:
        pass
    return devices[0] if devices else None


def find_startup_device(preferred_name: str = "") -> Optional[DeviceInfo]:
    """Автопоиск при запуске.

    1) устройство, с которым работали в прошлый раз (ищем по ИМЕНИ — индексы
       PortAudio меняются при каждом переподключении USB);
    2) любой ASIO-драйвер Behringer X32 (X-USB / X-LIVE).
    """
    devices = list_output_devices()
    if preferred_name:
        for d in devices:
            if d.name == preferred_name:
                return d
    for d in devices:
        if d.is_x32:
            return d
    return None


def find_fallback_device() -> Optional[DeviceInfo]:
    """РАБОТА БЕЗ ПУЛЬТА: пульт не найден — берём обычную звуковую карту
    компьютера. Предпочтение — системный выход через WASAPI (минимальная
    задержка без ASIO), затем выход по умолчанию в PortAudio, затем любой."""
    devices = list_output_devices()
    if not devices:
        return None
    by_index = {d.index: d for d in devices}
    for h in sd.query_hostapis():
        if "wasapi" in h["name"].lower() and h.get("default_output_device", -1) in by_index:
            return by_index[h["default_output_device"]]
    try:
        default_out = sd.default.device[1]
        if default_out in by_index:
            return by_index[default_out]
    except Exception:
        pass
    return devices[-1]


def rescan_devices() -> None:
    """Пересканировать устройства (пульт подключили после запуска программы).
    PortAudio кэширует список при инициализации, поэтому перезапускаем его.
    ВНИМАНИЕ: все открытые потоки должны быть закрыты до вызова."""
    sd._terminate()
    sd._initialize()


# ──────────────────────────────────────────────────────────────────────────────
# Загруженная в память песня
# ──────────────────────────────────────────────────────────────────────────────
class TrackRuntime:
    """Состояние дорожки, которое читает аудио-коллбэк.

    GUI меняет target_gain / mute / solo / output обычным присваиванием.
    В CPython присваивание атрибута атомарно (GIL), поэтому блокировки не нужны,
    и аудиопоток НИКОГДА не ждёт GUI-поток.
    """

    def __init__(self, cfg: TrackConfig, data: np.ndarray, slot: PluginSlot):
        self.cfg = cfg
        self.data = data                       # (frames, channels) float32, C-contiguous
        self.length = data.shape[0]
        self.channels = data.shape[1]
        self.output = cfg.output               # 1-based первый выход X32
        self.mute = cfg.mute
        self.solo = cfg.solo
        self.target_gain = db_to_gain(cfg.gain_db)
        self.norm_gain = 1.0                   # поправка нормализации LUFS (клик/гайд — всегда 1.0)
        self.current_gain = self.target_gain   # для плавного изменения (без «молнии» фейдера)
        self.limiter = SafetyLimiter()         # защита от перегруза этой дорожки
        self.slot = slot
        self.fx: Optional[TrackFX] = None      # встроенные EQ + многополосный компрессор
        self.peak = 0.0                        # общий пик (совместимость)
        self.peaks = np.zeros(self.channels, dtype=np.float32)  # пик по каждому каналу: L/R или моно
        self.scratch = np.zeros((0, self.channels), dtype=np.float32)

    def ensure_scratch(self, frames: int) -> None:
        if self.scratch.shape[0] < frames:
            self.scratch = np.zeros((frames, self.channels), dtype=np.float32)


class LoadedSong:
    def __init__(self, song: Song, tracks: list[TrackRuntime], samplerate: int, warnings: list[str]):
        self.song = song
        self.tracks = tracks
        self.samplerate = samplerate
        self.warnings = warnings
        self.length = max((t.length for t in tracks), default=0)  # самая длинная дорожка

    @property
    def duration(self) -> float:
        return self.length / self.samplerate

    @property
    def memory_mb(self) -> float:
        return sum(t.data.nbytes for t in self.tracks) / 1e6


def load_song(song: Song, samplerate: int, blocksize: int,
              progress: Optional[Callable[[str], None]] = None) -> LoadedSong:
    """Готовит все дорожки песни в RAM (обрезка, темп, тон — см. audio_edit.py)
    и загружает VST. Вызывать ТОЛЬКО из фонового потока — занимает секунды."""
    tracks: list[TrackRuntime] = []
    warnings: list[str] = []
    heavy = song.edit.speed != 1.0 or song.edit.pitch != 0.0
    if heavy:
        # Темп/тон всех дорожек — параллельно в отдельных процессах (см. audio_edit.py).
        # Ниже render_track уже мгновенно возьмёт готовое из кэша.
        render_heavy_parallel([c for c in song.tracks if os.path.exists(c.file)],
                              song.edit, samplerate, progress)
    for i, cfg in enumerate(song.tracks):
        if progress:
            what = "Обработка темпа/тона" if heavy else "Загрузка"
            progress(f"{what}: {cfg.name} ({i + 1}/{len(song.tracks)})")
        if not os.path.exists(cfg.file):
            # Нет файла — песня всё равно играет, дорожка молчит, в статусе предупреждение
            warnings.append(f"«{cfg.name}»: файл не найден")
            data = np.zeros((0, max(1, cfg.channels)), dtype=np.float32)
        else:
            song.duration = max(song.duration, _probe(Path(cfg.file))[1])
            data = render_track(cfg, song.edit, samplerate)  # ошибки обработки — наверх, в окно ошибки
        cfg.channels = data.shape[1]

        slot = PluginSlot()
        if cfg.plugin and cfg.plugin.path:
            try:
                slot.load(cfg.plugin.path, cfg.plugin.state_b64, samplerate, cfg.channels, blocksize)
                slot.bypass = cfg.plugin.bypass
            except Exception as exc:
                slot.load_error = str(exc)
                warnings.append(f"«{cfg.name}»: VST не загружен ({exc})")
        rt = TrackRuntime(cfg, data, slot)
        rt.fx = TrackFX(cfg.fx, samplerate, rt.channels)
        tracks.append(rt)
    return LoadedSong(song, tracks, samplerate, warnings)


# ──────────────────────────────────────────────────────────────────────────────
# Движок
# ──────────────────────────────────────────────────────────────────────────────
class AudioEngine:
    def __init__(self) -> None:
        self.stream: Optional[sd.OutputStream] = None
        self.device: Optional[DeviceInfo] = None
        self.samplerate = 48000
        self.blocksize = 256
        self.num_outputs = 0
        # ── состояние, разделяемое с аудио-коллбэком ──
        self._song: Optional[LoadedSong] = None
        self._pos = 0                 # позиция воспроизведения в сэмплах
        self._playing = False
        self._stop_requested = False
        self._fade = 1.0
        self.finished = False         # «песня доиграла до конца» — сбрасывает GUI
        self.xruns = 0                # счётчик пропусков буфера (щелчков)
        # Режим без пульта: все дорожки сводятся в выходы 1–2 (колонки/наушники),
        # выбранный роутинг Out N при этом сохраняется и вернётся с пультом.
        self.stereo_mix = False
        # Перемотка: GUI кладёт сюда целевую позицию, коллбэк делает микрофейд и прыгает
        self._seek_to = -1
        self._fade_in_next = False
        # Запись с пульта: при дуплексном ASIO-потоке входы X-USB уходят в recorder.push()
        self.recorder = None
        self.duplex_inputs = 0
        # Метроном: звучит и на паузе (свободный ход), и во время песни (привязан к её долям)
        self.metro_on = False
        self.metro_bpm = 120.0
        self.metro_offset = 0.0     # сек: где у песни первая доля (во времени воспроизведения)
        self.metro_output = 1       # 1-based выход пульта
        self.metro_gain = 0.5
        self.metro_accent = True
        self._metro_free = 0        # счётчик сэмплов для свободного хода
        self._bus_limiter = SafetyLimiter()   # защита выходов от суммарного перегруза
        self.metro_beats = 4        # долей в такте: акцент и длина отсчёта
        # Отсчёт перед стартом: сколько сэмплов щелчков осталось до начала трека
        self._countin_left = 0
        self._countin_total = 0
        # Петля: при достижении loop_end прыгаем на loop_start (сэмплы), с микрофейдом
        self.loop_on = False
        self.loop_start = 0
        self.loop_end = 0
        self._metro_click = np.zeros(0, np.float32)
        self._metro_click_hi = np.zeros(0, np.float32)

    # ── открытие ASIO-потока ────────────────────────────────────────────────
    def open(self, device: DeviceInfo, samplerate: int, blocksize: int) -> None:
        """Открывает ASIO-драйвер X32 на все выходы и запускает поток.

        * channels = ВСЕ выходы драйвера (32 у X-USB). Роутинг делаем сами —
          это проще и надёжнее, чем channel_selectors, и позволяет менять
          выход дорожки на лету, не переоткрывая драйвер.
        * samplerate ОБЯЗАН совпадать с частотой пульта (Setup → Config →
          Sample Rate на X32). Тактовым мастером является пульт: если
          запросить другую частоту, драйвер X-USB вернёт ошибку.
        * blocksize: 128–256 сэмплов — разумный компромисс для Python.
          Задержка для плеера не критична (клик и минус выходят синхронно
          между собой), а запас по буферу = отсутствие щелчков.
        * dtype float32: PortAudio сам конвертирует в 24/32-бит драйвера.

        БЕЗ ПУЛЬТА (WASAPI/MME): задержка «high» — у обычных драйверов Windows
        «low» часто даёт треск, а для плеера лишние 20–40 мс не важны.
        auto_convert позволяет играть 48 кГц на карте, выставленной на 44.1 кГц.
        """
        self.close()
        extra = None
        if device.is_wasapi:
            try:
                extra = sd.WasapiSettings(auto_convert=True)
            except TypeError:  # старый sounddevice без auto_convert
                extra = None
        common = dict(samplerate=samplerate, blocksize=blocksize, dtype="float32",
                      latency="low" if device.is_asio else "high",
                      clip_off=True,    # клиппинг делаем сами в коллбэке
                      dither_off=True)  # дизеринг при float→int32 не нужен
        self.duplex_inputs = 0
        stream = None
        if device.is_asio and device.inputs > 0:
            # ДУПЛЕКС: выходы X-USB → дорожки, входы X-USB → запись. Один поток —
            # потому что ASIO-драйвер пульта допускает только одного клиента.
            try:
                stream = sd.Stream(device=(device.index, device.index),
                                   channels=(device.inputs, device.outputs),
                                   callback=self._duplex_callback, **common)
                self.duplex_inputs = device.inputs
            except Exception:
                # Воспроизведение важнее записи: если драйвер не дал дуплекс —
                # открываем только выходы, запись с пульта будет недоступна.
                stream = None
        if stream is None:
            stream = sd.OutputStream(device=device.index, channels=device.outputs,
                                     extra_settings=extra, callback=self._callback, **common)
        if self._song is not None and self._song.samplerate != samplerate:
            self._song = None  # загруженные данные на другой частоте — GUI перезагрузит песню
        if self._song is not None:
            for t in self._song.tracks:
                t.ensure_scratch(blocksize * 2)
        self._make_metro_clicks(samplerate)
        stream.start()
        self.stream = stream
        self.device = device
        self.samplerate = samplerate
        self.blocksize = blocksize
        self.num_outputs = device.outputs

    def close(self) -> None:
        self._playing = False
        if self.stream is not None:
            try:
                self.stream.stop()
                self.stream.close()
            finally:
                self.stream = None

    # ── свойства для GUI ────────────────────────────────────────────────────
    @property
    def is_open(self) -> bool:
        return self.stream is not None and self.stream.active

    @property
    def is_playing(self) -> bool:
        return self._playing

    @property
    def song(self) -> Optional[LoadedSong]:
        return self._song

    @property
    def position_seconds(self) -> float:
        return self._pos / self.samplerate

    @property
    def cpu_load(self) -> float:
        try:
            return float(self.stream.cpu_load) if self.stream is not None else 0.0
        except Exception:
            return 0.0

    @property
    def latency_ms(self) -> float:
        """Задержка выхода в мс. У дуплексного потока (воспроизведение + запись)
        sounddevice отдаёт пару (вход, выход) — берём выход."""
        return self.output_latency * 1000

    @property
    def output_latency(self) -> float:
        if self.stream is None:
            return 0.0
        try:
            lat = self.stream.latency
            if isinstance(lat, (tuple, list)):
                lat = lat[-1]
            return float(lat)
        except Exception:
            return 0.0

    @property
    def roundtrip_latency(self) -> float:
        """Вход + выход, сек: на столько записанный звук отстаёт от воспроизводимого."""
        if self.stream is None:
            return 0.0
        try:
            lat = self.stream.latency
            return float(sum(lat)) if isinstance(lat, (tuple, list)) else float(lat)
        except Exception:
            return 0.0

    # ── транспорт (GUI-поток) ───────────────────────────────────────────────
    def set_song(self, loaded: Optional[LoadedSong]) -> None:
        if self._playing:
            raise RuntimeError("Нельзя менять трек во время воспроизведения")
        if loaded is not None:
            for t in loaded.tracks:
                t.ensure_scratch(self.blocksize * 2)  # память выделяем заранее, не в коллбэке
        self._pos = 0
        self.finished = False
        self._song = loaded

    def play(self, count_in_bars: int = 0) -> bool:
        """Старт. count_in_bars > 0 — сначала такты отсчёта метронома, причём последний
        щелчок отсчёта отстоит от ближайшей доли трека ровно на одну долю (без сбоя ритма)."""
        song = self._song
        if self.stream is None or song is None or song.length == 0:
            return False
        if self._pos >= song.length:
            self._pos = 0
        self.finished = False
        self._fade = 1.0
        self._stop_requested = False
        self._countin_left = self._countin_total = 0
        if self._pos > 0:
            self._fade_in_next = True     # продолжение с середины (после паузы) — без щелчка
        if count_in_bars > 0 and self.metro_bpm > 0:
            sr = self.samplerate
            period = sr * 60.0 / self.metro_bpm
            phase = (self._pos - self.metro_offset * sr) % period   # где мы внутри доли трека
            to_next_beat = (period - phase) % period
            total = int(round(count_in_bars * self.metro_beats * period - to_next_beat))
            if total > 0:
                self._countin_total = self._countin_left = total
        self._playing = True
        return True

    @property
    def counting_in(self) -> bool:
        return self._playing and self._countin_left > 0

    @property
    def countin_seconds_left(self) -> float:
        return self._countin_left / self.samplerate if self._playing else 0.0

    def set_loop(self, start_sec: Optional[float], end_sec: Optional[float]) -> None:
        if start_sec is None or end_sec is None or end_sec - start_sec < 0.1:
            self.loop_on = False
            return
        self.loop_start = int(start_sec * self.samplerate)
        self.loop_end = int(end_sec * self.samplerate)
        self.loop_on = True

    def stop(self) -> None:
        """Запрос остановки: коллбэк сделает фейд 30 мс и встанет на паузу."""
        if self._playing:
            self._stop_requested = True

    # ── метроном ────────────────────────────────────────────────────────────
    def _make_metro_clicks(self, sr: int) -> None:
        """Короткие щелчки ~25 мс: обычная доля 1000 Гц, сильная — 1600 Гц."""
        n = int(sr * 0.025)
        t = np.arange(n) / sr
        env = np.exp(-t * 180.0).astype(np.float32)
        self._metro_click = (np.sin(2 * np.pi * 1000 * t) * env).astype(np.float32)
        self._metro_click_hi = (np.sin(2 * np.pi * 1600 * t) * env).astype(np.float32)

    def _metro_render(self, outdata: np.ndarray, start: int, offset_sec: float, frames: int) -> None:
        """Добавляет щелчки, попадающие в блок [start, start+frames) (в сэмплах).
        Доли: offset + k·период. Вызывается из аудио-коллбэка — только numpy."""
        bpm = self.metro_bpm
        click = self._metro_click
        if bpm <= 0 or click.size == 0:
            return
        sr = self.samplerate
        period = sr * 60.0 / bpm
        off = offset_sec * sr
        clen = click.size
        n_out = outdata.shape[1]
        stereo = self.stereo_mix and n_out >= 2
        col = self.metro_output - 1
        if not stereo and not (0 <= col < n_out):
            return
        k = int(np.ceil((start - clen - off) / period))
        end = start + frames
        gain = np.float32(self.metro_gain)
        while True:
            beat = int(round(off + k * period))
            if beat >= end:
                break
            if beat + clen > start:
                src = self._metro_click_hi if (self.metro_accent and k % max(1, self.metro_beats) == 0) else click
                a = max(start, beat)
                b = min(end, beat + clen)
                piece = src[a - beat:b - beat] * gain
                if stereo:
                    outdata[a - start:b - start, 0] += piece
                    outdata[a - start:b - start, 1] += piece
                else:
                    outdata[a - start:b - start, col] += piece
            k += 1

    def seek(self, seconds: float) -> None:
        """Перемотка. Во время игры — через коллбэк с микрофейдом (без щелчка),
        на паузе — сразу."""
        song = self._song
        if song is None or song.length == 0:
            return
        target = int(min(max(seconds, 0.0) * self.samplerate, song.length - 1))
        if self._playing:
            self._seek_to = target
        else:
            self._pos = target
            self.finished = False

    def rewind(self) -> None:
        """В начало песни (только когда стоим)."""
        if not self._playing:
            self._pos = 0
            if self._song is not None:
                for t in self._song.tracks:
                    t.slot.reset()
                    if t.fx is not None:
                        t.fx.reset()

    # ╔══════════════════════════════════════════════════════════════════════╗
    # ║ АУДИО-КОЛЛБЭК                                                        ║
    # ║ Вызывается драйвером X-USB ASIO в высокоприоритетном потоке каждые   ║
    # ║ blocksize/samplerate секунд (256/48000 ≈ 5.3 мс). Должен успеть      ║
    # ║ заполнить outdata ЗАВЕДОМО быстрее — иначе щелчок в портале.         ║
    # ║ ЗАПРЕЩЕНО: print/логи, файлы, lock'и, sleep, вызовы Qt, создание     ║
    # ║ крупных объектов. Только numpy-операции над готовыми массивами.      ║
    # ╚══════════════════════════════════════════════════════════════════════╝
    def _duplex_callback(self, indata: np.ndarray, outdata: np.ndarray, frames: int,
                         time_info, status) -> None:
        # Входы пульта → очередь записи (только копия, без диска), затем обычное воспроизведение
        rec = self.recorder
        if rec is not None and rec.active:
            rec.push(indata)
        self._callback(outdata, frames, time_info, status)

    def _callback(self, outdata: np.ndarray, frames: int, time_info, status) -> None:
        if status.output_underflow:
            self.xruns += 1

        # outdata: (frames, 32) — по столбцу на каждый USB-выход X32.
        # Буфер содержит мусор от прошлого вызова, поэтому сначала тишина.
        outdata.fill(0.0)

        song = self._song                      # локальная ссылка: GUI может подменить song
        if song is None or not self._playing:
            if self.metro_on:                   # метроном на паузе — свободный ход
                self._metro_render(outdata, self._metro_free, 0.0, frames)
                self._metro_free += frames
                np.clip(outdata, -1.0, 1.0, out=outdata)
            else:
                self._metro_free = 0
            return

        # ОТСЧЁТ ПЕРЕД СТАРТОМ: сначала только щелчки метронома, позиция трека стоит.
        # Если отсчёт кончается внутри блока — остаток блока сразу заполняется треком,
        # поэтому стык точный до сэмпла.
        start = 0
        if self._countin_left > 0:
            if self._stop_requested:          # STOP во время отсчёта — тихо отменяем
                self._countin_left = 0
                self._playing = False
                self._stop_requested = False
                return
            c = min(self._countin_left, frames)
            self._metro_render(outdata[:c], self._countin_total - self._countin_left, 0.0, c)
            self._countin_left -= c
            start = c
            if start >= frames:
                np.clip(outdata, -1.0, 1.0, out=outdata)
                return
        self._render_song(outdata[start:], frames - start, song)
        if start:
            np.clip(outdata[:start], -1.0, 1.0, out=outdata[:start])

    def _render_song(self, outdata: np.ndarray, frames: int, song: "LoadedSong") -> None:
        """Основная часть коллбэка: дорожки трека → выходы. outdata — (часть) буфера драйвера."""
        seek_target = self._seek_to            # запрошена перемотка?
        pos = self._pos
        # ПЕТЛЯ: дошли до конца участка — прыжок на его начало тем же механизмом, что перемотка
        if (seek_target < 0 and self.loop_on and self.loop_end > self.loop_start
                and pos >= self.loop_end):
            seek_target = self.loop_start
        remaining = song.length - pos
        if remaining <= 0:
            self._playing = False
            self.finished = True
            return
        n = frames if frames < remaining else remaining   # полезных сэмплов в этом блоке
        sr = self.samplerate
        n_out = outdata.shape[1]
        stereo = self.stereo_mix and n_out >= 2
        tracks = song.tracks

        any_solo = False
        for t in tracks:
            if t.solo:
                any_solo = True
                break

        for t in tracks:
            audible = (not t.mute) and (t.solo or not any_solo)
            target = t.target_gain * t.norm_gain if audible else 0.0
            if target == 0.0 and t.current_gain == 0.0:
                continue                       # заглушённая дорожка — не тратим CPU

            # 1) Срез дорожки по общему счётчику позиции → синхронность гарантирована
            if t.scratch.shape[0] < frames:    # драйвер прислал блок больше обычного (редко)
                t.ensure_scratch(frames)
            buf = t.scratch[:frames]
            buf.fill(0.0)
            avail = min(n, t.length - pos)     # дорожка может быть короче песни
            if avail > 0:
                buf[:avail] = t.data[pos:pos + avail]

            # 2) Встроенные EQ и многополосный компрессор, затем инсерт VST — ДО выхода
            fx = t.fx
            if fx is not None and fx.active:
                buf = fx.process(buf)
            if t.slot.plugin is not None:
                buf = t.slot.process(buf, sr)

            # 3) Фейдер / Mute / Solo с линейной интерполяцией внутри блока
            g0 = t.current_gain
            if g0 != target:
                ramp = np.linspace(g0, target, frames, dtype=np.float32)
                np.multiply(buf, ramp[:, None], out=buf)
                t.current_gain = target
            elif g0 != 1.0:
                np.multiply(buf, np.float32(g0), out=buf)

            # 4) Пики для индикаторов, по каждому каналу (GUI забирает и обнуляет 30 раз в секунду)
            #    — ДО ограничителя: лампа перегруза честно показывает, что дорожку перегнали
            ch_pk = np.abs(buf).max(axis=0)
            np.maximum(t.peaks, ch_pk, out=t.peaks)
            # 4а) защита от перегруза дорожки (фейдер выше 0, EQ, нормализация, смена тона)
            pk = float(ch_pk.max()) if ch_pk.size else 0.0
            if pk > LIMIT_CEIL or t.limiter.gain < 0.999:
                t.limiter.process(buf, pk, sr)

            # 5) РОУТИНГ
            if stereo:
                # Без пульта: всё в колонки. Моно — по центру, стерео — L/R как есть.
                if t.channels == 1:
                    np.multiply(buf, CENTER_GAIN, out=buf)
                    outdata[:, 0] += buf[:, 0]
                    outdata[:, 1] += buf[:, 0]
                else:
                    outdata[:, 0] += buf[:, 0]
                    outdata[:, 1] += buf[:, 1]
                continue
            # С пультом: дорожка → столбец(цы) outdata = физический(е) выход(ы) X32
            first = t.output - 1
            for c in range(t.channels):
                oc = first + c
                if 0 <= oc < n_out:
                    outdata[:, oc] += buf[:, c]

        if self.metro_on:                       # метроном, привязанный к долям песни
            self._metro_render(outdata, pos, self.metro_offset, frames)
            self._metro_free = 0

        # Выходы пульта: несколько дорожек на одном выходе (или сведение в стерео без пульта)
        # в сумме могут перегрузить — общий ограничитель вместо жёсткого среза.
        bus_pk = float(np.abs(outdata).max()) if outdata.size else 0.0
        if bus_pk > LIMIT_CEIL or self._bus_limiter.gain < 0.999:
            self._bus_limiter.process(outdata, bus_pk, sr)
        np.clip(outdata, -1.0, 1.0, out=outdata)        # последняя страховка (обычно ничего не делает)

        if seek_target >= 0:
            # ПЕРЕМОТКА: текущий блок плавно гасим, следующий начнём с новой позиции
            # с плавным нарастанием — вместо щелчка слышен короткий «перескок».
            outdata *= np.linspace(1.0, 0.0, frames, dtype=np.float32)[:, None]
            self._pos = seek_target
            self._seek_to = -1
            self._fade_in_next = True
            return
        self._pos = pos + n
        if self._fade_in_next:
            outdata *= np.linspace(0.0, 1.0, frames, dtype=np.float32)[:, None]
            self._fade_in_next = False

        if self._stop_requested:
            # Плавный STOP за ~30 мс (несколько блоков), без щелчка
            f0 = self._fade
            f1 = max(0.0, f0 - frames / (STOP_FADE_SECONDS * sr))
            outdata *= np.linspace(f0, f1, frames, dtype=np.float32)[:, None]
            self._fade = f1
            if f1 <= 0.0:
                self._playing = False
                self._stop_requested = False
                self._fade = 1.0
        elif self._pos >= song.length:
            # Песня доиграла: дальше тишина, GUI увидит флаг и сделает Auto-cue
            self._playing = False
            self.finished = True
