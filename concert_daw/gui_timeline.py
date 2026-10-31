"""
gui_timeline.py — Общая временная шкала над фейдерами (таймлайн).

Что показывает
--------------
* Дорожки активного трека — волной, на общей шкале времени воспроизведения
  (с учётом обрезки и скорости из редактора трека).
* Каналы выбранной записи с пульта — под ними, выровненные по треку:
  запись знает, с какой позиции трека она началась, и задержку ASIO
  (session.json), поэтому записанный звук встаёт точно под дорожки.

Что можно делать
----------------
* Клик по волне или линейке — перемотка (в том числе во время игры).
* Протянуть мышью по дорожке — выделение участка.
* ножницы — обрезать дорожку трека по выделению: до и после — тишина, длина трека
  и синхронность остальных дорожек не меняются. стрелка — снять обрезку.
* «Мультитрек» — добавить в трек дорожки другого трека из плейлистов или файлы.
* Записи: выбор в списке; дискета — сохранить канал на компьютер, плюс — добавить
  канал в трек, корзина — удалить канал или всю запись (в корзину).
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

import numpy as np
import soundfile as sf
import shutil

from PySide6.QtCore import QFile, QLineF, QObject, QRect, QRectF, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QColor, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QComboBox, QDialog, QFileDialog, QFrame, QHBoxLayout, QInputDialog, QLabel,
                               QMenu, QMessageBox, QPushButton, QScrollArea, QScrollBar, QSlider,
                               QVBoxLayout, QWidget)

from audio_edit import file_peaks
from gui_icons import set_icon
from gui_layout import ElidedLabel, FlowLayout
from model import AppConfig, Song, is_audio_file
from recorder import load_session
import dsp

PEAK_RATE = 100     # столбиков огибающей на секунду
HEADER_W = 118   # колонка названий дорожек — узкая, больше места волнам
LANE_H = 42

_peaks_cache: dict[tuple, tuple[np.ndarray, float]] = {}


def fmt(t: float) -> str:
    sign = "-" if t < 0 else ""
    t = abs(t)
    return f"{sign}{int(t // 60):02d}:{t % 60:04.1f}"


# ──────────────────────────────────────────────────────────────────────────────
# Данные дорожки на таймлайне
# ──────────────────────────────────────────────────────────────────────────────
class Lane:
    """kind="song": дорожка песни; время файла = src_start + t·speed.
    kind="rec": канал записи; время файла = t − offset."""

    def __init__(self, kind: str, name: str, path: str, column: Optional[int] = None, cfg=None,
                 offset: float = 0.0, speed: float = 1.0, src_start: float = 0.0, song_end: float = 0.0):
        self.kind, self.name, self.path, self.column, self.cfg = kind, name, path, column, cfg
        self.offset, self.speed, self.src_start, self.song_end = offset, speed, src_start, song_end
        self.peaks: Optional[np.ndarray] = None
        self.length = 0.0                         # длина файла, сек
        self.selection: Optional[tuple[float, float]] = None  # во времени таймлайна

    def own(self, t):
        return self.src_start + t * self.speed if self.kind == "song" else t - self.offset

    def out(self, t_own):
        return (t_own - self.src_start) / self.speed if self.kind == "song" else t_own + self.offset

    @property
    def end_out(self) -> float:
        return self.out(self.length)

    def clip_out(self) -> tuple[Optional[float], Optional[float]]:
        if self.kind != "song" or self.cfg is None:
            return None, None
        a = self.out(self.cfg.clip_start) if self.cfg.clip_start > 0 else None
        b = self.out(self.cfg.clip_end) if self.cfg.clip_end > 0 else None
        return a, b


class TimelineView(QObject):
    """Общий масштаб и прокрутка для всех дорожек."""
    changed = Signal()          # масштаб/прокрутка — перерисовать волны
    playhead_moved = Signal()   # только курсор

    def __init__(self):
        super().__init__()
        self.pps = 20.0         # пикселей на секунду
        self.start = 0.0        # левый край, сек
        self.duration = 60.0
        self.playhead = 0.0


class _PeakSignals(QObject):
    done = Signal(object, object, float)  # ключ, пики, длина


class _PeakTask(QRunnable):
    def __init__(self, key, path, column, signals):
        super().__init__()
        self.key, self.path, self.column, self.signals = key, path, column, signals

    def run(self) -> None:
        try:
            peaks, length = file_peaks(self.path, PEAK_RATE, self.column)
        except Exception:
            peaks, length = np.zeros(0, np.float32), 0.0
        self.signals.done.emit(self.key, peaks, length)


# ──────────────────────────────────────────────────────────────────────────────
# Виджеты
# ──────────────────────────────────────────────────────────────────────────────
MARKER_PRESETS = ["Интро", "Куплет", "Предприпев", "Припев", "Бридж", "Соло", "Проигрыш", "Кода", "Финал"]


class Ruler(QWidget):
    """Линейка времени: деления, маркеры (флажки с названием), участок петли, курсор.
    Клик — перемотка; клик по флажку — на маркер; правый клик по флажку — меню маркера."""
    seek = Signal(float)
    marker_menu = Signal(int, object)   # индекс маркера, глобальная позиция

    def __init__(self, view: TimelineView, parent=None):
        super().__init__(parent)
        self.view = view
        self.markers: list[tuple[float, str]] = []    # (время воспроизведения, название)
        self.loop: Optional[tuple[float, float]] = None
        self.setFixedHeight(24)
        view.changed.connect(self.update)
        view.playhead_moved.connect(self.update)

    def _x(self, t: float) -> float:
        return (t - self.view.start) * self.view.pps

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor("#121518"))
        v = self.view
        h = self.height()
        if self.loop:
            a, b = (self._x(t) for t in self.loop)
            p.fillRect(QRectF(a, 0, b - a, h), QColor(255, 159, 26, 60))
            p.setPen(QPen(QColor("#ff9f43"), 2))
            p.drawLine(int(a), 0, int(a), h)
            p.drawLine(int(b), 0, int(b), h)
        step = next((s for s in (0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300) if s * v.pps >= 70), 600)
        t = (v.start // step) * step
        f = p.font()
        f.setPixelSize(11)
        p.setFont(f)
        while self._x(t) < self.width():
            x = int(self._x(t))
            p.setPen(QColor("#56616c"))
            p.drawLine(x, h - 7, x, h)
            p.setPen(QColor("#a3adb7"))
            p.drawText(x + 3, h - 2, fmt(t)[:-2] if step >= 1 else fmt(t))
            t += step
        # маркеры: жёлтый флажок + название
        for i, (mt, name) in enumerate(self.markers):
            x = self._x(mt)
            if -200 < x < self.width():
                p.fillRect(QRectF(x, 0, 2, h), QColor("#f7c948"))
                p.fillRect(QRectF(x, 0, 12 + len(name) * 6.5, 12), QColor(245, 197, 24, 210))
                p.setPen(QColor("#1b1705"))
                p.drawText(QRectF(x + 4, 0, 200, 12), Qt.AlignLeft | Qt.AlignVCenter, f"{i + 1} {name}")
        x = self._x(v.playhead)
        p.setPen(QPen(QColor("#ef4b55"), 2))
        p.drawLine(int(x), 0, int(x), h)
        p.end()

    def _marker_at(self, x: float) -> int:
        for i, (mt, name) in enumerate(self.markers):
            mx = self._x(mt)
            if mx - 4 <= x <= mx + 12 + len(name) * 6.5:
                return i
        return -1

    def mousePressEvent(self, e) -> None:
        x, y = e.position().x(), e.position().y()
        i = self._marker_at(x) if y <= 13 else -1
        if e.button() == Qt.RightButton:
            if i >= 0:
                self.marker_menu.emit(i, e.globalPosition().toPoint())
            return
        if i >= 0:
            self.seek.emit(max(0.0, self.markers[i][0]))
        else:
            self.seek.emit(max(0.0, self.view.start + x / self.view.pps))

    def mouseMoveEvent(self, e) -> None:
        if e.buttons() & Qt.LeftButton:
            self.seek.emit(max(0.0, self.view.start + e.position().x() / self.view.pps))


class LaneView(QWidget):
    seek = Signal(float)

    def __init__(self, lane: Lane, view: TimelineView, parent=None):
        super().__init__(parent)
        self.lane, self.view = lane, view
        self.setMinimumHeight(LANE_H)
        self.setMaximumHeight(LANE_H)
        self._cache: Optional[QPixmap] = None
        self._press_x: Optional[float] = None
        self._anchor = 0.0
        view.changed.connect(self.invalidate)
        view.playhead_moved.connect(self.update)

    def invalidate(self) -> None:
        self._cache = None
        self.update()

    def resizeEvent(self, _e) -> None:
        self._cache = None

    def _t(self, x: float) -> float:
        return self.view.start + x / self.view.pps

    def _x(self, t: float) -> float:
        return (t - self.view.start) * self.view.pps

    def _render(self) -> QPixmap:
        w, h = max(1, self.width()), max(1, self.height())
        dpr = self.devicePixelRatioF()
        pix = QPixmap(int(w * dpr), int(h * dpr))
        pix.setDevicePixelRatio(dpr)
        pix.fill(QColor("#0e1114"))
        p = QPainter(pix)
        lane = self.lane
        mid = h / 2
        wave = QColor("#36d399") if lane.kind == "song" else QColor("#ff6b6e")
        if lane.peaks is None:
            p.setPen(QColor("#56616c"))
            p.drawText(QRect(0, 0, w, h), Qt.AlignCenter, "загрузка волны…")
        elif lane.peaks.size:
            n = lane.peaks.size
            t_out = self.view.start + np.arange(w + 1) / self.view.pps
            idx = np.floor(np.asarray(lane.own(t_out)) * PEAK_RATE).astype(np.int64)
            run = np.nonzero((idx[:-1] >= 0) & (idx[:-1] < n))[0]
            if run.size:
                seg = np.maximum.reduceat(lane.peaks, idx[run])  # максимум по всем столбикам пикселя
                amp = (seg * mid * 0.9).tolist()
                p.setPen(QPen(wave, 1))
                p.drawLines([QLineF(x, mid - a, x, mid + a) for x, a in zip(run.tolist(), amp)])
        shade = QColor(0, 0, 0, 175)
        # за пределами песни (обрезка в редакторе песни)
        if lane.kind == "song" and lane.song_end > 0:
            xe = self._x(lane.song_end)
            if xe < w:
                p.fillRect(QRectF(xe, 0, w - xe, h), shade)
        # обрезка дорожки
        c0, c1 = lane.clip_out()
        p.setPen(QColor("#f7c948"))
        if c0 is not None and self._x(c0) > 0:
            p.fillRect(QRectF(0, 0, self._x(c0), h), shade)
            p.drawLine(int(self._x(c0)), 0, int(self._x(c0)), h)
        if c1 is not None and self._x(c1) < w:
            p.fillRect(QRectF(self._x(c1), 0, w - self._x(c1), h), shade)
            p.drawLine(int(self._x(c1)), 0, int(self._x(c1)), h)
        # выделение
        if lane.selection:
            a, b = (self._x(t) for t in lane.selection)
            p.fillRect(QRectF(a, 0, b - a, h), QColor(80, 160, 255, 70))
            p.setPen(QColor("#50a0ff"))
            p.drawLine(int(a), 0, int(a), h)
            p.drawLine(int(b), 0, int(b), h)
            p.drawText(QRectF(a + 4, h - 16, 200, 14), Qt.AlignLeft,
                       f"{fmt(lane.selection[0])} – {fmt(lane.selection[1])}")
        p.setPen(QColor("#232931"))
        p.drawLine(0, h - 1, w, h - 1)
        p.end()
        return pix

    def paintEvent(self, _e) -> None:
        if self._cache is None or self._cache.width() != int(self.width() * self.devicePixelRatioF()):
            self._cache = self._render()
        p = QPainter(self)
        p.drawPixmap(0, 0, self._cache)
        x = self._x(self.view.playhead)
        if 0 <= x <= self.width():
            p.setPen(QPen(QColor("#ef4b55"), 2))
            p.drawLine(int(x), 0, int(x), self.height())
        p.end()

    def mousePressEvent(self, e) -> None:
        self._press_x = e.position().x()
        self._anchor = self._t(self._press_x)

    def mouseMoveEvent(self, e) -> None:
        if self._press_x is None:
            return
        x = e.position().x()
        if abs(x - self._press_x) > 3:
            t = self._t(x)
            self.lane.selection = (max(0.0, min(self._anchor, t)), max(self._anchor, t))
            self.invalidate()

    def mouseReleaseEvent(self, e) -> None:
        if self._press_x is not None and abs(e.position().x() - self._press_x) <= 3:
            # простой клик — перемотка; выделение снимаем
            self.lane.selection = None
            self.invalidate()
            self.seek.emit(max(0.0, self._t(e.position().x())))
        self._press_x = None


# ──────────────────────────────────────────────────────────────────────────────
# Окно
# ──────────────────────────────────────────────────────────────────────────────
def move_to_trash(path: str) -> bool:
    """В корзину Windows (можно восстановить). Если корзина недоступна — False."""
    try:
        return bool(QFile.moveToTrash(path))
    except Exception:
        return False


class TimelinePanel(QWidget):
    """Таймлайн, встроенный в главное окно над фейдерами."""
    song_modified = Signal(object)   # Song: дорожки или обрезка изменились — перезагрузить
    seek_requested = Signal(float)
    recording_deleted = Signal(str)  # папка удалённой записи
    markers_changed = Signal(object) # Song — сохранить
    loop_changed = Signal(object)    # (начало, конец) во времени воспроизведения или None

    def __init__(self, cfg: AppConfig, can_edit: Callable[[], str], all_songs: Callable[[], list],
                 rec_root: Callable[[], Path], is_recording: Callable[[str], bool], parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.can_edit = can_edit
        self.all_songs = all_songs
        self.rec_root = rec_root
        self.is_recording = is_recording
        self.setObjectName("timelinePanel")
        self.view = TimelineView()
        self.song: Optional[Song] = None
        self.samplerate = 48000
        self.rec_folder = ""
        self.rec_info: dict = {}
        self.lanes: list[Lane] = []
        self.lane_views: list[LaneView] = []
        self.signals = _PeakSignals()
        self.signals.done.connect(self._on_peaks)
        self._pending: dict[tuple, list[Lane]] = {}

        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(2)
        bar = FlowLayout(hspacing=4, vspacing=2)          # кнопки переносятся на 2-ю строку, если не помещаются
        cap = QLabel("ТАЙМЛАЙН")
        cap.setObjectName("caption")
        bar.addWidget(cap)
        add_btn = QPushButton(" Мультитрек")
        set_icon(add_btn, "layers", 14)
        add_btn.setToolTip("Добавить в трек дорожки другого трека или аудиофайлы")
        add_menu = QMenu(add_btn)
        add_menu.addAction("Из плейлистов…", self._add_from_playlists)
        add_menu.addAction("Из файлов…", self._add_from_files)
        add_btn.setMenu(add_menu)
        bar.addWidget(add_btn)
        mk_btn = QPushButton(" Маркер")
        set_icon(mk_btn, "marker", 14)
        mk_btn.setToolTip("Поставить маркер в позиции курсора (Интро, Куплет, Припев…).\n"
                          "Переход: клик по флажку, клавиши 1–9, Shift+← / Shift+→")
        mk_btn.clicked.connect(self.add_marker)
        bar.addWidget(mk_btn)
        self.loop_btn = QPushButton(" Петля")
        set_icon(self.loop_btn, "loop", 14, on_color="#5ee6b0")
        self.loop_btn.setCheckable(True)
        self.loop_btn.setToolTip("Повторять участок: выделение на любой дорожке,\n"
                                 "а без выделения — часть между маркерами вокруг курсора")
        self.loop_btn.toggled.connect(self._toggle_loop)
        bar.addWidget(self.loop_btn)
        bar.addWidget(QLabel("Запись:"))
        self.rec_combo = QComboBox()
        self.rec_combo.setMinimumWidth(150)
        self.rec_combo.setMaximumWidth(320)
        self.rec_combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.rec_combo.setMinimumContentsLength(14)
        self.rec_combo.setToolTip("Какую запись показать под дорожками трека")
        self.rec_combo.activated.connect(self._on_rec_chosen)
        bar.addWidget(self.rec_combo)
        for text, name, tip, fn in (("", "refresh", "Обновить список записей", self.refresh_recordings),
                                    ("", "folder", "Открыть запись из другой папки", self._open_recording_dialog),
                                    (" Удалить запись", "trash", "Удалить выбранную запись целиком (в корзину)",
                                     self._delete_recording)):
            b = QPushButton(text)
            set_icon(b, name, 14)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            bar.addWidget(b)
        bar.addWidget(QLabel("Масштаб:"))
        self.zoom = QSlider(Qt.Horizontal)
        self.zoom.setRange(1, 400)
        self.zoom.setValue(20)
        self.zoom.setFixedWidth(120)
        self.zoom.valueChanged.connect(self._on_zoom)
        bar.addWidget(self.zoom)
        fit = QPushButton(" По размеру")
        set_icon(fit, "fit", 14)
        fit.clicked.connect(self.fit)
        bar.addWidget(fit)
        lay.addLayout(bar)
        for w in self.findChildren(QPushButton) + [self.rec_combo, self.zoom]:
            w.setFocusPolicy(Qt.NoFocus)   # Пробел всегда PLAY/STOP

        ruler_row = QHBoxLayout()
        ruler_row.addSpacing(HEADER_W + 6)
        self.ruler = Ruler(self.view)
        self.ruler.seek.connect(self.seek_requested.emit)
        self.ruler.marker_menu.connect(self._marker_menu)
        ruler_row.addWidget(self.ruler, 1)
        lay.addLayout(ruler_row)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.host = QWidget()
        self.rows = QVBoxLayout(self.host)
        self.rows.setContentsMargins(0, 0, 0, 0)
        self.rows.setSpacing(2)
        self.rows.addStretch(1)
        self.scroll.setWidget(self.host)
        lay.addWidget(self.scroll, 1)

        hs_row = QHBoxLayout()
        hs_row.addSpacing(HEADER_W + 6)
        self.hscroll = QScrollBar(Qt.Horizontal)
        self.hscroll.valueChanged.connect(self._on_hscroll)
        hs_row.addWidget(self.hscroll, 1)
        lay.addLayout(hs_row)
        self.refresh_recordings()

    # ── список записей ──────────────────────────────────────────────────────
    def refresh_recordings(self, select: Optional[str] = None) -> None:
        """Записи из папки записей (новые сверху) + выбранная, если она в другом месте."""
        select = select if select is not None else self.rec_folder
        self.rec_combo.blockSignals(True)
        self.rec_combo.clear()
        self.rec_combo.addItem("— не показывать запись —", "")
        folders = []
        root = self.rec_root()
        if root.exists():
            folders = sorted((d for d in root.iterdir() if d.is_dir() and any(d.glob("*.wav"))),
                             key=lambda d: d.name, reverse=True)
        paths = [str(d) for d in folders]
        if select and select not in paths and Path(select).exists():
            paths.insert(0, select)
        for path in paths:
            info = load_session(path)
            n = len(info.get("lanes", []))
            self.rec_combo.addItem(f"{Path(path).name}  ({n} кан.)", path)
        idx = self.rec_combo.findData(select) if select else 0
        self.rec_combo.setCurrentIndex(max(0, idx))
        self.rec_combo.blockSignals(False)

    def _on_rec_chosen(self, _i: int) -> None:
        folder = self.rec_combo.currentData() or ""
        if folder:
            self.set_recording(folder)
        else:
            self.rec_folder, self.rec_info = "", {}
            self._rebuild()

    def _delete_recording(self) -> None:
        folder = self.rec_combo.currentData() or ""
        if not folder:
            QMessageBox.information(self, "Удаление записи", "Выберите запись в списке.")
            return
        if self.is_recording(folder):
            QMessageBox.warning(self, "Удаление записи", "Эта запись сейчас идёт. Сначала остановите REC.")
            return
        files = list(Path(folder).glob("*"))
        size = sum(f.stat().st_size for f in files if f.is_file()) / 1e6
        used = [f"{label}" for label, s in self.all_songs()
                if any(Path(t.file).parent == Path(folder) for t in s.tracks)]
        text = f"Удалить запись «{Path(folder).name}» ({len(files)} файлов, {size:.0f} МБ) в корзину?"
        if used:
            text += "\n\nВнимание: дорожки из неё добавлены в треки:\n" + "\n".join(used[:5]) + \
                    "\nУ них эти дорожки станут пустыми."
        if QMessageBox.question(self, "Удаление записи", text) != QMessageBox.StandardButton.Yes:
            return
        if not move_to_trash(folder):
            if QMessageBox.question(self, "Удаление записи", "Корзина недоступна. Удалить безвозвратно?"
                                    ) != QMessageBox.StandardButton.Yes:
                return
            shutil.rmtree(folder, ignore_errors=True)
        if self.rec_folder == folder:
            self.rec_folder, self.rec_info = "", {}
        self.recording_deleted.emit(folder)
        self.refresh_recordings("")
        self._rebuild()

    def _delete_rec_lane(self, lane: Lane) -> None:
        if Path(lane.path).name == "Multitrack.wav":
            QMessageBox.information(self, "Удаление канала", "Запись сохранена одним многоканальным "
                                    "файлом — удалить можно только всю запись.")
            return
        if self.is_recording(self.rec_folder):
            QMessageBox.warning(self, "Удаление канала", "Эта запись сейчас идёт. Сначала остановите REC.")
            return
        if QMessageBox.question(self, "Удаление канала",
                                f"Удалить «{lane.name}» ({Path(lane.path).name}) в корзину?"
                                ) != QMessageBox.StandardButton.Yes:
            return
        if not move_to_trash(lane.path):
            Path(lane.path).unlink(missing_ok=True)
        self.set_recording(self.rec_folder)
        self.refresh_recordings()

    # ── данные ──────────────────────────────────────────────────────────────
    def set_song(self, song: Optional[Song], samplerate: int) -> None:
        changed = song is not self.song
        self.song, self.samplerate = song, samplerate
        if changed:
            self.reset_loop()
        self._update_markers()
        self._rebuild()

    # ── маркеры ─────────────────────────────────────────────────────────────
    def _update_markers(self) -> None:
        self.ruler.markers = self.song.markers_out() if self.song is not None else []
        self.ruler.update()

    def _sorted_src_markers(self) -> list[dict]:
        if self.song is None:
            return []
        self.song.markers.sort(key=lambda m: m["t"])
        dur = self.song.output_duration or float("inf")
        return [m for m in self.song.markers if 0 <= self.song.out_time(m["t"]) <= dur]

    def add_marker(self) -> None:
        if self.song is None:
            QMessageBox.information(self, "Маркер", "Сначала выберите трек.")
            return
        t_out = self.view.playhead
        used = {m.get("name") for m in self.song.markers}
        default = next((n for n in MARKER_PRESETS if n not in used), "Маркер")
        name, ok = QInputDialog.getItem(self, "Маркер", f"Название маркера в {fmt(t_out)}:",
                                        MARKER_PRESETS, MARKER_PRESETS.index(default)
                                        if default in MARKER_PRESETS else 0, True)
        if not ok or not name.strip():
            return
        self.song.markers.append({"t": round(self.song.src_time(t_out), 3), "name": name.strip()})
        self._sorted_src_markers()
        self._update_markers()
        self.markers_changed.emit(self.song)

    def _marker_menu(self, index: int, pos) -> None:
        markers = self._sorted_src_markers()
        if not (0 <= index < len(markers)):
            return
        m = markers[index]
        menu = QMenu(self)
        a_go = menu.addAction("Перейти")
        a_here = menu.addAction("Перенести в позицию курсора")
        a_ren = menu.addAction("Переименовать…")
        menu.addSeparator()
        a_del = menu.addAction("Удалить маркер")
        chosen = menu.exec(pos)
        if chosen is a_go:
            self.seek_requested.emit(max(0.0, self.song.out_time(m["t"])))
            return
        if chosen is a_here:
            m["t"] = round(self.song.src_time(self.view.playhead), 3)
        elif chosen is a_ren:
            name, ok = QInputDialog.getItem(self, "Маркер", "Название:", MARKER_PRESETS,
                                            MARKER_PRESETS.index(m["name"]) if m.get("name") in MARKER_PRESETS
                                            else 0, True)
            if not ok or not name.strip():
                return
            m["name"] = name.strip()
        elif chosen is a_del:
            self.song.markers.remove(m)
        else:
            return
        self._sorted_src_markers()
        self._update_markers()
        self.markers_changed.emit(self.song)

    # ── петля ───────────────────────────────────────────────────────────────
    def _loop_region(self) -> Optional[tuple[float, float]]:
        for lane in self.lanes:
            if lane.selection:
                return lane.selection
        # без выделения — участок между маркерами вокруг курсора
        if self.song is None:
            return None
        bounds = [0.0] + [t for t, _ in self.song.markers_out()] + [self.song.output_duration or 0.0]
        t = self.view.playhead
        for a, b in zip(bounds, bounds[1:]):
            if a <= t < b and b - a >= 0.5:
                return a, b
        return None

    def _toggle_loop(self, on: bool) -> None:
        if not on:
            self.ruler.loop = None
            self.ruler.update()
            self.loop_changed.emit(None)
            return
        region = self._loop_region()
        if region is None:
            self.loop_btn.blockSignals(True)
            self.loop_btn.setChecked(False)
            self.loop_btn.blockSignals(False)
            QMessageBox.information(self, "Петля", "Выделите участок мышью на любой дорожке "
                                    "или поставьте маркеры — петля будет между ними.")
            return
        self.ruler.loop = region
        self.ruler.update()
        self.loop_changed.emit(region)

    def reset_loop(self) -> None:
        if self.loop_btn.isChecked():
            self.loop_btn.setChecked(False)   # сигнал выключит петлю в движке

    def toggle_loop(self) -> None:
        self.loop_btn.toggle()

    def set_recording(self, folder: str) -> None:
        if not folder or not Path(folder).exists():
            return
        self.rec_folder = folder
        self.rec_info = load_session(folder)
        if self.rec_combo.findData(folder) < 0:
            self.refresh_recordings(folder)
        else:
            self.rec_combo.setCurrentIndex(self.rec_combo.findData(folder))
        self._rebuild()

    def _rec_offset(self) -> tuple[float, bool]:
        """Где во времени песни лежит начало записи; и синхронна ли запись с этой песней."""
        info = self.rec_info
        if self.song is not None and info.get("song_id") == self.song.id and info.get("song_pos") is not None:
            return float(info["song_pos"]) - float(info.get("latency", 0.0)), True
        return 0.0, False

    def _rebuild(self) -> None:
        for lv in self.lane_views:
            lv.setParent(None)
            lv.deleteLater()
        while self.rows.count():
            item = self.rows.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.lanes, self.lane_views = [], []
        song = self.song
        if song is not None:
            self._section(f"ТРЕК «{song.name}» — {len(song.tracks)} дор.")
            e = song.edit
            for cfg in song.tracks:
                self._add_lane(Lane("song", cfg.name, cfg.file, cfg=cfg, speed=e.speed,
                                    src_start=e.start, song_end=song.output_duration))
        else:
            self._section("Трек не выбран — двойной клик в списке треков")
        if self.rec_folder:
            offset, synced = self._rec_offset()
            note = f"с {fmt(offset)} трека" if synced else "не привязана к этому треку — с начала"
            self._section(f"ЗАПИСЬ: {Path(self.rec_folder).name}  ({note})")
            for name, path, col in self.rec_info.get("lanes", []):
                multi = Path(path).name == "Multitrack.wav"
                self._add_lane(Lane("rec", name, path, column=col if multi else None, offset=offset))
        self.rows.addStretch(1)
        self._update_duration()

    def _section(self, text: str) -> None:
        lbl = QLabel(text)
        lbl.setStyleSheet("color:#7f8b97;font-size:11px;font-weight:700;padding:1px 2px 0px 2px")
        self.rows.addWidget(lbl)

    def _add_lane(self, lane: Lane) -> None:
        row = QFrame()
        row.setStyleSheet("QFrame{background:#191d22;border-radius:4px}")
        hl = QHBoxLayout(row)
        hl.setContentsMargins(4, 2, 0, 2)
        head = QWidget()
        head.setFixedWidth(HEADER_W)
        hv = QVBoxLayout(head)
        hv.setContentsMargins(2, 0, 2, 0)
        hv.setSpacing(1)
        name = ElidedLabel(lane.name)
        name.setStyleSheet("font-weight:700;font-size:11px;background:transparent")
        name.setToolTip(lane.path)
        hv.addWidget(name)
        btns = QHBoxLayout()
        btns.setSpacing(3)
        if lane.kind == "song":
            for name, tip, fn in (("scissors", "Обрезать по выделению", self._trim),
                                  ("reset", "Снять обрезку", self._reset_trim),
                                  ("trash", "Убрать дорожку из трека (файл на диске не удаляется)",
                                   self._delete_track)):
                b = QPushButton("")
                set_icon(b, name, 13)
                b.setToolTip(tip)
                b.setFixedSize(24, 20)
                b.setObjectName("iconBtn")   # без отступов — иначе значок не виден
                b.setFocusPolicy(Qt.NoFocus)
                b.clicked.connect(lambda _=False, ln=lane, f=fn: f(ln))
                btns.addWidget(b)
        else:
            for name, tip, fn in (("save", "Сохранить на компьютер (выделение или целиком)", self._save_rec),
                                  ("plus", "Добавить в трек новой дорожкой", self._rec_to_song),
                                  ("trash", "Удалить этот канал записи (в корзину)", self._delete_rec_lane)):
                b = QPushButton("")
                set_icon(b, name, 13)
                b.setToolTip(tip)
                b.setFixedSize(24, 20)
                b.setObjectName("iconBtn")   # без отступов — иначе значок не виден
                b.setFocusPolicy(Qt.NoFocus)
                b.clicked.connect(lambda _=False, ln=lane, f=fn: f(ln))
                btns.addWidget(b)
        btns.addStretch(1)
        hv.addLayout(btns)
        hl.addWidget(head)
        lv = LaneView(lane, self.view)
        lv.seek.connect(self.seek_requested.emit)
        hl.addWidget(lv, 1)
        self.rows.addWidget(row)
        self.lanes.append(lane)
        self.lane_views.append(lv)
        self._load_peaks(lane)

    def _load_peaks(self, lane: Lane) -> None:
        try:
            mtime = Path(lane.path).stat().st_mtime_ns
        except OSError:
            lane.peaks, lane.length = np.zeros(0, np.float32), 0.0
            return
        key = (lane.path, lane.column, mtime)
        if key in _peaks_cache:
            lane.peaks, lane.length = _peaks_cache[key]
            return
        waiting = self._pending.setdefault(key, [])
        waiting.append(lane)
        if len(waiting) == 1:
            QThreadPool.globalInstance().start(_PeakTask(key, lane.path, lane.column, self.signals))

    def _on_peaks(self, key, peaks, length: float) -> None:
        _peaks_cache[key] = (peaks, length)
        for lane in self._pending.pop(key, []):
            lane.peaks, lane.length = peaks, length
        for lv in self.lane_views:
            if lv.lane.peaks is peaks:
                lv.invalidate()
        self._update_duration()

    # ── масштаб и прокрутка ────────────────────────────────────────────────
    def _visible_seconds(self) -> float:
        return max(1.0, self.ruler.width() / self.view.pps)

    def _update_duration(self) -> None:
        ends = [ln.end_out for ln in self.lanes if ln.length > 0]
        if self.song is not None and self.song.output_duration:
            ends.append(self.song.output_duration)
        self.view.duration = max(ends + [10.0])
        self.hscroll.setRange(0, int(max(0.0, self.view.duration - self._visible_seconds() + 2) * 10))
        self.hscroll.setPageStep(int(self._visible_seconds() * 10))
        self.view.changed.emit()

    def _on_zoom(self, v: int) -> None:
        center = self.view.start + self._visible_seconds() / 2
        self.view.pps = float(v)
        self.view.start = max(0.0, center - self._visible_seconds() / 2)
        self._update_duration()
        self.hscroll.setValue(int(self.view.start * 10))

    def _on_hscroll(self, v: int) -> None:
        self.view.start = v / 10.0
        self.view.changed.emit()

    def fit(self) -> None:
        pps = max(1, int(self.ruler.width() / max(1.0, self.view.duration)))
        self.zoom.setValue(pps)
        self.hscroll.setValue(0)

    def set_playhead(self, t: float, playing: bool) -> None:
        if abs(t - self.view.playhead) < 1e-4 and not playing:
            return                       # стоим — курсор на месте, 30 перерисовок в секунду не нужны
        self.view.playhead = t
        vis = self._visible_seconds()
        if playing and not (self.view.start <= t <= self.view.start + vis * 0.95):
            self.hscroll.setValue(int(max(0.0, t - vis * 0.1) * 10))  # автопрокрутка за курсором
        self.view.playhead_moved.emit()

    def showEvent(self, e) -> None:
        super().showEvent(e)
        self._update_duration()

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._update_duration()

    # ── правка дорожек песни ───────────────────────────────────────────────
    def _check_edit(self) -> bool:
        err = self.can_edit()
        if err:
            QMessageBox.information(self, "Таймлайн", err)
            return False
        return True

    def _trim(self, lane: Lane) -> None:
        if not lane.selection:
            QMessageBox.information(self, "Обрезка", "Протяните мышью по волне дорожки, "
                                    "чтобы выделить участок, который нужно оставить.")
            return
        if not self._check_edit():
            return
        a, b = (lane.own(t) for t in lane.selection)
        lane.cfg.clip_start = max(0.0, float(a))
        lane.cfg.clip_end = float(b)
        lane.selection = None
        self.song_modified.emit(self.song)
        self._refresh_views()

    def _reset_trim(self, lane: Lane) -> None:
        if not (lane.cfg.clip_start or lane.cfg.clip_end) or not self._check_edit():
            return
        lane.cfg.clip_start = lane.cfg.clip_end = 0.0
        self.song_modified.emit(self.song)
        self._refresh_views()

    def _delete_track(self, lane: Lane) -> None:
        if self.song is None or lane.cfg not in self.song.tracks or not self._check_edit():
            return
        if self.cfg.confirm_delete and QMessageBox.question(
                self, "Удаление дорожки", f"Убрать дорожку «{lane.name}» из трека «{self.song.name}»?\n"
                "(Файл на диске не удаляется)") != QMessageBox.StandardButton.Yes:
            return
        self.song.tracks.remove(lane.cfg)
        self.song_modified.emit(self.song)
        self._rebuild()

    def _refresh_views(self) -> None:
        for lv in self.lane_views:
            lv.invalidate()

    def _add_files_to_song(self, files: list[str]) -> None:
        if self.song is None:
            QMessageBox.information(self, "Мультитрек", "Сначала выберите трек (двойной клик в списке).")
            return
        if not self._check_edit():
            return
        added = self.song.add_files(files, self.cfg.routing_defaults)
        if not added:
            QMessageBox.information(self, "Мультитрек", "Эти дорожки уже есть в треке.")
            return
        self.song_modified.emit(self.song)
        self._rebuild()

    def _add_from_playlists(self) -> None:
        choices = [(label, s) for label, s in self.all_songs() if self.song is None or s.id != self.song.id]
        if not choices:
            QMessageBox.information(self, "Мультитрек", "В плейлистах нет других треков.")
            return
        labels = [c[0] for c in choices]
        label, ok = QInputDialog.getItem(self, "Добавить мультитрек",
                                         "Дорожки какого трека добавить в текущий?", labels, 0, False)
        if ok:
            other = choices[labels.index(label)][1]
            self._add_files_to_song([t.file for t in other.tracks])

    def _add_from_files(self) -> None:
        start = self.song.folder if self.song else self.cfg.songs_folder
        files, _ = QFileDialog.getOpenFileNames(self, "Аудиофайлы для трека", start,
                                                "Аудио (*.wav *.mp3 *.flac *.aif *.aiff *.ogg)")
        if files:
            self._add_files_to_song(files)

    # ── записи ──────────────────────────────────────────────────────────────
    def _open_recording_dialog(self) -> None:
        start = self.cfg.last_recording or str(self.rec_root())
        folder = QFileDialog.getExistingDirectory(self, "Папка записи", str(Path(start).parent))
        if folder:
            self.set_recording(folder)

    def _read_lane(self, lane: Lane) -> tuple[np.ndarray, int]:
        data, sr = sf.read(lane.path, dtype="float32", always_2d=True)
        col = lane.column if lane.column is not None else 0
        return np.ascontiguousarray(data[:, col]), sr

    def _save_rec(self, lane: Lane) -> None:
        song_name = self.rec_info.get("song_name") or Path(self.rec_folder).name
        default = str(Path(self.rec_folder) / f"{song_name} - {lane.name}.wav")
        path, _ = QFileDialog.getSaveFileName(self, "Сохранить дорожку", default, "WAV (*.wav)")
        if not path:
            return
        try:
            mono, sr = self._read_lane(lane)
            if lane.selection:
                a, b = (int(round(lane.own(t) * sr)) for t in lane.selection)
                mono = mono[max(0, a):max(0, b)]
            sf.write(path, mono, sr, subtype=self.rec_info.get("subtype", "PCM_24"))
        except Exception as exc:
            QMessageBox.critical(self, "Сохранение", str(exc))
            return
        QMessageBox.information(self, "Сохранение", f"Сохранено:\n{path}\n({len(mono) / sr:.1f} с)")

    def _rec_to_song(self, lane: Lane) -> None:
        song = self.song
        if song is None:
            QMessageBox.information(self, "Запись", "Сначала выберите трек (двойной клик в списке).")
            return
        if song.edit.speed != 1.0 or song.edit.pitch:
            QMessageBox.warning(self, "Запись", "У трека изменены темп или тон. Добавить запись можно "
                                "только к треку без этих правок — сначала экспортируйте его в WAV.")
            return
        offset, synced = self._rec_offset()
        if not synced and QMessageBox.question(
                self, "Запись", "Эта запись не привязана к треку — она встанет с начала трека. Продолжить?"
        ) != QMessageBox.StandardButton.Yes:
            return
        if not self._check_edit():
            return
        try:
            mono, sr = self._read_lane(lane)
            if lane.selection:   # вне выделения — тишина, позиция сохраняется
                a, b = (int(round(lane.own(t) * sr)) for t in lane.selection)
                mono = mono.copy()
                mono[:max(0, a)] = 0.0
                mono[max(0, b):] = 0.0
            # выравнивание: начало записи → позиция в исходнике песни
            shift = int(round((song.edit.start + offset) * sr))
            if shift >= 0:
                mono = np.concatenate([np.zeros(shift, np.float32), mono])
            else:
                mono = mono[-shift:]
            out = Path(self.rec_folder) / f"{lane.name} ({song.name})_aligned.wav"
            sf.write(str(out), mono, sr, subtype="PCM_24")
        except Exception as exc:
            QMessageBox.critical(self, "Запись", str(exc))
            return
        song.add_files([str(out)], self.cfg.routing_defaults)
        for t in song.tracks:
            if Path(t.file) == out.resolve():
                t.name = f"Запись: {lane.name}"
                t.fx = dsp.vocal_defaults()      # сразу с эквалайзером и компрессором для голоса
        self.song_modified.emit(song)
        self._rebuild()
