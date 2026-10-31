"""
recorder.py — Многоканальная запись с пульта во время воспроизведения.

Как устроено
------------
* С X32 (ASIO): аудиопоток открыт как ДУПЛЕКС — один и тот же коллбэк
  отдаёт дорожки на выходы карты X-USB и получает её входы (до 32 каналов).
  Отдельный поток записи на тот же ASIO-драйвер открыть нельзя: ASIO
  допускает одного клиента, поэтому вход и выход — в одном потоке.
* Без пульта: запись идёт с обычного входа звуковой карты (свой InputStream).
* В аудио-коллбэке — только копия нужных каналов в очередь (микросекунды).
  Запись на диск делает отдельный поток пачками по ~0.25 с, поэтому медленный
  диск не вызывает щелчков на выходе — в худшем случае растёт очередь.
* WAV 24 бит, по моно-файлу на канал (Ch01.wav … Ch32.wav — сразу в DAW)
  или один многоканальный файл RF64 (без ограничения 4 ГБ).
"""
from __future__ import annotations

import json
import re
import queue
import shutil
import threading
import time
from pathlib import Path
from typing import Optional

import numpy as np
import sounddevice as sd
import soundfile as sf


def parse_channels(spec: str, available: int) -> list[int]:
    """«1-8, 17, 18» → [0..7, 16, 17] (0-based), только существующие входы."""
    result: list[int] = []
    for part in spec.replace(";", ",").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            if "-" in part:
                a, b = (int(x) for x in part.split("-", 1))
                result.extend(range(min(a, b), max(a, b) + 1))
            else:
                result.append(int(part))
        except ValueError:
            continue
    seen = []
    for ch in result:
        if 1 <= ch <= available and ch - 1 not in seen:
            seen.append(ch - 1)
    return seen


def format_channels(numbers) -> str:
    """[1,2,3,4,8,17,18] (1-based) → «1-4, 8, 17-18» — обратное к parse_channels."""
    nums = sorted(set(int(n) for n in numbers))
    parts, i = [], 0
    while i < len(nums):
        j = i
        while j + 1 < len(nums) and nums[j + 1] == nums[j] + 1:
            j += 1
        parts.append(str(nums[i]) if i == j else f"{nums[i]}-{nums[j]}")
        i = j + 1
    return ", ".join(parts)


def safe_name(name: str) -> str:
    """Имя канала для имени файла: без запрещённых в Windows символов."""
    out = "".join("_" if c in '\\/:*?"<>|' or ord(c) < 32 else c for c in name).strip(" .")
    return out[:40]


def channel_file(ch: int, name: str = "") -> str:
    """Файл моно-канала: Ch05.wav или Ch05 Вокал.wav (ch — 1-based)."""
    clean = safe_name(name or "")
    return f"Ch{ch:02d} {clean}.wav" if clean else f"Ch{ch:02d}.wav"


def free_space_gb(folder: Path) -> float:
    p = folder
    while not p.exists() and p.parent != p:
        p = p.parent
    try:
        return shutil.disk_usage(p).free / 1e9
    except OSError:
        return float("inf")


class Recorder:
    BATCH_SECONDS = 0.25

    def __init__(self) -> None:
        self.active = False
        self.folder: Optional[Path] = None
        self.channels: list[int] = []
        self.samplerate = 48000
        self.frames = 0                 # записано сэмплов на канал
        self.pushed = 0                 # принято из аудиопотока (без задержки записи на диск)
        self.meta: dict = {}            # содержимое session.json
        self.bytes_per_frame = 0
        self.error = ""
        self.started_at = 0.0
        self._sel = np.zeros(0, dtype=np.intp)
        self._q: "queue.SimpleQueue[Optional[np.ndarray]]" = queue.SimpleQueue()
        self._thread: Optional[threading.Thread] = None
        self._files: list[sf.SoundFile] = []
        self._stream: Optional[sd.InputStream] = None

    # ── аудиопоток ──────────────────────────────────────────────────────────
    def push(self, indata: np.ndarray) -> None:
        """Вызывается ИЗ АУДИО-КОЛЛБЭКА. Только копия нужных столбцов в очередь."""
        if self.active:
            self._q.put(indata[:, self._sel])  # индексирование массивом = уже копия
            self.pushed += indata.shape[0]

    # ── старт / стоп (GUI-поток) ────────────────────────────────────────────
    def start(self, folder: Path, channels: list[int], samplerate: int,
              subtype: str = "PCM_24", split: bool = True,
              input_device: Optional[int] = None, meta: Optional[dict] = None,
              names: Optional[dict] = None) -> None:
        """input_device=None — входы приходят из дуплексного потока движка (ASIO X32).
        Иначе открываем собственный поток записи на этом устройстве.
        names — {"5": "Вокал"}: имена каналов (в имя файла и на таймлайн)."""
        if self.active:
            return
        if not channels:
            raise ValueError("Не выбрано ни одного входа для записи")
        folder.mkdir(parents=True, exist_ok=True)
        self.folder, self.channels, self.samplerate = folder, channels, samplerate
        self._sel = np.array(channels, dtype=np.intp)
        self.frames = 0
        self.pushed = 0
        self.error = ""
        bps = {"PCM_16": 2, "PCM_24": 3, "FLOAT": 4}.get(subtype, 3)
        self.bytes_per_frame = bps * len(channels)

        # session.json: с какой позиции песни шла запись — чтобы окно дорожек
        # выровняло записанные каналы точно под дорожки песни
        names = {str(k): str(v) for k, v in (names or {}).items() if str(v).strip()}
        info = dict(meta or {})
        info.update(samplerate=samplerate, channels=[c + 1 for c in channels], split=split,
                    names={str(c + 1): names[str(c + 1)] for c in channels if str(c + 1) in names},
                    subtype=subtype, started=time.strftime("%Y-%m-%d %H:%M:%S"))
        self.meta = info
        self._write_meta()

        if split:
            self._files = [sf.SoundFile(str(folder / channel_file(ch + 1, names.get(str(ch + 1), ""))),
                                        "w", samplerate, 1,
                                        subtype=subtype, format="WAV") for ch in channels]
        else:
            self._files = [sf.SoundFile(str(folder / "Multitrack.wav"), "w", samplerate, len(channels),
                                        subtype=subtype, format="RF64")]
        self._q = queue.SimpleQueue()
        self._thread = threading.Thread(target=self._writer, args=(split,), daemon=True, name="recorder")
        self._thread.start()

        if input_device is not None:
            n_in = max(channels) + 1
            self._stream = sd.InputStream(device=input_device, samplerate=samplerate, channels=n_in,
                                          dtype="float32", latency="high",
                                          callback=lambda indata, frames, t, status: self.push(indata))
            self._stream.start()
        self.started_at = time.monotonic()
        self.active = True

    def _write_meta(self) -> None:
        if self.folder is not None:
            (self.folder / "session.json").write_text(json.dumps(self.meta, ensure_ascii=False, indent=2),
                                                      "utf-8")

    def set_meta(self, **values) -> None:
        """Дописать в session.json (например, позицию трека, если PLAY нажали после REC)."""
        self.meta.update(values)
        try:
            self._write_meta()
        except OSError:
            pass

    def stop(self) -> Optional[Path]:
        if not self.active and self._thread is None:
            return None
        self.active = False
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            finally:
                self._stream = None
        self._q.put(None)               # сигнал писателю: дописать остаток и закрыть файлы
        if self._thread is not None:
            self._thread.join(timeout=10)
            self._thread = None
        return self.folder

    # ── поток записи на диск ────────────────────────────────────────────────
    def _writer(self, split: bool) -> None:
        batch: list[np.ndarray] = []
        batch_frames = 0
        limit = int(self.samplerate * self.BATCH_SECONDS)

        def flush() -> None:
            nonlocal batch, batch_frames
            if not batch:
                return
            block = np.concatenate(batch)
            try:
                if split:
                    for i, f in enumerate(self._files):
                        f.write(block[:, i])
                else:
                    self._files[0].write(block)
                self.frames += block.shape[0]
            except Exception as exc:  # диск заполнен и т.п. — сообщаем в интерфейс
                self.error = str(exc)
            batch, batch_frames = [], 0

        while True:
            item = self._q.get()
            if item is None:
                break
            batch.append(item)
            batch_frames += item.shape[0]
            if batch_frames >= limit:
                flush()
        flush()
        for f in self._files:
            try:
                f.close()
            except Exception:
                pass
        self._files = []

    # ── для индикации ───────────────────────────────────────────────────────
    @property
    def seconds(self) -> float:
        return self.frames / self.samplerate if self.samplerate else 0.0

    @property
    def size_mb(self) -> float:
        return self.frames * self.bytes_per_frame / 1e6

    @property
    def backlog(self) -> int:
        """Блоков в очереди, ещё не записанных на диск (растёт — диск не успевает)."""
        try:
            return self._q.qsize()
        except NotImplementedError:
            return 0


def attach_to_song(song, folder: str | Path, routing_defaults: dict, fx_defaults=None) -> tuple[list, list]:
    """Каналы записи → дорожки трека (фейдер с выходом на пульт).

    Каждый канал выравнивается под начало трека (позиция в song_pos минус задержка
    ASIO) и сохраняется рядом с записью как «<канал> (<трек>)_aligned.wav».
    Дорожка называется «Запись: <канал>». Если такая уже есть (новый дубль) —
    у неё меняется только файл: выход, громкость, EQ и компрессор сохраняются.
    Возвращает (новые дорожки, обновлённые дорожки)."""
    folder = Path(folder)
    info = load_session(folder)
    if info.get("song_pos") is None:
        raise ValueError("запись не привязана к треку")
    offset = float(info["song_pos"]) - float(info.get("latency", 0.0))
    added, replaced = [], []
    for name, path, col in info.get("lanes", []):
        data, sr = sf.read(path, dtype="float32", always_2d=True)
        mono = np.ascontiguousarray(data[:, col if col is not None else 0])
        shift = int(round((song.edit.start + offset) * sr))
        if shift >= 0:
            mono = np.concatenate([np.zeros(shift, np.float32), mono])
        else:
            mono = mono[-shift:]
        out = (folder / f"{safe_name(name)} ({safe_name(song.name)})_aligned.wav").resolve()
        sf.write(str(out), mono, sr, subtype="PCM_24")
        track_name = f"Запись: {name}"
        existing = next((t for t in song.tracks if t.name == track_name), None)
        if existing is not None:
            existing.file = str(out)
            existing.clip_start = existing.clip_end = 0.0
            replaced.append(existing)
            continue
        song.add_files([str(out)], routing_defaults)
        for t in song.tracks:
            if Path(t.file).resolve() == out:
                t.name = track_name
                if fx_defaults is not None:
                    t.fx = fx_defaults()
                added.append(t)
    return added, replaced


def load_session(folder: str | Path) -> dict:
    """Описание записи + список файлов: [(имя канала, путь, номер столбца в файле)]."""
    folder = Path(folder)
    try:
        info = json.loads((folder / "session.json").read_text("utf-8"))
    except Exception:
        info = {}
    lanes = []
    names = info.get("names") or {}

    def label(ch: int, fallback: str = "") -> str:
        name = names.get(str(ch)) or fallback
        return f"{ch} {name}" if name else f"Вход {ch}"

    multi = folder / "Multitrack.wav"
    if multi.exists():
        chans = info.get("channels") or list(range(1, sf.info(str(multi)).channels + 1))
        for i, ch in enumerate(chans):
            lanes.append((label(ch), str(multi), i))
    else:
        for f in sorted(folder.glob("*.wav")):
            if f.stem.endswith("_aligned"):
                continue
            m = re.match(r"Ch(\d+)(?: (.*))?$", f.stem)
            name = label(int(m.group(1)), m.group(2) or "") if m else f.stem
            lanes.append((name, str(f), 0))
    info["lanes"] = lanes
    return info
