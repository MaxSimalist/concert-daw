"""
dsp.py — Встроенная обработка дорожки: параметрический эквалайзер и многополосный компрессор.

Зачем свои, если есть VST
-------------------------
Для записанного голоса нужен быстрый «минимум» прямо на фейдере, без поиска
и загрузки плагинов: срезать гул, убрать бубнение, добавить разборчивости и
выровнять громкость. Обработка работает в реальном времени в аудио-коллбэке,
крутить ручки можно во время игры.

Как устроено
------------
* Фильтры — биквады по формулам RBJ (Audio EQ Cookbook), считаются функцией
  scipy.signal.sosfilt (код на C) с сохранением состояния между блоками —
  поэтому без щелчков на стыках блоков.
* Эквалайзер: срез низа (HPF 24 дБ/окт) + 4 полосы (полка НЧ / колокол / полка ВЧ,
  тип каждой полосы можно сменить).
* Многополосный компрессор: 3 полосы, разделение фильтрами Линквица–Райли 4-го
  порядка (НЧ+ВЧ складываются без провалов АЧХ). Верхняя полоса проходит через
  фазовый фильтр нижнего раздела, поэтому при выключенном сжатии сумма полос
  звучит так же, как вход (АЧХ ровная).
* Детектор компрессора — пиковый, общий для каналов стерео, считается кусочками
  по 32 сэмпла (0.7 мс при 48 кГц); атака/восстановление — по этим кусочкам,
  между ними усиление плавно интерполируется — ни ступенек, ни лишней нагрузки.
* Настройки меняются из GUI-потока заменой ссылок на массивы коэффициентов —
  аудиопоток никогда не видит «половину» изменения.
"""
from __future__ import annotations

import copy
import importlib
import math
import weakref
from typing import Optional

import numpy as np

try:
    from scipy.signal import sosfilt
    DSP_AVAILABLE = True
    DSP_ERROR = ""
except Exception as exc:  # pragma: no cover — зависит от окружения
    sosfilt = None
    DSP_AVAILABLE = False
    DSP_ERROR = f"нужен пакет scipy (pip install scipy): {exc}"

_INSTANCES: "weakref.WeakSet" = weakref.WeakSet()   # все TrackFX — чтобы «оживить» их после установки scipy


def try_enable() -> bool:
    """Повторная попытка подключить scipy (например, сразу после установки из окна плагина).
    Успех — все уже созданные обработчики дорожек начинают работать без перезапуска."""
    global sosfilt, DSP_AVAILABLE, DSP_ERROR
    if DSP_AVAILABLE:
        return True
    try:
        importlib.invalidate_caches()
        sosfilt = importlib.import_module("scipy.signal").sosfilt
    except Exception as exc:
        DSP_ERROR = f"нужен пакет scipy (pip install scipy): {exc}"
        return False
    DSP_AVAILABLE, DSP_ERROR = True, ""
    for fx in list(_INSTANCES):
        try:
            fx.update(fx.settings)
        except Exception:
            pass
    return True


UNITY = np.array([1.0, 0.0, 0.0, 1.0, 0.0, 0.0])
BAND_TYPES = {"lowshelf": "Полка НЧ", "peak": "Колокол", "highshelf": "Полка ВЧ"}
SUB = 32                     # шаг детектора компрессора, сэмплов
RING = 8192                  # буфер анализатора спектра, сэмплов
KNEE_DB = 6.0                # мягкое колено


# ══════════════════════════════════════════════════════════════════════════════
# Настройки по умолчанию и пресеты
# ══════════════════════════════════════════════════════════════════════════════
def default_eq() -> dict:
    return {"on": False, "hpf_on": True, "hpf": 80.0, "out": 0.0,
            "bands": [{"type": "lowshelf", "freq": 100.0, "gain": 0.0, "q": 0.71},
                      {"type": "peak", "freq": 350.0, "gain": 0.0, "q": 1.0},
                      {"type": "peak", "freq": 3000.0, "gain": 0.0, "q": 1.0},
                      {"type": "highshelf", "freq": 10000.0, "gain": 0.0, "q": 0.71}]}


def default_comp() -> dict:
    band = {"on": True, "solo": False, "thr": -20.0, "ratio": 2.5, "att": 15.0, "rel": 150.0, "gain": 0.0}
    return {"on": False, "xover": [250.0, 3000.0], "out": 0.0, "mix": 100.0,
            "bands": [dict(band) for _ in range(3)]}


def _eq(hpf, bands, hpf_on=True):
    e = default_eq()
    e["on"], e["hpf_on"], e["hpf"] = True, hpf_on, hpf
    for b, (typ, f, g, q) in zip(e["bands"], bands):
        b.update(type=typ, freq=f, gain=g, q=q)
    return e


def _comp(xover, bands, out=0.0):
    c = default_comp()
    c["on"], c["xover"], c["out"] = True, list(xover), out
    for b, (on, thr, ratio, att, rel, gain) in zip(c["bands"], bands):
        b.update(on=on, thr=thr, ratio=ratio, att=att, rel=rel, gain=gain)
    return c


EQ_PRESETS: dict[str, dict] = {
    "Ровно (сброс)": default_eq(),
    "Вокал": _eq(90, [("lowshelf", 150, -1.5, 0.71), ("peak", 320, -2.5, 1.2),
                     ("peak", 3500, 2.5, 1.0), ("highshelf", 10000, 2.0, 0.71)]),
    "Вокал — убрать гул и бубнение": _eq(120, [("lowshelf", 180, -3.0, 0.71), ("peak", 250, -4.0, 1.4),
                                              ("peak", 4000, 1.5, 1.0), ("highshelf", 12000, 1.0, 0.71)]),
    "Речь (ведущий)": _eq(100, [("lowshelf", 180, -2.0, 0.71), ("peak", 400, -1.5, 1.0),
                               ("peak", 2800, 2.0, 1.0), ("highshelf", 8000, 1.0, 0.71)]),
    "Хор / ансамбль": _eq(100, [("lowshelf", 200, -1.0, 0.71), ("peak", 500, -1.5, 0.9),
                               ("peak", 3000, 1.5, 0.8), ("highshelf", 11000, 2.5, 0.71)]),
    "Баян / гармонь": _eq(70, [("lowshelf", 120, 0.0, 0.71), ("peak", 600, -2.0, 1.0),
                              ("peak", 2500, 1.5, 1.0), ("highshelf", 9000, 1.0, 0.71)]),
    "Акустическая гитара": _eq(80, [("lowshelf", 150, -1.0, 0.71), ("peak", 220, -2.5, 1.2),
                                   ("peak", 3000, 1.5, 1.0), ("highshelf", 10000, 2.0, 0.71)]),
}

COMP_PRESETS: dict[str, dict] = {
    "Выключено (сброс)": default_comp(),
    "Вокал — мягко": _comp((200, 3000), [(True, -28, 2.5, 25, 160, 1.5), (True, -22, 2.5, 12, 120, 2.5),
                                         (True, -26, 3.0, 3, 80, 1.0)]),
    "Вокал — плотно": _comp((220, 3200), [(True, -32, 4.0, 20, 140, 3.0), (True, -26, 4.0, 8, 100, 4.5),
                                          (True, -30, 4.0, 2, 70, 2.0)]),
    "Де-эссер (свистящие)": _comp((300, 5500), [(False, 0, 1, 10, 100, 0), (False, 0, 1, 10, 100, 0),
                                                (True, -32, 6.0, 1, 60, 0.0)]),
    "Речь — выровнять": _comp((180, 2800), [(True, -30, 3.0, 20, 180, 2.0), (True, -26, 3.5, 10, 140, 4.0),
                                            (True, -28, 3.0, 4, 90, 2.0)]),
    "Мастер — склеить": _comp((150, 4000), [(True, -18, 1.8, 30, 200, 0.5), (True, -18, 1.6, 20, 180, 0.5),
                                            (True, -20, 1.8, 10, 150, 0.5)]),
}


def normalize(settings: Optional[dict]) -> dict:
    """Настройки из конфига → полный словарь {"eq": …, "comp": …} (недостающее — по умолчанию)."""
    settings = settings or {}
    eq, comp = default_eq(), default_comp()
    src_eq, src_comp = settings.get("eq") or {}, settings.get("comp") or {}
    for k in ("on", "hpf_on", "hpf", "out"):
        if k in src_eq:
            eq[k] = src_eq[k]
    for dst, src in zip(eq["bands"], src_eq.get("bands") or []):
        dst.update({k: src[k] for k in dst if k in src})
    for k in ("on", "out", "mix"):
        if k in src_comp:
            comp[k] = src_comp[k]
    if len(src_comp.get("xover") or []) == 2:
        comp["xover"] = [float(x) for x in src_comp["xover"]]
    for dst, src in zip(comp["bands"], src_comp.get("bands") or []):
        dst.update({k: src[k] for k in dst if k in src})
    return {"eq": eq, "comp": comp}


def vocal_defaults() -> dict:
    """Обработка, которая сразу включается на фейдере записанного голоса."""
    return {"eq": copy.deepcopy(EQ_PRESETS["Вокал"]), "comp": copy.deepcopy(COMP_PRESETS["Вокал — мягко"])}


# ══════════════════════════════════════════════════════════════════════════════
# Коэффициенты фильтров (RBJ Audio EQ Cookbook)
# ══════════════════════════════════════════════════════════════════════════════
def _norm(b0, b1, b2, a0, a1, a2) -> np.ndarray:
    return np.array([b0 / a0, b1 / a0, b2 / a0, 1.0, a1 / a0, a2 / a0])


def _w0(freq: float, sr: float) -> float:
    return 2.0 * math.pi * min(max(float(freq), 10.0), 0.45 * sr) / sr


def biquad(kind: str, freq: float, sr: float, gain_db: float = 0.0, q: float = 0.7071) -> np.ndarray:
    w0 = _w0(freq, sr)
    cw, sw = math.cos(w0), math.sin(w0)
    q = max(0.1, float(q))
    alpha = sw / (2.0 * q)
    if kind == "hpf":
        return _norm((1 + cw) / 2, -(1 + cw), (1 + cw) / 2, 1 + alpha, -2 * cw, 1 - alpha)
    if kind == "lpf":
        return _norm((1 - cw) / 2, 1 - cw, (1 - cw) / 2, 1 + alpha, -2 * cw, 1 - alpha)
    if abs(gain_db) < 1e-6:
        return UNITY.copy()
    A = 10.0 ** (gain_db / 40.0)
    if kind == "peak":
        return _norm(1 + alpha * A, -2 * cw, 1 - alpha * A, 1 + alpha / A, -2 * cw, 1 - alpha / A)
    sa = 2.0 * math.sqrt(A) * alpha
    if kind == "lowshelf":
        return _norm(A * ((A + 1) - (A - 1) * cw + sa), 2 * A * ((A - 1) - (A + 1) * cw),
                     A * ((A + 1) - (A - 1) * cw - sa), (A + 1) + (A - 1) * cw + sa,
                     -2 * ((A - 1) + (A + 1) * cw), (A + 1) + (A - 1) * cw - sa)
    if kind == "highshelf":
        return _norm(A * ((A + 1) + (A - 1) * cw + sa), -2 * A * ((A - 1) + (A + 1) * cw),
                     A * ((A + 1) + (A - 1) * cw - sa), (A + 1) - (A - 1) * cw + sa,
                     2 * ((A - 1) - (A + 1) * cw), (A + 1) - (A - 1) * cw - sa)
    raise ValueError(kind)


def eq_sos(eq: dict, sr: float) -> np.ndarray:
    """6 секций всегда (HPF — 2 секции Баттерворта 4-го порядка, + 4 полосы):
    форма массива не меняется, и состояние фильтров переживает любые правки."""
    rows = []
    if eq.get("hpf_on"):
        rows += [biquad("hpf", eq["hpf"], sr, q=0.5412), biquad("hpf", eq["hpf"], sr, q=1.3066)]
    else:
        rows += [UNITY.copy(), UNITY.copy()]
    for b in eq["bands"]:
        rows.append(biquad(b["type"], b["freq"], sr, b["gain"], b["q"]))
    return np.vstack(rows)


def lr4(kind: str, freq: float, sr: float) -> np.ndarray:
    """Фильтр Линквица–Райли 4-го порядка = два одинаковых биквада Баттерворта."""
    s = biquad(kind, freq, sr, q=0.7071)
    return np.vstack([s, s])


def sos_response(sos: np.ndarray, freqs: np.ndarray, sr: float) -> np.ndarray:
    """АЧХ каскада биквадов в dB на частотах freqs (для графика эквалайзера)."""
    z = np.exp(-1j * 2 * np.pi * np.asarray(freqs, dtype=float) / sr)
    h = np.ones_like(z)
    for b0, b1, b2, _a0, a1, a2 in sos:
        h *= (b0 + b1 * z + b2 * z * z) / (1 + a1 * z + a2 * z * z)
    return 20 * np.log10(np.maximum(np.abs(h), 1e-9))


def eq_response(eq: dict, freqs: np.ndarray, sr: float = 48000.0) -> np.ndarray:
    return sos_response(eq_sos(eq, sr), freqs, sr)


def comp_curve(level_db: np.ndarray, thr: float, ratio: float) -> np.ndarray:
    """Статическая кривая: насколько dB убавить при уровне level_db (мягкое колено 6 dB)."""
    over = np.asarray(level_db, dtype=float) - thr
    slope = 1.0 - 1.0 / max(1.0, ratio)
    w = KNEE_DB
    knee = slope * (over + w / 2) ** 2 / (2 * w)
    return np.where(over <= -w / 2, 0.0, np.where(over >= w / 2, over * slope, knee))


# ══════════════════════════════════════════════════════════════════════════════
# Обработчики
# ══════════════════════════════════════════════════════════════════════════════
class _Sos:
    """Каскад биквадов с памятью между блоками."""

    def __init__(self, sos: np.ndarray, channels: int):
        self.sos = sos
        self.channels = channels
        self.zi = np.zeros((sos.shape[0], 2, channels))

    def set(self, sos: np.ndarray) -> None:
        if sos.shape != self.sos.shape:
            self.zi = np.zeros((sos.shape[0], 2, self.channels))
        self.sos = sos                         # атомарная замена ссылки

    def reset(self) -> None:
        self.zi = np.zeros_like(self.zi)

    def __call__(self, x: np.ndarray) -> np.ndarray:
        y, self.zi = sosfilt(self.sos, x, axis=0, zi=self.zi)
        return y


class ParametricEQ:
    def __init__(self, sr: float, channels: int):
        self.sr = sr
        self.filt = _Sos(eq_sos(default_eq(), sr), channels)
        self.out_gain = 1.0

    def configure(self, eq: dict) -> None:
        self.filt.set(eq_sos(eq, self.sr))
        self.out_gain = 10.0 ** (float(eq.get("out", 0.0)) / 20.0)

    def reset(self) -> None:
        self.filt.reset()

    def process(self, x: np.ndarray) -> np.ndarray:
        y = self.filt(x)
        if self.out_gain != 1.0:
            y *= self.out_gain
        return y


class MultibandComp:
    """3 полосы: НЧ | СЧ | ВЧ. Разделы f1 < f2."""

    def __init__(self, sr: float, channels: int):
        self.sr = sr
        f1, f2 = default_comp()["xover"]
        self.lp2, self.hp2 = _Sos(lr4("lpf", f2, sr), channels), _Sos(lr4("hpf", f2, sr), channels)
        self.lp1, self.hp1 = _Sos(lr4("lpf", f1, sr), channels), _Sos(lr4("hpf", f1, sr), channels)
        # фазовая коррекция верхней полосы: тот же раздел f1 (НЧ+ВЧ = фазовый фильтр)
        self.ap_lp1, self.ap_hp1 = _Sos(lr4("lpf", f1, sr), channels), _Sos(lr4("hpf", f1, sr), channels)
        self.params: tuple = ()
        self.solo = (True, True, True)           # какие полосы слышно (кнопки S в окне)
        self.mix = 1.0                           # параллельная компрессия: доля обработанного
        self.out_gain = 1.0
        self.gr = [0.0, 0.0, 0.0]               # текущее ослабление по полосам, dB (состояние детектора)
        self.gr_meter = [0.0, 0.0, 0.0]          # максимум с прошлого чтения GUI
        self.configure(default_comp())

    def configure(self, comp: dict) -> None:
        f1, f2 = sorted(float(x) for x in comp["xover"])
        f1 = max(40.0, min(f1, 0.4 * self.sr))
        f2 = max(f1 * 1.5, min(f2, 0.45 * self.sr))
        sr = self.sr
        self.lp2.set(lr4("lpf", f2, sr)); self.hp2.set(lr4("hpf", f2, sr))
        self.lp1.set(lr4("lpf", f1, sr)); self.hp1.set(lr4("hpf", f1, sr))
        self.ap_lp1.set(lr4("lpf", f1, sr)); self.ap_hp1.set(lr4("hpf", f1, sr))
        params = []
        for b in comp["bands"]:
            att = math.exp(-SUB / (max(0.1, float(b["att"])) * 1e-3 * sr))
            rel = math.exp(-SUB / (max(1.0, float(b["rel"])) * 1e-3 * sr))
            params.append((bool(b["on"]), float(b["thr"]), max(1.0, float(b["ratio"])), att, rel,
                           10.0 ** (float(b["gain"]) / 20.0)))
        solo = tuple(bool(b.get("solo")) for b in comp["bands"])
        self.solo = solo if any(solo) else (True, True, True)
        self.mix = min(1.0, max(0.0, float(comp.get("mix", 100.0)) / 100.0))
        self.params = tuple(params)              # атомарная замена
        self.out_gain = 10.0 ** (float(comp.get("out", 0.0)) / 20.0)

    def reset(self) -> None:
        for f in (self.lp2, self.hp2, self.lp1, self.hp1, self.ap_lp1, self.ap_hp1):
            f.reset()
        self.gr = [0.0, 0.0, 0.0]

    def take_gr(self) -> list[float]:
        """Для индикаторов GUI: максимальное ослабление каждой полосы с прошлого вызова."""
        out = list(self.gr_meter)
        self.gr_meter = [0.0, 0.0, 0.0]
        return out

    def _band(self, i: int, x: np.ndarray, p: tuple) -> np.ndarray:
        on, thr, ratio, att, rel, makeup = p
        n = x.shape[0]
        if not on:
            self.gr[i] = 0.0
            return x * makeup if makeup != 1.0 else x
        starts = np.arange(0, n, SUB)
        peak = np.maximum.reduceat(np.abs(x).max(axis=1), starts)
        level = 20.0 * np.log10(np.maximum(peak, 1e-9))
        target = comp_curve(level, thr, ratio)
        g = prev = self.gr[i]
        smoothed = np.empty(len(target))
        for k, t in enumerate(target.tolist()):          # 8 шагов на блок 256 — дёшево
            g = t + (g - t) * (att if t > g else rel)
            smoothed[k] = g
        self.gr[i] = g
        if g > self.gr_meter[i] or smoothed.max() > self.gr_meter[i]:
            self.gr_meter[i] = float(smoothed.max())
        # плавно между точками детектора, начиная с последнего значения прошлого блока
        xp = np.concatenate(([-1.0], starts + (SUB - 1)))
        fp = np.concatenate(([prev], smoothed))
        gr_db = np.interp(np.arange(n), xp, fp)
        gain = makeup * np.power(10.0, -gr_db / 20.0)
        return x * gain[:, None]

    def process(self, x: np.ndarray) -> np.ndarray:
        low_all = self.lp2(x)
        high = self.hp2(x)
        low = self.lp1(low_all)
        mid = self.hp1(low_all)
        high = self.ap_lp1(high) + self.ap_hp1(high)
        p, solo, mix = self.params, self.solo, self.mix
        bands = (low, mid, high)
        out = None
        dry = None
        for i in range(3):
            wet = self._band(i, bands[i], p[i])            # детектор работает и у заглушённых соло
            if not solo[i]:
                continue
            out = wet if out is None else out + wet
            if mix < 1.0:
                dry = bands[i] if dry is None else dry + bands[i]
        if out is None:
            out = np.zeros_like(low)
        if mix < 1.0 and dry is not None:
            # «сухой» сигнал — та же сумма полос без сжатия: фаза совпадает, без гребёнки
            out = out * mix + dry * (1.0 - mix)
        if self.out_gain != 1.0:
            out *= self.out_gain
        return out


class TrackFX:
    """Встроенная обработка одной дорожки: EQ → многополосный компрессор."""

    def __init__(self, settings: Optional[dict], sr: float, channels: int):
        self.sr = float(sr)
        self.channels = max(1, int(channels))
        self.failed = ""
        self.eq_on = self.comp_on = False
        self.eq = self.comp = None
        self.settings = normalize(settings)
        # индикаторы и анализатор спектра для окон плагинов (пишет аудиопоток)
        self.analyze = False                       # окно плагина открыто — копируем звук для спектра
        self.ring_pre = np.zeros(RING, np.float32)
        self.ring_post = np.zeros(RING, np.float32)
        self.ring_pos = 0
        self.peak_in = 0.0
        self.peak_out = 0.0
        self.update(self.settings)
        _INSTANCES.add(self)

    @property
    def active(self) -> bool:
        return (DSP_AVAILABLE and self.eq is not None and not self.failed
                and (self.eq_on or self.comp_on))

    def update(self, settings: dict) -> None:
        """Из GUI-потока: новые настройки вступают в силу со следующего блока."""
        self.settings = normalize(settings)
        if not DSP_AVAILABLE:
            return
        if self.eq is None:                       # scipy появился только что — создаём обработчики
            self.comp = MultibandComp(self.sr, self.channels)
            self.eq = ParametricEQ(self.sr, self.channels)
        eq, comp = self.settings["eq"], self.settings["comp"]
        self.eq.configure(eq)
        self.comp.configure(comp)
        if eq["on"] and not self.eq_on:
            self.eq.reset()                      # не тянуть «хвост» старого состояния
        if comp["on"] and not self.comp_on:
            self.comp.reset()
        self.eq_on, self.comp_on = bool(eq["on"]), bool(comp["on"])

    def reset(self) -> None:
        if self.eq is not None:
            self.eq.reset()
            self.comp.reset()

    def take_gr(self) -> list[float]:
        return self.comp.take_gr() if self.comp is not None else [0.0, 0.0, 0.0]

    def take_peaks(self) -> tuple[float, float]:
        """Пики входа/выхода обработки с прошлого вызова (для индикаторов окна плагина)."""
        out = (self.peak_in, self.peak_out)
        self.peak_in = self.peak_out = 0.0
        return out

    def recent(self, n: int = 4096) -> tuple[np.ndarray, np.ndarray]:
        """Последние n сэмплов до и после обработки (моно) — для анализатора спектра."""
        n = min(n, RING)
        pos = self.ring_pos
        idx = (np.arange(pos - n, pos)) % RING
        return self.ring_pre[idx], self.ring_post[idx]

    def _tap(self, ring: np.ndarray, x: np.ndarray, pos: int) -> None:
        mono = x[:, 0] if x.shape[1] == 1 else x.mean(axis=1)
        n = min(len(mono), RING)
        mono = mono[-n:]
        end = pos + n
        if end <= RING:
            ring[pos:end] = mono
        else:
            k = RING - pos
            ring[pos:] = mono[:k]
            ring[:end - RING] = mono[k:]

    # ── АУДИО-ПОТОК ─────────────────────────────────────────────────────────
    def process(self, block: np.ndarray) -> np.ndarray:
        """block: (frames, channels) float32 → массив той же формы (float32)."""
        try:
            y = block
            if self.eq_on:
                y = self.eq.process(y)
            if self.comp_on:
                y = self.comp.process(y)
            y = y.astype(np.float32, copy=False)
            if self.analyze:                       # только пока открыто окно плагина
                pin = float(np.abs(block).max()) if block.size else 0.0
                pout = float(np.abs(y).max()) if y.size else 0.0
                if pin > self.peak_in:
                    self.peak_in = pin
                if pout > self.peak_out:
                    self.peak_out = pout
                pos = self.ring_pos
                self._tap(self.ring_pre, block, pos)
                self._tap(self.ring_post, y, pos)
                self.ring_pos = (pos + min(block.shape[0], RING)) % RING
            return y
        except Exception as exc:           # никогда не роняем аудиопоток — байпас
            self.failed = str(exc)
            return block
