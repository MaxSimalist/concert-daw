"""gui_main.py — Главное окно: транспорт, плейлисты, микшер дорожек, Auto-cue."""
from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Optional

from PySide6.QtCore import (QByteArray, QObject, QProcess, QRunnable, QSize, Qt, QThreadPool, QTimer,
                            QUrl, Signal)
from PySide6.QtGui import QBrush, QColor, QDesktopServices, QFont, QFontMetrics, QKeySequence, QShortcut
from PySide6.QtWidgets import (QAbstractItemView, QApplication, QFileDialog, QFrame, QHBoxLayout,
                               QInputDialog, QLabel, QListWidget, QListWidgetItem, QMainWindow,
                               QMenu, QMessageBox, QProgressBar, QPushButton, QScrollArea,
                               QSplitter, QStyle, QStyledItemDelegate, QStyleOptionViewItem,
                               QVBoxLayout, QWidget)

from audio_engine import (AudioEngine, DeviceInfo, LoadedSong, default_input_device,
                          find_fallback_device, find_startup_device, list_input_devices, load_song)
from gui_console import ConsolePanel
from gui_icons import icon, pixmap, set_icon, transport_icon
from gui_layout import ElidedLabel, FlowLayout, ReorderHost, fit_to_screen, move_in_list
from gui_timeline import TimelinePanel
import audio_edit
from key_detect import ALL_KEYS, analyze_song, key_label, tempo_label
from loudness import normalization_gain_db
from midi_ctl import ACTIONS, MidiController
from model import KEY_ALGO_VERSION, is_click_like
from recorder import Recorder, attach_to_song, free_space_gb, load_session, parse_channels
import dsp
from sync_server import SyncServer
from x32_osc import FxTempo, X32Osc
from gui_settings import SettingsDialog
from gui_rec_channels import RecChannelsDialog, summary_text
from gui_update import UpdateButton
from gui_song_editor import SongEditorDialog
from gui_widgets import TrackStrip, no_focus
from model import (ConfigStore, Playlist, Song, TrackConfig, is_audio_file, songs_from_files,
                   songs_from_paths)
from style import ACCENT, ORANGE, TEXT, repolish

ROLE_ID = Qt.UserRole
ROLE_RIGHT = Qt.UserRole + 1     # текст справа в строке трека: длительность, тональность, темп


class ElideItemDelegate(QStyledItemDelegate):
    """Строка списка: длинное название сокращается с «…», а справа всегда видна
    служебная часть (длительность, тональность, темп). Горизонтальной прокрутки нет."""

    def paint(self, painter, option, index) -> None:
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        text, opt.text = opt.text, ""
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, widget)   # фон, выделение
        rect = style.subElementRect(QStyle.SE_ItemViewItemText, opt, widget).adjusted(2, 0, -4, 0)
        fm = QFontMetrics(opt.font)
        right = index.data(ROLE_RIGHT) or ""
        selected = bool(opt.state & QStyle.State_Selected)
        brush = index.data(Qt.ForegroundRole)
        color = QColor("#ffffff") if selected else (brush.color() if isinstance(brush, QBrush)
                                                   else opt.palette.text().color())
        painter.save()
        painter.setFont(opt.font)
        right_w = 0
        if right:
            right_w = fm.horizontalAdvance(right) + 10
            painter.setPen(QColor("#c7d0d8") if selected else QColor("#7f8b97"))
            painter.drawText(rect, Qt.AlignRight | Qt.AlignVCenter, right)
        name_rect = rect.adjusted(0, 0, -right_w, 0)
        painter.setPen(color)
        painter.drawText(name_rect, Qt.AlignLeft | Qt.AlignVCenter,
                         fm.elidedText(text, Qt.ElideRight, max(10, name_rect.width())))
        painter.restore()

    def sizeHint(self, option, index) -> QSize:  # noqa: N802
        return QSize(20, super().sizeHint(option, index).height())   # ширина = ширине списка


def elide_list(widget: QListWidget) -> None:
    widget.setItemDelegate(ElideItemDelegate(widget))
    widget.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    widget.setTextElideMode(Qt.ElideRight)
    widget.setWordWrap(False)


def fmt_time(sec: float) -> str:
    sec = max(0, int(sec))
    return f"{sec // 60:02d}:{sec % 60:02d}"


def caption(text: str) -> QLabel:
    lbl = ElidedLabel(text, full_tooltip=True)
    lbl.setObjectName("caption")
    return lbl


def local_paths(event) -> list[str]:
    md = event.mimeData()
    if not md.hasUrls():
        return []
    return [u.toLocalFile() for u in md.urls() if u.isLocalFile()]


# ──────────────────────────────────────────────────────────────────────────────
# Фоновая загрузка песни в RAM (чтобы не морозить GUI и не трогать аудиопоток)
# ──────────────────────────────────────────────────────────────────────────────
class _LoaderSignals(QObject):
    done = Signal(str, object, str)  # song_id, LoadedSong | None, текст ошибки
    progress = Signal(str, str)      # song_id, текст


def song_signature(song: Song) -> str:
    """Всё, от чего зависит звук трека в памяти: правки (тон, темп, обрезка) и дорожки.
    Если за время загрузки трек изменили — результат устарел и выбрасывается."""
    from dataclasses import asdict
    return json.dumps([asdict(song.edit),
                       [(t.file, t.transpose, t.clip_start, t.clip_end) for t in song.tracks]],
                      sort_keys=True, ensure_ascii=False)


class _SongLoadTask(QRunnable):
    def __init__(self, song: Song, samplerate: int, blocksize: int, signals: _LoaderSignals):
        super().__init__()
        self.song, self.samplerate, self.blocksize, self.signals = song, samplerate, blocksize, signals
        self.sig = song_signature(song)   # снимок на момент запроса (в GUI-потоке)

    def run(self) -> None:
        sid = self.song.id
        try:
            loaded = load_song(self.song, self.samplerate, self.blocksize,
                               lambda text: self.signals.progress.emit(sid, text))
            loaded.sig = self.sig
            self.signals.done.emit(sid, loaded, "")
        except Exception as exc:
            self.signals.done.emit(sid, None, str(exc))


class _KeySignals(QObject):
    done = Signal(str, object)  # id трека, результат analyze_song (кортеж)


class _KeyTask(QRunnable):
    """Анализ песни в фоне: тональность (если ещё нет) и темп (если ещё нет)."""

    def __init__(self, song: Song, signals: _KeySignals):
        super().__init__()
        self.song, self.signals = song, signals

    def run(self) -> None:
        need_key = not self.song.key or self.song.key_outdated
        need_bpm, need_lufs = self.song.bpm == 0, self.song.lufs == 0
        need_silence = self.song.silence_lead < 0
        try:
            # анализ в процессе-работнике: не отнимает GIL у звука и интерфейса
            from dataclasses import asdict
            res = audio_edit.get_executor().submit(
                analyze_song, asdict(self.song), need_key, need_bpm, need_lufs, need_silence).result()
        except Exception:
            res = analyze_song_local(self.song, need_key, need_bpm, need_lufs, need_silence)
        self.signals.done.emit(self.song.id, tuple(res))


def analyze_song_local(song: Song, need_key: bool, need_bpm: bool, need_lufs: bool,
                       need_silence: bool) -> tuple:
    """Запасной вариант, если пул процессов недоступен."""
    from dataclasses import asdict
    return analyze_song(asdict(song), need_key, need_bpm, need_lufs, need_silence)


class FitScrollArea(QScrollArea):
    """Обёртка зоны: если окно уже, чем нужно содержимому, появляется горизонтальная
    прокрутка — содержимое никогда не уходит за край окна/экрана."""

    def __init__(self, widget: QWidget, parent=None):
        super().__init__(parent)
        self.setWidget(widget)
        self.setWidgetResizable(True)
        self.setFrameShape(QFrame.NoFrame)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)

    def _need_hbar(self) -> int:
        w = self.widget()
        if w is not None and w.minimumSizeHint().width() > self.viewport().width():
            return self.horizontalScrollBar().sizeHint().height()
        return 0

    def minimumSizeHint(self) -> QSize:  # noqa: N802
        w = self.widget()
        h = w.minimumSizeHint().height() if w is not None else 0
        return QSize(120, h + self._need_hbar())

    def sizeHint(self) -> QSize:  # noqa: N802
        w = self.widget()
        s = w.sizeHint() if w is not None else QSize(120, 40)
        return QSize(s.width(), max(s.height(), w.minimumSizeHint().height() if w else 0) + self._need_hbar())

    def resizeEvent(self, e) -> None:  # noqa: N802
        super().resizeEvent(e)
        self.updateGeometry()


class SeekBar(QProgressBar):
    """Полоса прогресса, по которой можно кликнуть или протащить — перемотка.
    Во время перетаскивания показываем время, прыгаем — при отпускании."""
    preview = Signal(float)         # 0..1 во время перетаскивания
    seek_requested = Signal(float)  # 0..1 при отпускании

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setRange(0, 1000)
        self.setTextVisible(False)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Клик или перетаскивание — перемотка. Стрелки ← / → — на 5 секунд")
        self.dragging = False

    def _frac(self, e) -> float:
        return min(max(e.position().x() / max(1, self.width()), 0.0), 1.0)

    def mousePressEvent(self, e) -> None:
        self.dragging = True
        self.setValue(int(self._frac(e) * 1000))
        self.preview.emit(self._frac(e))

    def mouseMoveEvent(self, e) -> None:
        if self.dragging:
            self.setValue(int(self._frac(e) * 1000))
            self.preview.emit(self._frac(e))

    def mouseReleaseEvent(self, e) -> None:
        if self.dragging:
            self.dragging = False
            self.seek_requested.emit(self._frac(e))


# ──────────────────────────────────────────────────────────────────────────────
# Списки и зоны, принимающие перетаскивание из Проводника
# ──────────────────────────────────────────────────────────────────────────────
class SongListWidget(QListWidget):
    """Список песен: перетаскивание внутри — смена порядка;
    папки/файлы из Проводника — добавление песен в место отпускания."""
    order_changed = Signal()
    paths_dropped = Signal(list, int)  # пути, строка вставки (-1 = в конец)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setDragDropMode(QAbstractItemView.InternalMove)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, e) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragEnterEvent(e)

    def dragMoveEvent(self, e) -> None:
        if e.mimeData().hasUrls():
            e.acceptProposedAction()
        else:
            super().dragMoveEvent(e)

    def dropEvent(self, e) -> None:
        paths = local_paths(e)
        if paths:
            e.acceptProposedAction()
            self.paths_dropped.emit(paths, self.indexAt(e.position().toPoint()).row())
            return
        super().dropEvent(e)
        self.order_changed.emit()


class PlaylistListWidget(QListWidget):
    """Папку бросили на плейлист — песни добавятся в него; на пустое место — новый плейлист."""
    paths_dropped = Signal(list, int)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)
        self.setDragDropMode(QAbstractItemView.DropOnly)

    def dragEnterEvent(self, e) -> None:
        e.acceptProposedAction() if e.mimeData().hasUrls() else e.ignore()

    def dragMoveEvent(self, e) -> None:
        e.acceptProposedAction() if e.mimeData().hasUrls() else e.ignore()

    def dropEvent(self, e) -> None:
        paths = local_paths(e)
        if paths:
            e.acceptProposedAction()
            self.paths_dropped.emit(paths, self.indexAt(e.position().toPoint()).row())


class MixerDropArea(QScrollArea):
    """Аудиофайлы, брошенные на микшер, становятся новыми дорожками текущей песни."""
    files_dropped = Signal(list)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAcceptDrops(True)

    def dragEnterEvent(self, e) -> None:
        e.acceptProposedAction() if e.mimeData().hasUrls() else e.ignore()

    def dragMoveEvent(self, e) -> None:
        e.acceptProposedAction() if e.mimeData().hasUrls() else e.ignore()

    def dropEvent(self, e) -> None:
        files = []
        for p in local_paths(e):
            if os.path.isdir(p):
                files += [str(f) for f in sorted(Path(p).iterdir()) if is_audio_file(f)]
            elif is_audio_file(p):
                files.append(p)
        if files:
            e.acceptProposedAction()
            self.files_dropped.emit(files)


# ──────────────────────────────────────────────────────────────────────────────
# Главное окно
# ──────────────────────────────────────────────────────────────────────────────
class MainWindow(QMainWindow):
    def __init__(self, store: Optional[ConfigStore] = None):
        super().__init__()
        self.setWindowTitle("Concert DAW — Behringer X32")
        self.setMinimumSize(640, 420)
        fit_to_screen(self, 1440, 860)   # не больше экрана ноутбука

        self.store = store or ConfigStore()
        self.cfg = self.store.load()
        self.engine = AudioEngine()
        self.engine.samplerate = self.cfg.samplerate
        self.engine.blocksize = self.cfg.blocksize

        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(1)          # грузим по одной песне — меньше нагрузка на диск
        self.loader_signals = _LoaderSignals()
        self.loader_signals.done.connect(self._on_song_loaded)
        self.loader_signals.progress.connect(self._on_load_progress)
        self.cache: dict[str, LoadedSong] = {}  # в RAM: текущая + следующая песня
        self.loading: set[str] = set()

        self.shown_playlist: Optional[Playlist] = None
        self.cued_id: Optional[str] = None
        self.strips: list[TrackStrip] = []
        self._play_btn_active = None
        self._tick_count = 0
        self._autoplay_id: Optional[str] = None
        self._autoplay_at = 0.0
        self._autoplay_silent = False
        self._using_fallback = False
        self._switch_to: Optional[str] = None     # песня, на которую переключаемся на ходу
        self._switch_force_play = False           # переключение двойным кликом — всегда играть
        self._pending_attach = ""                # запись, которую добавить в трек после STOP
        self._paused_id: Optional[str] = None   # трек, поставленный на PAUSE (продолжить без отсчёта)
        self._rewind_after_stop = False           # STOP: после затухания вернуться в начало
        self.recorder = Recorder()
        self.engine.recorder = self.recorder
        self._rec_auto = False                    # запись запущена автоматически с PLAY
        self.key_pool = QThreadPool(self)
        self.key_pool.setMaxThreadCount(1)
        self.key_signals = _KeySignals()
        self.key_signals.done.connect(self._on_key_detected)
        self._key_busy = False
        self.osc = X32Osc()                         # пульт по сети
        self.fx_tempo = FxTempo(self.osc, self.cfg.fx_time_scale)
        self.midi = MidiController()                # педаль / контроллер

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(800)
        self._save_timer.timeout.connect(self._save_now)

        self._build_ui()
        self._install_shortcuts()
        # мастер-часы для видео-программы (Concert Video на другом компьютере)
        self.sync = SyncServer()
        self._sync_tick = 0
        self._apply_video_sync()
        if not dsp.DSP_AVAILABLE:
            # без scipy встроенные EQ/компрессор не обрабатывают звук — подсказать, как исправить
            QTimer.singleShot(5000, lambda: self._status(
                "EQ и компрессор на дорожках ещё не работают: нажмите EQ на любой дорожке → «Установить»",
                warn=True) if not dsp.DSP_AVAILABLE else None)
        self._reload_playlists()
        self._apply_remote()

        self._poll = QTimer(self)
        self._poll.setInterval(33)               # ~30 кадров/с: время, индикаторы, Auto-cue
        self._poll.timeout.connect(self._on_tick)
        self._poll.start()

    # ═════════════════════════ построение интерфейса ═════════════════════════
    def _build_ui(self) -> None:
        # Все зоны окна разделены сплиттерами — границы можно тянуть мышью,
        # размеры запоминаются в конфиге (AppConfig.ui_splitters).
        self.split_root = QSplitter(Qt.Vertical)          # транспорт | рабочая область
        self.split_root.addWidget(self._build_transport())
        self.split_main = QSplitter(Qt.Horizontal)        # плейлисты/треки | таймлайн/микшер
        self.split_main.addWidget(self._build_left_panel())
        self.split_main.addWidget(self._build_mixer())
        self.split_main.setStretchFactor(1, 1)
        self.split_main.setSizes([300, 1000])
        self.split_root.addWidget(self.split_main)
        self.split_root.setStretchFactor(1, 1)
        self.split_root.setSizes([150, 750])
        self.splitters = {"root": self.split_root, "main": self.split_main,
                          "left": self.split_left, "right": self.split_right, "mixer": self.split_mixer}
        for sp in self.splitters.values():
            sp.setChildrenCollapsible(False)
            sp.setHandleWidth(6)
        self._restore_splitters()
        self.setCentralWidget(self.split_root)

        self.status_label = ElidedLabel("", full_tooltip=True)
        self.status_label.setObjectName("statusMsg")
        self.audio_label = ElidedLabel("Аудио не открыто", full_tooltip=True)
        self.audio_label.setMaximumWidth(620)
        self.audio_label.setObjectName("audioInfo")
        self.statusBar().addWidget(self.status_label, 1)
        self.video_label = QLabel("")
        self.video_label.setObjectName("statusMsg")
        self.video_label.setToolTip("Видео-программа Concert Video, подключённая к этому компьютеру")
        self.statusBar().addPermanentWidget(self.video_label)
        self.statusBar().addPermanentWidget(self.audio_label)
        self.update_btn = UpdateButton(self, self._update_blocker, lambda: self.cfg.update_checked,
                                       self._set_update_checked, lambda: self.cfg.update_auto)
        self.statusBar().addPermanentWidget(self.update_btn)

    def _update_blocker(self) -> str:
        """Можно ли сейчас закрыть программу ради обновления ("" — можно)."""
        if self.engine.is_playing or self.recorder.active:
            return "Сначала остановите воспроизведение и запись — обновление перезапускает программу."
        return ""

    def _set_update_checked(self, t: float) -> None:
        self.cfg.update_checked = t
        self._schedule_save()

    def _build_transport(self) -> QWidget:
        bar = QFrame()
        bar.setObjectName("transport")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(10, 8, 10, 8)
        lay.setSpacing(8)

        self.prev_btn = QPushButton("")
        self.prev_btn.setObjectName("navBtn")
        self.prev_btn.setToolTip("Предыдущий трек (PgUp)")
        self.play_btn = QPushButton(" PLAY")
        self.play_btn.setObjectName("playBtn")
        self.play_btn.setToolTip("Играть (Пробел — играть/пауза)")
        self.stop_btn = QPushButton(" STOP")
        self.stop_btn.setObjectName("stopBtn")
        self.stop_btn.setToolTip("Стоп и в начало трека (Esc)")
        self.pause_btn = QPushButton(" PAUSE")
        self.pause_btn.setObjectName("pauseBtn")
        self.pause_btn.setToolTip("Пауза / продолжить с того же места (Пробел)")
        self.next_btn = QPushButton("")
        self.next_btn.setObjectName("navBtn")
        self.next_btn.setToolTip("Следующий трек (PgDown)")
        self.rec_btn = QPushButton(" REC")
        self.rec_btn.setObjectName("recBtn")
        self.rec_btn.setCheckable(True)
        self.rec_btn.setToolTip("Запись с пульта (Ctrl+R). Входы и формат — Настройки → Запись")
        # одинаковые нарисованные значки (а не символы шрифта, которые Windows рисует по-разному)
        for btn, kind, color, on_color, size in (
                (self.prev_btn, "prev", "#e6eaee", "", 26), (self.play_btn, "play", "#ffffff", "", 24),
                (self.pause_btn, "pause", "#ffffff", "", 22),
                (self.stop_btn, "stop", "#ffffff", "", 22), (self.next_btn, "next", "#e6eaee", "", 26),
                (self.rec_btn, "rec", "#ef4b55", "#ffffff", 18)):
            btn.setIcon(transport_icon(kind, color, on_color))
            btn.setIconSize(QSize(size, size))
        for b in (self.prev_btn, self.play_btn, self.pause_btn, self.stop_btn, self.next_btn, self.rec_btn):
            lay.addWidget(b)

        info = QVBoxLayout()
        title_row = QHBoxLayout()
        self.song_label = ElidedLabel("— трек не выбран —")
        self.song_label.setObjectName("songTitle")
        self.key_label = ElidedLabel("")
        self.key_label.setObjectName("keyLabel")
        self.key_label.setToolTip("Тональность. Поправить — правый клик по треку → Тональность")
        self.metro_btn = QPushButton(" Метроном")
        set_icon(self.metro_btn, "metronome", 18, on_color="#1b1705")
        self.metro_btn.setObjectName("metroBtn")
        self.metro_btn.setCheckable(True)
        self.metro_btn.setToolTip("Метроном в темпе трека (M). Правый клик — задать темп вручную.\n"
                                  "Выход и громкость — Настройки → Звук")
        self.metro_btn.setContextMenuPolicy(Qt.CustomContextMenu)
        self.metro_btn.customContextMenuRequested.connect(self._set_tempo_manually)
        self.metro_btn.toggled.connect(self._on_metro_toggled)
        title_row.addWidget(self.song_label, 3)
        title_row.addWidget(self.key_label, 2)
        self.countin_btn = QPushButton("1-2-3-4")
        self.countin_btn.setObjectName("metroBtn")
        self.countin_btn.setCheckable(True)
        self.countin_btn.setChecked(self.cfg.countin_on)
        self.countin_btn.setToolTip("Отсчёт перед стартом трека (клавиша C).\n"
                                    "Число тактов и размер — Настройки → Звук")
        self.countin_btn.toggled.connect(self._on_countin_toggled)
        title_row.addWidget(self.metro_btn)
        title_row.addWidget(self.countin_btn)
        next_row = QHBoxLayout()
        self.next_label = ElidedLabel("")
        self.next_label.setObjectName("nextLabel")
        self.rec_label = ElidedLabel("")
        self.rec_label.setObjectName("recLabel")
        self.section_label = ElidedLabel("")
        self.section_label.setObjectName("keyLabel")
        self.section_label.setToolTip("Текущий маркер. Переход: 1–9, Shift+← / Shift+→")
        next_row.addWidget(self.next_label, 3)
        next_row.addWidget(self.section_label, 2)
        next_row.addWidget(self.rec_label, 2)
        self.progress = SeekBar()
        self.progress.setObjectName("songProgress")
        self.progress.preview.connect(self._on_seek_preview)
        self.progress.seek_requested.connect(self._on_seek)
        info.addLayout(title_row)
        info.addLayout(next_row)
        info.addWidget(self.progress)
        lay.addLayout(info, 1)

        times = QVBoxLayout()
        self.time_label = QLabel("00:00")
        self.time_label.setObjectName("timeLabel")
        self.time_label.setAlignment(Qt.AlignRight)
        self.remain_label = QLabel("-00:00")
        self.remain_label.setObjectName("remainLabel")
        self.remain_label.setAlignment(Qt.AlignRight)
        times.addWidget(self.time_label)
        times.addWidget(self.remain_label)
        lay.addLayout(times)

        side = QVBoxLayout()
        self.autocue_btn = QPushButton("AUTO-CUE")
        self.autocue_btn.setObjectName("autoCueBtn")
        self.autocue_btn.setCheckable(True)
        self.autocue_btn.setChecked(self.cfg.auto_cue)
        self.autocue_btn.setToolTip("Доиграть трек, остановиться и встать на начало следующего")
        self.settings_btn = QPushButton(" Настройки")
        set_icon(self.settings_btn, "settings", 18)
        side.addWidget(self.autocue_btn)
        side.addWidget(self.settings_btn)
        lay.addLayout(side)

        no_focus(self.prev_btn, self.play_btn, self.pause_btn, self.stop_btn, self.next_btn, self.rec_btn,
                 self.autocue_btn, self.settings_btn, self.progress, self.metro_btn, self.countin_btn)
        self.rec_btn.clicked.connect(self._on_rec_clicked)
        self.rec_btn.setContextMenuPolicy(Qt.CustomContextMenu)
        self.rec_btn.customContextMenuRequested.connect(lambda _p: self.pick_rec_channels())
        self._update_rec_tooltip()
        self.play_btn.clicked.connect(self.play)
        self.stop_btn.clicked.connect(self.stop)
        self.pause_btn.clicked.connect(self.pause)
        self.prev_btn.clicked.connect(lambda: self._cue_neighbor(-1))
        self.next_btn.clicked.connect(lambda: self._cue_neighbor(+1))
        self.autocue_btn.toggled.connect(self._on_autocue)
        self.settings_btn.clicked.connect(lambda: self.open_settings())
        return FitScrollArea(bar)

    def _build_left_panel(self) -> QWidget:
        self.split_left = QSplitter(Qt.Vertical)        # плейлисты | треки
        top = QFrame()
        top.setObjectName("leftPanel")
        lay = QVBoxLayout(top)
        lay.setContentsMargins(10, 10, 10, 6)

        lay.addWidget(caption("ПЛЕЙЛИСТЫ — можно бросить сюда папку сета"))
        self.playlist_list = PlaylistListWidget()
        elide_list(self.playlist_list)
        self.playlist_list.setMinimumHeight(60)
        self.playlist_list.currentRowChanged.connect(self._on_playlist_selected)
        self.playlist_list.paths_dropped.connect(self._on_playlist_drop)
        lay.addWidget(self.playlist_list)
        row = FlowLayout()
        for text, name, slot, tip in ((" Новый", "plus", self._add_playlist, "Новый плейлист"),
                                      ("", "edit", self._rename_playlist, "Переименовать плейлист"),
                                      ("", "trash", self._delete_playlist, "Удалить плейлист")):
            b = QPushButton(text)
            set_icon(b, name)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            no_focus(b)
            row.addWidget(b)
        lay.addLayout(row)
        self.split_left.addWidget(top)

        bottom = QFrame()
        bottom.setObjectName("leftPanel")
        lay = QVBoxLayout(bottom)
        lay.setContentsMargins(10, 6, 10, 10)
        lay.addWidget(caption("ТРЕКИ — перетаскивайте сюда файлы WAV / MP3 и папки"))
        self.song_list = SongListWidget()
        elide_list(self.song_list)
        self.song_list.setObjectName("songList")
        self.song_list.itemDoubleClicked.connect(self._on_song_activated)
        self.song_list.order_changed.connect(self._on_songs_reordered)
        self.song_list.paths_dropped.connect(self._on_songs_drop)
        self.song_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.song_list.customContextMenuRequested.connect(self._song_menu)
        lay.addWidget(self.song_list, 1)
        row2 = FlowLayout()
        for text, name, slot, tip in ((" Трек", "music", self._add_track_files,
                                       "Выбрать аудиофайлы (WAV, MP3, FLAC…) — каждый станет треком"),
                                      (" Мультитрек", "layers", self._add_song_folder,
                                       "Папка с дорожками одного трека (Клик, Гайд, Бас…)"),
                                      (" Сет", "list", self._add_songs_bulk,
                                       "Папка, где каждая подпапка — трек"),
                                      ("", "close", self._remove_song, "Убрать трек из плейлиста")):
            b = QPushButton(text)
            set_icon(b, name)
            b.setToolTip(tip)
            b.clicked.connect(slot)
            no_focus(b)
            row2.addWidget(b)
        lay.addLayout(row2)
        lay.addWidget(caption("Двойной клик — играть, правый клик — меню"))
        self.split_left.addWidget(bottom)
        self.split_left.setStretchFactor(1, 1)
        self.split_left.setSizes([200, 500])
        return self.split_left

    def _build_mixer(self) -> QWidget:
        self.split_right = QSplitter(Qt.Vertical)       # таймлайн | микшер
        tl_frame = QFrame()
        tl_frame.setObjectName("mixerPanel")
        tl_lay = QVBoxLayout(tl_frame)
        tl_lay.setContentsMargins(10, 8, 10, 4)
        self.timeline = TimelinePanel(self.cfg, self._timeline_can_edit, self._all_songs,
                                      self._rec_root, self._is_active_recording)
        self.timeline.song_modified.connect(self._on_timeline_song_modified)
        self.timeline.seek_requested.connect(self._on_timeline_seek)
        self.timeline.recording_deleted.connect(self._on_recording_deleted)
        self.timeline.markers_changed.connect(lambda _s: self._schedule_save())
        self.timeline.loop_changed.connect(self._on_loop_changed)
        tl_lay.addWidget(self.timeline)
        self.split_right.addWidget(tl_frame)

        w = QFrame()
        w.setObjectName("mixerPanel")
        lay = QVBoxLayout(w)
        lay.setContentsMargins(10, 4, 10, 10)
        head = FlowLayout()
        self.mode_label = caption("ДОРОЖКИ")
        head.addWidget(self.mode_label)
        self.loading_label = ElidedLabel("")
        self.loading_label.setObjectName("loadingLabel")
        self.edit_btn = QPushButton(" Редактор трека")
        set_icon(self.edit_btn, "edit")
        self.edit_btn.setToolTip("Темп, тональность, обрезка, фейды, экспорт WAV")
        self.edit_btn.clicked.connect(self.open_song_editor)
        self.add_tracks_btn = QPushButton(" Дорожки")
        set_icon(self.add_tracks_btn, "plus")
        self.add_tracks_btn.setToolTip("Добавить аудиофайлы в текущий трек (или перетащите их сюда)")
        self.add_tracks_btn.clicked.connect(self._add_tracks_dialog)
        self.timeline_btn = QPushButton(" Таймлайн")
        set_icon(self.timeline_btn, "timeline", on_color="#5ee6b0")
        self.timeline_btn.setCheckable(True)
        self.timeline_btn.setChecked(True)
        self.timeline_btn.setToolTip("Показать / скрыть таймлайн над фейдерами (Ctrl+T)")
        self.timeline_btn.toggled.connect(self._toggle_timeline)
        no_focus(self.edit_btn, self.add_tracks_btn, self.timeline_btn)
        head.addWidget(self.timeline_btn)
        head.addWidget(self.add_tracks_btn)
        head.addWidget(self.edit_btn)
        lay.addLayout(head)
        lay.addWidget(self.loading_label)
        lay.addWidget(self._build_trim_banner())

        self.scroll = MixerDropArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.files_dropped.connect(self._on_tracks_drop)
        self.strip_host = ReorderHost("track")          # дорожки переставляются мышью за название
        self.strip_host.moved.connect(self._on_strip_moved)
        self.strip_layout = QHBoxLayout(self.strip_host)
        self.strip_layout.setContentsMargins(0, 0, 0, 0)
        self.strip_layout.setSpacing(8)
        self.empty_hint = caption("Двойной клик по треку — играть. Сюда можно перетащить аудиофайлы")
        self.strip_layout.addWidget(self.empty_hint)
        self.strip_layout.addStretch(1)
        self.scroll.setWidget(self.strip_host)
        lay.addWidget(self.scroll, 1)

        # дорожки трека | фейдеры пульта X32 (граница двигается мышью)
        self.split_mixer = QSplitter(Qt.Horizontal)
        self.split_mixer.addWidget(w)
        self.console = ConsolePanel(self.cfg, self.osc)
        self.console.changed.connect(self._schedule_save)
        self.split_mixer.addWidget(self.console)
        self.split_mixer.setStretchFactor(0, 1)
        self.split_mixer.setSizes([800, 200])
        self.split_right.addWidget(self.split_mixer)
        self.split_right.setStretchFactor(1, 1)
        self.split_right.setSizes([300, 420])
        return self.split_right

    def _install_shortcuts(self) -> None:
        # ApplicationShortcut: Пробел работает, где бы ни был фокус (кроме полей ввода текста)
        for keys, handler in [("Space", self.toggle_play), ("Esc", self.stop),
                              ("PgDown", lambda: self._cue_neighbor(+1)),
                              ("PgUp", lambda: self._cue_neighbor(-1)),
                              ("Ctrl+,", self.open_settings),
                              ("Ctrl+E", self.open_song_editor),
                              ("Left", lambda: self._seek_relative(-5)),
                              ("Right", lambda: self._seek_relative(+5)),
                              ("Ctrl+R", self.toggle_record),
                              ("Ctrl+T", lambda: self.timeline_btn.toggle()),
                              ("M", self.metro_btn.toggle),
                              ("C", self.countin_btn.toggle),
                              ("L", lambda: self.timeline.toggle_loop()),
                              ("Shift+Left", lambda: self._marker_step(-1)),
                              ("Shift+Right", lambda: self._marker_step(+1))] + [
                              (str(n), lambda n=n: self._jump_marker(n)) for n in range(1, 10)]:
            sc = QShortcut(QKeySequence(keys), self)
            sc.setContext(Qt.ApplicationShortcut)
            sc.activated.connect(lambda h=handler: self._shortcut(h))

    def _shortcut(self, handler) -> None:
        """Пока открыт любой диалог (настройки, редактор, выбор файла), горячие клавиши
        главного окна не срабатывают — иначе Пробел на галочке в настройках запустил бы трек."""
        if QApplication.activeModalWidget() is None:
            handler()

    # ═════════════════════════════ аудиоустройство ════════════════════════════
    def start_audio(self) -> None:
        """Автопоиск ASIO-драйвера X32. Нет пульта — работаем через звуковую карту."""
        try:
            dev = find_startup_device(self.cfg.device_name)
            fallback = dev is None
            if fallback:
                dev = find_fallback_device()
        except Exception as exc:
            QMessageBox.critical(self, "Аудио", f"PortAudio не инициализировался:\n{exc}")
            return
        if dev is None:
            QMessageBox.warning(self, "Аудио", "Не найдено ни одного устройства вывода звука.")
            return
        if self._open_device(dev, self.cfg.samplerate, self.cfg.blocksize, remember=not fallback,
                             quiet=fallback) and fallback:
            self._using_fallback = True
            self._status(f"Пульт X32 не найден — играю через «{dev.name}». "
                         "Подключите пульт и выберите его в Настройки → Звук.", warn=True)

    def _open_device(self, dev: DeviceInfo, samplerate: int, blocksize: int,
                     remember: bool = True, quiet: bool = False) -> bool:
        """remember=False — временное устройство (пульт не найден): в конфиге
        остаётся пульт, и при следующем запуске программа снова будет искать его."""
        old_sr = self.engine.samplerate
        error = None
        for sr in dict.fromkeys((samplerate, int(dev.default_samplerate))):
            try:
                self.engine.open(dev, sr, blocksize)
                samplerate, error = sr, None
                break
            except Exception as exc:
                error = exc
                if dev.is_asio:
                    break  # у ASIO частоту задаёт пульт — подбирать бессмысленно
        if error is not None:
            if not quiet:
                QMessageBox.critical(
                    self, "Аудио",
                    f"Не удалось открыть «{dev.name}»:\n{error}\n\n"
                    "• Частота в программе должна совпадать с частотой пульта (X32: Setup → Config).\n"
                    "• ASIO-драйвер может использовать только одна программа — закройте другие DAW.\n"
                    "• Попробуйте буфер побольше.")
            self._update_audio_label()
            return False
        if remember:
            self.cfg.device_name, self.cfg.samplerate, self.cfg.blocksize = dev.name, samplerate, blocksize
            self._using_fallback = False
            self._schedule_save()
        self._apply_output_mode()
        if samplerate != old_sr or self.engine.song is None:
            # данные в RAM на старой частоте — перезагружаем текущую песню
            self.cache.clear()
            song = self._song_by_id(self.cued_id)
            if song:
                self.cue_song(song)
        else:
            self._rebuild_strips(self.engine.song)
        self._update_audio_label()
        return True

    def _apply_output_mode(self) -> None:
        dev = self.engine.device
        mode = self.cfg.output_mode
        if mode == "stereo":
            stereo = True
        elif mode == "routed":
            stereo = False
        else:  # авто: пульт и многоканальные карты — по выходам, обычная стерео-карта — сведение
            stereo = dev is not None and not dev.is_x32 and dev.outputs <= 2
        self.engine.stereo_mix = stereo
        self.mode_label.setText("ДОРОЖКИ — СВЕДЕНИЕ В СТЕРЕО (без пульта)" if stereo
                                else "ДОРОЖКИ И РОУТИНГ НА ВЫХОДЫ X32")
        for s in self.strips:
            s.set_stereo_mix(stereo)

    def open_settings(self, tab: str = "") -> None:
        if self.engine.is_playing or self.recorder.active:
            self._status("Остановите воспроизведение и запись перед изменением настроек", warn=True)
            return
        prev = self.engine.device
        dlg = SettingsDialog(self.cfg, self.engine, prev.name if prev else self.cfg.device_name, self,
                             osc=self.osc, midi=self.midi, initial_tab=tab, fx_types=self.fx_tempo.types,
                             console_names=self._console_channel_names,
                             video_clients=self.sync.clients, update_button=self.update_btn)
        accepted = bool(dlg.exec())
        if not accepted:
            self._apply_remote()   # диалог мог открыть другой MIDI-вход — возвращаем свой
            if prev is not None and not self.engine.is_open:
                # диалог пересканировал устройства (поток закрыт) и был отменён — возвращаем как было
                again = find_startup_device(prev.name) or find_fallback_device()
                if again:
                    self._open_device(again, self.engine.samplerate, self.engine.blocksize,
                                      remember=not self._using_fallback)
            self._update_audio_label()
            return

        r = dlg.result_values()
        scale_changed = abs(r.ui_scale - self.cfg.ui_scale) > 0.001
        c = self.cfg
        c.ui_scale, c.start_maximized = r.ui_scale, r.start_maximized
        c.auto_cue, c.auto_play_next, c.auto_play_delay = r.auto_cue, r.auto_play_next, r.auto_play_delay
        c.preload_next, c.confirm_delete, c.songs_folder = r.preload_next, r.confirm_delete, r.songs_folder
        c.output_mode = r.output_mode
        c.switch_while_playing = r.switch_while_playing
        c.rec_folder, c.rec_channels, c.rec_format = r.rec_folder, r.rec_channels, r.rec_format
        c.rec_split, c.rec_mode, c.rec_input_device = r.rec_split, r.rec_mode, r.rec_input_device
        c.rec_names, c.rec_chosen, c.rec_to_song = r.rec_names, r.rec_chosen, r.rec_to_song
        self._update_rec_tooltip()
        c.metro_output, c.metro_volume_db, c.metro_accent = r.metro_output, r.metro_volume_db, r.metro_accent
        c.metro_beats, c.countin_bars = r.metro_beats, r.countin_bars
        c.norm_on, c.norm_target, c.norm_max_boost = r.norm_on, r.norm_target, r.norm_max_boost
        c.osc_on, c.osc_ip, c.osc_when, c.osc_faders = r.osc_on, r.osc_ip, r.osc_when, r.osc_faders
        c.fx_tempo, c.silence_offer = r.fx_tempo, r.silence_offer
        c.fx_auto, c.fx_auto_div = r.fx_auto, r.fx_auto_div
        c.update_auto = r.update_auto
        c.video_sync_on, c.video_sync_port = r.video_sync_on, r.video_sync_port
        self._apply_video_sync()
        if r.reset_layout:
            self.reset_layout()
        c.midi_port, c.midi_map, c.midi_cc_any = r.midi_port, r.midi_map, r.midi_cc_any
        self._apply_metronome()
        self._apply_remote()
        for loaded in self.cache.values():
            self._apply_norm(loaded)
        self.autocue_btn.blockSignals(True)
        self.autocue_btn.setChecked(c.auto_cue)
        self.autocue_btn.blockSignals(False)

        sound_changed = (r.device is not None and (
            prev is None or r.device.name != prev.name or not self.engine.is_open
            or r.samplerate != self.engine.samplerate or r.blocksize != self.engine.blocksize))
        if sound_changed:
            self._open_device(r.device, r.samplerate, r.blocksize, remember=True)
        else:
            self._apply_output_mode()
        self._preload_next()
        self._update_next_label()
        self._save_now()

        if scale_changed and QMessageBox.question(
                self, "Масштаб", "Новый масштаб применится после перезапуска.\nПерезапустить сейчас?"
        ) == QMessageBox.StandardButton.Yes:
            self._restart()

    def _restart(self) -> None:
        self._save_now()
        self.recorder.stop()
        self.engine.close()
        audio_edit.shutdown()
        QProcess.startDetached(sys.executable, [os.path.abspath(sys.argv[0])] + sys.argv[1:],
                               os.getcwd())
        QApplication.quit()

    def _update_audio_label(self) -> None:
        """Строка состояния звука. Обновляется 2 раза в секунду, поэтому любая
        ошибка здесь не должна выбрасываться наружу (иначе окно ошибки — каждые 0.5 с)."""
        e = self.engine
        try:
            if not e.is_open or e.device is None:
                text, warn = "⚠ Аудиоустройство не открыто", True
            else:
                mode = "стерео" if e.stereo_mix else f"{e.num_outputs} вых."
                rec = f" + {e.duplex_inputs} вх." if e.duplex_inputs else ""
                text = (f"{e.device.name} ({mode}{rec}) | {e.samplerate} Гц | буфер {e.blocksize} "
                        f"({e.latency_ms:.1f} мс) | CPU {e.cpu_load * 100:.0f}% | выпадений: {e.xruns}")
                warn = e.xruns > 0
        except Exception as exc:
            text, warn = f"Звук: {exc}", True
        if self.audio_label.text() != text or self.audio_label.property("warn") != warn:
            self.audio_label.setText(text)
            self.audio_label.setProperty("warn", warn)
            repolish(self.audio_label)

    # ═══════════════════════════════ плейлисты ════════════════════════════════
    def _reload_playlists(self, select_id: Optional[str] = None) -> None:
        self.playlist_list.blockSignals(True)
        self.playlist_list.clear()
        for pl in self.cfg.playlists:
            item = QListWidgetItem(pl.name)
            item.setData(ROLE_ID, pl.id)
            self.playlist_list.addItem(item)
        self.playlist_list.blockSignals(False)
        target = select_id or self.cfg.last_playlist_id
        row = next((i for i, p in enumerate(self.cfg.playlists) if p.id == target), 0)
        if self.cfg.playlists:
            self.playlist_list.setCurrentRow(row)
            self._on_playlist_selected(row)
        else:
            self.shown_playlist = None
            self.song_list.clear()

    def _on_playlist_selected(self, row: int) -> None:
        self.shown_playlist = self.cfg.playlists[row] if 0 <= row < len(self.cfg.playlists) else None
        if self.shown_playlist:
            self.cfg.last_playlist_id = self.shown_playlist.id
        self._reload_songs()

    def _add_playlist(self) -> None:
        name, ok = QInputDialog.getText(self, "Новый плейлист", "Название:", text="Тур 2026 - ")
        if ok and name.strip():
            pl = Playlist(name=name.strip())
            self.cfg.playlists.append(pl)
            self._reload_playlists(pl.id)
            self._schedule_save()

    def _rename_playlist(self) -> None:
        pl = self.shown_playlist
        if not pl:
            return
        name, ok = QInputDialog.getText(self, "Переименовать", "Название:", text=pl.name)
        if ok and name.strip():
            pl.name = name.strip()
            self._reload_playlists(pl.id)
            self._schedule_save()

    def _confirm(self, text: str) -> bool:
        if not self.cfg.confirm_delete:
            return True
        return QMessageBox.question(self, "Удаление", text) == QMessageBox.StandardButton.Yes

    def _delete_playlist(self) -> None:
        pl = self.shown_playlist
        if not pl:
            return
        if self.engine.is_playing and any(s.id == self.cued_id for s in pl.songs):
            self._status("Нельзя удалить плейлист, из которого сейчас играет трек", warn=True)
            return
        if not self._confirm(f"Удалить плейлист «{pl.name}»?\n(Аудиофайлы на диске не трогаются)"):
            return
        self.cfg.playlists.remove(pl)
        for s in pl.songs:
            self._forget_song(s.id)
        self._reload_playlists()
        self._schedule_save()

    def _on_playlist_drop(self, paths: list, row: int) -> None:
        """Папка брошена на плейлист → песни в него; на пустое место → новый плейлист."""
        songs = self._songs_from(paths)
        if not songs:
            return
        if 0 <= row < len(self.cfg.playlists):
            pl = self.cfg.playlists[row]
        else:
            first = Path(paths[0])
            pl = Playlist(name=first.name if first.is_dir() else first.parent.name or "Новый плейлист")
            self.cfg.playlists.append(pl)
        pl.songs.extend(songs)
        self._reload_playlists(pl.id)
        self._preload_next()
        self._schedule_save()
        self._status(f"«{pl.name}»: добавлено треков — {len(songs)}")

    # ═════════════════════════════════ песни ══════════════════════════════════
    def _reload_songs(self) -> None:
        self.song_list.clear()
        pl = self.shown_playlist
        if pl:
            for s in pl.songs:
                item = QListWidgetItem(s.name)
                item.setData(ROLE_ID, s.id)
                self.song_list.addItem(item)
        self._refresh_song_items()

    def _refresh_song_items(self) -> None:
        """Нумерация, длительность, отметки ▸ (загружена) и » (следующая), правки темпа/тона."""
        nxt = self._neighbor(+1)
        nxt_id = nxt.id if nxt else None
        bold = QFont(self.song_list.font())
        bold.setBold(True)
        for i in range(self.song_list.count()):
            item = self.song_list.item(i)
            sid = item.data(ROLE_ID)
            song = self._song_by_id(sid)
            if song is None:
                continue
            prefix = "▸ " if sid == self.cued_id else ("» " if sid == nxt_id else "   ")
            right = []
            if song.key and song.key != "?":
                right.append(f"♪{key_label(song)}")
            if song.play_bpm >= 1:
                right.append(f"{song.play_bpm:.0f}♩")
            if song.duration:
                right.append(fmt_time(song.output_duration))
            mark = "✎" if not song.edit.is_identity else ""
            end = {"next": "→", "stop": "■"}.get(song.end_action, "")
            right += [x for x in (mark, end) if x]
            missing = song.missing_files
            gone = bool(song.tracks) and len(missing) == len(song.tracks)
            if gone:
                right = ["⚠ файл удалён"]
            elif missing:
                right.insert(0, f"⚠ нет {len(missing)} из {len(song.tracks)}")
            item.setText(f"{prefix}{i + 1}. {song.name}")
            item.setData(ROLE_RIGHT, "  ".join(right))
            tip = [song.name]
            if missing:
                tip.append("Нет на диске (удалены, переименованы или диск отключён):")
                tip += [f"  • {t.file}" for t in missing[:8]] + (["  …"] if len(missing) > 8 else [])
                tip.append("Правый клик → «Найти файлы…» или «Убрать из плейлиста»")
            if song.edit.summary():
                tip.append(song.edit.summary())
            tip.append(song.folder)
            item.setToolTip("\n".join(tip))
            font = QFont(bold if sid == self.cued_id else self.song_list.font())
            font.setStrikeOut(gone)
            item.setFont(font)
            color = ACCENT if sid == self.cued_id else (ORANGE if sid == nxt_id else TEXT)
            if gone:
                color = "#ef4b55"
            elif missing:
                color = "#f7c948"
            item.setForeground(QBrush(QColor(color)))

    def _songs_from(self, paths: list) -> list[Song]:
        try:
            songs = songs_from_paths(paths, self.cfg.routing_defaults)
        except Exception as exc:
            QMessageBox.warning(self, "Добавление треков", str(exc))
            return []
        if not songs:
            self._status("В перетащенном нет аудиофайлов (WAV, AIFF, FLAC, MP3)", warn=True)
        return songs

    def _ensure_playlist(self) -> Playlist:
        if self.shown_playlist is None:
            pl = Playlist(name="Новый плейлист")
            self.cfg.playlists.append(pl)
            self._reload_playlists(pl.id)
        return self.shown_playlist

    def _insert_songs(self, songs: list[Song], row: int = -1) -> None:
        pl = self._ensure_playlist()
        if 0 <= row <= len(pl.songs):
            pl.songs[row:row] = songs
        else:
            pl.songs.extend(songs)
        self._reload_songs()
        self._preload_next()
        self._update_next_label()
        self._schedule_save()
        self._status(f"Добавлено треков: {len(songs)}")

    def _on_songs_drop(self, paths: list, row: int) -> None:
        songs = self._songs_from(paths)
        if songs:
            self._insert_songs(songs, row)

    def _add_track_files(self) -> None:
        """«+ Трек»: выбираем аудиофайлы, каждый — отдельный трек с названием песни."""
        files, _ = QFileDialog.getOpenFileNames(
            self, "Добавить треки", self.cfg.songs_folder,
            "Аудио (*.wav *.mp3 *.flac *.aif *.aiff *.ogg);;WAV (*.wav);;MP3 (*.mp3);;Все файлы (*)")
        if not files:
            return
        try:
            songs = songs_from_files(files, self.cfg.routing_defaults)
        except Exception as exc:
            QMessageBox.warning(self, "Добавление треков", str(exc))
            return
        if songs:
            self.cfg.songs_folder = str(Path(files[0]).parent)
            self._insert_songs(songs)

    def _add_song_folder(self) -> None:
        """«+ Мультитрек»: папка с дорожками ОДНОГО трека (Клик, Гайд, Бас, Плейбек…)."""
        folder = QFileDialog.getExistingDirectory(self, "Папка с дорожками трека (Клик, Гайд, Бас, …)",
                                                  self.cfg.songs_folder)
        if not folder:
            return
        try:
            songs = [Song.from_folder(folder, self.cfg.routing_defaults)]
        except Exception as exc:
            QMessageBox.warning(self, "Мультитрек", str(exc))
            return
        self._insert_songs(songs)

    def _add_songs_bulk(self) -> None:
        parent = QFileDialog.getExistingDirectory(self, "Папка сета: каждая подпапка — отдельный трек",
                                                  self.cfg.songs_folder)
        if parent:
            songs = self._songs_from([parent])
            if songs:
                self._insert_songs(songs)

    def _remove_song(self) -> None:
        pl = self.shown_playlist
        item = self.song_list.currentItem()
        if not pl or item is None:
            return
        sid = item.data(ROLE_ID)
        if sid == self.cued_id and self.engine.is_playing:
            self._status("Нельзя убрать трек, который сейчас играет", warn=True)
            return
        song = self._song_by_id(sid)
        if song and not self._confirm(f"Убрать «{song.name}» из плейлиста?\n(Файлы на диске не трогаются)"):
            return
        pl.songs = [s for s in pl.songs if s.id != sid]
        self._forget_song(sid)
        self._reload_songs()
        self._update_next_label()
        self._schedule_save()

    def _song_menu(self, pos) -> None:
        item = self.song_list.itemAt(pos)
        if item is None:
            return
        song = self._song_by_id(item.data(ROLE_ID))
        if song is None:
            return
        m = QMenu(self)
        a_play = m.addAction(icon("play"), "Играть")
        a_load = m.addAction("Загрузить (встать на начало, не запуская)")
        a_edit = m.addAction("Редактор трека (темп, тон, обрезка)…")
        a_rename = m.addAction("Переименовать…")
        km = m.addMenu(f"Тональность: {song.key or 'не определена'}")
        a_detect = km.addAction("Определить заново")
        a_setkey = km.addAction("Указать вручную…")
        em = m.addMenu("В конце трека")
        end_actions = {}
        for key, text in (("default", "как в настройках"), ("stop", "стоп и ждать PLAY"),
                          ("next", "играть следующую")):
            a = em.addAction(text)
            a.setCheckable(True)
            a.setChecked(song.end_action == key)
            end_actions[a] = key
        a_trim = m.addAction(icon("scissors"), "Обрезать тишину в начале и в конце")
        lead, trail = song.silence_offer() if not song.silence_ignore else (0.0, 0.0)
        a_trim.setEnabled(bool(lead or trail) or song.silence_lead < 0)
        a_trim_all = m.addAction(icon("scissors"), "Обрезать тишину во всех треках плейлиста…")
        a_folder = m.addAction("Открыть папку")
        a_relink = a_drop = None
        if song.missing_files:
            m.addSeparator()
            a_relink = m.addAction(icon("search"), "Найти файлы… (указать папку, куда их перенесли)")
            if len(song.missing_files) < len(song.tracks):
                a_drop = m.addAction("Убрать из трека дорожки без файлов")
        a_missing_all = m.addAction("Убрать из плейлиста все треки без файлов") \
            if any(s.tracks and len(s.missing_files) == len(s.tracks)
                   for s in (self._locate(song.id)[0].songs if self._locate(song.id) else [])) else None
        m.addSeparator()
        a_remove = m.addAction("Убрать из плейлиста")
        chosen = m.exec(self.song_list.mapToGlobal(pos))
        if chosen is a_play:
            self.play_song(song)
        elif chosen is a_load:
            self.cue_song(song)
        elif chosen is a_edit:
            self._edit_song(song)
        elif chosen is a_rename:
            name, ok = QInputDialog.getText(self, "Трек", "Название:", text=song.name)
            if ok and name.strip():
                song.name = name.strip()
                self._refresh_song_items()
                if song.id == self.cued_id:
                    self.song_label.setText(song.name)
                self._update_next_label()
                self._schedule_save()
        elif chosen is a_detect:
            song.key, song.key_manual = "", False
            self._refresh_song_items()
        elif chosen is a_setkey:
            items = ["не указана"] + ALL_KEYS
            cur = items.index(song.key) if song.key in items else 0
            k, ok = QInputDialog.getItem(self, "Тональность", f"«{song.name}»:", items, cur, False)
            if ok:
                song.key = "" if k == "не указана" else k
                song.key_manual = bool(song.key)
                self._after_song_meta_change(song)
        elif chosen in end_actions:
            song.end_action = end_actions[chosen]
            self._after_song_meta_change(song)
        elif chosen is a_trim:
            if song.silence_lead < 0:
                self._status("Тишина в этом треке ещё анализируется — попробуйте через несколько секунд")
            else:
                song.silence_ignore = False
                self._apply_trim(song, *song.silence_offer())
        elif chosen is a_trim_all:
            self._trim_playlist(self._locate(song.id)[0])
        elif chosen is a_folder and song.folder:
            QDesktopServices.openUrl(QUrl.fromLocalFile(song.folder))
        elif a_relink is not None and chosen is a_relink:
            self._relink_song(song)
        elif a_drop is not None and chosen is a_drop:
            if self.engine.is_playing and song.id == self.cued_id:
                self._status("Остановите воспроизведение, чтобы менять дорожки трека", warn=True)
            else:
                gone = {t.file for t in song.missing_files}
                song.tracks = [t for t in song.tracks if t.file not in gone]
                self._song_files_changed(song)
        elif a_missing_all is not None and chosen is a_missing_all:
            self._remove_missing_songs(self._locate(song.id)[0])
        elif chosen is a_remove:
            self.song_list.setCurrentItem(item)
            self._remove_song()

    # ── треки, файлы которых удалили ───────────────────────────────────────
    def _song_files_changed(self, song: Song) -> None:
        self.cache.pop(song.id, None)
        if song.id == self.cued_id and not self.engine.is_playing:
            self._reload_cued()
        self._missing_sig = None
        self._refresh_song_items()
        self._schedule_save()

    def _relink_song(self, song: Song) -> None:
        """Файлы перенесли в другую папку — ищем их там по имени (и во вложенных папках)."""
        start = song.folder if song.folder and os.path.isdir(song.folder) else str(Path.home())
        folder = QFileDialog.getExistingDirectory(self, f"Где теперь файлы «{song.name}»?", start)
        if not folder:
            return
        index: dict[str, str] = {}
        for p in Path(folder).rglob("*"):
            if p.is_file():
                index.setdefault(p.name.lower(), str(p))
        found = 0
        for t in song.missing_files:
            new = index.get(Path(t.file).name.lower())
            if new:
                t.file, found = new, found + 1
        if found:
            song.folder = folder if found == len(song.tracks) else song.folder
            self._song_files_changed(song)
        left = len(song.missing_files)
        self._status(f"«{song.name}»: найдено файлов {found}" + (f", не найдено {left}" if left else ""),
                     warn=bool(left))

    def _remove_missing_songs(self, pl: Playlist) -> None:
        gone = [s for s in pl.songs if s.tracks and len(s.missing_files) == len(s.tracks)]
        if not gone or QMessageBox.question(
                self, "Треки без файлов", f"Убрать из плейлиста «{pl.name}» треков без файлов: {len(gone)}?\n\n"
                + "\n".join(s.name for s in gone[:15])) != QMessageBox.StandardButton.Yes:
            return
        for s in gone:
            if s.id == self.cued_id and self.engine.is_playing:
                continue
            pl.songs.remove(s)
            self._forget_song(s.id)
        self._reload_playlists()
        self._schedule_save()

    def _check_missing(self) -> None:
        """Раз в пару секунд (на стопе): не пропали ли файлы треков на диске."""
        pl = self.shown_playlist
        if pl is None:
            return
        sig = tuple((s.id, len(s.missing_files)) for s in pl.songs)
        if sig != getattr(self, "_missing_sig", None):
            first = getattr(self, "_missing_sig", None) is None
            self._missing_sig = sig
            self._refresh_song_items()
            if not first:
                gone = [s.name for s in pl.songs if s.tracks and len(s.missing_files) == len(s.tracks)]
                if gone:
                    self._status(f"Файлы удалены с диска: {', '.join(gone[:3])}"
                                 + ("…" if len(gone) > 3 else ""), warn=True)

    def _after_song_meta_change(self, song: Song) -> None:
        self._refresh_song_items()
        if song.id == self.cued_id:
            self.key_label.setText(self._badge(song))
            self._apply_metronome()
            if song.play_bpm != getattr(self, "_fx_bpm_sent", None) and not self.engine.is_playing:
                self._fx_bpm_sent = song.play_bpm      # темп изменился (вручную/анализ) — обновить FX
                self._fx_tempo_for(song)
        self._schedule_save()

    def _forget_song(self, sid: str) -> None:
        loaded = self.cache.pop(sid, None)
        if loaded:
            for rt in loaded.tracks:
                rt.slot.close_editor()
        if sid == self.cued_id and not self.engine.is_playing:
            self.cued_id = None
            self.engine.set_song(None)
            self._rebuild_strips(None)
            self.song_label.setText("— трек не выбран —")
            self.key_label.setText("")
            self.timeline.set_song(None, self.engine.samplerate)

    def _on_songs_reordered(self) -> None:
        pl = self.shown_playlist
        if not pl:
            return
        by_id = {s.id: s for s in pl.songs}
        ids = [self.song_list.item(i).data(ROLE_ID) for i in range(self.song_list.count())]
        pl.songs = [by_id[i] for i in ids if i in by_id]
        self._refresh_song_items()
        self._update_next_label()
        self._preload_next()
        self._schedule_save()

    def _on_song_activated(self, item: QListWidgetItem) -> None:
        """Двойной клик по треку — сразу играть его."""
        song = self._song_by_id(item.data(ROLE_ID))
        if song:
            self.play_song(song)

    def play_song(self, song: Song) -> None:
        """Выбрать трек и запустить:
        * стоим — загружаем и играем с начала (если трек ещё готовится — стартует, как будет готов);
        * играет другой трек — он плавно гаснет, выбранный стартует;
        * этот трек уже играет — ничего не делаем (случайный двойной клик не перезапустит его)."""
        if self.engine.is_playing:
            if self.engine.song is not None and self.engine.song.song.id == song.id:
                return
            self.cue_song(song)               # переключение на ходу: текущий трек гаснет
            self._switch_force_play = True
            return
        self._autoplay_id = None
        self.cue_song(song)                   # встаёт на начало (из памяти — мгновенно)
        if self.engine.song is not None and self.engine.song.song.id == song.id:
            self._start_playback()
        else:
            self._autoplay_id = song.id       # ещё грузится — запустится сам, как только будет готов
            self._autoplay_at = time.monotonic()
            self._status(f"«{song.name}» готовится — запустится автоматически (STOP — отменить)")

    # ── дорожки текущей песни ───────────────────────────────────────────────
    def _cued_song_editable(self) -> Optional[Song]:
        song = self._song_by_id(self.cued_id)
        if song is None:
            self._status("Сначала выберите трек (двойной клик в списке)", warn=True)
            return None
        if self.engine.is_playing:
            self._status("Остановите воспроизведение, чтобы менять трек", warn=True)
            return None
        return song

    def _on_tracks_drop(self, files: list) -> None:
        if self.cued_id is None:
            # песни нет — перетащенное становится новой песней
            songs = self._songs_from(files)
            if songs:
                self._insert_songs(songs)
                self.cue_song(songs[0])
            return
        song = self._cued_song_editable()
        if song is None:
            return
        added = song.add_files(files, self.cfg.routing_defaults)
        if added:
            self._status(f"«{song.name}»: добавлено дорожек — {added}")
            self._reload_cued()
            self._schedule_save()
        else:
            self._status("Эти файлы уже есть в треке", warn=True)

    def _add_tracks_dialog(self) -> None:
        song = self._cued_song_editable()
        if song is None:
            return
        files, _ = QFileDialog.getOpenFileNames(self, "Аудиофайлы для трека", song.folder,
                                                "Аудио (*.wav *.wave *.aif *.aiff *.flac *.mp3 *.ogg)")
        if files:
            self._on_tracks_drop(files)

    def _on_track_remove(self, cfg: TrackConfig) -> None:
        song = self._cued_song_editable()
        if song is None or cfg not in song.tracks:
            return
        if not self._confirm(f"Убрать дорожку «{cfg.name}» из трека?\n(Файл на диске не трогается)"):
            return
        song.tracks.remove(cfg)
        self._reload_cued()
        self._schedule_save()

    def _reload_cued(self) -> None:
        """Песня изменилась (дорожки, темп, тон) — пересобрать её в памяти."""
        song = self._song_by_id(self.cued_id)
        if song is None or self.engine.is_playing:
            return
        self.cache.pop(song.id, None)
        self.cue_song(song)

    # ── редактор песни ──────────────────────────────────────────────────────
    def open_song_editor(self) -> None:
        song = self._song_by_id(self.cued_id)
        if song is None:
            item = self.song_list.currentItem()
            song = self._song_by_id(item.data(ROLE_ID)) if item else None
        if song is None:
            self._status("Выберите трек для редактирования", warn=True)
            return
        self._edit_song(song)

    def _edit_song(self, song: Song) -> None:
        if self.engine.is_playing and song.id == self.cued_id:
            self._status("Остановите воспроизведение, чтобы редактировать этот трек", warn=True)
            return
        dlg = SongEditorDialog(song, self.engine.samplerate, self._add_exported_song, self)
        if dlg.exec():
            self.cache.pop(song.id, None)
            if song.id == self.cued_id:
                self._reload_cued()
            else:
                # сразу начинаем считать новый тон/темп в фоне — к моменту выбора трек будет готов
                audio_edit.prerender_song(song, self.engine.samplerate)
                self._preload_next()
            self._refresh_song_items()
            self._schedule_save()

    def _add_exported_song(self, folder: str) -> None:
        songs = self._songs_from([folder])
        if not songs:
            return
        loc = self._locate(self.cued_id)
        pl = loc[0] if loc else self._ensure_playlist()
        row = loc[1] + 1 if loc else len(pl.songs)
        pl.songs[row:row] = songs
        self._reload_songs()
        self._schedule_save()

    # ── поиск песен ─────────────────────────────────────────────────────────
    def _locate(self, song_id: Optional[str]) -> Optional[tuple[Playlist, int]]:
        if not song_id:
            return None
        for pl in self.cfg.playlists:
            for i, s in enumerate(pl.songs):
                if s.id == song_id:
                    return pl, i
        return None

    def _song_by_id(self, song_id: Optional[str]) -> Optional[Song]:
        loc = self._locate(song_id)
        return loc[0].songs[loc[1]] if loc else None

    def _neighbor(self, delta: int) -> Optional[Song]:
        """Соседняя песня в плейлисте ТЕКУЩЕЙ песни (даже если открыт другой плейлист)."""
        loc = self._locate(self.cued_id)
        if loc is None:
            pl = self.shown_playlist
            return pl.songs[0] if pl and pl.songs and delta > 0 else None
        pl, i = loc
        j = i + delta
        return pl.songs[j] if 0 <= j < len(pl.songs) else None

    # ═══════════════════════ загрузка / подготовка песни ═════════════════════
    def cue_song(self, song: Song) -> None:
        """Сделать песню текущей и встать на её начало.
        Если сейчас играет другая песня — она плавно гасится, и (по настройке)
        выбранная сразу начинает играть. STOP нажимать не нужно."""
        if song.tracks and len(song.missing_files) == len(song.tracks):
            self._status(f"«{song.name}»: файлы удалены с диска — правый клик по треку → «Найти файлы…»",
                         warn=True)
            self._refresh_song_items()
            return
        if self.engine.is_playing:
            if self.engine.song is not None and self.engine.song.song.id == song.id:
                return
            self._switch_to = song.id
            self._autoplay_id = None
            self.engine.stop()           # фейд 30 мс; переключение завершит таймер (_finish_switch)
            return
        self.cued_id = song.id
        self.song_label.setText(song.name)
        self.key_label.setText(self._badge(song))
        if self.cfg.osc_when == "cue":
            self._osc_for(song)
        loaded = self.cache.get(song.id)
        if loaded is not None and loaded.samplerate == self.engine.samplerate:
            self._activate(loaded)
        else:
            self.cache.pop(song.id, None)
            self.engine.set_song(None)
            self._clear_strips()
            self.loading_label.setText(f"Загрузка «{song.name}»…")
            self._request_load(song, force=True)
        self._refresh_song_items()
        self._update_next_label()

    def _cue_neighbor(self, delta: int) -> None:
        if self._switch_to:              # уже переключаемся — листаем от целевой песни
            loc = self._locate(self._switch_to)
            if loc and 0 <= loc[1] + delta < len(loc[0].songs):
                self._switch_to = loc[0].songs[loc[1] + delta].id
            return
        song = self._neighbor(delta)
        if song:
            self.cue_song(song)

    def _finish_switch(self) -> None:
        """Старая песня догасла — ставим новую и (если так настроено) запускаем."""
        sid, self._switch_to = self._switch_to, None
        song = self._song_by_id(sid)
        if song is None:
            return
        self.cue_song(song)
        force, self._switch_force_play = self._switch_force_play, False
        if force or self.cfg.switch_while_playing == "play":
            self._autoplay_id = song.id      # стартует сразу, как только загрузится
            self._autoplay_at = time.monotonic()
            self._autoplay_silent = True

    def _request_load(self, song: Song, force: bool = False) -> None:
        if song.id in self.loading or (song.id in self.cache and not force):
            return
        self.loading.add(song.id)
        self.pool.start(_SongLoadTask(song, self.engine.samplerate, self.engine.blocksize, self.loader_signals))

    def _preload_next(self) -> None:
        """Пока играет песня — следующая уже грузится в RAM: переход мгновенный."""
        if not self.cfg.preload_next:
            return
        nxt = self._neighbor(+1)
        if nxt:
            self._request_load(nxt)

    def _on_load_progress(self, song_id: str, text: str) -> None:
        if song_id == self.cued_id and self.engine.song is None:
            self.loading_label.setText(text)

    def _on_song_loaded(self, song_id: str, loaded: Optional[LoadedSong], err: str) -> None:
        self.loading.discard(song_id)
        if err or loaded is None:
            if song_id == self.cued_id:
                self.loading_label.setText("")
                QMessageBox.critical(self, "Загрузка трека", err)
            else:
                self._status(f"Не удалось подготовить следующий трек: {err}", warn=True)
            return
        song = self._song_by_id(song_id)
        if loaded.samplerate != self.engine.samplerate or song is None:
            return  # устарело: сменили частоту или трек удалили
        if getattr(loaded, "sig", None) != song_signature(song):
            # пока грузили, трек изменили (тон, темп, обрезка, дорожки) — грузим заново
            if song_id == self.cued_id or self._neighbor(+1) is song:
                self._request_load(song, force=True)
            return
        self.cache[song_id] = loaded
        self._apply_norm(loaded)
        if song_id == self.cued_id and not self.engine.is_playing and self.engine.song is not loaded:
            self._activate(loaded)
        self._trim_cache()
        self._refresh_song_items()

    def _activate(self, loaded: LoadedSong) -> None:
        self.engine.set_song(loaded)
        self.loading_label.setText("")
        self._rebuild_strips(loaded)
        self.key_label.setText(self._badge(loaded.song))
        self._apply_metronome()
        self._apply_norm(loaded)
        self._maybe_offer_trim()
        self.timeline.set_song(loaded.song, self.engine.samplerate)
        self.song_label.setText(loaded.song.name)
        self.time_label.setText("00:00")
        self.remain_label.setText("-" + fmt_time(loaded.duration))
        self.progress.setValue(0)
        if loaded.warnings:
            self._status(" | ".join(loaded.warnings), warn=True)
        elif not self._using_fallback:
            extra = f", {loaded.song.edit.summary()}" if loaded.song.edit.summary() else ""
            self._status(f"Готово: «{loaded.song.name}» — {fmt_time(loaded.duration)}, "
                         f"{len(loaded.tracks)} дор.{extra}, {loaded.memory_mb:.0f} МБ в RAM")
        self._preload_next()
        self._trim_cache()
        self._refresh_song_items()
        self._update_next_label()
        self._schedule_save()

    def _trim_cache(self) -> None:
        nxt = self._neighbor(+1) if self.cfg.preload_next else None
        keep = {self.cued_id, nxt.id if nxt else None}
        for sid in list(self.cache):
            if sid not in keep:
                loaded = self.cache.pop(sid)
                self._export_plugin_states(loaded)
                for rt in loaded.tracks:
                    rt.slot.close_editor()

    def _clear_strips(self) -> None:
        while self.strip_layout.count():
            item = self.strip_layout.takeAt(0)
            w = item.widget()
            if w is not None and w is not self.empty_hint:
                w.deleteLater()
        self.empty_hint.setVisible(False)
        self.strips.clear()

    def _rebuild_strips(self, loaded: Optional[LoadedSong]) -> None:
        self._clear_strips()
        if loaded is not None:
            for rt in loaded.tracks:
                strip = TrackStrip(rt, self.engine.num_outputs, self.engine.samplerate,
                                   self.engine.blocksize, self.engine.stereo_mix)
                strip.changed.connect(self._schedule_save)
                strip.routing_changed.connect(self._on_routing_changed)
                strip.remove_requested.connect(self._on_track_remove)
                strip.reload_requested.connect(self._reload_cued)
                self.strip_layout.addWidget(strip)
                self.strips.append(strip)
        if not self.strips:
            self.strip_layout.addWidget(self.empty_hint)
            self.empty_hint.setVisible(True)
        self.strip_layout.addStretch(1)

    def _on_strip_moved(self, key: str, index: int) -> None:
        """Дорожку перетащили мышью на другое место в микшере."""
        song = self._song_by_id(self.cued_id)
        if song is None:
            return
        files = [t.file for t in song.tracks]
        if key not in files:
            return
        order = move_in_list(list(range(len(files))), files.index(key), index)
        if order == list(range(len(files))):
            return
        song.tracks = [song.tracks[i] for i in order]
        loaded = self.engine.song
        if loaded is not None and loaded.song is song:
            by_cfg = {id(rt.cfg): rt for rt in loaded.tracks}
            # новый список целиком (а не перестановка на месте) — аудиопоток дочитает старый
            loaded.tracks = [by_cfg[id(c)] for c in song.tracks if id(c) in by_cfg]
            loaded.sig = song_signature(song)
            self._rebuild_strips(loaded)
        self.timeline.set_song(song, self.engine.samplerate)
        self._schedule_save()

    def _on_routing_changed(self, track_name: str, output: int) -> None:
        self.cfg.remember_routing(track_name, output)

    def _update_next_label(self) -> None:
        nxt = self._neighbor(+1)
        self.next_label.setText(f"Далее: {nxt.name}" if nxt else "Далее: — конец плейлиста —")

    # ═══════════════════════════════ транспорт ════════════════════════════════
    def pause(self) -> None:
        """PAUSE: играет — остановиться на месте; на паузе — продолжить с того же места."""
        if self._switch_to or self._autoplay_id:
            self.stop()                    # отменить переключение/автостарт
            return
        if self.engine.is_playing:
            counting = self.engine.counting_in
            self.engine.stop()             # плавная остановка, позиция сохраняется
            if not counting and self.engine.song is not None:
                self._paused_id = self.engine.song.song.id
                self._status("Пауза на " + fmt_time(self.engine.position_seconds) + " — Пробел/PAUSE продолжит")
        else:
            self.play()                    # на паузе — продолжить, иначе обычный старт

    def _is_paused(self) -> bool:
        song = self.engine.song
        return (bool(self._paused_id) and not self.engine.is_playing and song is not None
                and song.song.id == self._paused_id
                and 0 < self.engine._pos < song.length and not self._rewind_after_stop)

    def toggle_play(self) -> None:
        """Пробел: играть ↔ пауза (позиция сохраняется). В начало — STOP / Esc."""
        if self.engine.is_playing or self._switch_to or self._autoplay_id:
            self.pause()
        else:
            self.play()

    def play(self) -> None:
        self._autoplay_id = None
        if self._switch_to:
            return
        if not self.engine.is_open:
            self._status("Аудиоустройство не открыто — Настройки → Звук", warn=True)
            return
        if self.engine.song is None:
            if self.cued_id and self.cued_id in self.loading:
                # трек ещё готовится (например, после смены тона) — запустится сам, как только будет готов
                self._autoplay_id = self.cued_id
                self._autoplay_at = time.monotonic()
                self._status("Трек готовится — запустится автоматически (STOP — отменить)")
            elif self.cued_id is None and self._neighbor(+1):
                self._cue_neighbor(+1)  # ничего не выбрано — берём первую песню плейлиста
            return
        if self._is_paused():
            self._resume()
            return
        self._start_playback()

    def _resume(self) -> None:
        """Продолжить после PAUSE с того же места: без отсчёта, без повторного вызова
        сцены пульта и без новой папки автозаписи — это та же песня."""
        self._paused_id = None
        if self.engine.play(0):
            self._status("Продолжаем с " + fmt_time(self.engine.position_seconds))

    def _start_playback(self) -> bool:
        """Старт трека: отсчёт (если включён и известен темп), сцена пульта, автозапись."""
        self._paused_id = None
        bars = 0
        if self.cfg.countin_on:
            if self.engine.metro_bpm > 0 and self.engine.song is not None and self.engine.song.song.play_bpm >= 1:
                bars = self.cfg.countin_bars
            else:
                self._status("Отсчёт пропущен: темп трека неизвестен (правый клик по кнопке метронома — задать)",
                         warn=True)
        if not self.engine.play(bars):
            return False
        rec, loaded = self.recorder, self.engine.song
        if rec.active and rec.meta.get("song_pos") is None and loaded is not None:
            # REC нажали раньше PLAY: запись началась до трека — запоминаем, где он в записи,
            # чтобы потом выровнять каналы под трек
            elapsed = rec.pushed / max(1, rec.samplerate) + self.engine.countin_seconds_left
            rec.set_meta(song_id=loaded.song.id, song_name=loaded.song.name,
                         song_pos=self.engine.position_seconds - elapsed)
        self.trim_banner.hide()
        if self.cfg.osc_when == "play" and self.engine.song is not None:
            self._osc_for(self.engine.song.song)
        self._maybe_auto_record()
        return True

    def _seek_relative(self, delta: float) -> None:
        if self.engine.song is not None:
            self.engine.seek(self.engine.position_seconds + delta)

    def _on_seek_preview(self, frac: float) -> None:
        song = self.engine.song
        if song is not None:
            self.time_label.setText(fmt_time(frac * song.duration))

    def _on_seek(self, frac: float) -> None:
        song = self.engine.song
        if song is not None:
            self.engine.seek(frac * song.duration)

    def stop(self) -> None:
        self._paused_id = None
        if self._switch_to:
            self._switch_to = None         # STOP во время переключения — просто стоп
            self._switch_force_play = False
            return
        if self._autoplay_id:
            self._autoplay_id = None       # STOP отменяет автозапуск следующей песни
            self.loading_label.setText("")
            self._status("Автозапуск отменён")
            return
        if self.engine.is_playing:
            self.engine.stop()
            self._rewind_after_stop = True   # после плавной остановки — в начало (см. _on_tick)
        else:
            self.engine.rewind()   # STOP на паузе — в начало трека
            self.progress.setValue(0)
            self.time_label.setText("00:00")

    def _on_autocue(self, on: bool) -> None:
        self.cfg.auto_cue = on
        self._schedule_save()

    def _on_song_finished(self) -> None:
        """Auto-cue: песня доиграла → встаём на начало следующей и ЖДЁМ команды PLAY
        (или запускаем её сами через паузу, если включён автозапуск)."""
        finished = self._song_by_id(self.cued_id)
        action = finished.end_action if finished else "default"
        if action == "next":
            go_next, autoplay, delay = True, True, finished.end_gap
        elif action == "stop":
            go_next, autoplay, delay = True, False, 0.0
        else:
            go_next, autoplay, delay = self.cfg.auto_cue, self.cfg.auto_play_next, self.cfg.auto_play_delay
        if go_next:
            nxt = self._neighbor(+1)
            if nxt:
                self.cue_song(nxt)
                if autoplay:
                    self._autoplay_id = nxt.id
                    self._autoplay_at = time.monotonic() + delay
                    self._status(f"Трек закончился. «{nxt.name}» стартует автоматически "
                                 "(STOP — отменить)")
                else:
                    self._status(f"Трек закончился. Готов: «{nxt.name}» — ждём PLAY")
            else:
                self.engine.rewind()
                self._status("Конец плейлиста")
        else:
            self.engine.rewind()
            self._status("Трек закончился")

    def _check_autoplay(self) -> None:
        if not self._autoplay_id:
            return
        if self.cued_id != self._autoplay_id:
            self._autoplay_id = None       # пользователь выбрал другую песню
            return
        left = self._autoplay_at - time.monotonic()
        if left > 0:
            self.loading_label.setText(f"Автостарт через {left:.0f} с")
            return
        if self.engine.song is not None and self.engine.song.song.id == self._autoplay_id:
            self.loading_label.setText("")
            self._autoplay_id = None
            self._autoplay_silent = False
            self._start_playback()
        elif self._autoplay_id not in self.loading:
            self._autoplay_id = None       # загрузка не удалась — не ждём вечно
        # иначе трек ещё грузится — запустим, как только будет готов

    # ════════════════════════════ таймер интерфейса ═══════════════════════════
    # ── синхронизация с видео-программой ───────────────────────────────────
    def _apply_video_sync(self) -> None:
        if self.cfg.video_sync_on:
            if not self.sync.running or self.sync.port != self.cfg.video_sync_port:
                if not self.sync.start(self.cfg.video_sync_port):
                    self._status(f"Синхронизация с видео не запустилась: {self.sync.error}", warn=True)
                self._publish_songs()
        else:
            self.sync.stop()
        self._update_video_label()

    def _publish_songs(self) -> None:
        """Плейлисты и треки для видео-программы (к ним привязываются сцены и видео)."""
        pls = []
        for pl in self.cfg.playlists:
            songs = []
            for sg in pl.songs:
                songs.append({"id": sg.id, "name": sg.name, "duration": round(sg.output_duration, 3),
                              "bpm": round(float(sg.play_bpm or 0.0), 2),
                              "markers": [{"t": round(t, 3), "name": n} for t, n in sg.markers_out()]})
            pls.append({"name": pl.name, "songs": songs})
        self.sync.set_songs(pls)

    def _publish_state(self) -> None:
        eng = self.engine
        loaded = eng.song
        if loaded is not None:
            sg = loaded.song
            playing = eng.is_playing and not eng._stop_requested
            # позиция того, что СЕЙЧАС слышно: минус задержка звуковой карты;
            # во время отсчёта — отрицательная (трек начнётся через столько секунд)
            pos = eng.position_seconds - eng.countin_seconds_left
            if playing:
                pos -= eng.output_latency
            self.sync.publish_state(sg.id, sg.name, loaded.duration, pos, playing, eng.countin_seconds_left)
        else:
            sg = self._song_by_id(self.cued_id)
            self.sync.publish_state(sg.id if sg else "", sg.name if sg else "",
                                    sg.output_duration if sg else 0.0, 0.0, False)

    def _update_video_label(self) -> None:
        if not self.cfg.video_sync_on:
            text = ""
        elif not self.sync.running:
            text = "Видео: ошибка"
        else:
            n = len(self.sync.clients)
            text = f"Видео: {n} подкл." if n else "Видео: ждёт"
        if self.video_label.text() != text:
            self.video_label.setText(text)
            self.video_label.setToolTip("Concert Video: " + (", ".join(self.sync.clients) or "не подключено")
                                        + f"\nПорт {self.cfg.video_sync_port}. Настройки → Видео")

    def _on_tick(self) -> None:
        eng = self.engine
        if self.sync.running:
            self._publish_state()
            self._sync_tick += 1
            if self._sync_tick % 60 == 0:           # раз в ~2 с: список треков и счётчик подключений
                self._publish_songs()
                self._update_video_label()
        song = eng.song
        if song is not None and song.length:
            pos = eng.position_seconds
            if not self.progress.dragging:
                self.time_label.setText(fmt_time(pos))
                self.progress.setValue(int(1000 * min(1.0, pos / song.duration)))
            self.remain_label.setText("-" + fmt_time(song.duration - pos))
        playing = eng.is_playing
        if self._pending_attach and not playing:
            folder, self._pending_attach = self._pending_attach, ""
            self._attach_recording(folder)
        if self._rewind_after_stop and not playing:
            self._rewind_after_stop = False      # STOP: дождались затухания — встаём в начало
            eng.rewind()
            self.progress.setValue(0)
            self.time_label.setText("00:00")
        if playing != self._play_btn_active:
            self._play_btn_active = playing
            self.play_btn.setProperty("active", playing)
            repolish(self.play_btn)
        paused = self._is_paused()
        if paused != self.pause_btn.property("active"):
            self.pause_btn.setProperty("active", paused)
            repolish(self.pause_btn)
        for s in self.strips:
            s.refresh_meter()
        self.console.tick_meters()               # уровни с пульта на его фейдерах
        if self._switch_to and not eng.is_playing:
            self._finish_switch()
        if eng.finished:
            eng.finished = False
            self._on_song_finished()
        self._check_autoplay()
        self._tick_record()
        self._poll_midi()
        for address, args in self.osc.poll():   # фейдеры, mute, названия с пульта
            self.console.handle(address, args)
            self.fx_tempo.handle(address, args)
        reports = self.fx_tempo.take_reports()
        if reports:
            self._status("Темп FX пульта: " + " | ".join(reports),
                         warn=any("✔" not in r for r in reports))
        if self.fx_tempo.scale_changed:
            self.fx_tempo.scale_changed = False
            self.cfg.fx_time_scale = self.fx_tempo.scale
            self._schedule_save()
        if self._tick_count % 60 == 30 and not eng.is_playing:
            self._check_missing()
        if self._tick_count % 5 == 0:
            self._update_section()
        if self.timeline.isVisible() and song is not None:
            self.timeline.set_playhead(eng.position_seconds, eng.is_playing)
        if not eng.is_playing and self._tick_count % 30 == 0:
            self._detect_next_key()
        self._tick_count += 1
        if self._tick_count % 15 == 0:
            self._update_audio_label()

    # ═══════════════════════════════ сохранение ═══════════════════════════════
    def _schedule_save(self) -> None:
        self._save_timer.start()

    def _export_plugin_states(self, loaded: LoadedSong) -> None:
        for rt in loaded.tracks:
            if rt.cfg.plugin is not None and rt.slot.plugin is not None:
                state = rt.slot.export_state()
                if state:
                    rt.cfg.plugin.state_b64 = state

    def _restore_splitters(self) -> None:
        for name, sp in self.splitters.items():
            state = self.cfg.ui_splitters.get(name)
            if state:
                try:
                    sp.restoreState(QByteArray.fromBase64(state.encode("ascii")))
                except Exception:
                    pass
            sp.splitterMoved.connect(lambda *_: self._schedule_save())

    def _store_splitters(self) -> None:
        self.cfg.ui_splitters = {name: bytes(sp.saveState().toBase64()).decode("ascii")
                                 for name, sp in self.splitters.items()}

    def _save_now(self) -> None:
        self._store_splitters()
        for loaded in self.cache.values():
            self._export_plugin_states(loaded)
        try:
            self.store.save(self.cfg)
        except Exception as exc:
            self._status(f"Не удалось сохранить конфиг: {exc}", warn=True)

    def _status(self, text: str, warn: bool = False) -> None:
        self.status_label.setText(text)
        self.status_label.setProperty("warn", warn)
        repolish(self.status_label)

    def reset_layout(self) -> None:
        """Расположение зон по умолчанию — пропорционально текущему размеру окна."""
        w, h = max(800, self.width()), max(500, self.height())
        self.split_root.setSizes([int(h * 0.16), int(h * 0.84)])
        self.split_main.setSizes([int(w * 0.24), int(w * 0.76)])
        self.split_left.setSizes([int(h * 0.25), int(h * 0.75)])
        self.split_right.setSizes([int(h * 0.36), int(h * 0.64)])
        self.split_mixer.setSizes([int(w * 0.58), int(w * 0.18)])
        self.cfg.ui_splitters = {}
        self._schedule_save()

    def closeEvent(self, event) -> None:
        if self.engine.is_playing and QMessageBox.question(
                self, "Выход", "Идёт воспроизведение. Точно выйти?") != QMessageBox.StandardButton.Yes:
            event.ignore()
            return
        if self.recorder.active and QMessageBox.question(
                self, "Выход", "Идёт запись. Остановить её и выйти?") != QMessageBox.StandardButton.Yes:
            event.ignore()
            return
        self.recorder.stop()
        self.console.shutdown()          # дослать последние движения фейдеров на пульт
        self._save_now()
        audio_edit.shutdown()
        self.osc.close()
        self.midi.close()
        self.sync.stop()
        for loaded in self.cache.values():
            for rt in loaded.tracks:
                rt.slot.close_editor()
        self.engine.close()
        event.accept()

    # ═══════════════════════════ тональность (фон) ════════════════════════════
    def _detect_next_key(self) -> None:
        """По одной песне за раз и только когда ничего не играет — чтобы анализ
        не отнимал ресурсы у воспроизведения."""
        if self._key_busy:
            return

        def pending(song: Song) -> bool:
            if not song.tracks or len(song.missing_files) == len(song.tracks):
                return False                      # файлов нет — анализировать нечего
            return (not song.key or song.key_outdated or song.bpm == 0 or song.lufs == 0
                    or song.silence_lead < 0)
        # сначала выбранный трек, затем следующий, затем все остальные
        order = [self._song_by_id(self.cued_id), self._neighbor(+1)]
        order += [s for pl in self.cfg.playlists for s in pl.songs]
        for song in order:
            if song is not None and pending(song):
                self._key_busy = True
                self.key_pool.start(_KeyTask(song, self.key_signals))
                return

    def _on_key_detected(self, song_id: str, res: tuple) -> None:
        key, bpm, offset, lufs, peak, lead, trail, duration = res
        self._key_busy = False
        song = self._song_by_id(song_id)
        if song is None:
            return
        if not song.key_manual and (not song.key or song.key_outdated):
            song.key = key
            song.key_ver = KEY_ALGO_VERSION
        if song.bpm == 0:
            song.bpm, song.beat_offset = bpm, offset
        if song.lufs == 0:
            song.lufs, song.peak = lufs, peak
            loaded = self.cache.get(song.id)
            if loaded is not None:
                self._apply_norm(loaded)
        if song.silence_lead < 0:
            song.silence_lead, song.silence_trail = lead, trail
            song.duration = max(song.duration, duration)
            if song.id == self.cued_id:
                self._maybe_offer_trim()
        self._after_song_meta_change(song)

    # ═══════════════════════════════ запись ═══════════════════════════════════
    def _on_rec_clicked(self) -> None:
        # кнопка уже переключила своё состояние — решаем по реальному состоянию записи
        self.toggle_record()

    def toggle_record(self) -> None:
        if self.recorder.active:
            self.stop_record()
        else:
            if not self.cfg.rec_chosen:
                # первый раз: сначала выбрать, какие каналы писать (а не все 32 подряд)
                self.rec_btn.setChecked(False)
                if not self.pick_rec_channels():
                    return
            self.start_record()

    # ── выбор каналов для записи ───────────────────────────────────────────
    def _console_channel_names(self) -> dict:
        """{номер канала: имя} с пульта X32 (только реально пришедшие названия)."""
        names = self.console.mirror.names
        out = {}
        for n in range(1, 33):
            name = names.get(f"ch/{n:02d}", "").strip()
            if name:
                out[n] = name
        return out

    def pick_rec_channels(self) -> bool:
        """Окно выбора входов записи. True — пользователь нажал OK."""
        if self.recorder.active:
            self._status("Каналы записи меняются после остановки записи", warn=True)
            return False
        if self.console.enabled and self.osc.connected:
            for n in range(1, 33):                 # освежить названия каналов с пульта
                self.osc.send(f"/ch/{n:02d}/config/name")
        available = self.engine.duplex_inputs
        if not available:                          # без пульта — входы обычной звуковой карты
            try:
                dev = next((d for d in list_input_devices() if d.name == self.cfg.rec_input_device),
                           None) or default_input_device()
                available = dev.inputs if dev else 32
            except Exception:
                available = 32
        dlg = RecChannelsDialog(self.cfg.rec_channels, self.cfg.rec_names,
                                available, self.engine.samplerate or 48000,
                                self.cfg.rec_format, self._console_channel_names, self)
        if not dlg.exec():
            return False
        self.cfg.rec_channels, self.cfg.rec_names = dlg.channels_spec(), dlg.names()
        self.cfg.rec_chosen = True
        self._schedule_save()
        self._update_rec_tooltip()
        self._status("Запись: " + summary_text(self.cfg.rec_channels, self.cfg.rec_names))
        return True

    def _update_rec_tooltip(self) -> None:
        what = summary_text(self.cfg.rec_channels, self.cfg.rec_names) if self.cfg.rec_chosen \
            else "каналы ещё не выбраны"
        self.rec_btn.setToolTip(f"Запись с пульта (Ctrl+R)\nКаналы: {what}\n"
                                "Правый клик — выбрать каналы. Формат и папка — Настройки → Запись")

    def _rec_root(self) -> Path:
        if self.cfg.rec_folder:
            return Path(self.cfg.rec_folder)
        music = Path.home() / "Music"
        return (music if music.exists() else Path.home()) / "ConcertDAW Записи"

    def start_record(self, tag: str = "") -> bool:
        if not self.engine.is_open:
            self._status("Запись невозможна: аудиоустройство не открыто", warn=True)
            self.rec_btn.setChecked(False)
            return False
        name = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        if tag:
            name += "_" + re.sub(r'[\\/:*?"<>|]+', "_", tag).strip()
        folder = self._rec_root() / name
        if free_space_gb(folder) < 2:
            if QMessageBox.question(self, "Запись", "На диске меньше 2 ГБ свободного места. "
                                    "Всё равно записывать?") != QMessageBox.StandardButton.Yes:
                self.rec_btn.setChecked(False)
                return False
        try:
            if self.engine.duplex_inputs > 0:
                # пульт X32 по ASIO: входы карты X-USB приходят в дуплексном потоке движка
                channels = parse_channels(self.cfg.rec_channels, self.engine.duplex_inputs)
                self.recorder.start(folder, channels, self.engine.samplerate,
                                    self.cfg.rec_format, self.cfg.rec_split, meta=self._rec_meta(),
                                    names=self.cfg.rec_names)
                src = f"входы X-USB ({len(channels)} кан.)"
            else:
                dev = next((d for d in list_input_devices() if d.name == self.cfg.rec_input_device),
                           None) or default_input_device()
                if dev is None:
                    raise RuntimeError("Не найдено ни одного устройства записи")
                channels = parse_channels(self.cfg.rec_channels, dev.inputs)
                self.recorder.start(folder, channels, self.engine.samplerate,
                                    self.cfg.rec_format, self.cfg.rec_split, input_device=dev.index,
                                    meta=self._rec_meta(), names=self.cfg.rec_names)
                src = f"«{dev.name}» ({len(channels)} кан.)"
        except Exception as exc:
            self.recorder.stop()
            self.rec_btn.setChecked(False)
            QMessageBox.critical(self, "Запись", f"Не удалось начать запись:\n{exc}")
            return False
        self.rec_btn.setChecked(True)
        self._status(f"● Запись: {src} → {folder}")
        return True

    def stop_record(self) -> None:
        folder = self.recorder.stop()
        self._rec_auto = False
        self.rec_btn.setChecked(False)
        self.rec_label.setText("")
        if folder:
            err = f" (ошибка: {self.recorder.error})" if self.recorder.error else ""
            self._status(f"Запись сохранена: {folder}{err}  — видна на таймлайне", warn=bool(err))
            self.cfg.last_recording = str(folder)
            self._schedule_save()
            self.timeline.refresh_recordings(str(folder))
            self.timeline.set_recording(str(folder))
            if not err:
                self._attach_recording(str(folder))

    # ── после записи: фейдеры записанных каналов ───────────────────────────
    def _attach_recording(self, folder: str) -> None:
        """Записанные каналы → новые дорожки трека: фейдер с выходом на пульт,
        сразу с эквалайзером и компрессором для голоса. Новый дубль того же канала
        заменяет файл, а настройки фейдера (выход, громкость, EQ, компрессор) остаются."""
        if not self.cfg.rec_to_song:
            return
        info = load_session(folder)
        song = self._song_by_id(info.get("song_id"))
        if song is None or info.get("song_pos") is None:
            self._status("Запись сохранена. Она не привязана к треку (PLAY не нажимали) — "
                         "добавить её в трек можно кнопкой «+» у канала на таймлайне", warn=True)
            return
        if song.edit.speed != 1.0 or song.edit.pitch:
            self._status(f"Запись сохранена. У «{song.name}» изменены темп или тон — фейдеры записи "
                         "не добавлены (сначала экспортируйте трек в WAV)", warn=True)
            return
        if self.engine.is_playing and self.engine.song is not None and self.engine.song.song.id == song.id:
            self._pending_attach = folder          # трек ещё играет — добавим после остановки
            self._status("Запись сохранена. Фейдеры записи появятся, когда трек остановится")
            return
        try:
            added, replaced = attach_to_song(song, folder, self.cfg.routing_defaults, dsp.vocal_defaults)
        except Exception as exc:
            self._status(f"Не удалось добавить запись в трек: {exc}", warn=True)
            return
        self.cache.pop(song.id, None)
        if song.id == self.cued_id:
            self._reload_cued()
        self._refresh_song_items()
        self._schedule_save()
        parts = [f"{t.name.replace('Запись: ', '')} → Out {t.output}" for t in added]
        msg = f"Фейдеры записи в «{song.name}»: " + (", ".join(parts) if parts else "")
        if replaced:
            msg += ("; " if parts else "") + f"новый дубль: {', '.join(t.name.replace('Запись: ', '') for t in replaced)}"
        self._status(msg + ". EQ и компрессор — кнопки на фейдере")

    def _maybe_auto_record(self) -> None:
        """Режим «автоматически с каждым PLAY»: своя папка на каждую песню."""
        if self.cfg.rec_mode != "auto" or self.engine.song is None:
            return
        if self.recorder.active:
            if not self._rec_auto:
                return              # пользователь сам ведёт запись кнопкой REC — не трогаем
            self.stop_record()      # песни идут подряд — закрываем папку предыдущей
        if self.start_record(self.engine.song.song.name):
            self._rec_auto = True

    def _tick_record(self) -> None:
        rec = self.recorder
        if not rec.active:
            return
        # авто-запись заканчивается вместе с песней (но не во время переключения/автостарта)
        if self._rec_auto and not self.engine.is_playing and not self._switch_to and not self._autoplay_id:
            self.stop_record()
            return
        blink = "●" if int(time.monotonic() * 2) % 2 else "○"
        warn = "  ⚠ диск не успевает" if rec.backlog > 200 else ""
        if rec.error:
            warn = f"  ⚠ {rec.error}"
        self.rec_label.setText(f"{blink} REC {fmt_time(rec.seconds)}  {len(rec.channels)} кан.  "
                               f"{rec.size_mb / 1000:.2f} ГБ{warn}")

    def _rec_meta(self) -> dict:
        """Что звучало в момент старта записи — для выравнивания в окне дорожек.
        Задержка = вход + выход ASIO: на столько записанный звук отстаёт от песни."""
        meta: dict = {}
        song = self.engine.song
        if song is not None:
            meta.update(song_id=song.song.id, song_name=song.song.name)
            if self.engine.is_playing:
                # во время отсчёта трек ещё стоит: его начало наступит через countin_seconds_left
                meta["song_pos"] = self.engine.position_seconds - self.engine.countin_seconds_left
        meta["latency"] = self.engine.roundtrip_latency
        return meta

    # ═══════════════════════════ окно дорожек ═════════════════════════════════
    def _toggle_timeline(self, on: bool) -> None:
        self.split_right.widget(0).setVisible(on)
        if on:
            self.timeline.set_song(self._song_by_id(self.cued_id), self.engine.samplerate)

    def _is_active_recording(self, folder: str) -> bool:
        return bool(folder) and self.recorder.active and str(self.recorder.folder) == str(folder)

    def _on_recording_deleted(self, folder: str) -> None:
        if self.cfg.last_recording == folder:
            self.cfg.last_recording = ""
        self._status(f"Запись удалена в корзину: {Path(folder).name}")
        self._schedule_save()

    def open_timeline(self) -> None:
        self.timeline_btn.setChecked(True)

    def _timeline_can_edit(self) -> str:
        if self.engine.is_playing:
            return "Остановите воспроизведение, чтобы менять дорожки трека."
        return ""

    def _all_songs(self) -> list:
        return [(f"{pl.name} — {s.name}", s) for pl in self.cfg.playlists for s in pl.songs]

    def _on_timeline_song_modified(self, song: Song) -> None:
        self.cache.pop(song.id, None)
        if song.id == self.cued_id:
            self._reload_cued()
        self._refresh_song_items()
        self._schedule_save()

    def _on_timeline_seek(self, t: float) -> None:
        if self.engine.song is not None:
            self.engine.seek(t)

    # ═════════════════════════════ метроном ═══════════════════════════════════
    def _badge(self, song: Optional[Song]) -> str:
        """Подпись рядом с названием: тональность и темп."""
        if song is None:
            return ""
        parts = [p for p in (key_label(song), tempo_label(song)) if p]
        return "   •   ".join(parts)

    def _apply_metronome(self) -> None:
        """Темп, привязка к долям, выход и громкость метронома."""
        eng = self.engine
        song = eng.song.song if eng.song is not None else self._song_by_id(self.cued_id)
        bpm = song.play_bpm if song is not None else 0.0
        eng.metro_bpm = bpm if bpm >= 1 else self.cfg.metro_bpm
        if song is not None and bpm >= 1:
            # первая доля во времени воспроизведения (с учётом обрезки и скорости)
            eng.metro_offset = (song.beat_offset - song.edit.start) / song.edit.speed
        else:
            eng.metro_offset = 0.0
        out = self.cfg.metro_output
        if out <= 0:
            click = next((t for t in (song.tracks if song else []) if re.search(
                r"click|клик|metronom|метроном", t.name.lower())), None)
            out = click.output if click else 1
        eng.metro_output = out
        eng.metro_gain = float(10 ** (self.cfg.metro_volume_db / 20))
        eng.metro_accent = self.cfg.metro_accent
        eng.metro_beats = max(1, self.cfg.metro_beats)
        self.metro_btn.setText(f" {eng.metro_bpm:.0f}")

    def _on_metro_toggled(self, on: bool) -> None:
        self._apply_metronome()
        self.engine.metro_on = on

    def _set_tempo_manually(self, _pos=None) -> None:
        song = self._song_by_id(self.cued_id)
        current = song.source_bpm if song and song.source_bpm else self.cfg.metro_bpm
        title = f"Исходный темп «{song.name}»" if song else "Темп метронома"
        bpm, ok = QInputDialog.getDouble(self, "Темп", title + " (BPM):", current, 20.0, 400.0, 1)
        if not ok:
            return
        if song is not None:
            song.edit.original_bpm = bpm   # ручной темп важнее автоопределения
            self._after_song_meta_change(song)
        else:
            self.cfg.metro_bpm = bpm
            self._apply_metronome()
            self._schedule_save()

    # ═══════════════════════ отсчёт, нормализация ═════════════════════════════
    def _on_countin_toggled(self, on: bool) -> None:
        self.cfg.countin_on = on
        self._schedule_save()

    def _apply_norm(self, loaded: LoadedSong) -> None:
        """Поправка громкости трека к цели LUFS — только для музыкальных дорожек."""
        song = loaded.song
        g_db = 0.0
        if self.cfg.norm_on and not song.norm_exclude and song.lufs not in (0.0, -99.0):
            g_db = normalization_gain_db(song.lufs, song.peak, self.cfg.norm_target,
                                         self.cfg.norm_max_boost)
        gain = float(10 ** (g_db / 20))
        for rt in loaded.tracks:
            rt.norm_gain = 1.0 if is_click_like(rt.cfg.name) else gain
        loaded.norm_db = g_db

    # ═══════════════════════════ маркеры и петля ══════════════════════════════
    def _on_loop_changed(self, region) -> None:
        if region is None:
            self.engine.set_loop(None, None)
            self._status("Петля выключена")
        else:
            self.engine.set_loop(*region)
            self._status(f"Петля: {fmt_time(region[0])} – {fmt_time(region[1])} (L — выключить)")

    def _current_markers(self) -> list:
        song = self.engine.song
        return song.song.markers_out() if song is not None else []

    def _jump_marker(self, n: int) -> None:
        markers = self._current_markers()
        if 1 <= n <= len(markers):
            self.engine.seek(markers[n - 1][0])

    def _marker_step(self, delta: int) -> None:
        markers = self._current_markers()
        if not markers:
            return
        pos = self.engine.position_seconds
        if delta > 0:
            nxt = next((t for t, _ in markers if t > pos + 0.25), None)
        else:
            # назад: если только что прошли маркер (< 2 с) — на предыдущий, иначе на текущий
            prev = [t for t, _ in markers if t < pos - 2.0]
            nxt = prev[-1] if prev else 0.0
        if nxt is not None:
            self.engine.seek(nxt)

    def _update_section(self) -> None:
        markers = self._current_markers()
        pos = self.engine.position_seconds
        name = ""
        for i, (t, n) in enumerate(markers):
            if t <= pos + 0.05:
                name = f"▸ {i + 1}. {n}"
        if self.engine.counting_in:
            name = f"Отсчёт… {self.engine.countin_seconds_left:.1f} с"
        if self.section_label.text() != name:
            self.section_label.setText(name)

    # ═══════════════════════ пульт по сети и MIDI ═════════════════════════════
    def _apply_remote(self) -> None:
        self.osc.configure(self.cfg.osc_ip)
        self.console.apply_config()
        self.midi.cc_any = self.cfg.midi_cc_any
        if self.cfg.midi_port != self.midi.port_name:
            if self.cfg.midi_port and not self.midi.open(self.cfg.midi_port):
                self._status(f"MIDI-вход «{self.cfg.midi_port}» не открылся: {self.midi.error}", warn=True)
            elif not self.cfg.midi_port:
                self.midi.close()

    def _osc_for(self, song: Song) -> None:
        if not self.cfg.osc_ip:
            return
        sent = []
        if song.x32_scene >= 0 and self.osc.recall_scene(song.x32_scene):
            sent.append(f"сцена {song.x32_scene}")
        if song.x32_snippet >= 0 and self.osc.recall_snippet(song.x32_snippet):
            sent.append(f"сниппет {song.x32_snippet}")
        if self.osc.last_error:
            self._status(f"Пульт по сети: {self.osc.last_error}", warn=True)
        elif sent:
            self._status(f"Пульт X32: {', '.join(sent)} — «{song.name}»")
        self._fx_tempo_for(song)

    def _fx_tempo_for(self, song: Optional[Song]) -> None:
        """Время задержек FX пульта — под темп трека (с учётом скорости из редактора).
        Сцена пульта может поменять эффекты, поэтому время отправляется чуть позже неё."""
        if song is None or not self.cfg.osc_ip or song.play_bpm < 1:
            return
        slots = dict(self.cfg.fx_tempo)              # слоты, настроенные вручную, — со своей долей
        quiet = set()
        if self.cfg.fx_auto:                          # авто: проверить все FX 1–4
            for n in "1234":
                if n not in slots:
                    slots[n] = self.cfg.fx_auto_div
                    quiet.add(n)
        if not slots:
            return
        bpm = song.play_bpm
        QTimer.singleShot(300, lambda: self.fx_tempo.apply(bpm, slots, quiet))

    def _poll_midi(self) -> None:
        if self.midi.learning or self.midi.port is None:
            return
        sigs = self.midi.poll()
        if not sigs or QApplication.activeModalWidget() is not None:
            return
        by_sig = {v: k for k, v in self.cfg.midi_map.items()}
        handlers = {
            "play_stop": self.toggle_play, "play": self.play, "stop": self.stop, "pause": self.pause,
            "next": lambda: self._cue_neighbor(+1), "prev": lambda: self._cue_neighbor(-1),
            "next_marker": lambda: self._marker_step(+1), "prev_marker": lambda: self._marker_step(-1),
            "loop": self.timeline.toggle_loop, "metro": self.metro_btn.toggle,
            "countin": self.countin_btn.toggle, "rec": self.toggle_record,
        }
        for sig in sigs:
            action = by_sig.get(sig)
            if action in handlers:
                handlers[action]()

    # ═════════════════════════ тишина в начале/конце ══════════════════════════
    def _build_trim_banner(self) -> QWidget:
        self.trim_banner = QFrame()
        self.trim_banner.setObjectName("trimBanner")
        row = FlowLayout(self.trim_banner)
        row.setContentsMargins(8, 4, 8, 4)
        mark = QLabel()
        mark.setPixmap(pixmap("wave", 18, "#f7c948"))
        row.addWidget(mark)
        self.trim_text = ElidedLabel("")
        self.trim_text.setObjectName("trimText")
        row.addWidget(self.trim_text)
        for text, name, tip, fn in ((" Обрезать", "scissors", "Убрать тишину (исходные файлы не меняются)",
                                     self._trim_cued),
                                    (" Настроить…", "edit", "Открыть редактор трека и подвинуть точки "
                                     "обрезки вручную", self._trim_manual),
                                    (" Не надо", "close", "Не предлагать для этого трека", self._trim_dismiss)):
            b = QPushButton(text)
            set_icon(b, name)
            b.setToolTip(tip)
            b.setFocusPolicy(Qt.NoFocus)   # Пробел — всегда PLAY/STOP, а не кнопка баннера
            b.clicked.connect(fn)
            row.addWidget(b)
        self.trim_banner.hide()
        return self.trim_banner

    def _maybe_offer_trim(self) -> None:
        """Показать (не модальную!) подсказку, если в выбранном треке тишина в начале/конце."""
        song = self._song_by_id(self.cued_id)
        if song is None or not self.cfg.silence_offer or self.engine.is_playing:
            self.trim_banner.hide()
            return
        lead, trail = song.silence_offer()
        if not lead and not trail:
            self.trim_banner.hide()
            return
        parts = []
        if lead:
            parts.append(f"в начале {lead:.1f} с")
        if trail:
            parts.append(f"в конце {trail:.1f} с")
        self.trim_text.setText(f"В треке «{song.name}» тишина: {', '.join(parts)}.")
        self.trim_banner.show()

    def _apply_trim(self, song: Song, lead: float, trail: float) -> None:
        if not lead and not trail:
            self._status("Обрезать нечего: тишины в начале/конце нет (или трек уже обрезан)")
            return
        if self.engine.is_playing and song.id == self.cued_id:
            self._status("Остановите воспроизведение, чтобы обрезать этот трек", warn=True)
            return
        song.trim_silence(lead, trail)
        self.cache.pop(song.id, None)
        audio_edit.prerender_song(song, self.engine.samplerate)
        if song.id == self.cued_id:
            self._reload_cued()
        self.trim_banner.hide()
        self._refresh_song_items()
        self._schedule_save()
        done = []
        if lead:
            done.append(f"{lead:.1f} с в начале")
        if trail:
            done.append(f"{trail:.1f} с в конце")
        self._status(f"«{song.name}»: обрезано {', '.join(done)}. Поправить или вернуть — «Редактор трека»")

    def _trim_cued(self) -> None:
        song = self._song_by_id(self.cued_id)
        if song is not None:
            self._apply_trim(song, *song.silence_offer())

    def _trim_manual(self) -> None:
        song = self._song_by_id(self.cued_id)
        if song is not None:
            self.trim_banner.hide()
            self._edit_song(song)

    def _trim_dismiss(self) -> None:
        song = self._song_by_id(self.cued_id)
        if song is not None:
            song.silence_ignore = True
            self._schedule_save()
        self.trim_banner.hide()

    def _trim_playlist(self, pl: Optional[Playlist]) -> None:
        if pl is None:
            return
        offers = [(s, *s.silence_offer()) for s in pl.songs]
        offers = [o for o in offers if o[1] or o[2]]
        unknown = sum(1 for s in pl.songs if s.silence_lead < 0)
        if not offers:
            msg = "В треках плейлиста нет тишины, которую можно обрезать."
            if unknown:
                msg += f"\n(ещё анализируется треков: {unknown})"
            QMessageBox.information(self, "Тишина", msg)
            return
        total = sum(o[1] + o[2] for o in offers)
        lines = "\n".join(f"• {s.name}: " + ", ".join(
            p for p in (f"начало {a:.1f} с" if a else "", f"конец {b:.1f} с" if b else "") if p)
            for s, a, b in offers[:12])
        more = f"\n…и ещё {len(offers) - 12}" if len(offers) > 12 else ""
        note = f"\n\nЕщё анализируется треков: {unknown} — их можно будет обрезать позже." if unknown else ""
        if QMessageBox.question(self, "Обрезать тишину",
                                f"Тишина найдена в {len(offers)} треках (всего {total:.0f} с):\n\n"
                                f"{lines}{more}{note}\n\nОбрезать? Исходные файлы не меняются.") \
                != QMessageBox.StandardButton.Yes:
            return
        for s, a, b in offers:
            if self.engine.is_playing and s.id == self.cued_id:
                continue
            s.trim_silence(a, b)
            self.cache.pop(s.id, None)
            audio_edit.prerender_song(s, self.engine.samplerate)
        if not self.engine.is_playing:
            self._reload_cued()
        self.trim_banner.hide()
        self._refresh_song_items()
        self._schedule_save()
        self._status(f"Тишина обрезана в {len(offers)} треках")
