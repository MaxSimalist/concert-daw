"""
sources.py — Источники сцены на холсте (QGraphicsScene), как в OBS.

Каждый источник = элемент сцены (QGraphicsItem) + то, что его наполняет:
* video     — QMediaPlayer (FFmpeg внутри Qt 6) → QGraphicsVideoItem;
               «идти за треком» — VideoSync из director.py;
* image     — картинка; slideshow — картинки из папки по очереди;
* text      — текст (шрифт, размер, цвет, выравнивание, обводка, подложка);
* color     — заливка цветом;
* camera    — камера / карта захвата (QCamera);
* screen    — захват экрана (QScreenCapture, Qt ≥ 6.5);
* window    — захват окна (QWindowCapture, Qt ≥ 6.6).

Медиа источника работает, только пока его сцена в эфире или в превью
(activate/deactivate) — остальные не тратят процессор. Видеофайл открывается
только когда сцена становится активной и закрывается, когда уходит из эфира/превью:
50 треков с видео не держат открытыми 50 декодеров (и не упираются в лимит
аппаратного декодирования видеокарты).
"""
from __future__ import annotations

import time
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QRectF, QSizeF, Qt, QUrl
from PySide6.QtGui import QColor, QFont, QFontMetricsF, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QGraphicsItem, QGraphicsObject

from director import VideoSync
from vmodel import IMAGE_EXT, Source

try:
    from PySide6.QtMultimedia import QAudioOutput, QCamera, QMediaCaptureSession, QMediaDevices, QMediaPlayer
    from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
    MULTIMEDIA = True
    MULTIMEDIA_ERROR = ""
except Exception as exc:  # pragma: no cover
    MULTIMEDIA = False
    MULTIMEDIA_ERROR = str(exc)
try:
    from PySide6.QtMultimedia import QAudioBufferOutput       # Qt 6.8+: «отвод» звука для индикаторов
except Exception:  # pragma: no cover
    QAudioBufferOutput = None
try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None
try:
    from PySide6.QtMultimedia import QScreenCapture
except Exception:  # pragma: no cover
    QScreenCapture = None
try:
    from PySide6.QtMultimedia import QWindowCapture
except Exception:  # pragma: no cover
    QWindowCapture = None

ASPECT = {"fit": Qt.KeepAspectRatio, "fill": Qt.KeepAspectRatioByExpanding, "stretch": Qt.IgnoreAspectRatio}


# ══════════════════════════════════════════════════════════════════════════════
# Прямоугольник с собственной отрисовкой (картинка, текст, цвет, заглушки)
# ══════════════════════════════════════════════════════════════════════════════
class BoxItem(QGraphicsObject):
    def __init__(self, runtime: "SourceRuntime"):
        super().__init__()
        self.rt = runtime
        self.w = self.h = 100.0

    def set_size(self, w: float, h: float) -> None:
        self.prepareGeometryChange()
        self.w, self.h = max(1.0, w), max(1.0, h)
        self.update()

    def boundingRect(self) -> QRectF:  # noqa: N802
        return QRectF(0, 0, self.w, self.h)

    def paint(self, painter: QPainter, _opt, _w=None) -> None:
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        self.rt.paint(painter, QRectF(0, 0, self.w, self.h))


def draw_pixmap(painter: QPainter, rect: QRectF, pix: QPixmap, fit: str) -> None:
    if pix is None or pix.isNull():
        return
    pw, ph = pix.width(), pix.height()
    if fit == "stretch":
        painter.drawPixmap(rect, pix, QRectF(0, 0, pw, ph))
        return
    k = (min if fit == "fit" else max)(rect.width() / pw, rect.height() / ph)
    w, h = pw * k, ph * k
    target = QRectF(rect.x() + (rect.width() - w) / 2, rect.y() + (rect.height() - h) / 2, w, h)
    if fit == "fill":
        painter.save()
        painter.setClipRect(rect)
        painter.drawPixmap(target, pix, QRectF(0, 0, pw, ph))
        painter.restore()
    else:
        painter.drawPixmap(target, pix, QRectF(0, 0, pw, ph))


def placeholder(painter: QPainter, rect: QRectF, text: str) -> None:
    painter.fillRect(rect, QColor(30, 34, 40, 200))
    painter.setPen(QPen(QColor("#56616c"), 2, Qt.DashLine))
    painter.drawRect(rect.adjusted(2, 2, -2, -2))
    f = QFont()
    f.setPixelSize(int(max(14, min(48, rect.height() / 10))))
    painter.setFont(f)
    painter.setPen(QColor("#a3adb7"))
    painter.drawText(rect, Qt.AlignCenter | Qt.TextWordWrap, text)


# ══════════════════════════════════════════════════════════════════════════════
# Базовый источник
# ══════════════════════════════════════════════════════════════════════════════
class SourceRuntime:
    """Живой источник на холсте. src — его настройки из проекта (vmodel.Source)."""

    has_audio = False
    syncable = False
    ended = False             # видео доиграло (для «после окончания» в расписании)
    scene_gain = 1.0          # громкость сцены (линейная)
    master_gain = 1.0         # общий выход
    scene_muted = False

    def set_mix(self, scene_gain: float, master_gain: float, muted: bool) -> None:
        """Громкость сцены и общего выхода (из микшера)."""
        self.scene_gain, self.master_gain, self.scene_muted = scene_gain, master_gain, muted
        self.apply_gain()

    def apply_gain(self) -> None:
        pass

    def set_output_device(self, name: str) -> None:
        pass

    def take_peaks(self) -> list:
        """Пики звука (по каналам, 0…1, после громкости) с прошлого вызова; [] — звука нет."""
        return []

    def restart(self) -> None:
        """Начать заново (у видео — с первого кадра)."""

    def __init__(self, src: Source):
        self.src = src
        self.item: QGraphicsItem = BoxItem(self)
        self.active = False
        self.status = ""            # что не так (нет файла, нет камеры…) — для списка источников

    # — геометрия и общие свойства (вызывается при любой правке источника) —
    def apply(self) -> None:
        s = self.src
        self.item.setPos(s.x, s.y)
        self._set_size(s.w, s.h)
        self.item.setTransformOriginPoint(s.w / 2, s.h / 2)
        self.item.setRotation(s.rotation)
        self.item.setOpacity(max(0.0, min(1.0, s.opacity)))
        self.item.setVisible(s.visible)
        movable = not s.locked
        self.item.setFlag(QGraphicsItem.ItemIsMovable, movable)
        self.item.setFlag(QGraphicsItem.ItemIsSelectable, True)
        self.reload()
        self.item.update()

    def _set_size(self, w: float, h: float) -> None:
        if isinstance(self.item, BoxItem):
            self.item.set_size(w, h)

    def reload(self) -> None:
        """Файл/текст/цвет изменились — перечитать."""

    def paint(self, painter: QPainter, rect: QRectF) -> None:
        placeholder(painter, rect, self.src.name)

    def activate(self) -> None:
        self.active = True

    def deactivate(self) -> None:
        self.active = False

    def tick(self, media_pos: float, playing: bool, connected: bool) -> None:
        """~30 раз в секунду, пока сцена активна."""

    def release(self) -> None:
        self.deactivate()


# ══════════════════════════════════════════════════════════════════════════════
class ColorSource(SourceRuntime):
    def paint(self, painter, rect) -> None:
        painter.fillRect(rect, QColor(self.src.params.get("color", "#000000")))


class TextSource(SourceRuntime):
    def paint(self, painter, rect) -> None:
        p = self.src.params
        if p.get("bg"):
            painter.fillRect(rect, QColor(p["bg"]))
        text = str(p.get("text", ""))
        f = QFont(p.get("font", "Segoe UI"))
        f.setPixelSize(int(p.get("size", 72)))
        f.setBold(bool(p.get("bold", True)))
        f.setItalic(bool(p.get("italic", False)))
        align = {"left": Qt.AlignLeft, "right": Qt.AlignRight}.get(p.get("align", "center"), Qt.AlignHCenter)
        flags = int(align | Qt.AlignVCenter | Qt.TextWordWrap)
        outline = float(p.get("outline", 0))
        if outline > 0:
            # обводка: текст как контур, по строкам с переносом — через QPainterPath
            painter.setFont(f)
            fm = QFontMetricsF(f)
            lines = _wrap(text, fm, rect.width())
            total = fm.height() * len(lines)
            y = rect.y() + (rect.height() - total) / 2 + fm.ascent()
            path = QPainterPath()
            for line in lines:
                lw = fm.horizontalAdvance(line)
                if align == Qt.AlignLeft:
                    x = rect.x()
                elif align == Qt.AlignRight:
                    x = rect.right() - lw
                else:
                    x = rect.x() + (rect.width() - lw) / 2
                path.addText(x, y, f, line)
                y += fm.height()
            painter.setPen(QPen(QColor(p.get("outline_color", "#000000")), outline * 2, Qt.SolidLine,
                                Qt.RoundCap, Qt.RoundJoin))
            painter.setBrush(Qt.NoBrush)
            painter.drawPath(path)
            painter.setPen(Qt.NoPen)
            painter.setBrush(QColor(p.get("color", "#ffffff")))
            painter.drawPath(path)
            return
        painter.setFont(f)
        painter.setPen(QColor(p.get("color", "#ffffff")))
        painter.drawText(rect, flags, text)


def _wrap(text: str, fm: QFontMetricsF, width: float) -> list[str]:
    out = []
    for para in text.split("\n"):
        line = ""
        for word in para.split(" "):
            cand = f"{line} {word}".strip()
            if line and fm.horizontalAdvance(cand) > width:
                out.append(line)
                line = word
            else:
                line = cand
        out.append(line)
    return out


class ImageSource(SourceRuntime):
    def __init__(self, src: Source):
        super().__init__(src)
        self.pix: Optional[QPixmap] = None
        self._file = None

    def reload(self) -> None:
        f = self.src.params.get("file", "")
        if f != self._file:
            self._file = f
            self.pix = QPixmap(f) if f and Path(f).exists() else None
            self.status = "" if self.pix is not None and not self.pix.isNull() else "нет файла"

    def paint(self, painter, rect) -> None:
        if self.pix is None or self.pix.isNull():
            placeholder(painter, rect, f"{self.src.name}\n(нет картинки)")
            return
        draw_pixmap(painter, rect, self.pix, self.src.fit)


class SlideshowSource(SourceRuntime):
    """Картинки из папки по кругу. Интервал — params["interval"] сек; смена — растворением."""

    FADE = 0.6

    def __init__(self, src: Source):
        super().__init__(src)
        self.files: list[str] = []
        self.index = 0
        self.cache: dict[str, QPixmap] = {}
        self.started = time.monotonic()
        self._folder = None

    def reload(self) -> None:
        folder = self.src.params.get("folder", "")
        if folder != self._folder:
            self._folder = folder
            p = Path(folder) if folder else None
            self.files = sorted(str(f) for f in p.iterdir() if f.suffix.lower() in IMAGE_EXT) \
                if p is not None and p.is_dir() else []
            self.cache.clear()
            self.index = 0
            self.status = "" if self.files else "в папке нет картинок"

    def _pix(self, i: int) -> Optional[QPixmap]:
        if not self.files:
            return None
        f = self.files[i % len(self.files)]
        if f not in self.cache:
            if len(self.cache) > 6:
                self.cache.clear()
            self.cache[f] = QPixmap(f)
        return self.cache[f]

    def activate(self) -> None:
        super().activate()
        self.started = time.monotonic()

    def tick(self, media_pos, playing, connected) -> None:
        if self.files:
            self.item.update()

    def paint(self, painter, rect) -> None:
        if not self.files:
            placeholder(painter, rect, f"{self.src.name}\n(папка без картинок)")
            return
        interval = max(1.0, float(self.src.params.get("interval", 5.0)))
        el = time.monotonic() - self.started
        i = int(el // interval)
        phase = el - i * interval
        if self.src.params.get("shuffle"):
            import random
            i = random.Random(i).randrange(len(self.files))
        draw_pixmap(painter, rect, self._pix(i), self.src.fit)
        if phase < self.FADE and el > interval:
            painter.save()
            painter.setOpacity(1.0 - phase / self.FADE)
            draw_pixmap(painter, rect, self._pix(i - 1), self.src.fit)
            painter.restore()


# ══════════════════════════════════════════════════════════════════════════════
# Видео
# ══════════════════════════════════════════════════════════════════════════════
class _QtPlayer:
    """Адаптер QMediaPlayer для VideoSync (секунды вместо миллисекунд)."""

    def __init__(self, player):
        self.player = player

    def position(self) -> float:
        return self.player.position() / 1000.0

    def duration(self) -> float:
        return max(0, self.player.duration()) / 1000.0

    def is_playing(self) -> bool:
        return self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    def play(self) -> None:
        self.player.play()

    def pause(self) -> None:
        self.player.pause()

    def seek(self, t: float) -> None:
        self.player.setPosition(int(t * 1000))

    def set_rate(self, r: float) -> None:
        self.player.setPlaybackRate(float(r))


class VideoSource(SourceRuntime):
    has_audio = True
    syncable = True

    def __init__(self, src: Source):
        super().__init__(src)
        self._file = None
        self.player = None
        self._peaks: list = []
        self.meter_ok = False
        self.meter_seen = False                # хоть один звуковой буфер пришёл
        self.meter_error = ""
        self._free = False                     # видео «за треком» играет само (нет связи с DAW)
        self._wd_pos, self._wd_t = -1, 0.0
        if not MULTIMEDIA:
            self.status = f"нет QtMultimedia: {MULTIMEDIA_ERROR}"
            return
        self.item = QGraphicsVideoItem()
        self.audio = QAudioOutput()
        self.player = QMediaPlayer()
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.item)
        self.player.errorOccurred.connect(self._on_error)
        self.player.mediaStatusChanged.connect(self._on_status)
        self._peaks: list = []
        self.meter_ok = False
        if QAudioBufferOutput is not None and np is not None:
            try:
                self.buf_out = QAudioBufferOutput()
                self.player.setAudioBufferOutput(self.buf_out)
                self.buf_out.audioBufferReceived.connect(self._on_buffer)
                self.meter_ok = True
            except Exception:
                self.meter_ok = False
        self.ended = False                     # обычное видео (не «за треком») доиграло до конца
        self.sync = VideoSync(_QtPlayer(self.player))
        self.state = ""
        self._loaded: Optional[str] = None

    def _on_error(self, _err, text: str) -> None:
        self.status = f"ошибка: {text}"

    def _on_status(self, status) -> None:
        if status == QMediaPlayer.MediaStatus.EndOfMedia and not self.src.loop:
            self.ended = True

    # ── звук ───────────────────────────────────────────────────────────────
    def _gain(self) -> float:
        return max(0.0, min(1.0, self.src.volume * self.scene_gain * self.master_gain))

    def _silent(self) -> bool:
        return self.src.muted or self.scene_muted

    def apply_gain(self) -> None:
        if self.player is not None:
            # «без звука» = громкость 0, а не mute: так звуковые буферы для индикатора
            # продолжают приходить и индикатор показывает, что в видео есть звук
            self.audio.setMuted(False)
            self.audio.setVolume(0.0 if self._silent() else self._gain())

    def set_output_device(self, name: str) -> None:
        if self.player is None or name == getattr(self, "_dev_name", None):
            return
        self._dev_name = name
        from PySide6.QtMultimedia import QMediaDevices as _MD
        dev = next((d for d in _MD.audioOutputs() if d.description() == name), None) if name else None
        self.audio.setDevice(dev if dev is not None else _MD.defaultAudioOutput())

    def _on_buffer(self, buf) -> None:
        """Звук, который сейчас играет плеер → пики по каналам (для индикатора микшера)."""
        try:
            fmt = buf.format()
            ch = max(1, fmt.channelCount())
            data = buffer_bytes(buf)
            if not data:
                return
            sf = fmt.sampleFormat()
            SF = type(sf)
            if sf == SF.Float:
                a = np.frombuffer(data, dtype=np.float32)
            elif sf == SF.Int16:
                a = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
            elif sf == SF.Int32:
                a = np.frombuffer(data, dtype=np.int32).astype(np.float32) / 2147483648.0
            elif sf == SF.UInt8:
                a = (np.frombuffer(data, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
            else:
                return
            n = len(a) // ch
            if n <= 0:
                return
            pk = np.abs(a[:n * ch].reshape(n, ch)).max(axis=0)
            if len(self._peaks) != ch:
                self._peaks = [0.0] * ch
            self._peaks = [max(p, float(v)) for p, v in zip(self._peaks, pk)]
            self.meter_seen = True
        except Exception as exc:              # индикатор не должен ронять программу
            self.meter_error = str(exc)

    def take_peaks(self) -> list:
        """Уровень после фейдеров. Без звука (M) — тоже показываем (серым), чтобы было видно,
        что в видео есть звук."""
        if not self._peaks:
            return []
        g = self._gain()
        out = [p * g for p in self._peaks]
        self._peaks = [0.0] * len(self._peaks)
        return out

    def restart(self) -> None:
        """С начала (расписание запустило ту же сцену ещё раз)."""
        if self.player is None or self.src.sync:
            return
        self.ended = False
        self.player.setPosition(0)
        self.player.play()

    def _set_size(self, w, h) -> None:
        if MULTIMEDIA:
            self.item.setSize(QSizeF(w, h))
            self.item.setAspectRatioMode(ASPECT.get(self.src.fit, Qt.KeepAspectRatio))
        else:
            super()._set_size(w, h)

    def _load(self) -> None:
        """Открыть файл в плеере (только для активной сцены)."""
        f = self.src.params.get("file", "")
        if f and Path(f).exists():
            self.player.setSource(QUrl.fromLocalFile(f))
            self.status = ""
        else:
            self.player.setSource(QUrl())
            self.status = "нет файла"
        self._loaded = f

    def reload(self) -> None:
        if self.player is None:
            return
        f = self.src.params.get("file", "")
        if f != self._file:
            self._file = f
            self.status = "" if f and Path(f).exists() else "нет файла"
            if self.active:
                self._load()
        self.apply_gain()
        if not self.src.sync:
            self.player.setLoops(-1 if self.src.loop else 1)   # -1 = бесконечно
        else:
            self.player.setLoops(1)

    def activate(self) -> None:
        super().activate()
        if self.player is None:
            return
        self.ended = False
        self._free = False
        self._wd_pos, self._wd_t = -1, time.monotonic()
        if self._loaded != self.src.params.get("file", ""):
            self._load()
        if not self.src.sync:
            if self.src.params.get("restart", True):
                self.player.setPosition(0)
            self.player.play()
        else:
            self.player.pause()               # первый кадр, дальше ведёт tick()

    def deactivate(self) -> None:
        super().deactivate()
        if self.player is not None:
            self.player.pause()
            self.player.setPlaybackRate(1.0)
            self.sync.rate = 1.0
            self.player.setSource(QUrl())          # освободить декодер до следующего показа
            self._loaded = None

    def tick(self, media_pos, playing, connected) -> None:
        if self.player is None or not self.active:
            return
        self._watchdog()
        if not self.src.sync:
            return
        if not connected:
            # Concert DAW не подключён (программа работает сама по себе): видео «за треком»
            # играет как обычное — с начала, до конца (или по кругу), а не стоит на первом кадре
            if not self._free and not self.ended:
                self._free = True
                self.player.setLoops(-1 if self.src.loop else 1)
                self.player.setPlaybackRate(1.0)
                self.sync.rate = 1.0
                self.player.play()
            return
        if self._free:                          # связь появилась — дальше видео ведёт трек
            self._free = False
            self.player.setLoops(1)
        self.state = self.sync.update(media_pos - self.src.offset, playing, self.src.loop)

    def _watchdog(self) -> None:
        """Страховка: если видео «играет», но стоит на месте больше 2 с, а подключён отвод
        звука для индикаторов — отключаем индикаторы этого видео (на некоторых сборках Qt
        отвод звука может мешать воспроизведению). Картинка важнее индикатора."""
        if not self.meter_ok or self.player.playbackState() != QMediaPlayer.PlaybackState.PlayingState:
            self._wd_pos, self._wd_t = -1, time.monotonic()
            return
        pos = self.player.position()
        now = time.monotonic()
        if pos != self._wd_pos:
            self._wd_pos, self._wd_t = pos, now
            return
        if now - self._wd_t > 2.0 and self.player.duration() > 0:
            try:
                self.player.setAudioBufferOutput(None)
            except Exception:
                pass
            self.meter_ok = False
            self.status = "индикатор звука отключён (мешал воспроизведению)"
            pos, cur = self.player.position(), self.player.source()
            self.player.setSource(QUrl())
            self.player.setSource(cur)
            self.player.setPosition(pos)
            self.player.play()
            self._wd_pos, self._wd_t = -1, now

    def set_volume(self, v: float, muted: bool) -> None:
        self.src.volume, self.src.muted = v, muted
        self.apply_gain()

    # ── ручное управление (кнопки Play/Пауза и шкала времени под эфиром) ───
    def controllable(self, connected: bool) -> bool:
        """Можно ли управлять вручную: обычное видео — всегда; «за треком» — только пока
        Concert DAW не подключён (иначе видео ведёт трек)."""
        return self.player is not None and (not self.src.sync or not connected)

    def is_playing(self) -> bool:
        return self.player is not None and \
            self.player.playbackState() == QMediaPlayer.PlaybackState.PlayingState

    def position_s(self) -> float:
        return self.player.position() / 1000.0 if self.player is not None else 0.0

    def duration_s(self) -> float:
        return max(0, self.player.duration()) / 1000.0 if self.player is not None else 0.0

    def toggle_play(self) -> None:
        if self.player is None:
            return
        if self.is_playing():
            self.player.pause()
        else:
            if self.ended or (self.duration_s() > 0 and self.position_s() >= self.duration_s() - 0.05):
                self.player.setPosition(0)
            self.ended = False
            self._free = True                  # «за треком» без DAW: дальше — вручную
            self.player.play()

    def seek_s(self, t: float) -> None:
        if self.player is None:
            return
        self.ended = False
        self.player.setPosition(int(max(0.0, t) * 1000))

    def to_start(self) -> None:
        if self.player is None:
            return
        self._free = True
        self.player.pause()
        self.player.setPosition(0)
        self.ended = False

    def release(self) -> None:
        super().release()
        if self.player is not None:
            self.player.stop()
            self.player.setSource(QUrl())


# ══════════════════════════════════════════════════════════════════════════════
# Камера и захват
# ══════════════════════════════════════════════════════════════════════════════
def buffer_bytes(buf) -> bytes:
    """Байты QAudioBuffer — в разных версиях PySide6 constData()/data() возвращают
    bytes, memoryview или «сырой» указатель shiboken (VoidPtr)."""
    try:
        n = int(buf.byteCount())
    except Exception:
        n = 0
    for getter in ("constData", "data"):
        try:
            raw = getattr(buf, getter)()
        except Exception:
            continue
        if raw is None:
            continue
        if isinstance(raw, (bytes, bytearray)):
            return bytes(raw[:n] if n else raw)
        try:
            data = bytes(memoryview(raw))
            if data:
                return data[:n] if n else data
        except Exception:
            pass
        try:
            import shiboken6
            vp = shiboken6.VoidPtr(raw, n, False) if n else raw
            return bytes(vp.toBytes())
        except Exception:
            continue
    return b""


def meters_unavailable_reason() -> str:
    """Почему индикаторы звука не могут работать ("" — могут)."""
    if not MULTIMEDIA:
        return "нет Qt Multimedia"
    if QAudioBufferOutput is None:
        try:
            import PySide6
            ver = PySide6.__version__
        except Exception:
            ver = "?"
        return f"нужна PySide6 6.8 или новее (сейчас {ver}) — запустите install.bat"
    if np is None:
        return "нужна библиотека numpy — запустите install.bat"
    return ""


def camera_names() -> list[str]:
    if not MULTIMEDIA:
        return []
    return [d.description() for d in QMediaDevices.videoInputs()]


class _CaptureSource(SourceRuntime):
    """Общее для камеры/экрана/окна: сессия захвата → видеоэлемент."""

    def __init__(self, src: Source):
        super().__init__(src)
        self.session = None
        if not MULTIMEDIA:
            self.status = f"нет QtMultimedia: {MULTIMEDIA_ERROR}"
            return
        self.item = QGraphicsVideoItem()
        self.session = QMediaCaptureSession()
        self.session.setVideoOutput(self.item)

    def _set_size(self, w, h) -> None:
        if self.session is not None:
            self.item.setSize(QSizeF(w, h))
            self.item.setAspectRatioMode(ASPECT.get(self.src.fit, Qt.KeepAspectRatio))
        else:
            super()._set_size(w, h)


class CameraSource(_CaptureSource):
    def __init__(self, src: Source):
        super().__init__(src)
        self.camera = None
        self._dev = None

    def reload(self) -> None:
        if self.session is None:
            return
        name = self.src.params.get("device", "")
        if name == self._dev and self.camera is not None:
            return
        self._dev = name
        was = self.active
        if self.camera is not None:
            self.camera.stop()
        devs = QMediaDevices.videoInputs()
        dev = next((d for d in devs if d.description() == name), None) or (devs[0] if devs else None)
        if dev is None:
            self.camera = None
            self.status = "камера не найдена"
            return
        self.camera = QCamera(dev)
        self.session.setCamera(self.camera)
        self.status = ""
        if was:
            self.camera.start()

    def activate(self) -> None:
        super().activate()
        if self.camera is not None:
            self.camera.start()

    def deactivate(self) -> None:
        super().deactivate()
        if self.camera is not None and not self.src.params.get("keep_on", False):
            self.camera.stop()


class ScreenSource(_CaptureSource):
    def __init__(self, src: Source):
        super().__init__(src)
        self.cap = None
        if self.session is not None and QScreenCapture is not None:
            self.cap = QScreenCapture()
            self.session.setScreenCapture(self.cap)
        elif self.session is not None:
            self.status = "нужна Qt 6.5+ (обновите PySide6)"

    def reload(self) -> None:
        if self.cap is None:
            return
        from PySide6.QtGui import QGuiApplication
        name = self.src.params.get("screen", "")
        screens = QGuiApplication.screens()
        scr = next((s for s in screens if s.name() == name), None) or QGuiApplication.primaryScreen()
        self.cap.setScreen(scr)

    def activate(self) -> None:
        super().activate()
        if self.cap is not None:
            self.cap.start()

    def deactivate(self) -> None:
        super().deactivate()
        if self.cap is not None:
            self.cap.stop()


def window_titles() -> list[str]:
    if QWindowCapture is None:
        return []
    try:
        return [w.description() for w in QWindowCapture.capturableWindows() if w.description()]
    except Exception:
        return []


class WindowSource(_CaptureSource):
    def __init__(self, src: Source):
        super().__init__(src)
        self.cap = None
        if self.session is not None and QWindowCapture is not None:
            self.cap = QWindowCapture()
            self.session.setWindowCapture(self.cap)
        elif self.session is not None:
            self.status = "нужна Qt 6.6+ (обновите PySide6)"

    def reload(self) -> None:
        if self.cap is None:
            return
        title = self.src.params.get("window", "")
        wins = QWindowCapture.capturableWindows()
        win = next((w for w in wins if w.description() == title), None)
        if win is None:
            win = next((w for w in wins if title and title.lower() in w.description().lower()), None)
        if win is None:
            self.status = "окно не найдено"
            return
        self.status = ""
        self.cap.setWindow(win)

    def activate(self) -> None:
        super().activate()
        if self.cap is not None:
            self.cap.start()

    def deactivate(self) -> None:
        super().deactivate()
        if self.cap is not None:
            self.cap.stop()


KINDS = {"color": ColorSource, "text": TextSource, "image": ImageSource, "slideshow": SlideshowSource,
         "video": VideoSource, "camera": CameraSource, "screen": ScreenSource, "window": WindowSource}


def make_runtime(src: Source) -> SourceRuntime:
    cls = KINDS.get(src.kind, SourceRuntime)
    rt = cls(src)
    rt.item.setData(0, src.id)          # по элементу на холсте находим источник
    rt.apply()
    return rt
