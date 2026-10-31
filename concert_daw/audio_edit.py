"""
audio_edit.py — Офлайн-обработка дорожек: обрезка, темп, тон, фейды, кэш, экспорт.

Почему офлайн, а не в аудио-коллбэке
-----------------------------------
Качественное изменение темпа без смены тона (и наоборот) — это Rubber Band
(pedalboard.time_stretch). На живом потоке из Python он не гарантирует
стабильности, поэтому каждая дорожка обрабатывается ОДИН раз при загрузке
песни в фоновом потоке, а результат складывается в кэш на диске
(%APPDATA%\\ConcertDAW\\render_cache). Повторная загрузка — мгновенная.

Синхронность: темп и обрезка применяются ко всем дорожкам одинаково,
а итоговая длина каждой дорожки подгоняется до точного числа сэмплов.
Тон меняется только у «музыкальных» дорожек: клик и гайд остаются как были.
"""
from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
from pathlib import Path
from typing import Callable, Optional

import numpy as np
import soundfile as sf

from model import Song, SongEdit, TrackConfig, app_data_dir

try:
    from pedalboard import time_stretch as _pb_time_stretch
    STRETCH_AVAILABLE = True
except Exception:  # pedalboard не установлен или слишком старый (< 0.9.12)
    _pb_time_stretch = None
    STRETCH_AVAILABLE = False

try:
    import soxr
except ImportError:
    soxr = None

CACHE_VERSION = 2
Progress = Optional[Callable[[str], None]]


def cache_dir() -> Path:
    return app_data_dir() / "render_cache"


def cache_size_mb() -> float:
    d = cache_dir()
    if not d.exists():
        return 0.0
    return sum(f.stat().st_size for f in d.glob("*.npy")) / 1e6


def clear_cache() -> None:
    for f in cache_dir().glob("*.npy"):
        try:
            f.unlink()
        except OSError:
            pass


# ──────────────────────────────────────────────────────────────────────────────
# Темп / тон
# ──────────────────────────────────────────────────────────────────────────────
_speeds_up: Optional[bool] = None


def _stretch_factor(speed: float) -> float:
    """Разные версии pedalboard трактуют stretch_factor по-разному
    («ускорение» или «растяжение»). Один раз проверяем на тестовом сигнале,
    чтобы 110 % всегда означало «на 10 % быстрее»."""
    global _speeds_up
    if _speeds_up is None:
        test = (np.random.default_rng(0).standard_normal((1, 48000)) * 0.1).astype(np.float32)
        out = _pb_time_stretch(test, 48000.0, stretch_factor=2.0)
        _speeds_up = out.shape[-1] < test.shape[-1]
    return speed if _speeds_up else 1.0 / speed


def _fit_length(data: np.ndarray, n: int) -> np.ndarray:
    if data.shape[0] >= n:
        return data[:n]
    pad = np.zeros((n - data.shape[0], data.shape[1]), dtype=np.float32)
    return np.concatenate([data, pad])


def _apply_fades(data: np.ndarray, sr: int, fade_in_ms: float, fade_out_ms: float) -> None:
    n = data.shape[0]
    fi = min(n, int(sr * fade_in_ms / 1000))
    fo = min(n, int(sr * fade_out_ms / 1000))
    if fi > 1:
        data[:fi] *= np.linspace(0.0, 1.0, fi, dtype=np.float32)[:, None]
    if fo > 1:
        data[n - fo:] *= np.linspace(1.0, 0.0, fo, dtype=np.float32)[:, None]


def silence_clip(data: np.ndarray, sr: int, start: float, end: float, fade_ms: float = 10.0) -> None:
    """Глушит всё до start и после end (сек), на границах — короткие фейды без щелчков."""
    n = data.shape[0]
    a = max(0, min(n, int(round(start * sr))))
    b = n if end <= 0 else max(a, min(n, int(round(end * sr))))
    f = max(1, int(sr * fade_ms / 1000))
    data[:a] = 0.0
    data[b:] = 0.0
    fi = min(f, b - a)
    if fi > 1:
        data[a:a + fi] *= np.linspace(0.0, 1.0, fi, dtype=np.float32)[:, None]
        data[b - fi:b] *= np.linspace(1.0, 0.0, fi, dtype=np.float32)[:, None]


def file_peaks(path: str, rate: int = 100, column: Optional[int] = None) -> tuple[np.ndarray, float]:
    """Огибающая файла для окна дорожек: rate столбиков на секунду, 0..1.
    column — один канал многоканального файла (запись Multitrack.wav)."""
    data, sr = sf.read(path, dtype="float32", always_2d=True)
    if column is not None and column < data.shape[1]:
        mono = np.abs(data[:, column])
    else:
        mono = np.abs(data).max(axis=1)
    spb = max(1, int(sr / rate))
    nb = mono.shape[0] // spb
    peaks = mono[:nb * spb].reshape(nb, spb).max(axis=1) if nb else np.zeros(0, np.float32)
    if mono.shape[0] > nb * spb:
        peaks = np.append(peaks, mono[nb * spb:].max())
    return peaks.astype(np.float32), mono.shape[0] / sr


def _semis_heavy(cfg: TrackConfig, edit: SongEdit, transpose: Optional[bool]) -> tuple[float, bool]:
    do_transpose = cfg.transposes if transpose is None else transpose
    semis = float(edit.pitch) if do_transpose else 0.0
    return semis, (edit.speed != 1.0 or semis != 0.0)


def cache_path_for(cfg: TrackConfig, edit: SongEdit, samplerate: int,
                   transpose: Optional[bool] = None) -> Path:
    semis, _ = _semis_heavy(cfg, edit, transpose)
    st = os.stat(cfg.file)
    key = json.dumps([CACHE_VERSION, os.path.abspath(cfg.file), st.st_mtime_ns, st.st_size,
                      samplerate, edit.start, edit.end, edit.speed, semis,
                      edit.fade_in, edit.fade_out, cfg.clip_start, cfg.clip_end])
    return cache_dir() / (hashlib.sha1(key.encode()).hexdigest() + ".npy")


def needs_heavy_render(cfg: TrackConfig, edit: SongEdit, samplerate: int,
                       transpose: Optional[bool] = None) -> bool:
    """Нужна ли долгая обработка (темп/тон), которой ещё нет в кэше."""
    if not os.path.exists(cfg.file):
        return False
    _, heavy = _semis_heavy(cfg, edit, transpose)
    return heavy and not cache_path_for(cfg, edit, samplerate, transpose).exists()


# ──────────────────────────────────────────────────────────────────────────────
# ОТДЕЛЬНЫЕ ПРОЦЕССЫ для тяжёлой обработки
# ──────────────────────────────────────────────────────────────────────────────
# Rubber Band (темп/тон) и анализ тональности — долгие вычисления. В том же
# процессе они конкурируют за GIL с аудио-коллбэком и интерфейсом (подвисания,
# щелчки). Поэтому они идут в пуле процессов: дорожки считаются ПАРАЛЛЕЛЬНО на
# разных ядрах, результат складывается в кэш на диске, а основной процесс только
# читает готовый .npy. Процессы-работники — с пониженным приоритетом.
_executor = None
_inflight: dict = {}          # путь в кэше → Future: одна дорожка не считается дважды
_inflight_lock = threading.RLock()  # RLock: колбэк завершения может сработать сразу


def _worker_init() -> None:
    try:
        if sys.platform == "win32":
            import ctypes
            k32 = ctypes.windll.kernel32
            k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00004000)  # BELOW_NORMAL
        else:
            os.nice(10)
    except Exception:
        pass


def get_executor():
    global _executor
    if _executor is None:
        import concurrent.futures as cf
        import multiprocessing as mp
        workers = max(1, min(4, (os.cpu_count() or 2) - 1))
        _executor = cf.ProcessPoolExecutor(max_workers=workers, mp_context=mp.get_context("spawn"),
                                           initializer=_worker_init)
    return _executor


def _noop() -> bool:
    return STRETCH_AVAILABLE  # импорт numpy/pedalboard в работнике уже произошёл


def warm_up() -> None:
    """Запустить процессы заранее (импорт библиотек ~1–2 с), чтобы первая
    смена тона не ждала их старта."""
    try:
        ex = get_executor()
        for _ in range(ex._max_workers):
            ex.submit(_noop)
    except Exception:
        pass


def shutdown() -> None:
    global _executor
    if _executor is not None:
        try:
            _executor.shutdown(wait=False, cancel_futures=True)
        except Exception:
            pass
        _executor = None


def _render_job(cfg_dict: dict, edit_dict: dict, samplerate: int) -> str:
    """Выполняется В ПРОЦЕССЕ-РАБОТНИКЕ: считает дорожку и кладёт в кэш."""
    cfg = TrackConfig.from_dict(cfg_dict)
    edit = SongEdit.from_dict(edit_dict)
    render_track(cfg, edit, samplerate)
    return str(cache_path_for(cfg, edit, samplerate))


def _submit(t: TrackConfig, edit: SongEdit, samplerate: int):
    """Поставить дорожку в очередь пула; если она уже считается — вернуть тот же Future."""
    from dataclasses import asdict
    key = str(cache_path_for(t, edit, samplerate))
    with _inflight_lock:
        fut = _inflight.get(key)
        if fut is None or (fut.done() and fut.exception() is not None):
            fut = get_executor().submit(_render_job, asdict(t), asdict(edit), samplerate)
            _inflight[key] = fut
            fut.add_done_callback(lambda f, k=key: _forget(k, f))
        return fut


def _forget(key: str, fut) -> None:
    with _inflight_lock:
        if _inflight.get(key) is fut:
            _inflight.pop(key, None)


def render_heavy_parallel(tracks: list[TrackConfig], edit: SongEdit, samplerate: int,
                          progress: Progress = None) -> None:
    """Параллельно посчитать темп/тон для всех дорожек, которых нет в кэше.
    После этого render_track() для них мгновенно берёт результат из кэша."""
    todo = [t for t in tracks if needs_heavy_render(t, edit, samplerate)]
    if not todo:
        return
    import concurrent.futures as cf
    try:
        futures = [_submit(t, edit, samplerate) for t in todo]
    except Exception:
        return  # пул недоступен — render_track посчитает сам, по очереди
    done = 0
    if progress:
        progress(f"Обработка темпа/тона: 0/{len(todo)} дорожек")
    for fut in cf.as_completed(futures):
        done += 1
        try:
            fut.result()
        except Exception:
            pass  # ошибку покажет повторный render_track в основном процессе
        if progress:
            progress(f"Обработка темпа/тона: {done}/{len(todo)} дорожек")


def prerender_song(song: Song, samplerate: int) -> None:
    """Фоновый предрасчёт (после правки в редакторе): ничего не ждём."""
    try:
        for t in song.tracks:
            if needs_heavy_render(t, song.edit, samplerate):
                _submit(t, song.edit, samplerate)
    except Exception:
        pass


def render_track(cfg: TrackConfig, edit: SongEdit, samplerate: int,
                 transpose: Optional[bool] = None, use_cache: bool = True) -> np.ndarray:
    """Возвращает готовую к воспроизведению дорожку (frames, channels) float32.

    Порядок: чтение → обрезка → передискретизация → темп/тон → фейды.
    Обрезка идёт ДО растяжения — меньше работы для Rubber Band."""
    semis, heavy = _semis_heavy(cfg, edit, transpose)

    cache_path = None
    if use_cache:
        cache_path = cache_path_for(cfg, edit, samplerate, transpose)
        if cache_path.exists():
            try:
                return np.load(cache_path)
            except Exception:
                pass  # битый файл кэша — пересчитаем

    data, sr = sf.read(cfg.file, dtype="float32", always_2d=True)

    # 0) обрезка ОТДЕЛЬНОЙ дорожки (окно дорожек): вне участка — тишина,
    #    длина не меняется, поэтому дорожка остаётся синхронной с остальными
    if cfg.clip_start > 0 or cfg.clip_end > 0:
        data = np.array(data, dtype=np.float32)
        silence_clip(data, sr, cfg.clip_start, cfg.clip_end)

    # 1) обрезка песни в секундах исходника
    a = max(0, min(data.shape[0], int(round(edit.start * sr))))
    b = int(round(edit.end * sr)) if edit.end > 0 else data.shape[0]
    b = max(a, min(data.shape[0], b))
    data = data[a:b]

    # 2) частота дискретизации под пульт / звуковую карту
    resampled = False
    if sr != samplerate:
        if soxr is None:
            raise ValueError(f"«{cfg.name}»: файл {sr} Гц, а устройство работает на {samplerate} Гц.\n"
                             "Установите пакет soxr (pip install soxr) или сконвертируйте файлы.")
        data = soxr.resample(data, sr, samplerate, quality="HQ").astype(np.float32, copy=False)
        resampled = True

    # 3) темп без смены тона и/или тон без смены темпа (Rubber Band)
    if heavy and data.shape[0] > 0:
        if not STRETCH_AVAILABLE:
            raise RuntimeError("Для смены темпа и тона нужен pedalboard 0.9.12 или новее:\n"
                               "pip install -U pedalboard")
        n_target = int(round(data.shape[0] / edit.speed))
        out = _pb_time_stretch(np.ascontiguousarray(data.T), float(samplerate),
                               stretch_factor=_stretch_factor(edit.speed),
                               pitch_shift_in_semitones=semis)
        data = _fit_length(np.asarray(out, dtype=np.float32).T, n_target)

    data = np.array(data, dtype=np.float32, order="C")  # своя копия: фейды пишут на месте
    # 4) фейды на краях обрезки — чтобы обрезанная песня не начиналась со щелчка
    _apply_fades(data, samplerate, edit.fade_in, edit.fade_out)

    if cache_path is not None and (heavy or resampled):
        try:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = cache_path.with_name(f"{cache_path.stem}.{os.getpid()}.part.npy")  # свой у каждого процесса
            np.save(tmp, data)
            os.replace(tmp, cache_path)
        except OSError:
            pass  # нет места на диске — просто не кэшируем
    return data


# ──────────────────────────────────────────────────────────────────────────────
# Обзор волны для редактора
# ──────────────────────────────────────────────────────────────────────────────
def compute_overview(song: Song, bins: int = 1600) -> tuple[np.ndarray, float]:
    """Пики всей песни (максимум по всем дорожкам) для отрисовки волны.
    Возвращает (пики 0..1, длительность исходника в секундах)."""
    infos = []
    for cfg in song.tracks:
        try:
            infos.append((cfg, sf.info(cfg.file)))
        except Exception:
            pass
    if not infos:
        return np.zeros(bins, np.float32), 0.0
    duration = max(i.frames / i.samplerate for _, i in infos)
    peaks = np.zeros(bins, np.float32)
    for cfg, info in infos:
        try:
            data, sr = sf.read(cfg.file, dtype="float32", always_2d=True)
        except Exception:
            continue
        mono = np.abs(data).max(axis=1)
        spb = max(1, int(duration * sr / bins))     # сэмплов на столбик
        nb = min(bins, mono.shape[0] // spb)
        if nb:
            tr = mono[:nb * spb].reshape(nb, spb).max(axis=1)
            np.maximum(peaks[:nb], tr, out=peaks[:nb])
    top = float(peaks.max())
    return (peaks / top if top > 0 else peaks), duration


# ──────────────────────────────────────────────────────────────────────────────
# Экспорт обработанной песни в WAV
# ──────────────────────────────────────────────────────────────────────────────
def export_song(song: Song, edit: SongEdit, transposes: dict[int, bool], samplerate: int,
                parent_folder: str, progress: Progress = None) -> str:
    """Записывает обработанные дорожки (24 бит) в новую папку рядом с выбранной.
    transposes: индекс дорожки → менять ли тон."""
    suffix = edit.summary() or "копия"
    folder = Path(parent_folder) / f"{song.name} ({suffix})"
    n = 2
    base = folder
    while folder.exists():
        folder = Path(f"{base} {n}")
        n += 1
    folder.mkdir(parents=True)
    for i, cfg in enumerate(song.tracks):
        if progress:
            progress(f"Экспорт {i + 1}/{len(song.tracks)}: {cfg.name}")
        data = render_track(cfg, edit, samplerate, transposes.get(i), use_cache=True)
        sf.write(str(folder / f"{Path(cfg.file).stem}.wav"), data, samplerate, subtype="PCM_24")
    return str(folder)


# ──────────────────────────────────────────────────────────────────────────────
# Тишина в начале и в конце трека
# ──────────────────────────────────────────────────────────────────────────────
SILENCE_START_DB = -54.0   # тише этого в начале — тишина (шум/дизер обычно ниже −70)
SILENCE_END_DB = -60.0     # в конце порог ниже — чтобы не срезать хвосты ревербераций
LEAD_MARGIN = 0.05         # оставляем 50 мс перед первым звуком — атака не срезается
TAIL_MARGIN = 0.25         # и 250 мс после последнего — хвост затухает естественно


def detect_silence(files) -> tuple[float, float, float]:
    """(тишина в начале, тишина в конце, длительность) в секундах исходника.
    Звук ищется по ВСЕМ дорожкам: тишина — это когда молчат все сразу
    (если клик начинается раньше музыки, его щелчки — не тишина)."""
    first, last, duration = float("inf"), 0.0, 0.0
    thr_start = 10 ** (SILENCE_START_DB / 20)
    thr_end = 10 ** (SILENCE_END_DB / 20)
    for path in files:
        try:
            data, sr = sf.read(path, dtype="float32", always_2d=True)
        except Exception:
            continue
        n = data.shape[0]
        duration = max(duration, n / sr)
        hop = max(1, int(sr * 0.01))        # окна по 10 мс
        nb = n // hop
        if nb == 0:
            continue
        peaks = np.abs(data[:nb * hop]).max(axis=1).reshape(nb, hop).max(axis=1)
        loud = np.nonzero(peaks > thr_start)[0]
        if loud.size:
            first = min(first, loud[0] * hop / sr)
        tail = np.nonzero(peaks > thr_end)[0]
        if tail.size:
            last = max(last, (tail[-1] + 1) * hop / sr)
    if first == float("inf"):
        return 0.0, 0.0, duration           # всё молчит — ничего не предлагаем
    lead = max(0.0, first - LEAD_MARGIN)
    trail = max(0.0, duration - min(duration, last + TAIL_MARGIN))
    return round(lead, 3), round(trail, 3), duration
