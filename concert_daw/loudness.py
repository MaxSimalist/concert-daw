"""
loudness.py — Измерение громкости трека в LUFS (ITU-R BS.1770-4 / EBU R128).

Зачем
-----
Треки в сете сведены по-разному: один громкий мастер, другой тихий. Нормализация
выравнивает их по воспринимаемой громкости: каждый трек получает поправку
«целевой уровень − измеренный уровень» (например −16 − (−11.4) = −4.6 dB).
Поправка применяется только к музыкальным дорожкам; клик и гайд не трогаются.

Как считается (без scipy, только numpy)
---------------------------------------
* K-фильтр (полка +4 dB на ВЧ и срез НЧ 38 Гц) применяется в частотной области:
  энергия блока = Σ |X(f)|²·|H(f)|² (теорема Парсеваля). Для измерения энергии
  это эквивалентно фильтрации во времени с точностью до долей дБ.
* Блоки 400 мс с перекрытием 75 %, абсолютный порог −70 LUFS, относительный −10 LU.
* Каналы L и R складываются с весом 1 (как в стандарте для стерео).
"""
from __future__ import annotations

import math

import numpy as np


def _biquad_mag2(b, a, w: np.ndarray) -> np.ndarray:
    z = np.exp(-1j * w)
    num = b[0] + b[1] * z + b[2] * z * z
    den = a[0] + a[1] * z + a[2] * z * z
    return np.abs(num / den) ** 2


def k_weighting_mag2(freqs: np.ndarray, sr: float) -> np.ndarray:
    """|H(f)|² K-фильтра BS.1770 для любой частоты дискретизации
    (та же схема расчёта коэффициентов, что в эталонной библиотеке libebur128;
    на 48 кГц совпадает с коэффициентами из текста стандарта)."""
    w = 2 * np.pi * freqs / sr
    # 1) high-shelf ≈ +4 dB выше ~1.7 кГц (модель головы)
    f0, G, Q = 1681.974450955533, 3.999843853973347, 0.7071752369554196
    K = math.tan(math.pi * f0 / sr)
    Vh = 10.0 ** (G / 20.0)
    Vb = Vh ** 0.4996667741545416
    a0 = 1.0 + K / Q + K * K
    b = [(Vh + Vb * K / Q + K * K) / a0, 2.0 * (K * K - Vh) / a0, (Vh - Vb * K / Q + K * K) / a0]
    a = [1.0, 2.0 * (K * K - 1.0) / a0, (1.0 - K / Q + K * K) / a0]
    shelf = _biquad_mag2(b, a, w)
    # 2) high-pass ~38 Гц (RLB)
    f0, Q = 38.13547087602444, 0.5003270373238773
    K = math.tan(math.pi * f0 / sr)
    d = 1.0 + K / Q + K * K
    hp = _biquad_mag2([1.0, -2.0, 1.0], [1.0, 2.0 * (K * K - 1.0) / d, (1.0 - K / Q + K * K) / d], w)
    return shelf * hp


def integrated_lufs(audio: np.ndarray, sr: float) -> float:
    """audio: (frames, channels) float. Возвращает LUFS; −inf для тишины."""
    if audio.ndim == 1:
        audio = audio[:, None]
    block = int(round(0.4 * sr))
    hop = int(round(0.1 * sr))
    n = audio.shape[0]
    if n < block:
        audio = np.pad(audio, ((0, block - n), (0, 0)))
        n = block
    starts = np.arange(0, n - block + 1, hop)
    nfft = 1 << (block - 1).bit_length()
    weight = k_weighting_mag2(np.fft.rfftfreq(nfft, 1.0 / sr), sr)
    # двусторонний спектр: удваиваем все бины, кроме 0 и Найквиста
    dup = np.full(weight.shape, 2.0)
    dup[0] = 1.0
    if nfft % 2 == 0:
        dup[-1] = 1.0
    weight = weight * dup
    z = np.zeros(len(starts))
    for ch in range(audio.shape[1]):
        x = audio[:, ch].astype(np.float32)
        # кадры порциями, чтобы не держать в памяти все сразу
        for i in range(0, len(starts), 256):
            idx = starts[i:i + 256]
            frames = np.stack([x[s:s + block] for s in idx])
            spec = np.fft.rfft(frames, nfft, axis=1)
            power = (np.abs(spec) ** 2 * weight).sum(axis=1) / (nfft * block)
            z[i:i + len(idx)] += power
    with np.errstate(divide="ignore"):
        loud = -0.691 + 10 * np.log10(z)
    gated = z[loud > -70.0]
    if gated.size == 0:
        return float("-inf")
    rel = -0.691 + 10 * np.log10(gated.mean()) - 10.0
    gated2 = z[(loud > -70.0) & (loud > rel)]
    if gated2.size == 0:
        return float("-inf")
    return float(-0.691 + 10 * np.log10(gated2.mean()))


def normalization_gain_db(lufs: float, peak: float, target: float, max_boost: float) -> float:
    """Поправка к громкости трека: к цели, но не громче max_boost и без клиппинга по пику."""
    if not math.isfinite(lufs):
        return 0.0
    g = max(-24.0, min(max_boost, target - lufs))
    if peak > 0:
        g = min(g, 20 * math.log10(0.99 / peak))   # запас до 0 dBFS по пиковому уровню
    return g
