"""
key_detect.py — Определение тональности песни.

Как работает (версия 2 — точнее прежнего «чистого» Крумхансла–Шмуклера)
-------------------------------------------------------------------------
1. Берём музыкальные дорожки (без клика, гайда и БАРАБАНОВ — у тарелок и бочки нет
   высоты, они только «шумят» в профиле нот), сводим в моно, понижаем до ~11 кГц.
2. Спектр по кадрам (FFT 8192, ~0.7 с), в каждом кадре оставляем только ТОНАЛЬНЫЕ пики
   (локальные максимумы заметно выше «шумового пола» кадра) — шум, дыхание, удары
   не попадают в ноты. Амплитуды сжимаем (корень), чтобы громкие ноты не забивали остальные.
3. Строй: если фонограмма сыграна не точно на A=440 (например, на +30 центов), ноты
   «съезжают» между соседними полутонами. Строй оценивается по самим пикам и
   учитывается.
4. Получаются два профиля из 12 нот: общий (70–2000 Гц) и басовый (40–250 Гц, бас
   чаще всего играет тонику), отдельно — профиль конца песни (песни обычно
   заканчиваются на тонике).
5. Профиль сравнивается с эталонами 24 тональностей — сразу с тремя наборами
   эталонов (Крумхансл–Кесслер, Темперли, Альбрехт–Шанахан), результаты
   складываются. Бас и концовка добавляют «голос» тонике — это лучше всего различает
   параллельные мажор/минор (C ↔ Am), главную ошибку простых алгоритмов.

Тональность всегда можно поправить вручную (правый клик по треку → Тональность).
"""
from __future__ import annotations

import re

import numpy as np
import soundfile as sf

from model import KEY_ALGO_VERSION, Song, is_click_like  # noqa: F401


NOTES = ["C", "C#", "D", "D#", "E", "F", "F#", "G", "G#", "A", "A#", "B"]
# Привычные музыкантам названия: мажор — Eb, Ab, Bb; минор — C#m, F#m, G#m
MAJOR_NAMES = ["C", "Db", "D", "Eb", "E", "F", "F#", "G", "Ab", "A", "Bb", "B"]
MINOR_NAMES = ["Cm", "C#m", "Dm", "Ebm", "Em", "Fm", "F#m", "Gm", "G#m", "Am", "Bbm", "Bm"]
ALL_KEYS = MAJOR_NAMES + MINOR_NAMES
_ENHARMONIC = {"Db": "C#", "Eb": "D#", "Gb": "F#", "Ab": "G#", "Bb": "A#", "Cb": "B", "Fb": "E"}

# Эталонные профили (тоника — первый элемент)
MAJOR = np.array([6.35, 2.23, 3.48, 2.33, 4.38, 4.09, 2.52, 5.19, 2.39, 3.66, 2.29, 2.88])   # Крумхансл–Кесслер
MINOR = np.array([6.33, 2.68, 3.52, 5.38, 2.60, 3.53, 2.54, 4.75, 3.98, 2.69, 3.34, 3.17])
PROFILES = [
    (MAJOR, MINOR),
    (np.array([5.0, 2.0, 3.5, 2.0, 4.5, 4.0, 2.0, 4.5, 2.0, 3.5, 1.5, 4.0]),                   # Темперли
     np.array([5.0, 2.0, 3.5, 4.5, 2.0, 4.0, 2.0, 4.5, 3.5, 2.0, 1.5, 4.0])),
    (np.array([0.238, 0.006, 0.111, 0.006, 0.137, 0.094, 0.016, 0.214, 0.009, 0.080, 0.008, 0.081]),  # Альбрехт–Шанахан
     np.array([0.220, 0.006, 0.104, 0.123, 0.019, 0.103, 0.012, 0.214, 0.062, 0.022, 0.061, 0.052])),
]
DRUMS = re.compile(r"drum|бочк|барабан|перкус|perc|kick|snare|hat|hh\b|tom\b|overhead|\boh\b|cymbal|тарелк|"
                   r"рабочий|малый|shaker|шейкер|clap|хлоп", re.IGNORECASE)

TARGET_SR = 11025
N_FFT = 8192
MAX_FRAMES = 700  # ограничиваем объём работы: кадры берутся равномерно по всей песне
BASS_WEIGHT = 0.10
EDGE_SECONDS = 2.5            # первый/последний аккорд песни
END_WEIGHT = 0.30             # песня почти всегда заканчивается на тонике…
START_WEIGHT = 0.12           # …и часто с неё начинается
END_BASS_WEIGHT = 0.10        # последняя басовая нота
CLOSE_MARGIN = 0.15           # «почти так же похоже» — второе семейство тоже рассматривается
MAJ_TRIAD = np.array([1, 0, 0, 0, 1, 0, 0, 1, 0, 0, 0, 0], dtype=float)
MIN_TRIAD = np.array([1, 0, 0, 1, 0, 0, 0, 1, 0, 0, 0, 0], dtype=float)


def _frames(mono: np.ndarray) -> np.ndarray:
    if mono.shape[0] < N_FFT:
        mono = np.pad(mono, (0, N_FFT - mono.shape[0]))
    hop = N_FFT // 2
    n_frames = (mono.shape[0] - N_FFT) // hop + 1
    starts = np.linspace(0, (n_frames - 1) * hop, min(n_frames, MAX_FRAMES)).astype(int)
    return np.stack([mono[s:s + N_FFT] for s in starts]) * np.hanning(N_FFT).astype(np.float32)


def _peaks(spec: np.ndarray) -> np.ndarray:
    """Оставить только тональные пики: локальный максимум и выше 4× медианы кадра."""
    left = np.roll(spec, 1, axis=1)
    right = np.roll(spec, -1, axis=1)
    floor = np.median(spec, axis=1, keepdims=True)
    keep = (spec > left) & (spec >= right) & (spec > 4.0 * floor + 1e-12)
    return np.where(keep, spec, 0.0)


def _tuning(freqs: np.ndarray, weights: np.ndarray) -> float:
    """Отклонение строя от A=440 в долях полутона (−0.5…0.5): круговое среднее по пикам."""
    ok = (freqs > 80) & (freqs < 2000) & (weights > 0)
    if not np.any(ok):
        return 0.0
    midi = 69 + 12 * np.log2(freqs[ok] / 440.0)
    ang = 2 * np.pi * (midi - np.round(midi))
    w = weights[ok]
    return float(np.angle(np.sum(w * np.exp(1j * ang))) / (2 * np.pi))


def _pc_profile(freqs, weights, lo, hi, tuning) -> np.ndarray:
    sel = (freqs >= lo) & (freqs <= hi) & (weights > 0)
    if not np.any(sel):
        return np.zeros(12)
    midi = 69 + 12 * np.log2(freqs[sel] / 440.0) - tuning
    dev = np.abs(midi - np.round(midi))
    w = weights[sel] * np.clip(1.0 - dev / 0.4, 0.0, 1.0)     # далеко от ноты (неточный пик) — не считаем
    pc = np.mod(np.round(midi).astype(int), 12)
    return np.bincount(pc, weights=w, minlength=12)


def analyze_chroma(mono: np.ndarray, sr: float) -> dict:
    """Профили нот песни: {"all", "bass", "end"} + {"tuning"} (строй, полутона)."""
    frames = _frames(mono)
    spec = np.sqrt(np.abs(np.fft.rfft(frames, axis=1)))          # сжатие амплитуд
    spec = spec / (spec.sum(axis=1, keepdims=True) + 1e-9)        # громкий припев не перевешивает
    pk = _peaks(spec)
    freqs = np.fft.rfftfreq(N_FFT, 1.0 / sr)
    total = pk.sum(axis=0)
    tuning = _tuning(freqs, total)
    # начало и самый конец МУЗЫКИ (тишину по краям не считаем): первый и последний аккорды
    loud = np.flatnonzero(np.abs(frames).max(axis=1) > 0.05 * (np.abs(frames).max() + 1e-9))
    first, last = (loud[0], loud[-1]) if loud.size else (0, pk.shape[0] - 1)
    span = max(1, int(round(EDGE_SECONDS * sr / (N_FFT // 2))))
    head = pk[first:first + span].sum(axis=0)
    tail = pk[max(first, last - span + 1):last + 1].sum(axis=0)
    return {"all": _pc_profile(freqs, total, 70, 2000, tuning),
            "bass": _pc_profile(freqs, total, 40, 250, tuning),
            "end": _pc_profile(freqs, tail, 40, 1000, tuning),
            "end_bass": _pc_profile(freqs, tail, 40, 200, tuning),
            "start": _pc_profile(freqs, head, 40, 1000, tuning),
            "tuning": tuning}


def chroma_profile(mono: np.ndarray, sr: float) -> np.ndarray:
    """12-мерный профиль энергии нот (совместимость)."""
    return analyze_chroma(mono, sr)["all"]


def _norm(v: np.ndarray) -> np.ndarray:
    s = v.sum()
    return v / s if s > 0 else v


def key_scores(prof: dict) -> list[tuple[float, str]]:
    """Оценка всех 24 тональностей (больше — лучше)."""
    chroma = prof["all"]
    bass = _norm(prof.get("bass", np.zeros(12)))
    end, start = prof.get("end"), prof.get("start")
    end_bass = _norm(prof.get("end_bass", np.zeros(12)))

    def triad_fit(seg, tonic, mode):
        """Насколько отрезок звучит как тоническое трезвучие (корреляция, −1…1)."""
        if seg is None or not np.any(seg):
            return 0.0
        return float(np.corrcoef(seg, np.roll(MIN_TRIAD if mode else MAJ_TRIAD, tonic))[0, 1])
    # 1) «звукоряд»: профили нот + бас — надёжно находят семейство тональности
    base = {}
    for tonic in range(12):
        for mode in (0, 1):
            r = 0.0
            for maj, mnr in PROFILES:
                r += np.corrcoef(chroma, np.roll(mnr if mode else maj, tonic))[0, 1]
            base[(tonic, mode)] = r / len(PROFILES) + BASS_WEIGHT * (bass[tonic] - 1 / 12) * 4

    # 2) мажор или параллельный минор (C ↔ Am — одни и те же ноты): решают первый и
    #    последний аккорды и последняя басовая нота. Только между этими двумя —
    #    поэтому концовка на «чужом» аккорде не может увести в другую тональность.
    def cues(tonic, mode):
        return (END_WEIGHT * triad_fit(end, tonic, mode)
                + END_BASS_WEIGHT * (end_bass[tonic] - 1 / 12) * 4
                + START_WEIGHT * triad_fit(start, tonic, mode))
    def relative(k):
        return ((k[0] + 9) % 12, 1) if k[1] == 0 else ((k[0] + 3) % 12, 0)
    ranked = sorted(base, key=base.get, reverse=True)
    top = ranked[0]
    cands = {top, relative(top)}
    # близкий второй вариант (например, вамп «Am–G–F–G» похож на G-мажор) — тоже в споре
    for k in ranked[1:3]:
        if base[top] - base[k] < CLOSE_MARGIN and k not in cands:
            cands |= {k, relative(k)}
    scored = {k: base[k] + cues(*k) for k in cands}
    winner = max(scored, key=scored.get)
    final = dict(base)
    final[winner] = max(base[top], scored[winner]) + 1e-6   # победитель — первый в списке
    out = [(float(v), (MINOR_NAMES if m else MAJOR_NAMES)[t]) for (t, m), v in final.items()]
    out.sort(reverse=True)
    return out


def key_from_chroma(chroma, extra: dict | None = None) -> tuple[str, float]:
    """(тональность, уверенность 0..1). chroma — 12 чисел или словарь из analyze_chroma()."""
    prof = chroma if isinstance(chroma, dict) else {"all": np.asarray(chroma, dtype=float)}
    if extra:
        prof = {**prof, **extra}
    if not np.any(prof["all"]):
        return "?", 0.0
    scores = key_scores(prof)
    best, second = scores[0], scores[1]
    confidence = max(0.0, min(1.0, (best[0] - second[0]) * 5 + best[0] * 0.5))
    return best[1], confidence


def music_tracks(song: Song) -> list:
    """Дорожки для тональности: без клика/гайда и без барабанов (если есть что-то кроме них)."""
    tracks = [t for t in song.tracks if not is_click_like(t.name)] or list(song.tracks)
    tonal = [t for t in tracks if not DRUMS.search(t.name or "")]
    return tonal or tracks


def detect_song_key(song: Song) -> tuple[str, float]:
    """Тональность исходника песни (без учёта транспонирования в редакторе)."""
    mix = None
    sr_out = TARGET_SR
    for t in music_tracks(song):
        try:
            data, sr = sf.read(t.file, dtype="float32", always_2d=True)
        except Exception:
            continue
        mono = data.mean(axis=1)
        factor = max(1, int(round(sr / TARGET_SR)))
        n = mono.shape[0] // factor * factor
        mono = mono[:n].reshape(-1, factor).mean(axis=1)   # понижение частоты (усреднение = фильтр)
        sr_out = sr / factor
        if mix is None:
            mix = mono
        else:
            if mono.shape[0] > mix.shape[0]:
                mono, mix = mix, mono
            mix[:mono.shape[0]] += mono
    if mix is None:
        return "?", 0.0
    return key_from_chroma(analyze_chroma(mix, sr_out))


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
