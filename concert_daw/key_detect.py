"""
key_detect.py — Определение тональности песни (алгоритм Крумхансла–Шмуклера).

Как работает
------------
1. Берём «музыкальные» дорожки песни (без клика и гайда), сводим в моно
   и понижаем частоту до ~11 кГц — для тональности этого достаточно.
2. Спектр по кадрам (FFT 8192) → энергия каждой из 12 нот (C, C#, … B)
   в диапазоне 60–2000 Гц, суммируем по всей песне = «хрома-профиль».
3. Сравниваем профиль с эталонными профилями мажора и минора для всех
   24 тональностей (корреляция). Лучшее совпадение — ответ.

Точность для поп/рок-материала обычно 70–85 %. Частые ошибки — соседние
тональности по квинте и параллельный мажор/минор (C ↔ Am). Поэтому
тональность всегда можно поправить вручную (правый клик по песне).
"""
from __future__ import annotations

import re

import numpy as np
import soundfile as sf

from model import Song, is_click_like

NOTES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
# Привычные музыкантам названия: мажор — Eb, Ab, Bb; минор — C#m, F#m, G#m
MAJOR_NAMES = ["C", "Db", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]
MINOR_NAMES = ["Cm", "C#m", "Dm", "Ebm", "Em", "Fm", "F#m", "Gm", "G#m", "Am", "Bbm", "Bm"]
ALL_KEYS = MAJOR_NAMES + MINOR_NAMES
_ENHARMONIC = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#", "Cb": "B", "Fb": "E"}

# Эталонные профили Крумхансла–Кесслер (тоника — первый элемент)
MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])
MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])

TARGET_SR = 11025
N_FFT = 8192
MAX_FRAMES = 700  # ограничиваем объём работы: кадры берутся равномерно по всей песне


def chroma_profile(mono: np.ndarray, sr: float) -> np.ndarray:
    """12-мерный профиль энергии нот."""
    if mono.shape[0] < N_FFT:
        mono = np.pad(mono, (0, N_FFT - mono.shape[0]))
    hop = N_FFT // 2
    n_frames = (mono.shape[0] - N_FFT) // hop + 1
    starts = np.linspace(0, (n_frames - 1) * hop, min(n_frames, MAX_FRAMES)).astype(int)
    frames = np.stack([mono[s:s + N_FFT] for s in starts]) * np.hanning(N_FFT)
    spec = np.abs(np.fft.rfft(frames, axis=1))
    freqs = np.fft.rfftfreq(N_FFT, 1.0 / sr)
    band = (freqs >= 60) & (freqs <= 2000)
    energy = spec[:, band]
    # каждый кадр нормируем, чтобы громкий припев не перевешивал всю песню
    energy = energy / (energy.sum(axis=1, keepdims=True) + 1e-9)
    midi = 69 + 12 * np.log2(freqs[band] / 440.0)
    pitch_class = np.mod(np.round(midi).astype(int), 12)
    return np.bincount(pitch_class, weights=energy.sum(axis=0), minlength=12)


def key_from_chroma(chroma: np.ndarray) -> tuple[str, float]:
    """(тональность, уверенность 0..1)."""
    if not np.any(chroma):
        return "?", 0.0
    scores = []
    for tonic in range(12):
        for profile, suffix in ((MAJOR, ""), (MINOR, "m")):
            r = np.corrcoef(chroma, np.roll(profile, tonic))[0, 1]
            scores.append((r, (MINOR_NAMES if suffix else MAJOR_NAMES)[tonic]))
    scores.sort(reverse=True)
    best, second = scores[0], scores[1]
    confidence = max(0.0, min(1.0, (best[0] - second[0]) * 5 + best[0] * 0.5))
    return best[1], confidence


def detect_song_key(song: Song) -> tuple[str, float]:
    """Тональность исходника песни (без учёта транспонирования в редакторе)."""
    tracks = [t for t in song.tracks if not is_click_like(t.name)] or list(song.tracks)
    mix = None
    sr_out = TARGET_SR
    for t in tracks:
        try:
            data, sr = sf.read(t.file, dtype="float32", always_2d=True)
        except Exception:
            continue
        mono = data.mean(axis=1)
        factor = max(1, int(round(sr / TARGET_SR)))
        n = mono.shape[0] // factor * factor
        mono = mono[:n].reshape(-1, factor).mean(axis=1)   # простое понижение частоты
        sr_out = sr / factor
        if mix is None:
            mix = mono
        else:
            if mono.shape[0] > mix.shape[0]:
                mono, mix = mix, mono
            mix[:mono.shape[0]] += mono
    if mix is None:
        return "?", 0.0
    return key_from_chroma(chroma_profile(mix, sr_out))


def transpose_key(key: str, semitones: float) -> str:
    """Тональность после сдвига тона в редакторе: Am +2 → Bm."""
    if not key or key == "?":
        return key
    minor = key.endswith("m")
    root = key[:-1] if minor else key
    root = _ENHARMONIC.get(root, root)
    if root not in NOTES:
        return key
    shift = int(round(semitones))
    idx = (NOTES.index(root) + shift) % 12
    new = (MINOR_NAMES if minor else MAJOR_NAMES)[idx]
    return ("≈" if abs(semitones - shift) > 0.01 else "") + new


def key_label(song: Song) -> str:
    """Подпись для списка/заголовка: «Am» или «Am → Bm» при транспонировании."""
    if not song.key:
        return ""
    if song.key == "?":
        return "тон ?"
    if song.edit.pitch:
        return f"{song.key} → {transpose_key(song.key, song.edit.pitch)}"
    return song.key


# ══════════════════════════════════════════════════════════════════════════════
# Темп (BPM) и первая доля
# ══════════════════════════════════════════════════════════════════════════════
# Если в песне есть дорожка клика — темп считается по ней почти без ошибок
# (медиана интервалов между щелчками). Иначе — автокорреляция огибающей атак
# всего микса в диапазоне 70–180 BPM; здесь возможны ошибки «вдвое» (60 ↔ 120),
# поэтому темп можно поправить вручную в редакторе песни (поле «исходный темп»).
ENV_SR = 1000  # частота огибающей: 1 мс на отсчёт


def _onset_envelope(mono: np.ndarray, sr: float) -> tuple[np.ndarray, float]:
    """Огибающая атак с шагом ~1 мс: рост энергии сигнала. Возвращает (огибающая, её точная частота)."""
    hop = max(1, int(round(sr / ENV_SR)))
    rate = sr / hop          # точная частота огибающей (44100/44 = 1002.3, а не 1000)
    n = mono.shape[0] // hop
    if n < 10:
        return np.zeros(0), rate
    energy = np.sqrt((mono[:n * hop].reshape(n, hop) ** 2).mean(axis=1))
    log_e = np.log1p(energy * 1000)
    flux = np.maximum(0.0, np.diff(log_e, prepend=log_e[0]))
    return flux, rate


def _read_mono(files: list[str]) -> tuple[np.ndarray, float]:
    mix, sr_out = None, 44100.0
    for f in files:
        try:
            data, sr = sf.read(f, dtype="float32", always_2d=True)
        except Exception:
            continue
        mono = data.mean(axis=1)
        sr_out = float(sr)
        if mix is None:
            mix = mono.copy()
        else:
            if mono.shape[0] > mix.shape[0]:
                mono, mix = mix, mono.copy()
            mix[:mono.shape[0]] += mono
    return (mix if mix is not None else np.zeros(0, np.float32)), sr_out


def tempo_from_clicks(env: np.ndarray, rate: float = ENV_SR) -> tuple[float, float]:
    """По дорожке клика: (BPM, время первого щелчка в сек)."""
    if env.size == 0 or env.max() <= 0:
        return 0.0, 0.0
    thr = env.max() * 0.3
    min_gap = int(0.15 * rate)  # не быстрее 400 BPM
    onsets, last = [], -min_gap
    for i in np.nonzero(env > thr)[0]:
        if i - last >= min_gap:
            onsets.append(i)
            last = i
    if len(onsets) < 4:
        return 0.0, 0.0
    onsets = np.array(onsets, dtype=np.float64)
    period = float(np.median(np.diff(onsets)))
    # Уточнение: номер доли каждого щелчка (паузы в клике не мешают) и прямая
    # «время = первая доля + номер·период» по всем щелчкам — точность до сотых BPM.
    beats = np.round((onsets - onsets[0]) / period)
    if beats[-1] > 0:
        period, first = np.polyfit(beats, onsets, 1)
    else:
        first = onsets[0]
    return 60.0 * rate / period, max(0.0, float(first)) / rate


def tempo_from_mix(env: np.ndarray, rate: float = ENV_SR) -> tuple[float, float]:
    """По миксу: автокорреляция огибающей. (BPM, сдвиг первой доли в сек)."""
    if env.size < rate * 4:
        return 0.0, 0.0
    env = env - env.mean()
    lo, hi = int(60 * rate / 180), int(60 * rate / 70)
    n = env.size
    spec = np.fft.rfft(env, 2 * n)
    ac = np.fft.irfft(spec * np.conj(spec))[:hi + 2]
    lags = np.arange(lo, hi + 1)
    # лёгкое предпочтение «типичным» темпам 90–140, против ошибок вдвое
    weight = np.exp(-0.5 * (np.log2(60 * rate / lags / 115.0) / 0.9) ** 2)
    best = int(lags[np.argmax(ac[lo:hi + 1] * weight)])
    # параболическая интерполяция для точности долей BPM
    a, b, c = ac[best - 1], ac[best], ac[best + 1]
    shift = 0.5 * (a - c) / (a - 2 * b + c) if (a - 2 * b + c) != 0 else 0.0
    period = best + shift
    phases = [env[int(p)::best].sum() for p in range(best)]
    return 60.0 * rate / period, int(np.argmax(phases)) / rate


def detect_song_tempo(song: Song) -> tuple[float, float]:
    """(BPM исходника, время первой доли в исходнике); (0, 0) — не удалось."""
    clicks = [t.file for t in song.tracks if re.search(r"click|клик|metronom|метроном", t.name.lower())]
    if clicks:
        mono, sr = _read_mono(clicks[:1])
        bpm, off = tempo_from_clicks(*_onset_envelope(mono, sr))
        if bpm:
            return round(bpm, 2), off
    music = [t.file for t in song.tracks if not is_click_like(t.name)] or [t.file for t in song.tracks]
    mono, sr = _read_mono(music)
    bpm, off = tempo_from_mix(*_onset_envelope(mono, sr))
    return round(bpm, 1), off


def tempo_label(song: Song) -> str:
    bpm = song.play_bpm
    return f"{bpm:.0f} BPM" if bpm >= 1 else ""


def measure_song_loudness(song: Song) -> tuple[float, float]:
    """(LUFS, пик) музыки трека — без клика и гайда, как её услышит зал.
    Моно-дорожки ставятся по центру (−3 dB в каждый канал), стерео — как есть."""
    from loudness import integrated_lufs
    music = [t for t in song.tracks if not is_click_like(t.name)] or list(song.tracks)
    mix, sr0 = None, None
    for t in music:
        try:
            data, sr = sf.read(t.file, dtype="float32", always_2d=True)
        except Exception:
            continue
        if sr0 is None:
            sr0 = sr
        elif sr != sr0:
            continue  # другая частота — в оценку громкости не берём
        if data.shape[1] == 1:
            st = np.repeat(data * 0.7071, 2, axis=1)
        else:
            st = data[:, :2]
        if mix is None:
            mix = st.copy()
        else:
            if st.shape[0] > mix.shape[0]:
                mix = np.pad(mix, ((0, st.shape[0] - mix.shape[0]), (0, 0)))
            mix[:st.shape[0]] += st
    if mix is None:
        return -99.0, 0.0
    lufs = integrated_lufs(mix, sr0)
    return (lufs if np.isfinite(lufs) else -99.0), float(np.abs(mix).max())


def analyze_song(song_dict: dict, need_key: bool, need_bpm: bool,
                 need_lufs: bool = False, need_silence: bool = False) -> tuple:
    """Для запуска в процессе-работнике:
    (тональность, BPM, первая доля, LUFS, пик, тишина в начале, тишина в конце, длительность)."""
    song = Song.from_dict(song_dict)
    key, bpm, offset, lufs, peak = song.key, song.bpm, song.beat_offset, song.lufs, song.peak
    lead, trail, duration = song.silence_lead, song.silence_trail, song.duration
    if need_silence:
        try:
            from audio_edit import detect_silence
            lead, trail, duration = detect_silence([t.file for t in song.tracks])
        except Exception:
            lead, trail = 0.0, 0.0
    if need_key:
        try:
            key, _ = detect_song_key(song)
        except Exception:
            key = "?"
    if need_bpm:
        try:
            bpm, offset = detect_song_tempo(song)
        except Exception:
            bpm, offset = 0.0, 0.0
        bpm = bpm or -1.0          # -1 — не удалось, больше не пытаемся
    if need_lufs:
        try:
            lufs, peak = measure_song_loudness(song)
        except Exception:
            lufs, peak = -99.0, 0.0
    # обычные числа Python: numpy 2 может вернуть float32, а его не сохранить в JSON
    return (str(key), float(bpm), float(offset), float(lufs), float(peak),
            float(lead), float(trail), float(duration))
