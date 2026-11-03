"""
gui_main.py — Главное окно Concert Video (по образцу OBS Studio).

Сверху: слева — редактор выбранной сцены (источники двигаются мышью), справа —
ЭФИР (то, что идёт на проекторы). Посередине — переход «▶ В эфир», выбор перехода,
затемнение. Снизу панели: Сцены · Источники · Concert DAW · Микшер · Управление.

Синхронизация: раз в кадр (~30 раз/с) берём состояние мастера (Concert DAW),
«режиссёр» (director.decide) говорит, какую сцену вывести, а видео с «Идти за
треком» подстраиваются под позицию трека (director.VideoSync).
"""
from __future__ import annotations

import copy
import datetime
import os
import threading
import time
from dataclasses import fields
from pathlib import Path
from typing import Optional

from PySide6.QtCore import QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QAction, QColor, QFont, QGuiApplication, QImage, QKeySequence, QPainter, QShortcut
from PySide6.QtWidgets import (QTabWidget, QAbstractItemView, QApplication, QCheckBox, QComboBox, QFileDialog, QFrame,
                               QHBoxLayout, QInputDialog, QLabel, QListWidget, QListWidgetItem, QMainWindow, QMenu,
                               QMessageBox, QPushButton, QSlider, QSpinBox, QSplitter, QTreeWidget, QTreeWidgetItem,
                               QVBoxLayout, QWidget)

from director import decide
from gui_dialogs import (BindingDialog, ConnectDialog, ScheduleDialog, SettingsDialog, SourceDialog, VIDEO_FILTER,
                         fmt_t)
from schedule import AFTER_ACTIONS, ScheduleItem, Scheduler, fmt_left
from gui_icons import set_icon
from gui_layout import ElidedLabel, scrollable
from gui_mixer import MixerStrip, db_to_gain, gain_to_db
from gui_update import UpdateButton
from output import FfmpegOutput, ffmpeg_exe
from sources import MULTIMEDIA, MULTIMEDIA_ERROR, meters_unavailable_reason
from stage import Stage
from style import repolish
from sync_client import SyncClient, discover
from views import ProjectorWindow, StageView, TransitionView
from vmodel import (IMAGE_EXT, SOURCE_KINDS, TRANSITIONS, VIDEO_EXT, Binding, Project, ProjectStore, Scene, Source,
                    default_project, match_video, new_id, scene_for_video)

KIND_ICONS = {"video": "video", "image": "image", "slideshow": "slides", "text": "text", "color": "color",
              "camera": "camera", "screen": "monitor", "window": "window"}

EXTRA_STYLE = """
QLabel#monTitle { font-size: 12px; font-weight: 800; letter-spacing: 1.5px; color: #7f8b97; background: transparent; }
QLabel#monTitle[live="true"] { color: #ef4b55; }
QFrame#monitor { background: #0b0d10; border: 1px solid #2c333b; border-radius: 6px; }
QFrame#monitor[live="true"] { border: 2px solid #ef4b55; }
QPushButton#takeBtn { background: #33161a; border: 1px solid #ef4b55; color: white; font-weight: 800;
                      font-size: 16px; min-height: 44px; border-radius: 10px; }
QPushButton#takeBtn:hover { background: #45191f; }
QPushButton#blackBtn:checked { background: #000000; color: #ef4b55; border: 1px solid #ef4b55; }
QLabel#dawTrack { font-size: 18px; font-weight: 800; background: transparent; }
QLabel#dawTime { font-size: 26px; font-weight: 600; font-family: "Consolas", monospace; background: transparent; }
QLabel#dawState { font-size: 13px; font-weight: 800; background: transparent; }
QLabel#dawState[state="play"] { color: #36d399; }
QLabel#dawState[state="pause"] { color: #f7c948; }
QLabel#dawState[state="off"] { color: #7f8b97; }
QPushButton#recOut:checked { background: #ef4b55; color: white; border-color: #ff7d85; }
QPushButton#liveOut:checked { background: #ef4b55; color: white; border-color: #ff7d85; }
"""


class SeekSlider(QSlider):
    """Шкала времени: клик в любое место — сразу туда (а не «шаг» к курсору, как у обычного QSlider)."""

    clicked_to = Signal(int)

    def __init__(self, parent=None):
        super().__init__(Qt.Horizontal, parent)

    def mousePressEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.LeftButton and self.isEnabled():
            from PySide6.QtWidgets import QStyle
            v = QStyle.sliderValueFromPosition(self.minimum(), self.maximum(), int(e.position().x()),
                                               max(1, self.width()))
            self.setValue(v)
            self.clicked_to.emit(v)
        super().mousePressEvent(e)


def _image_bytes(img: QImage) -> bytes:
    """Пиксели картинки (BGRA построчно) — для ffmpeg."""
    ptr = img.constBits()
    try:
        return bytes(ptr)
    except TypeError:                     # старые сборки PySide отдают «сырой» указатель
        ptr.setsize(img.sizeInBytes())
        return bytes(ptr)


def _icon_btn(name: str, tip: str, fn, size: int = 16) -> QPushButton:
    b = QPushButton("")
    set_icon(b, name, size)
    b.setToolTip(tip)
    b.setObjectName("iconBtn")
    b.setFixedSize(30, 26)
    b.setFocusPolicy(Qt.NoFocus)
    b.clicked.connect(fn)
    return b


def _panel(title: str) -> tuple[QFrame, QVBoxLayout]:
    f = QFrame()
    f.setObjectName("leftPanel")
    v = QVBoxLayout(f)
    v.setContentsMargins(6, 6, 6, 6)
    v.setSpacing(4)
    cap = QLabel(title)
    cap.setObjectName("caption")
    v.addWidget(cap)
    return f, v


class DragList(QListWidget):
    """Список, строки которого переставляются мышью (reordered), и куда можно бросить
    файлы/папки из Проводника (files_dropped, если accept_files)."""
    reordered = Signal()
    files_dropped = Signal(list)

    def __init__(self, accept_files: bool = False, parent=None):
        super().__init__(parent)
        self.accept_files = accept_files
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDefaultDropAction(Qt.MoveAction)
        self.setDragDropMode(QAbstractItemView.InternalMove)

    @staticmethod
    def _local_paths(e) -> list:
        md = e.mimeData()
        if md is None or not md.hasUrls():
            return []
        return [u.toLocalFile() for u in md.urls() if u.isLocalFile() and u.toLocalFile()]

    def dragEnterEvent(self, e) -> None:
        if self.accept_files and self._local_paths(e):
            e.setDropAction(Qt.CopyAction)
            e.accept()
            return
        super().dragEnterEvent(e)

    def dragMoveEvent(self, e) -> None:
        if self.accept_files and self._local_paths(e):
            e.setDropAction(Qt.CopyAction)
            e.accept()
            return
        super().dragMoveEvent(e)

    def dropEvent(self, e) -> None:
        paths = self._local_paths(e) if self.accept_files else []
        if paths:
            e.setDropAction(Qt.CopyAction)
            e.accept()
            self.files_dropped.emit(paths)
            return
        if e.source() is not self:
            e.ignore()
            return
        self.blockSignals(True)          # пока строки двигаются — без «выбора» промежуточных строк
        try:
            super().dropEvent(e)
        finally:
            self.blockSignals(False)
        self.reordered.emit()

    def ids(self) -> list:
        return [self.item(i).data(Qt.UserRole) for i in range(self.count())]


class MainWindow(QMainWindow):
    def __init__(self, store: ProjectStore):
        super().__init__()
        self.store = store
        self.project: Project = store.load()
        if not self.project.scenes:
            self.project = default_project()
        self.setWindowTitle("Concert Video")
        self.setStyleSheet(EXTRA_STYLE)
        self.client = SyncClient()
        self.stage = Stage(self.project)
        self.projectors: list[ProjectorWindow] = []
        self.outputs: dict[str, FfmpegOutput] = {}
        self.black = False
        self.sel_scene = ""
        self._auto_scene: Optional[str] = None
        self._auto_black: Optional[bool] = None
        self._songs_sig = None
        self._last_daw_text = ("", "", "")

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(1500)
        self._save_timer.timeout.connect(self.save_now)

        self._build_ui()
        self._install_shortcuts()
        self._apply_studio_layout()

        first = self.project.program_scene if self.project.scene(self.project.program_scene) else \
            (self.project.scenes[0].id if self.project.scenes else "")
        self.select_scene(first)
        self.take(first, "cut")

        self.tick_timer = QTimer(self)
        self.tick_timer.setInterval(33)
        self.tick_timer.timeout.connect(self._tick)
        self.tick_timer.start()
        self.capture_timer = QTimer(self)
        self.capture_timer.timeout.connect(self._capture)

        self.scheduler = Scheduler()
        self._sched_active: Optional[ScheduleItem] = None   # пункт, чьё видео сейчас в эфире
        self._sched_check = 0.0
        self._sched_view_t = 0.0
        self._media_icon = ""
        self._sched_sig = None
        self._found: Optional[list] = None        # результат фонового поиска Concert DAW
        self._searching = False
        self._lost_since = 0.0
        self._last_search = 0.0
        if self.project.auto_connect and self.project.daw_ip:
            self.client.connect(self.project.daw_ip, self.project.daw_port)
        elif self.project.auto_connect:
            self._search_daw()                     # первый запуск: найти DAW в сети самим
        QTimer.singleShot(800, self._restore_projectors)
        self.refresh_songs(force=True)
        if not MULTIMEDIA:
            self.status(f"Видео недоступно: {MULTIMEDIA_ERROR}. Переустановите PySide6 (install.bat)", warn=True)

    # ═════════════════════════════ интерфейс ═══════════════════════════════
    def _build_ui(self) -> None:
        # ── мониторы ───────────────────────────────────────────────────────
        self.edit_frame = QFrame()
        self.edit_frame.setObjectName("monitor")
        ev = QVBoxLayout(self.edit_frame)
        ev.setContentsMargins(4, 4, 4, 4)
        self.edit_title = ElidedLabel("СЦЕНА")
        self.edit_title.setObjectName("monTitle")
        ev.addWidget(self.edit_title)
        self.edit_view = StageView(editable=True)
        self.edit_view.setMinimumSize(240, 135)
        self.edit_view.source_selected.connect(self._on_view_selected)
        self.edit_view.source_moved.connect(self._on_view_moved)
        self.edit_view.source_resized.connect(self._on_view_resized)
        self.edit_view.source_menu.connect(self._source_menu)
        ev.addWidget(self.edit_view, 1)

        mid = QWidget()
        mv = QVBoxLayout(mid)
        mv.setContentsMargins(4, 4, 4, 4)
        mv.addStretch(1)
        self.studio_btn = QPushButton("Студийный режим")
        self.studio_btn.setCheckable(True)
        self.studio_btn.setChecked(self.project.studio_mode)
        self.studio_btn.setToolTip("Включён: клик по сцене открывает её слева для подготовки, в эфир — кнопкой "
                                   "«В эфир». Выключен: клик по сцене сразу выводит её в эфир.")
        self.studio_btn.toggled.connect(self._on_studio)
        mv.addWidget(self.studio_btn)
        self.take_btn = QPushButton(" В ЭФИР")
        self.take_btn.setObjectName("takeBtn")
        set_icon(self.take_btn, "arrow_right", 18)
        self.take_btn.setToolTip("Вывести сцену слева в эфир выбранным переходом (Ctrl+Enter)")
        self.take_btn.clicked.connect(lambda: self.take(self.sel_scene))
        mv.addWidget(self.take_btn)
        self.trans_combo = QComboBox()
        for key, text in TRANSITIONS.items():
            self.trans_combo.addItem(text, key)
        self.trans_combo.setCurrentIndex(max(0, self.trans_combo.findData(self.project.transition)))
        self.trans_combo.currentIndexChanged.connect(self._on_transition)
        mv.addWidget(self.trans_combo)
        self.trans_ms = QSpinBox()
        self.trans_ms.setRange(0, 5000)
        self.trans_ms.setSingleStep(100)
        self.trans_ms.setSuffix(" мс")
        self.trans_ms.setValue(self.project.transition_ms)
        self.trans_ms.valueChanged.connect(self._on_transition)
        mv.addWidget(self.trans_ms)
        self.black_btn = QPushButton(" Затемнение (B)")
        self.black_btn.setObjectName("blackBtn")
        self.black_btn.setCheckable(True)
        set_icon(self.black_btn, "eye_off", 16)
        self.black_btn.setToolTip("Плавно увести экраны в чёрное и обратно")
        self.black_btn.toggled.connect(lambda on: self.set_black(on))
        mv.addWidget(self.black_btn)
        mv.addStretch(1)
        mid.setMaximumWidth(220)
        mid = scrollable(mid)
        mid.setMaximumWidth(230)

        self.prog_frame = QFrame()
        self.prog_frame.setObjectName("monitor")
        self.prog_frame.setProperty("live", True)
        pv = QVBoxLayout(self.prog_frame)
        pv.setContentsMargins(4, 4, 4, 4)
        self.prog_title = ElidedLabel("ЭФИР")
        self.prog_title.setObjectName("monTitle")
        self.prog_title.setProperty("live", True)
        pv.addWidget(self.prog_title)
        self.program_tv = TransitionView()
        self.program_tv.setMinimumSize(240, 135)
        pv.addWidget(self.program_tv, 1)

        self.split_top = QSplitter(Qt.Horizontal)
        self.split_top.addWidget(self.edit_frame)
        self.split_top.addWidget(mid)
        self.split_top.addWidget(self.prog_frame)
        self.split_top.setStretchFactor(0, 1)
        self.split_top.setStretchFactor(2, 1)

        # ── панели ─────────────────────────────────────────────────────────
        self.split_bottom = QSplitter(Qt.Horizontal)
        # каждая панель — в прокрутке: на небольшом экране (ноутбук) окно не «распирает»
        # и оно нормально разворачивается на весь экран
        self.split_bottom.addWidget(scrollable(self._build_scenes()))
        self.split_bottom.addWidget(scrollable(self._build_sources()))
        self.tabs_right = QTabWidget()          # Concert DAW | Расписание — одна зона, вкладки
        self.tabs_right.addTab(scrollable(self._build_daw()), "Concert DAW")
        self.tabs_right.addTab(scrollable(self._build_schedule()), "Расписание")
        self.tabs_right.setCurrentIndex(0 if self.project.daw_ip else 1)
        self.split_bottom.addWidget(self.tabs_right)
        self.split_bottom.addWidget(scrollable(self._build_mixer()))
        self.split_bottom.addWidget(scrollable(self._build_controls()))
        self.split_bottom.setSizes([220, 260, 420, 220, 220])

        self.split_root = QSplitter(Qt.Vertical)
        top_box = QWidget()                       # эфир + панель управления видео под ним
        tb = QVBoxLayout(top_box)
        tb.setContentsMargins(0, 0, 0, 0)
        tb.setSpacing(2)
        tb.addWidget(self.split_top, 1)
        tb.addWidget(self._build_media_bar())
        self.split_root.addWidget(top_box)
        self.split_root.addWidget(self.split_bottom)
        self.split_root.setStretchFactor(0, 3)
        self.split_root.setStretchFactor(1, 2)
        for sp in (self.split_top, self.split_bottom, self.split_root):
            sp.setChildrenCollapsible(False)
            sp.setHandleWidth(6)
        self._restore_splitters()
        self.setCentralWidget(self.split_root)

        self.status_label = ElidedLabel("", full_tooltip=True)
        self.status_label.setObjectName("statusMsg")
        self.out_label = QLabel("")
        self.daw_label = QLabel("")
        self.statusBar().addWidget(self.status_label, 1)
        self.statusBar().addPermanentWidget(self.out_label)
        self.statusBar().addPermanentWidget(self.daw_label)
        self.update_btn = UpdateButton(self, self._update_blocker, lambda: self.project.update_checked,
                                       self._set_update_checked, lambda: self.project.update_auto)
        self.statusBar().addPermanentWidget(self.update_btn)

    def _update_blocker(self) -> str:
        if self.outputs:
            return "Сначала остановите запись и трансляцию — обновление перезапускает программу."
        return ""

    def _set_update_checked(self, t: float) -> None:
        self.project.update_checked = t
        self.schedule_save()

    def _build_media_bar(self) -> QWidget:
        """Play/Пауза, «в начало» и шкала времени для видео в эфире."""
        bar = QFrame()
        bar.setObjectName("monitor")
        h = QHBoxLayout(bar)
        h.setContentsMargins(6, 3, 6, 3)
        h.setSpacing(6)
        from gui_icons import transport_icon
        self.media_start = QPushButton("")
        self.media_start.setIcon(transport_icon("prev"))
        self.media_start.setToolTip("В начало (пауза на первом кадре)")
        self.media_play = QPushButton("")
        self.media_play.setIcon(transport_icon("play"))
        self.media_play.setToolTip("Play / Пауза видео в эфире")
        for b in (self.media_start, self.media_play):
            b.setIconSize(QSize(18, 18))
            b.setFixedSize(40, 30)
            b.setFocusPolicy(Qt.NoFocus)
        self.media_start.clicked.connect(self._media_to_start)
        self.media_play.clicked.connect(self._media_toggle)
        h.addWidget(self.media_start)
        h.addWidget(self.media_play)
        self.media_name = ElidedLabel("Нет видео в эфире")
        self.media_name.setMinimumWidth(80)
        self.media_name.setMaximumWidth(260)
        h.addWidget(self.media_name)
        self.media_time = QLabel("00:00")
        self.media_time.setStyleSheet("font-family:Consolas,monospace;font-size:15px;font-weight:700;"
                                      "background:transparent")
        h.addWidget(self.media_time)
        self.media_slider = SeekSlider()
        self.media_slider.setRange(0, 10000)
        self.media_slider.setFocusPolicy(Qt.NoFocus)
        self.media_slider.setToolTip("Перемотка видео в эфире: тяните или кликните в нужное место")
        self.media_slider.sliderMoved.connect(self._media_preview)
        self.media_slider.sliderReleased.connect(self._media_seek_release)
        self.media_slider.clicked_to.connect(lambda _v: self._media_seek_release())
        h.addWidget(self.media_slider, 1)
        self.media_left = QLabel("-00:00")
        self.media_left.setStyleSheet("font-family:Consolas,monospace;font-size:15px;color:#7f8b97;"
                                      "background:transparent")
        h.addWidget(self.media_left)
        self._media_rt = None
        return bar

    def _media_target(self):
        """Какое видео в эфире управляется: выбранное в списке источников (если оно в эфире),
        иначе верхнее видимое видео сцены эфира."""
        rts = self.stage.runtimes.get(self.stage.program_id, {})
        sel = self._current_source_id()
        if sel in rts and hasattr(rts[sel], "toggle_play") and self.sel_scene == self.stage.program_id:
            return rts[sel]
        sc = self.project.scene(self.stage.program_id)
        if sc is None:
            return None
        for src in reversed(sc.sources):
            rt = rts.get(src.id)
            if rt is not None and hasattr(rt, "toggle_play") and src.visible:
                return rt
        return None

    def _media_toggle(self) -> None:
        rt = self._media_target()
        if rt is not None and rt.controllable(self.client.connected):
            rt.toggle_play()

    def _media_to_start(self) -> None:
        rt = self._media_target()
        if rt is not None and rt.controllable(self.client.connected):
            rt.to_start()

    def _media_preview(self, v: int) -> None:
        rt = self._media_rt
        if rt is not None:
            self.media_time.setText(fmt_t(rt.duration_s() * v / 10000.0)[:8])

    def _media_seek_release(self) -> None:
        rt = self._media_rt
        if rt is not None and rt.controllable(self.client.connected):
            rt.seek_s(rt.duration_s() * self.media_slider.value() / 10000.0)

    def _tick_media(self) -> None:
        rt = self._media_target()
        self._media_rt = rt
        ok = rt is not None and rt.controllable(self.client.connected)
        for w in (self.media_play, self.media_start, self.media_slider):
            if w.isEnabled() != ok:
                w.setEnabled(ok)
        if rt is None:
            name, pos, dur, playing = "Нет видео в эфире", 0.0, 0.0, False
        else:
            name = rt.src.name + ("   (идёт за треком Concert DAW)" if not ok else "")
            pos, dur, playing = rt.position_s(), rt.duration_s(), rt.is_playing()
        if self.media_name.text() != name:
            self.media_name.setText(name)
        icon_kind = "pause" if playing else "play"
        if self._media_icon != icon_kind:
            from gui_icons import transport_icon
            self._media_icon = icon_kind
            self.media_play.setIcon(transport_icon(icon_kind))
        if not self.media_slider.isSliderDown():
            self.media_time.setText(fmt_t(pos)[:8])
            self.media_slider.blockSignals(True)
            self.media_slider.setValue(int(10000 * pos / dur) if dur > 0 else 0)
            self.media_slider.blockSignals(False)
        self.media_left.setText("-" + fmt_t(max(0.0, dur - pos))[:8])

    def _build_scenes(self) -> QWidget:
        f, v = _panel("СЦЕНЫ")
        self.scene_list = DragList()
        self.scene_list.setToolTip("Сцены можно переставлять мышью. Двойной клик — в эфир")
        self.scene_list.reordered.connect(self._scenes_reordered)
        self.scene_list.itemClicked.connect(self._on_scene_clicked)
        self.scene_list.currentRowChanged.connect(self._on_scene_row)
        self.scene_list.itemDoubleClicked.connect(lambda _i: self.take(self.sel_scene))
        self.scene_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.scene_list.customContextMenuRequested.connect(self._scene_menu)
        v.addWidget(self.scene_list, 1)
        row = QHBoxLayout()
        for name, tip, fn in (("plus", "Новая сцена", self.add_scene),
                              ("trash", "Удалить сцену", self.delete_scene),
                              ("edit", "Переименовать", self.rename_scene),
                              ("copy", "Дублировать", self.duplicate_scene),
                              ("up", "Выше", lambda: self.move_scene(-1)),
                              ("down", "Ниже", lambda: self.move_scene(1))):
            row.addWidget(_icon_btn(name, tip, fn))
        row.addStretch(1)
        v.addLayout(row)
        return f

    def _build_sources(self) -> QWidget:
        f, v = _panel("ИСТОЧНИКИ")
        self.source_list = DragList(accept_files=True)
        self.source_list.reordered.connect(self._sources_reordered)
        self.source_list.files_dropped.connect(self.add_dropped_files)
        self.source_list.currentRowChanged.connect(self._on_source_row)
        self.source_list.itemChanged.connect(self._on_source_item_changed)
        self.source_list.itemDoubleClicked.connect(lambda _i: self.edit_source())
        self.source_list.setContextMenuPolicy(Qt.CustomContextMenu)
        self.source_list.customContextMenuRequested.connect(
            lambda pos: self._source_menu(self._current_source_id(), self.source_list.mapToGlobal(pos)))
        v.addWidget(self.source_list, 1)
        row = QHBoxLayout()
        add = _icon_btn("plus", "Добавить источник", lambda: None)
        menu = QMenu(add)
        for kind, text in SOURCE_KINDS.items():
            act = menu.addAction(text)
            from gui_icons import icon
            act.setIcon(icon(KIND_ICONS.get(kind, "plus")))
            act.triggered.connect(lambda _=False, k=kind: self.add_source(k))
        add.setMenu(menu)
        row.addWidget(add)
        for name, tip, fn in (("trash", "Удалить источник", self.delete_source),
                              ("settings", "Свойства источника", self.edit_source),
                              ("up", "Выше (поверх)", lambda: self.move_source(1)),
                              ("down", "Ниже", lambda: self.move_source(-1))):
            row.addWidget(_icon_btn(name, tip, fn))
        row.addStretch(1)
        v.addLayout(row)
        return f

    def _build_daw(self) -> QWidget:
        f, v = _panel("CONCERT DAW — СИНХРОНИЗАЦИЯ")
        top = QHBoxLayout()
        self.daw_status = ElidedLabel("не подключено")
        self.daw_status.setObjectName("dawState")
        self.daw_status.setProperty("state", "off")
        top.addWidget(self.daw_status, 1)
        conn = QPushButton(" Подключить…")
        set_icon(conn, "link", 15)
        conn.clicked.connect(self.open_connect)
        top.addWidget(conn)
        v.addLayout(top)
        self.daw_track = ElidedLabel("—")
        self.daw_track.setObjectName("dawTrack")
        v.addWidget(self.daw_track)
        trow = QHBoxLayout()
        self.daw_time = QLabel("00:00.0")
        self.daw_time.setObjectName("dawTime")
        trow.addWidget(self.daw_time)
        self.daw_state = QLabel("")
        self.daw_state.setObjectName("dawState")
        self.daw_state.setProperty("state", "off")
        trow.addWidget(self.daw_state)
        trow.addStretch(1)
        v.addLayout(trow)
        opt = QHBoxLayout()
        self.follow_cb = QCheckBox("Автоматика (сцены за треками)")
        self.follow_cb.setChecked(self.project.follow_daw)
        self.follow_cb.setToolTip("Включено: сцены переключаются сами по трекам и времени. "
                                  "Выключено: сцены — вручную, а видео «за треком» всё равно идут синхронно.")
        self.follow_cb.toggled.connect(self._on_follow)
        opt.addWidget(self.follow_cb, 1)
        opt.addWidget(QLabel("Сдвиг:"))
        self.delay_spin = QSpinBox()
        self.delay_spin.setRange(-2000, 2000)
        self.delay_spin.setSingleStep(10)
        self.delay_spin.setSuffix(" мс")
        self.delay_spin.setValue(self.project.video_delay_ms)
        self.delay_spin.setToolTip("Сдвиг видео относительно звука. Проектор «опаздывает» — ставьте минус")
        self.delay_spin.valueChanged.connect(self._on_delay)
        opt.addWidget(self.delay_spin)
        v.addLayout(opt)
        self.songs_tree = QTreeWidget()
        self.songs_tree.setHeaderLabels(["Трек", "Длит.", "Сцена", "Смены"])
        self.songs_tree.setRootIsDecorated(True)
        self.songs_tree.setColumnWidth(0, 190)
        self.songs_tree.setColumnWidth(1, 54)
        self.songs_tree.setColumnWidth(2, 130)
        self.songs_tree.itemDoubleClicked.connect(lambda _i, _c: self.edit_binding())
        v.addWidget(self.songs_tree, 1)
        row = QHBoxLayout()
        for icon_name, text, tip, fn in (
                ("video", " Видео…", "Выбрать видео для трека: создаётся сцена «видео на весь экран» "
                                     "синхронно с треком", self.video_for_song),
                ("folder", " По папке…", "Автопривязка: в папке ищутся видео с названиями треков", self.auto_bind),
                ("settings", " Сцены…", "Сцены трека и смена сцен по времени", self.edit_binding),
                ("close", "", "Снять привязку", self.unbind)):
            b = QPushButton(text)
            set_icon(b, icon_name, 15)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        return f

    def _build_schedule(self) -> QWidget:
        f, v = _panel("РАСПИСАНИЕ — ВИДЕО И СЦЕНЫ ПО ВРЕМЕНИ")
        top = QHBoxLayout()
        self.sched_cb = QCheckBox("Расписание включено")
        self.sched_cb.setChecked(self.project.schedule_on)
        self.sched_cb.setToolTip("Включено: в назначенное время сцена/видео выводятся в эфир сами")
        self.sched_cb.toggled.connect(self._on_schedule_on)
        top.addWidget(self.sched_cb, 1)
        self.clock_label = QLabel("")
        self.clock_label.setObjectName("dawTime")
        self.clock_label.setToolTip("Время этого компьютера — по нему работает расписание")
        top.addWidget(self.clock_label)
        v.addLayout(top)
        self.next_label = ElidedLabel("")
        self.next_label.setObjectName("dawState")
        self.next_label.setProperty("state", "play")
        v.addWidget(self.next_label)
        self.sched_tree = QTreeWidget()
        self.sched_tree.setHeaderLabels(["Время", "Дни", "Что", "После видео", "Статус"])
        self.sched_tree.setRootIsDecorated(False)
        for col, w in enumerate((80, 100, 170, 130)):
            self.sched_tree.setColumnWidth(col, w)
        self.sched_tree.itemDoubleClicked.connect(lambda _i, _c: self.edit_schedule())
        v.addWidget(self.sched_tree, 1)
        row = QHBoxLayout()
        for icon_name, text, tip, fn in (
                ("video", " Видео…", "Видео в нужное время: выбрать файл и время", self.add_schedule_video),
                ("plus", " Сцена…", "Сцену в нужное время", self.add_schedule_scene),
                ("edit", "", "Изменить пункт", self.edit_schedule),
                ("play", "", "Выполнить пункт сейчас", self.run_schedule_now),
                ("trash", "", "Удалить пункт", self.delete_schedule)):
            b = QPushButton(text)
            set_icon(b, icon_name, 15)
            b.setToolTip(tip)
            b.clicked.connect(fn)
            row.addWidget(b)
        row.addStretch(1)
        v.addLayout(row)
        return f

    def _build_mixer(self) -> QWidget:
        f, v = _panel("МИКШЕР (звук видео)")
        self.mixer_host = QWidget()
        self.mixer_lay = QVBoxLayout(self.mixer_host)
        self.mixer_lay.setContentsMargins(0, 0, 0, 0)
        self.mixer_lay.addStretch(1)
        v.addWidget(scrollable(self.mixer_host), 1)
        return f

    def _build_controls(self) -> QWidget:
        f, v = _panel("УПРАВЛЕНИЕ")
        self.proj_btn = QPushButton(" Проектор")
        set_icon(self.proj_btn, "monitor", 16)
        self.proj_menu = QMenu(self.proj_btn)
        self.proj_menu.aboutToShow.connect(self._fill_projector_menu)
        self.proj_btn.setMenu(self.proj_menu)
        v.addWidget(self.proj_btn)
        self.rec_btn = QPushButton(" Начать запись")
        self.rec_btn.setObjectName("recOut")
        self.rec_btn.setCheckable(True)
        set_icon(self.rec_btn, "rec", 14, color="#ef4b55", on_color="#ffffff")
        self.rec_btn.clicked.connect(lambda on: self.toggle_output("rec", on))
        v.addWidget(self.rec_btn)
        self.live_btn = QPushButton(" Начать трансляцию")
        self.live_btn.setObjectName("liveOut")
        self.live_btn.setCheckable(True)
        set_icon(self.live_btn, "broadcast", 16, on_color="#ffffff")
        self.live_btn.clicked.connect(lambda on: self.toggle_output("stream", on))
        v.addWidget(self.live_btn)
        st = QHBoxLayout()
        st.addWidget(QLabel("Заставка:"))
        self.standby_combo = QComboBox()
        self.standby_combo.setToolTip("Сцена-заставка: показывается между треками (по настройкам привязки)")
        self.standby_combo.activated.connect(self._on_standby)
        st.addWidget(self.standby_combo, 1)
        v.addLayout(st)
        v.addStretch(1)
        prj = QPushButton(" Проект")
        set_icon(prj, "save", 16)
        m = QMenu(prj)
        m.addAction("Сохранить", self.save_now)
        m.addAction("Сохранить как…", self.save_as)
        m.addAction("Открыть…", self.open_project)
        m.addSeparator()
        m.addAction("Новый проект", self.new_project)
        prj.setMenu(m)
        v.addWidget(prj)
        sett = QPushButton(" Настройки")
        set_icon(sett, "settings", 16)
        sett.clicked.connect(self.open_settings)
        v.addWidget(sett)
        return f

    def _install_shortcuts(self) -> None:
        for n in range(1, 13):
            sc = QShortcut(QKeySequence(f"F{n}"), self)
            sc.setContext(Qt.ApplicationShortcut)
            sc.activated.connect(lambda n=n: self._scene_hotkey(n - 1))
        for keys, fn in (("Ctrl+Return", lambda: self.take(self.sel_scene) if self.project.studio_mode else None),
                         ("B", lambda: self.black_btn.toggle()),
                         ("Ctrl+S", self.save_now)):
            sc = QShortcut(QKeySequence(keys), self)
            sc.setContext(Qt.ApplicationShortcut)
            sc.activated.connect(lambda f=fn: f() if QApplication.activeModalWidget() is None else None)

    def _scene_hotkey(self, i: int) -> None:
        if QApplication.activeModalWidget() is None and 0 <= i < len(self.project.scenes):
            sid = self.project.scenes[i].id
            self.select_scene(sid)
            self.take(sid)

    def status(self, text: str, warn: bool = False) -> None:
        self.status_label.setText(text)
        self.status_label.setProperty("warn", warn)
        repolish(self.status_label)

    # ═════════════════════════════ сцены ═══════════════════════════════════
    def refresh_scenes(self) -> None:
        self.scene_list.blockSignals(True)
        self.scene_list.clear()
        for i, sc in enumerate(self.project.scenes):
            tags = []
            if sc.id == self.stage.program_id:
                tags.append("● ЭФИР")
            if sc.id == self.project.standby_scene:
                tags.append("заставка")
            hot = f"F{i + 1}  " if i < 12 else ""
            it = QListWidgetItem(f"{hot}{sc.name}" + (f"   — {', '.join(tags)}" if tags else ""))
            it.setData(Qt.UserRole, sc.id)
            if sc.id == self.stage.program_id:
                it.setForeground(QColor("#ef4b55"))
                f = it.font()
                f.setBold(True)
                it.setFont(f)
            self.scene_list.addItem(it)
            if sc.id == self.sel_scene:
                self.scene_list.setCurrentItem(it)
        self.scene_list.blockSignals(False)
        self.standby_combo.blockSignals(True)
        self.standby_combo.clear()
        self.standby_combo.addItem("— нет —", "")
        for sc in self.project.scenes:
            self.standby_combo.addItem(sc.name, sc.id)
        self.standby_combo.setCurrentIndex(max(0, self.standby_combo.findData(self.project.standby_scene)))
        self.standby_combo.blockSignals(False)

    def _on_scene_row(self, row: int) -> None:
        it = self.scene_list.item(row) if row >= 0 else None
        sid = it.data(Qt.UserRole) if it is not None else ""
        if not sid or self.project.scene(sid) is None:
            return
        self.select_scene(sid)
        if not self.project.studio_mode:
            if QApplication.mouseButtons() & Qt.LeftButton:
                return        # мышь ещё нажата — может, сцену тащат; в эфир — по отпусканию (клик)
            self.take(sid)    # стрелки клавиатуры — сразу

    def _on_scene_clicked(self, it: QListWidgetItem) -> None:
        """Обычный клик (без перетаскивания) — в обычном режиме сцена сразу в эфир."""
        sid = it.data(Qt.UserRole)
        if not self.project.studio_mode and sid and sid != self.stage.program_id:
            self.take(sid)

    def _apply_studio_layout(self) -> None:
        """Как в OBS: студийный режим — ПРЕВЬЮ слева + ЭФИР справа и кнопка «В эфир»;
        обычный — одно большое окно эфира (его же и редактируете)."""
        studio = self.project.studio_mode
        self.prog_frame.setVisible(studio)
        self.take_btn.setVisible(studio)
        self.edit_frame.setProperty("live", not studio)
        self.edit_title.setProperty("live", not studio)
        for w in (self.edit_frame, self.edit_title):
            repolish(w)
        self.studio_btn.blockSignals(True)
        self.studio_btn.setChecked(studio)
        self.studio_btn.blockSignals(False)
        self._update_edit_title()

    def _update_edit_title(self) -> None:
        sc = self.project.scene(self.sel_scene)
        name = sc.name if sc else ""
        if self.project.studio_mode:
            self.edit_title.setText(f"ПРЕВЬЮ: {name}" if name else "ПРЕВЬЮ")
        else:
            self.edit_title.setText(f"ЭФИР: {name}" + ("   (затемнено)" if self.black else "") if name else "ЭФИР")

    def select_scene(self, sid: str) -> None:
        """Показать сцену в редакторе (слева)."""
        self.sel_scene = sid
        self.stage.set_preview(sid)
        canvas = self.stage.canvas(sid)
        self.edit_view.setScene(canvas)
        self._update_edit_title()
        self.refresh_scenes()
        self.refresh_sources()

    def take(self, sid: str, kind: Optional[str] = None, ms: Optional[int] = None) -> None:
        """Вывести сцену в эфир (на все проекторы) выбранным переходом."""
        if not sid or self.project.scene(sid) is None:
            return
        kind = kind or self.project.transition
        ms = self.project.transition_ms if ms is None else ms
        canvas = self.stage.canvas(sid)
        for tv in self._tvs():                 # сначала снимок старой сцены для перехода…
            tv.show_scene(canvas, kind, ms)
        self.stage.set_program(sid)            # …потом старая сцена освобождает видео
        if not self.project.studio_mode and self.sel_scene != sid:
            self.select_scene(sid)
        sc = self.project.scene(sid)
        self.prog_title.setText(f"ЭФИР: {sc.name}" + ("   (затемнено)" if self.black else ""))
        self.refresh_scenes()
        self.refresh_mixer()
        self.schedule_save()

    def _tvs(self) -> list[TransitionView]:
        return [self.program_tv] + [p.tv for p in self.projectors]

    def set_black(self, on: bool, ms: int = 500) -> None:
        if on == self.black:
            return
        self.black = on
        for tv in self._tvs():
            tv.set_black(on, ms)
        self.black_btn.blockSignals(True)
        self.black_btn.setChecked(on)
        self.black_btn.blockSignals(False)
        sc = self.project.scene(self.stage.program_id)
        self.prog_title.setText(f"ЭФИР: {sc.name if sc else ''}" + ("   (затемнено)" if on else ""))
        self._update_edit_title()

    def add_scene(self) -> None:
        name, ok = QInputDialog.getText(self, "Новая сцена", "Название:",
                                        text=self.project.unique_scene_name("Сцена"))
        if not ok or not name.strip():
            return
        sc = Scene(self.project.unique_scene_name(name.strip()))
        self.project.scenes.append(sc)
        self.stage.add_scene(sc)
        self.select_scene(sc.id)
        self.schedule_save()

    def _scene_menu(self, pos) -> None:
        if not self.sel_scene:
            return
        m = QMenu(self)
        m.addAction("Вывести в эфир", lambda: self.take(self.sel_scene))
        m.addAction("Сделать заставкой", lambda: self._set_standby(self.sel_scene))
        m.addSeparator()
        m.addAction("Переименовать…", self.rename_scene)
        m.addAction("Дублировать", self.duplicate_scene)
        m.addAction("Удалить", self.delete_scene)
        m.exec(self.scene_list.mapToGlobal(pos))

    def delete_scene(self) -> None:
        sc = self.project.scene(self.sel_scene)
        if sc is None:
            return
        if len(self.project.scenes) == 1:
            QMessageBox.information(self, "Сцены", "Нужна хотя бы одна сцена.")
            return
        if QMessageBox.question(self, "Удалить сцену", f"Удалить сцену «{sc.name}»?") \
                != QMessageBox.StandardButton.Yes:
            return
        idx = self.project.scenes.index(sc)
        self.project.scenes.remove(sc)
        for b in self.project.bindings.values():          # убрать ссылки на сцену из привязок
            if b.scene_id == sc.id:
                b.scene_id = ""
            b.cues = [c for c in b.cues if c.scene_id != sc.id]
        if self.project.standby_scene == sc.id:
            self.project.standby_scene = ""
        was_live = self.stage.program_id == sc.id
        self.stage.remove_scene(sc.id)
        nxt = self.project.scenes[min(idx, len(self.project.scenes) - 1)].id
        if was_live:
            self.take(nxt, "cut")
        self.select_scene(nxt)
        self.refresh_songs(force=True)
        self.schedule_save()

    def rename_scene(self) -> None:
        sc = self.project.scene(self.sel_scene)
        if sc is None:
            return
        name, ok = QInputDialog.getText(self, "Сцена", "Название:", text=sc.name)
        if ok and name.strip():
            sc.name = name.strip()
            self.select_scene(sc.id)
            self.refresh_songs(force=True)
            self.schedule_save()

    def duplicate_scene(self) -> None:
        sc = self.project.scene(self.sel_scene)
        if sc is None:
            return
        dup = copy.deepcopy(sc)
        dup.id = new_id()
        dup.name = self.project.unique_scene_name(sc.name)
        for s in dup.sources:
            s.id = new_id()
        self.project.scenes.insert(self.project.scenes.index(sc) + 1, dup)
        self.stage.add_scene(dup)
        self.select_scene(dup.id)
        self.schedule_save()

    def move_scene(self, d: int) -> None:
        sc = self.project.scene(self.sel_scene)
        if sc is None:
            return
        i = self.project.scenes.index(sc)
        j = i + d
        if 0 <= j < len(self.project.scenes):
            self.project.scenes[i], self.project.scenes[j] = self.project.scenes[j], self.project.scenes[i]
            self.refresh_scenes()
            self.schedule_save()

    def _scenes_reordered(self) -> None:
        """Сцены переставили мышью: новый порядок (и горячие клавиши F1…F12) — как в списке."""
        by_id = {sc.id: sc for sc in self.project.scenes}
        order = [by_id[i] for i in self.scene_list.ids() if i in by_id]
        order += [sc for sc in self.project.scenes if sc not in order]      # на всякий случай
        if order != self.project.scenes:
            self.project.scenes[:] = order
            self.schedule_save()
        self.refresh_scenes()

    def _set_standby(self, sid: str) -> None:
        self.project.standby_scene = sid
        self.refresh_scenes()
        self.schedule_save()

    def _on_standby(self, _i: int) -> None:
        self._set_standby(self.standby_combo.currentData() or "")

    def _on_studio(self, on: bool) -> None:
        self.project.studio_mode = on
        self._apply_studio_layout()
        if not on and self.stage.program_id:
            self.select_scene(self.stage.program_id)   # обычный режим: слева — то, что в эфире
        self.status("Студийный режим: слева готовите сцену, «В ЭФИР» — вывести" if on else
                    "Обычный режим: клик по сцене сразу выводит её в эфир")
        self.schedule_save()

    def _on_transition(self, *_a) -> None:
        self.project.transition = self.trans_combo.currentData()
        self.project.transition_ms = self.trans_ms.value()
        self.schedule_save()

    # ═════════════════════════════ источники ═══════════════════════════════
    def _scene(self) -> Optional[Scene]:
        return self.project.scene(self.sel_scene)

    def refresh_sources(self, select: str = "") -> None:
        sc = self._scene()
        keep = select or self._current_source_id()
        self.source_list.blockSignals(True)
        self.source_list.clear()
        if sc is not None:
            from gui_icons import icon
            for src in reversed(sc.sources):              # сверху списка — верхний слой
                rt = self.stage.runtime(sc.id, src.id)
                note = f"   ⚠ {rt.status}" if rt is not None and rt.status else ""
                extra = "  ♪ за треком" if src.kind == "video" and src.sync else ""
                it = QListWidgetItem(icon(KIND_ICONS.get(src.kind, "plus")),
                                     f"{src.name}{extra}{'  (закреплён)' if src.locked else ''}{note}")
                it.setData(Qt.UserRole, src.id)
                it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
                it.setCheckState(Qt.Checked if src.visible else Qt.Unchecked)
                it.setToolTip("Галочка — показать/скрыть. Двойной клик — свойства. "
                              "Мышью — поменять слои местами. Сюда можно перетащить файлы из папки")
                self.source_list.addItem(it)
                if src.id == keep:
                    self.source_list.setCurrentItem(it)
        self.source_list.blockSignals(False)

    def _current_source_id(self) -> str:
        it = self.source_list.currentItem()
        return it.data(Qt.UserRole) if it is not None else ""

    def _source(self, sid: str) -> Optional[Source]:
        sc = self._scene()
        return sc.source(sid) if sc is not None and sid else None

    def _on_source_row(self, _row: int) -> None:
        self.edit_view.select_source(self._current_source_id())

    def _on_source_item_changed(self, it: QListWidgetItem) -> None:
        src = self._source(it.data(Qt.UserRole))
        if src is not None:
            vis = it.checkState() == Qt.Checked
            if vis != src.visible:
                src.visible = vis
                self.stage.update_source(self.sel_scene, src.id)
                self.schedule_save()

    def _on_view_selected(self, sid: str) -> None:
        self.source_list.blockSignals(True)
        for i in range(self.source_list.count()):
            it = self.source_list.item(i)
            if it.data(Qt.UserRole) == sid:
                self.source_list.setCurrentItem(it)
                break
        else:
            self.source_list.clearSelection()
        self.source_list.blockSignals(False)

    def _on_view_moved(self, sid: str, x: float, y: float) -> None:
        src = self._source(sid)
        if src is not None:
            src.x, src.y = round(x), round(y)
            self.stage.update_source(self.sel_scene, sid)
            self.schedule_save()

    def _on_view_resized(self, sid: str, x: float, y: float, w: float, h: float) -> None:
        src = self._source(sid)
        if src is not None:
            src.x, src.y, src.w, src.h = round(x), round(y), round(w), round(h)
            self.stage.update_source(self.sel_scene, sid)
            self.edit_view.viewport().update()
            self.schedule_save()

    def add_source(self, kind: str) -> None:
        sc = self._scene()
        if sc is None:
            return
        cw, ch = self.project.canvas_w, self.project.canvas_h
        names = {s.name for s in sc.sources}
        base = SOURCE_KINDS[kind].split(" /")[0].split(" (")[0]
        name, i = base, 2
        while name in names:
            name, i = f"{base} {i}", i + 1
        src = Source(kind, name, w=cw, h=ch)
        if kind == "video":
            f, _ = QFileDialog.getOpenFileName(self, "Видеофайл", "", VIDEO_FILTER)
            if not f:
                return
            src.params["file"] = f
            src.name = Path(f).stem
            src.muted = False                      # звук видео включается/выключается фейдером сцены
        elif kind == "text":
            src.params = {"text": "Текст", "size": 96, "color": "#ffffff", "align": "center", "outline": 4,
                          "outline_color": "#000000"}
            src.x, src.y, src.w, src.h = cw * 0.1, ch * 0.75, cw * 0.8, ch * 0.18
        elif kind == "color":
            src.params = {"color": "#000000"}
        elif kind == "camera":
            src.sync = False
        sc.sources.append(src)
        self.stage.add_source(sc.id, src)
        self.refresh_sources(select=src.id)
        self.edit_view.select_source(src.id)
        if kind != "video":
            if not self.edit_source(new=True):
                self._remove_source(src.id)
        self.schedule_save()

    def edit_source(self, new: bool = False) -> bool:
        src = self._source(self._current_source_id())
        if src is None:
            return False
        sid = self.sel_scene

        def apply(res: Source) -> None:
            for fl in fields(Source):
                if fl.name != "id":
                    setattr(src, fl.name, getattr(res, fl.name))
            self.stage.update_source(sid, src.id)
            self.refresh_sources(select=src.id)
            self.refresh_mixer()
            self.schedule_save()
        dlg = SourceDialog(src, self.project, apply, self)
        return bool(dlg.exec())

    def _remove_source(self, sid: str) -> None:
        sc = self._scene()
        src = self._source(sid)
        if sc is None or src is None:
            return
        sc.sources.remove(src)
        self.stage.remove_source(sc.id, sid)
        self.refresh_sources()
        self.refresh_mixer()
        self.schedule_save()

    def delete_source(self) -> None:
        src = self._source(self._current_source_id())
        if src is not None and QMessageBox.question(self, "Удалить", f"Удалить источник «{src.name}»?") \
                == QMessageBox.StandardButton.Yes:
            self._remove_source(src.id)

    def move_source(self, d: int) -> None:
        """d = +1 — выше (поверх), −1 — ниже."""
        sc = self._scene()
        src = self._source(self._current_source_id())
        if sc is None or src is None:
            return
        i = sc.sources.index(src)
        j = i + d
        if 0 <= j < len(sc.sources):
            sc.sources[i], sc.sources[j] = sc.sources[j], sc.sources[i]
            self.stage.restack(sc.id)
            self.refresh_sources(select=src.id)
            self.schedule_save()

    def _sources_reordered(self) -> None:
        """Источники переставили мышью: сверху списка — верхний слой."""
        sc = self._scene()
        if sc is None:
            return
        keep = self._current_source_id()
        by_id = {s.id: s for s in sc.sources}
        top_first = [by_id[i] for i in self.source_list.ids() if i in by_id]
        order = list(reversed(top_first)) + [s for s in sc.sources if s not in top_first]
        if order != sc.sources:
            sc.sources[:] = order
            self.stage.restack(sc.id)
            self.schedule_save()
        self.refresh_sources(select=keep)

    def add_dropped_files(self, paths: list) -> None:
        """Файлы/папки, брошенные из Проводника в «Источники»: видео и картинки — источники,
        папка с картинками — слайд-шоу, папка с видео — все видео из неё."""
        sc = self._scene()
        if sc is None:
            self.status("Сначала создайте или выберите сцену", warn=True)
            return
        cw, ch = self.project.canvas_w, self.project.canvas_h
        added, skipped = [], []

        def add(kind: str, name: str, params: dict) -> None:
            src = Source(kind, name, w=cw, h=ch)
            src.params = params
            if kind == "video":
                src.muted = False                  # звук видео — фейдером сцены
            sc.sources.append(src)
            self.stage.add_source(sc.id, src)
            added.append(src)

        for raw in paths:
            p = Path(raw)
            ext = p.suffix.lower()
            if p.is_dir():
                files = sorted(x for x in p.iterdir() if x.is_file())
                images = [x for x in files if x.suffix.lower() in IMAGE_EXT]
                videos = [x for x in files if x.suffix.lower() in VIDEO_EXT]
                if images:
                    add("slideshow", p.name, {"folder": str(p), "interval": 5.0})
                for x in videos:
                    add("video", x.stem, {"file": str(x)})
                if not images and not videos:
                    skipped.append(p.name)
            elif ext in VIDEO_EXT:
                add("video", p.stem, {"file": str(p)})
            elif ext in IMAGE_EXT:
                add("image", p.stem, {"file": str(p)})
            else:
                skipped.append(p.name)
        if added:
            self.refresh_sources(select=added[-1].id)
            self.edit_view.select_source(added[-1].id)
            self.refresh_mixer()
            self.schedule_save()
        msg = f"Добавлено источников: {len(added)}" if added else "Ничего не добавлено"
        if skipped:
            msg += f"; пропущено (не видео и не картинки): {', '.join(skipped[:3])}" + ("…" if len(skipped) > 3 else "")
        self.status(msg, warn=bool(skipped) or not added)

    def _source_menu(self, sid: str, gpos) -> None:
        src = self._source(sid)
        if src is None:
            return
        cw, ch = self.project.canvas_w, self.project.canvas_h

        def geo(x, y, w, h):
            src.x, src.y, src.w, src.h = x, y, w, h
            self.stage.update_source(self.sel_scene, sid)
            self.edit_view.viewport().update()
            self.schedule_save()
        m = QMenu(self)
        m.addAction("Свойства…", self.edit_source)
        m.addSeparator()
        m.addAction("На весь экран", lambda: geo(0, 0, cw, ch))
        m.addAction("По центру", lambda: geo((cw - src.w) / 2, (ch - src.h) / 2, src.w, src.h))
        m.addAction("Выше (поверх)", lambda: self.move_source(1))
        m.addAction("Ниже", lambda: self.move_source(-1))
        m.addSeparator()
        a_vis = m.addAction("Показан")
        a_vis.setCheckable(True)
        a_vis.setChecked(src.visible)
        a_lock = m.addAction("Закреплён (не двигать)")
        a_lock.setCheckable(True)
        a_lock.setChecked(src.locked)
        m.addSeparator()
        m.addAction("Удалить", self.delete_source)
        chosen = m.exec(gpos)
        if chosen is a_vis:
            src.visible = a_vis.isChecked()
        elif chosen is a_lock:
            src.locked = a_lock.isChecked()
        else:
            return
        self.stage.update_source(self.sel_scene, sid)
        self.refresh_sources(select=sid)
        self.schedule_save()

    # ═════════════════════════════ микшер ══════════════════════════════════
    def refresh_mixer(self) -> None:
        """Микшер сцены в эфире: Выход (все видео) · Сцена (запоминается для каждой сцены) · источники."""
        while self.mixer_lay.count() > 1:
            item = self.mixer_lay.takeAt(0)
            if item.widget():
                item.widget().deleteLater()
        self.mixer_strips: list = []
        p = self.project
        sc = p.scene(self.stage.program_id)
        rts = self.stage.audio_runtimes()
        rows = []

        def master_changed(db, muted):
            p.master_db, p.master_muted = db, muted
            self.stage.apply_audio()
            self.schedule_save()
        rows.append((MixerStrip("ВЫХОД (все видео)", p.master_db, p.master_muted, "#e6eaee"), master_changed,
                     lambda: rts))
        if sc is not None:
            def scene_changed(db, muted, sc=sc):
                if sc.muted and not muted:
                    # включили звук сцены — включаем и видео в ней (у видео отдельного фейдера нет;
                    # тонкая громкость конкретного видео — в его свойствах)
                    for src in sc.sources:
                        if src.kind == "video":
                            src.muted = False
                    for rt in rts:
                        rt.apply_gain()
                sc.volume_db, sc.muted = db, muted
                self.stage.apply_audio(sc.id)
                self.schedule_save()
            rows.append((MixerStrip(f"Сцена «{sc.name}»", sc.volume_db, sc.muted, "#36d399"), scene_changed,
                         lambda: rts))
        for i, (strip, fn, srcs) in enumerate(rows):
            strip.changed.connect(fn)
            self.mixer_lay.insertWidget(i, strip)
            self.mixer_strips.append((strip, srcs))
        if not rts:
            lbl = QLabel("В сцене эфира нет видео")
            lbl.setWordWrap(True)
            lbl.setObjectName("caption")
            self.mixer_lay.insertWidget(len(rows), lbl)
        why = meters_unavailable_reason()
        note = QLabel((f"Индикаторы уровня не работают: {why}. " if why else "")
                      + "Громкость запоминается для каждой сцены. Громкость отдельного видео — "
                      "в его свойствах (двойной клик по видео в источниках). "
                      "Звуковая карта — Настройки → Звук")
        note.setWordWrap(True)
        note.setObjectName("caption")
        self.mixer_lay.insertWidget(self.mixer_lay.count() - 1, note)

    def _tick_mixer(self) -> None:
        """Индикаторы микшера ~30 раз/с: пики источников (уже с учётом громкости)."""
        strips = getattr(self, "mixer_strips", [])
        if not strips:
            return
        peaks = {id(rt): rt.take_peaks() for rt in self.stage.audio_runtimes()}
        for strip, srcs in strips:
            chans: list = []
            for rt in srcs():
                pk = peaks.get(id(rt), [])
                if len(pk) > len(chans):
                    chans += [0.0] * (len(pk) - len(chans))
                for i, v in enumerate(pk):
                    chans[i] = max(chans[i], v)
            strip.meter.push(chans)

    # ═════════════════════════════ Concert DAW ═════════════════════════════
    def open_connect(self) -> None:
        dlg = ConnectDialog(self.project.daw_ip, self.project.daw_port, self)
        if dlg.exec():
            self.project.daw_ip = dlg.ip.text().strip()
            self.project.daw_port = dlg.port.value()
            self.project.auto_connect = True
            self.client.connect(self.project.daw_ip, self.project.daw_port)
            self.status(f"Подключаюсь к Concert DAW {self.project.daw_ip}…")
            self.schedule_save()

    # ═════════════════════════════ расписание ═════════════════════════════
    def _on_schedule_on(self, on: bool) -> None:
        self.project.schedule_on = on
        self.scheduler.last = datetime.datetime.now()   # включили — прошедшее не запускаем
        self._sched_sig = None
        self.schedule_save()

    def _sched_selected(self) -> Optional[ScheduleItem]:
        it = self.sched_tree.currentItem()
        sid = it.data(0, Qt.UserRole) if it is not None else ""
        return next((x for x in self.project.schedule if x.id == sid), None)

    def _schedule_video_scene(self, _name: str = "") -> str:
        """Сцена «видео на весь экран» для расписания: видео играет само (не за треком DAW)."""
        f, _ = QFileDialog.getOpenFileName(self, "Видео для расписания", "", VIDEO_FILTER)
        if not f:
            return ""
        sc = scene_for_video(self.project, Path(f).stem, f, sync=False, muted=False)   # со звуком
        self.project.scenes.append(sc)
        self.stage.add_scene(sc)
        self.refresh_scenes()
        return sc.id

    def _next_free_time(self) -> str:
        """Новый пункт — на ближайшие «круглые» 5 минут."""
        now = datetime.datetime.now() + datetime.timedelta(minutes=5)
        return now.replace(minute=now.minute - now.minute % 5, second=0).strftime("%H:%M:%S")

    def _add_schedule(self, item: ScheduleItem) -> None:
        dlg = ScheduleDialog(item, self.project, self._schedule_video_scene, self)
        if dlg.exec():
            self.project.schedule.append(dlg.result_item())
            self._sort_schedule()

    def add_schedule_video(self) -> None:
        sid = self._schedule_video_scene()
        if sid:
            sc = self.project.scene(sid)
            self._add_schedule(ScheduleItem(time=self._next_free_time(), scene_id=sid, name=sc.name,
                                            after="standby" if self.project.standby_scene else "keep"))

    def add_schedule_scene(self) -> None:
        self._add_schedule(ScheduleItem(time=self._next_free_time(), scene_id=self.sel_scene))

    def edit_schedule(self) -> None:
        item = self._sched_selected()
        if item is None:
            return
        dlg = ScheduleDialog(item, self.project, self._schedule_video_scene, self)
        if dlg.exec():
            new = dlg.result_item()
            self.project.schedule[self.project.schedule.index(item)] = new
            self._sort_schedule()

    def delete_schedule(self) -> None:
        item = self._sched_selected()
        if item is not None:
            self.project.schedule.remove(item)
            self._sort_schedule()

    def run_schedule_now(self) -> None:
        item = self._sched_selected()
        if item is not None:
            self._run_schedule(item)

    def _sort_schedule(self) -> None:
        self.project.schedule.sort(key=lambda x: (x.clock(), x.date))
        self._sched_sig = None
        self.schedule_save()

    def _run_schedule(self, item: ScheduleItem) -> None:
        """Время пункта пришло: сцена в эфир (видео — с начала), потом — действие «после видео»."""
        sc = self.project.scene(item.scene_id)
        if sc is None:
            self.status(f"Расписание {item.time}: сцена удалена — пункт пропущен", warn=True)
            return
        if self.black:
            self.set_black(False)
        already = self.stage.program_id == sc.id
        self.take(sc.id)
        if self.project.studio_mode:
            self.select_scene(sc.id)
        if already and item.restart:
            for rt in self.stage.runtimes.get(sc.id, {}).values():
                rt.restart()
        self._sched_active = item
        self.status(f"Расписание {item.time}: в эфире «{sc.name}»")
        self._sched_sig = None
        self.schedule_save()

    def _after_video(self) -> None:
        """Видео пункта расписания доиграло — что дальше."""
        item, self._sched_active = self._sched_active, None
        if item.after == "standby" and self.project.scene(self.project.standby_scene):
            self.take(self.project.standby_scene)
        elif item.after == "black":
            self.set_black(True)
        elif item.after == "scene" and self.project.scene(item.after_scene):
            self.take(item.after_scene)

    def _tick_schedule(self) -> None:
        now = datetime.datetime.now()
        mono = time.monotonic()
        if mono - self._sched_check >= 0.25:              # 4 раза в секунду хватает с запасом
            self._sched_check = mono
            if self.project.schedule_on:
                for item in self.scheduler.due(self.project.schedule, now):
                    self._run_schedule(item)
            else:
                self.scheduler.last = now
        act = self._sched_active
        if act is not None:
            if self.stage.program_id != act.scene_id:
                self._sched_active = None                  # оператор вывел другую сцену
            elif act.after != "keep" and any(rt.ended for rt in self.stage.runtimes.get(act.scene_id, {}).values()):
                self._after_video()
        if mono - self._sched_view_t >= 0.25:   # список и отсчёт — 4 раза/с хватает
            self._sched_view_t = mono
            self._refresh_schedule_view(now)

    def _refresh_schedule_view(self, now: datetime.datetime) -> None:
        clock = now.strftime("%H:%M:%S")
        if self.clock_label.text() != clock:
            self.clock_label.setText(clock)
        nxt = Scheduler.upcoming(self.project.schedule, now) if self.project.schedule_on else None
        if not self.project.schedule_on:
            text = "Расписание выключено"
        elif nxt is None:
            text = "Ближайших пунктов нет" if self.project.schedule else "Пусто — добавьте видео или сцену по времени"
        else:
            m, it = nxt
            text = f"Следующее: {m:%H:%M:%S} — «{it.name}» через {fmt_left((m - now).total_seconds())}"
        if self.next_label.text() != text:
            self.next_label.setText(text)
        today = now.date().isoformat()
        sig = (tuple((x.id, x.time, x.mode, x.date, tuple(x.weekdays), x.scene_id, x.after, x.enabled, x.fired,
                      x.name) for x in self.project.schedule), nxt[1].id if nxt else "", today,
               tuple(sc.name for sc in self.project.scenes))
        if sig == self._sched_sig:
            return
        self._sched_sig = sig
        cur = self._sched_selected()
        cur_id = cur.id if cur else ""
        self.sched_tree.clear()
        for x in self.project.schedule:
            sc = self.project.scene(x.scene_id)
            if not x.enabled:
                state, color = "выключен", "#7f8b97"
            elif x.fired == today:
                state, color = "✔ выполнен сегодня", "#7f8b97"
            elif nxt and nxt[1].id == x.id:
                state, color = "► следующий", "#36d399"
            elif x.next_run(now) is None:
                state, color = "прошёл", "#7f8b97"
            else:
                state, color = "ждёт", "#e6eaee"
            after = AFTER_ACTIONS.get(x.after, "")
            if x.after == "scene":
                a_sc = self.project.scene(x.after_scene)
                after = f"→ {a_sc.name}" if a_sc else "→ ?"
            it = QTreeWidgetItem([x.time, x.days_text(), sc.name if sc else "(сцена удалена)", after, state])
            it.setData(0, Qt.UserRole, x.id)
            for c in range(5):
                it.setForeground(c, QColor(color))
            self.sched_tree.addTopLevelItem(it)
            if x.id == cur_id:
                self.sched_tree.setCurrentItem(it)

    # ── автопоиск Concert DAW ──────────────────────────────────────────────
    def _search_daw(self) -> None:
        """Поиск в фоне (не тормозит интерфейс); результат забирает _tick."""
        if self._searching:
            return
        self._searching = True
        self._last_search = time.monotonic()

        def run():
            try:
                self._found = discover(1.5)
            except Exception:
                self._found = []
        threading.Thread(target=run, daemon=True, name="daw-discover").start()

    def _check_search(self) -> None:
        if not self._searching or self._found is None:
            return
        found, self._found, self._searching = self._found, None, False
        if not found:
            if not self.project.daw_ip:
                self.status("Concert DAW в сети не найден — запустите его или нажмите «Подключить…»", warn=True)
            return
        want = self.project.daw_name
        pick = next((d for d in found if want and d["name"] == want), None)
        if pick is None and not self.project.daw_ip:
            pick = found[0]                          # первый запуск — берём найденный
        if pick is None or (pick["ip"], pick["port"]) == (self.client.ip, self.client.port) and self.client.connected:
            return
        self.project.daw_ip, self.project.daw_port = pick["ip"], int(pick["port"])
        self.client.connect(pick["ip"], int(pick["port"]))
        self.status(f"Найден Concert DAW «{pick['name']}» ({pick['ip']}) — подключаюсь")
        self.schedule_save()

    def _watch_link(self) -> None:
        """Связь пропала надолго (например, у компьютера с DAW сменился IP) — ищем его по имени."""
        c = self.client
        now = time.monotonic()
        if c.connected:
            self._lost_since = 0.0
            if c.server_name and c.server_name != self.project.daw_name:
                self.project.daw_name = c.server_name
                self.schedule_save()
            return
        if not self.project.auto_connect or not self.project.daw_name:
            return
        if not self._lost_since:
            self._lost_since = now
        elif now - self._lost_since > 10 and now - self._last_search > 15:
            self._search_daw()

    def _on_follow(self, on: bool) -> None:
        self.project.follow_daw = on
        self._auto_scene = self._auto_black = None      # сразу применить автоматику
        self.schedule_save()

    def _on_delay(self, v: int) -> None:
        self.project.video_delay_ms = v
        self.schedule_save()

    def _song_list(self) -> list[tuple[str, dict]]:
        songs = self.client.all_songs()
        if songs:
            return songs
        # нет связи: показываем треки, к которым уже есть привязки
        return [("(без связи)", {"id": b.song_id, "name": b.song_name, "duration": 0, "markers": []})
                for b in self.project.bindings.values()]

    def refresh_songs(self, force: bool = False) -> None:
        songs = self._song_list()
        sig = (tuple((pl, s.get("id"), s.get("name"), s.get("duration")) for pl, s in songs),
               tuple((k, b.scene_id, len(b.cues)) for k, b in self.project.bindings.items()),
               tuple(sc.name for sc in self.project.scenes), self.client.state.song_id)
        if sig == self._songs_sig and not force:
            return
        self._songs_sig = sig
        cur = self._selected_song_id()
        self.songs_tree.clear()
        groups: dict[str, QTreeWidgetItem] = {}
        for pl, sg in songs:
            parent = groups.get(pl)
            if parent is None:
                parent = QTreeWidgetItem([pl])
                f = parent.font(0)
                f.setBold(True)
                parent.setFont(0, f)
                self.songs_tree.addTopLevelItem(parent)
                parent.setExpanded(True)
                groups[pl] = parent
            b = self.project.bindings.get(sg.get("id", ""))
            sc = self.project.scene(b.scene_id) if b else None
            it = QTreeWidgetItem([sg.get("name", ""), fmt_t(float(sg.get("duration", 0)))[:5],
                                  sc.name if sc else ("—" if not b else "?"),
                                  str(len(b.cues)) if b and b.cues else ""])
            it.setData(0, Qt.UserRole, sg)
            if sg.get("id") == self.client.state.song_id and sg.get("id"):
                for c in range(4):
                    it.setForeground(c, QColor("#36d399"))
            parent.addChild(it)
            if sg.get("id") == cur:
                self.songs_tree.setCurrentItem(it)

    def _selected_song(self) -> Optional[dict]:
        it = self.songs_tree.currentItem()
        return it.data(0, Qt.UserRole) if it is not None else None

    def _selected_song_id(self) -> str:
        sg = self._selected_song()
        return sg.get("id", "") if sg else ""

    def _need_song(self) -> Optional[dict]:
        sg = self._selected_song()
        if not sg or not sg.get("id"):
            QMessageBox.information(self, "Треки", "Выберите трек в списке Concert DAW.")
            return None
        return sg

    def _binding(self, sg: dict) -> Binding:
        b = self.project.bindings.get(sg["id"])
        if b is None:
            b = Binding(sg["id"], sg.get("name", ""))
            self.project.bindings[sg["id"]] = b
        b.song_name = sg.get("name", b.song_name)
        return b

    def _new_video_scene(self, name: str) -> str:
        f, _ = QFileDialog.getOpenFileName(self, f"Видео для «{name}»", "", VIDEO_FILTER)
        if not f:
            return ""
        sc = scene_for_video(self.project, name, f)
        self.project.scenes.append(sc)
        self.stage.add_scene(sc)
        self.refresh_scenes()
        return sc.id

    def video_for_song(self) -> None:
        sg = self._need_song()
        if sg is None:
            return
        sid = self._new_video_scene(sg.get("name", "Трек"))
        if sid:
            self._binding(sg).scene_id = sid
            self.refresh_songs(force=True)
            self._auto_scene = None
            self.schedule_save()
            self.status(f"Трек «{sg.get('name')}» → видео на весь экран, синхронно с треком")

    def auto_bind(self) -> None:
        songs = [sg for _pl, sg in self.client.all_songs()]
        if not songs:
            QMessageBox.information(self, "Автопривязка", "Сначала подключитесь к Concert DAW — нужен список треков.")
            return
        folder = QFileDialog.getExistingDirectory(self, "Папка с видео для треков")
        if not folder:
            return
        files = [str(p) for p in Path(folder).rglob("*") if p.suffix.lower() in VIDEO_EXT]
        done = []
        for sg in songs:
            b = self.project.bindings.get(sg["id"])
            if b is not None and b.scene_id:
                continue
            f = match_video(sg.get("name", ""), files)
            if f:
                sc = scene_for_video(self.project, sg["name"], f)
                self.project.scenes.append(sc)
                self.stage.add_scene(sc)
                self._binding(sg).scene_id = sc.id
                done.append(f"{sg['name']}  ←  {Path(f).name}")
        self.refresh_scenes()
        self.refresh_songs(force=True)
        self.schedule_save()
        QMessageBox.information(self, "Автопривязка",
                                (f"Привязано треков: {len(done)}\n\n" + "\n".join(done[:30])) if done else
                                "Не нашлось видео с названиями треков (без привязки).")

    def edit_binding(self) -> None:
        sg = self._need_song()
        if sg is None:
            return
        b = self._binding(sg)
        dlg = BindingDialog(b, self.project, sg, lambda: self._new_video_scene(sg.get("name", "Трек")), self)
        if dlg.exec():
            self.project.bindings[sg["id"]] = dlg.result_binding()
            self._auto_scene = None
            self.refresh_songs(force=True)
            self.schedule_save()

    def unbind(self) -> None:
        sg = self._need_song()
        if sg and sg["id"] in self.project.bindings:
            del self.project.bindings[sg["id"]]
            self.refresh_songs(force=True)
            self.schedule_save()

    def _update_daw(self) -> None:
        c = self.client
        st = c.state
        if not c.ip:
            text, state = "не подключено — нажмите «Подключить…»", "off"
        elif c.connected:
            text, state = f"● {c.server_name or c.ip}   (сеть {c.rtt * 1000:.1f} мс)", "play"
        else:
            text, state = f"○ нет связи с {c.ip} — переподключаюсь…", "pause"
        if self.daw_status.text() != text:
            self.daw_status.setText(text)
            self.daw_status.setProperty("state", state)
            repolish(self.daw_status)
        pos = c.position() if c.connected else 0.0
        track = st.song_name or "—"
        if pos < 0 and st.playing:
            tstr = f"-{abs(pos):.1f}"
            sstate, stext = "pause", "ОТСЧЁТ"
        else:
            tstr = f"{fmt_t(pos)}" + (f" / {fmt_t(st.duration)[:5]}" if st.duration else "")
            sstate, stext = ("play", "▶ ИГРАЕТ") if st.playing else (
                ("pause", "❚❚ ПАУЗА") if pos > 0.05 else ("off", "■ СТОП"))
        if not c.connected:
            sstate, stext = "off", ""
        new = (track, tstr, stext)
        if new != self._last_daw_text:
            self._last_daw_text = new
            self.daw_track.setText(track)
            self.daw_time.setText(tstr)
            self.daw_state.setText(stext)
            self.daw_state.setProperty("state", sstate)
            repolish(self.daw_state)
        self.daw_label.setText(f"DAW: {'●' if c.connected else '○'}")

    # ═════════════════════════════ главный цикл ═════════════════════════════
    def _tick(self) -> None:
        for kind, value in self.client.poll():
            if kind == "connection":
                self.status("Concert DAW подключён" if value else "Связь с Concert DAW потеряна — переподключаюсь",
                            warn=not value)
                self._auto_scene = self._auto_black = None
            elif kind == "songs":
                self.refresh_songs()
            elif kind == "transport":
                self.refresh_songs()
        c = self.client
        st = c.state
        pos = c.position() if c.connected else 0.0
        d = decide(self.project, c.connected, st.song_id, pos, st.playing)
        # автоматика срабатывает на ИЗМЕНЕНИЕ решения — ручное переключение оператора
        # держится до следующего события (новый трек, PLAY, смена сцены по времени)
        if d.scene_id != self._auto_scene:
            self._auto_scene = d.scene_id
            if d.scene_id and d.scene_id != self.stage.program_id and self.project.scene(d.scene_id):
                self.take(d.scene_id)
        if d.black != self._auto_black:
            self._auto_black = d.black
            if d.black is not None:
                self.set_black(d.black)
        self.stage.tick(d.media_pos, d.playing, c.connected)
        self._check_search()
        self._watch_link()
        self._update_daw()
        self._update_outputs_label()
        self._tick_schedule()
        self._tick_mixer()
        self._tick_media()

    # ═════════════════════════════ проекторы ═══════════════════════════════
    def _fill_projector_menu(self) -> None:
        m = self.proj_menu
        m.clear()
        for i, scr in enumerate(QGuiApplication.screens()):
            g = scr.geometry()
            busy = any(p.screen_name == scr.name() and not p.windowed for p in self.projectors)
            act = m.addAction(f"На весь экран: {i + 1}. {scr.name()}  ({g.width()}×{g.height()})"
                              + ("  — открыт" if busy else ""))
            act.triggered.connect(lambda _=False, s=scr: self.open_projector(s))
        m.addAction("В окне", lambda: self.open_projector(None, windowed=True))
        m.addSeparator()
        a = m.addAction("Закрыть все проекторы", self.close_projectors)
        a.setEnabled(bool(self.projectors))

    def open_projector(self, screen, windowed: bool = False, remember: bool = True) -> None:
        if screen is not None and not windowed:
            for p in list(self.projectors):
                if p.screen_name == screen.name() and not p.windowed:
                    p.close()                       # тот же экран — заменить
            if screen == self.screen() and QMessageBox.question(
                    self, "Проектор", "Это экран, на котором открыто окно программы. Всё равно вывести "
                                      "эфир на весь этот экран?") != QMessageBox.StandardButton.Yes:
                return
        pw = ProjectorWindow(screen, windowed)
        pw.closed.connect(self._projector_closed)
        canvas = self.stage.canvas(self.stage.program_id)
        pw.tv.show_scene(canvas, "cut", 0)
        if self.black:
            pw.tv.set_black(True, 1)
        self.projectors.append(pw)
        pw.open()
        if remember:
            self._remember_projectors()

    def _projector_closed(self, pw) -> None:
        if pw in self.projectors:
            self.projectors.remove(pw)
            if not self._closing:
                self._remember_projectors()

    _closing = False

    def close_projectors(self) -> None:
        for p in list(self.projectors):
            p.close()

    def _remember_projectors(self) -> None:
        self.project.projectors = [{"screen": p.screen_name, "windowed": p.windowed} for p in self.projectors
                                   if not p.windowed]
        self.schedule_save()

    def _restore_projectors(self) -> None:
        """Проекторы, открытые в прошлый раз, — снова на свои экраны (если экран подключён)."""
        screens = {s.name(): s for s in QGuiApplication.screens()}
        for pr in list(self.project.projectors):
            scr = screens.get(pr.get("screen", ""))
            if scr is not None and scr != self.screen():
                self.open_projector(scr, remember=False)

    # ═════════════════════════════ запись / трансляция ═════════════════════
    def toggle_output(self, kind: str, on: bool) -> None:
        btn = self.rec_btn if kind == "rec" else self.live_btn
        if not on:
            self._stop_output(kind)
            return
        p = self.project
        if kind == "rec":
            folder = Path(p.rec_folder) if p.rec_folder else Path.home() / "Videos" / "Concert Video"
            folder.mkdir(parents=True, exist_ok=True)
            target = str(folder / f"Concert {datetime.datetime.now():%Y-%m-%d %H-%M-%S}.{p.rec_format}")
            bitrate = p.rec_bitrate
        else:
            if not p.stream_url or not p.stream_key:
                QMessageBox.information(self, "Трансляция", "Укажите сервер и ключ трансляции: "
                                                            "Настройки → Запись и трансляция.")
                btn.setChecked(False)
                return
            target = p.stream_url.rstrip("/") + "/" + p.stream_key
            bitrate = p.stream_bitrate
        out = FfmpegOutput(kind, target, p.out_w, p.out_h, p.out_fps, bitrate, p.audio_device)
        if not out.start():
            QMessageBox.warning(self, "Запись" if kind == "rec" else "Трансляция", out.error)
            btn.setChecked(False)
            return
        self.outputs[kind] = out
        btn.setText(" Остановить запись" if kind == "rec" else " Остановить трансляцию")
        self.capture_timer.setInterval(int(1000 / max(1, p.out_fps)))
        if not self.capture_timer.isActive():
            self.capture_timer.start()
        self.status(("Запись: " + target) if kind == "rec" else "Трансляция началась")

    def _stop_output(self, kind: str) -> None:
        out = self.outputs.pop(kind, None)
        btn = self.rec_btn if kind == "rec" else self.live_btn
        btn.setChecked(False)
        btn.setText(" Начать запись" if kind == "rec" else " Начать трансляцию")
        if not self.outputs:
            self.capture_timer.stop()
        if out is not None:
            out.stop()
            if out.error:
                self.status(f"{'Запись' if kind == 'rec' else 'Трансляция'}: {out.error}", warn=True)
            elif kind == "rec":
                self.status(f"Запись сохранена: {out.target}")

    def _capture(self) -> None:
        """Кадр эфира (с переходом и затемнением) → в ffmpeg."""
        p = self.project
        w, h = p.out_w, p.out_h
        img = QImage(w, h, QImage.Format.Format_RGB32)
        img.fill(QColor("#000000"))
        painter = QPainter(img)
        painter.setRenderHint(QPainter.SmoothPixmapTransform)
        canvas = self.stage.canvas(self.stage.program_id)
        if canvas is not None:
            canvas.render(painter, QRectF(0, 0, w, h), canvas.sceneRect(), Qt.KeepAspectRatio)
        tv = self.program_tv
        if tv.snapshot is not None and tv.overlay_level > 0.001:
            painter.setOpacity(tv.overlay_level)
            painter.drawPixmap(QRectF(0, 0, w, h), tv.snapshot, QRectF(tv.snapshot.rect()))
        if tv.black_level > 0.001:
            painter.setOpacity(tv.black_level)
            painter.fillRect(QRectF(0, 0, w, h), QColor("#000000"))
        painter.end()
        data = _image_bytes(img)
        for kind, out in list(self.outputs.items()):
            if out.running:
                out.push(data)
            else:
                self._stop_output(kind)

    def _update_outputs_label(self) -> None:
        parts = []
        for kind, out in self.outputs.items():
            t = int(out.seconds)
            label = "● REC" if kind == "rec" else "● LIVE"
            drop = f" (пропущено {out.dropped})" if out.dropped else ""
            parts.append(f"{label} {t // 3600:02d}:{t // 60 % 60:02d}:{t % 60:02d}{drop}")
        text = "   ".join(parts)
        if self.out_label.text() != text:
            self.out_label.setText(text)
            self.out_label.setStyleSheet("color:#ef4b55;font-weight:800" if text else "")

    # ═════════════════════════════ проект / настройки ══════════════════════
    def schedule_save(self) -> None:
        self._save_timer.start()

    def save_now(self) -> None:
        self.project.ui_splitters = {"top": self.split_top.sizes(), "bottom": self.split_bottom.sizes(),
                                     "root": self.split_root.sizes()}
        try:
            self.store.save(self.project)
        except OSError as exc:
            self.status(f"Не удалось сохранить проект: {exc}", warn=True)

    def _restore_splitters(self) -> None:
        sizes = self.project.ui_splitters or {}
        for key, sp in (("top", self.split_top), ("bottom", self.split_bottom), ("root", self.split_root)):
            s = sizes.get(key)
            if s and len(s) == sp.count() and all(isinstance(x, int) and x > 0 for x in s):
                sp.setSizes(s)

    def open_settings(self) -> None:
        dlg = SettingsDialog(self.project, self, update_button=self.update_btn)
        if not dlg.exec():
            return
        old_ip, old_port = self.project.daw_ip, self.project.daw_port
        if dlg.apply_to(self.project):
            self.stage.set_canvas_size()
            for v in [self.edit_view] + [tv.view for tv in self._tvs()]:
                v.refit()
        self.trans_combo.setCurrentIndex(max(0, self.trans_combo.findData(self.project.transition)))
        self.trans_ms.setValue(self.project.transition_ms)
        self.delay_spin.setValue(self.project.video_delay_ms)
        if (self.project.daw_ip, self.project.daw_port) != (old_ip, old_port):
            if self.project.daw_ip:
                self.client.connect(self.project.daw_ip, self.project.daw_port)
            else:
                self.client.disconnect()
        self._auto_scene = self._auto_black = None
        self.stage.apply_audio()
        self.refresh_mixer()
        self.schedule_save()

    def _load_project(self, project: Project, store: Optional[ProjectStore] = None) -> None:
        self.close_projectors()
        self.stage.release()
        if store is not None:
            self.store = store
        self.project = project
        self.stage = Stage(project)
        self.follow_cb.setChecked(project.follow_daw)
        self._apply_studio_layout()
        self.delay_spin.setValue(project.video_delay_ms)
        first = project.program_scene if project.scene(project.program_scene) else \
            (project.scenes[0].id if project.scenes else "")
        self.program_tv.view.setScene(None)
        self.select_scene(first)
        self.take(first, "cut")
        self._auto_scene = self._auto_black = None
        self.refresh_songs(force=True)
        if project.auto_connect and project.daw_ip and project.daw_ip != self.client.ip:
            self.client.connect(project.daw_ip, project.daw_port)
        QTimer.singleShot(300, self._restore_projectors)

    def save_as(self) -> None:
        f, _ = QFileDialog.getSaveFileName(self, "Сохранить проект", "", "Проект Concert Video (*.json)")
        if f:
            self.store = ProjectStore(Path(f))
            self.save_now()
            self.status(f"Проект сохранён: {f}")

    def open_project(self) -> None:
        f, _ = QFileDialog.getOpenFileName(self, "Открыть проект", "", "Проект Concert Video (*.json)")
        if f:
            self.save_now()
            store = ProjectStore(Path(f))
            self._load_project(store.load(), store)
            self.status(f"Открыт проект: {f}")

    def new_project(self) -> None:
        if QMessageBox.question(self, "Новый проект", "Начать новый проект? Текущий сохранён.") \
                != QMessageBox.StandardButton.Yes:
            return
        self.save_now()
        f, _ = QFileDialog.getSaveFileName(self, "Файл нового проекта", "", "Проект Concert Video (*.json)")
        if not f:
            return
        p = default_project()
        p.daw_ip, p.daw_port = self.project.daw_ip, self.project.daw_port
        store = ProjectStore(Path(f))
        self._load_project(p, store)
        self.save_now()

    def closeEvent(self, e) -> None:  # noqa: N802
        if self.outputs and QMessageBox.question(self, "Выход", "Идёт запись/трансляция. Остановить и выйти?") \
                != QMessageBox.StandardButton.Yes:
            e.ignore()
            return
        self._closing = True
        for kind in list(self.outputs):
            self._stop_output(kind)
        self.save_now()
        self.close_projectors()
        self.client.disconnect()
        self.stage.release()
        super().closeEvent(e)
