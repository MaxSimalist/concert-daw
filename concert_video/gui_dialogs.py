"""
gui_dialogs.py — Окна настроек: свойства источника, привязка сцен к треку,
подключение к Concert DAW, настройки программы.
"""
from __future__ import annotations

import copy
import threading
from pathlib import Path
from typing import Callable, Optional

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QGuiApplication
from PySide6.QtWidgets import (QCheckBox, QColorDialog, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFileDialog, QFontComboBox, QFormLayout, QGroupBox, QHBoxLayout, QHeaderView,
                               QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPlainTextEdit,
                               QPushButton, QSpinBox, QTableWidget, QTableWidgetItem, QTabWidget, QVBoxLayout,
                               QWidget)

from gui_icons import set_icon
from gui_layout import fit_to_screen, scrollable
from output import STREAM_SERVICES, audio_devices
from sources import camera_names, window_titles
from sync_client import DEFAULT_PORT, discover
from vmodel import (IMAGE_EXT, SOURCE_KINDS, STOP_ACTIONS, TRANSITIONS, VIDEO_EXT, Binding, Cue, Project,
                    Source)


def fmt_t(t: float) -> str:
    m, s = divmod(max(0.0, t), 60)
    return f"{int(m):02d}:{s:04.1f}"


def parse_t(text: str) -> Optional[float]:
    """«1:23.5» / «83.5» / «1:02:03» → секунды."""
    text = text.strip().replace(",", ".")
    if not text:
        return None
    try:
        parts = [float(p) for p in text.split(":")]
    except ValueError:
        return None
    t = 0.0
    for p in parts:
        t = t * 60 + p
    return t


class ColorButton(QPushButton):
    """Кнопка выбора цвета: показывает цвет, по клику — палитра."""

    def __init__(self, color: str = "#ffffff", allow_none: bool = False, parent=None):
        super().__init__(parent)
        self.allow_none = allow_none
        self.color = color
        self.clicked.connect(self._pick)
        self._show()

    def _show(self) -> None:
        if self.color:
            c = QColor(self.color)
            fg = "#000000" if c.lightness() > 140 else "#ffffff"
            self.setText(self.color.upper())
            self.setStyleSheet(f"background:{self.color};color:{fg};font-weight:700")
        else:
            self.setText("нет")
            self.setStyleSheet("")

    def _pick(self) -> None:
        c = QColorDialog.getColor(QColor(self.color or "#ffffff"), self, "Цвет",
                                  QColorDialog.ColorDialogOption.ShowAlphaChannel)
        if c.isValid():
            self.color = c.name(QColor.NameFormat.HexArgb) if c.alpha() < 255 else c.name()
            self._show()
        elif self.allow_none and QMessageBox.question(self, "Цвет", "Убрать цвет?") == QMessageBox.StandardButton.Yes:
            self.color = ""
            self._show()


def _file_row(edit: QLineEdit, title: str, filt: str, folder: bool = False) -> QHBoxLayout:
    row = QHBoxLayout()
    row.addWidget(edit, 1)
    b = QPushButton("")
    set_icon(b, "folder", 16)
    b.setToolTip("Выбрать…")

    def pick():
        start = str(Path(edit.text()).parent) if edit.text() else ""
        if folder:
            d = QFileDialog.getExistingDirectory(edit, title, edit.text() or start)
        else:
            d, _ = QFileDialog.getOpenFileName(edit, title, start, filt)
        if d:
            edit.setText(d)
    b.clicked.connect(pick)
    row.addWidget(b)
    return row


VIDEO_FILTER = "Видео (" + " ".join(f"*{e}" for e in sorted(VIDEO_EXT)) + ");;Все файлы (*)"
IMAGE_FILTER = "Изображения (" + " ".join(f"*{e}" for e in sorted(IMAGE_EXT)) + ");;Все файлы (*)"


# ══════════════════════════════════════════════════════════════════════════════
# Свойства источника
# ══════════════════════════════════════════════════════════════════════════════
class SourceDialog(QDialog):
    """Свойства источника. Правка идёт по копии; «Применить» / OK — в проект."""

    def __init__(self, src: Source, project: Project, apply: Callable[[Source], None], parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Свойства — {src.name}")
        self.src = copy.deepcopy(src)
        self.project = project
        self.apply_cb = apply
        k = src.kind
        p = self.src.params
        root = QVBoxLayout(self)
        tabs = QTabWidget()
        root.addWidget(tabs, 1)

        # ── содержимое ─────────────────────────────────────────────────────
        main = QWidget()
        form = QFormLayout(main)
        self.name = QLineEdit(src.name)
        form.addRow("Название:", self.name)
        form.addRow("Тип:", QLabel(SOURCE_KINDS.get(k, k)))
        self.w: dict = {}
        if k == "video":
            self.w["file"] = QLineEdit(p.get("file", ""))
            form.addRow("Файл:", _file_row(self.w["file"], "Видеофайл", VIDEO_FILTER))
            self.sync = QCheckBox("Идти за треком Concert DAW (синхронно с позицией трека)")
            self.sync.setChecked(self.src.sync)
            form.addRow(self.sync)
            self.offset = QDoubleSpinBox()
            self.offset.setRange(-3600, 3600)
            self.offset.setDecimals(2)
            self.offset.setSingleStep(0.1)
            self.offset.setSuffix(" с")
            self.offset.setValue(self.src.offset)
            self.offset.setToolTip("Видео начинается на столько секунд позже начала трека (минус — раньше)")
            form.addRow("Сдвиг от начала трека:", self.offset)
            self.loop = QCheckBox("По кругу")
            self.loop.setChecked(self.src.loop)
            form.addRow(self.loop)
            self.restart = QCheckBox("Без синхронизации: начинать сначала при показе сцены")
            self.restart.setChecked(bool(p.get("restart", True)))
            form.addRow(self.restart)
            self.volume = QSpinBox()
            self.volume.setRange(0, 100)
            self.volume.setSuffix(" %")
            self.volume.setValue(int(round(self.src.volume * 100)))
            self.muted = QCheckBox("Без звука (звук концерта идёт из Concert DAW)")
            self.muted.setChecked(self.src.muted)
            form.addRow("Громкость:", self.volume)
            form.addRow(self.muted)
        elif k == "image":
            self.w["file"] = QLineEdit(p.get("file", ""))
            form.addRow("Файл:", _file_row(self.w["file"], "Изображение", IMAGE_FILTER))
        elif k == "slideshow":
            self.w["folder"] = QLineEdit(p.get("folder", ""))
            form.addRow("Папка:", _file_row(self.w["folder"], "Папка с картинками", "", folder=True))
            self.interval = QDoubleSpinBox()
            self.interval.setRange(1, 600)
            self.interval.setSuffix(" с")
            self.interval.setValue(float(p.get("interval", 5.0)))
            form.addRow("Смена каждые:", self.interval)
            self.shuffle = QCheckBox("Случайный порядок")
            self.shuffle.setChecked(bool(p.get("shuffle")))
            form.addRow(self.shuffle)
        elif k == "text":
            self.text = QPlainTextEdit(p.get("text", ""))
            self.text.setMinimumHeight(90)
            form.addRow("Текст:", self.text)
            self.font = QFontComboBox()
            self.font.setCurrentFont(QFont(p.get("font", "Segoe UI")))
            form.addRow("Шрифт:", self.font)
            row = QHBoxLayout()
            self.size = QSpinBox()
            self.size.setRange(8, 600)
            self.size.setSuffix(" px")
            self.size.setValue(int(p.get("size", 72)))
            self.bold = QCheckBox("Жирный")
            self.bold.setChecked(bool(p.get("bold", True)))
            self.italic = QCheckBox("Курсив")
            self.italic.setChecked(bool(p.get("italic", False)))
            for wd in (self.size, self.bold, self.italic):
                row.addWidget(wd)
            form.addRow("Размер:", row)
            self.color = ColorButton(p.get("color", "#ffffff"))
            form.addRow("Цвет:", self.color)
            self.align = QComboBox()
            for key, text in (("left", "Слева"), ("center", "По центру"), ("right", "Справа")):
                self.align.addItem(text, key)
            self.align.setCurrentIndex(max(0, self.align.findData(p.get("align", "center"))))
            form.addRow("Выравнивание:", self.align)
            row2 = QHBoxLayout()
            self.outline = QSpinBox()
            self.outline.setRange(0, 40)
            self.outline.setSuffix(" px")
            self.outline.setValue(int(p.get("outline", 0)))
            self.outline_color = ColorButton(p.get("outline_color", "#000000"))
            row2.addWidget(self.outline)
            row2.addWidget(self.outline_color)
            form.addRow("Обводка:", row2)
            self.bg = ColorButton(p.get("bg", ""), allow_none=True)
            form.addRow("Подложка:", self.bg)
        elif k == "color":
            self.color = ColorButton(p.get("color", "#000000"))
            form.addRow("Цвет:", self.color)
        elif k == "camera":
            self.device = QComboBox()
            names = camera_names()
            self.device.addItems(names or ["(камеры не найдены)"])
            if p.get("device") in names:
                self.device.setCurrentText(p["device"])
            form.addRow("Устройство:", self.device)
            self.keep_on = QCheckBox("Не выключать камеру, когда сцена не в эфире (переключение без задержки)")
            self.keep_on.setChecked(bool(p.get("keep_on", False)))
            form.addRow(self.keep_on)
        elif k == "screen":
            self.screen_combo = QComboBox()
            for s in QGuiApplication.screens():
                g = s.geometry()
                self.screen_combo.addItem(f"{s.name()} ({g.width()}×{g.height()})", s.name())
            self.screen_combo.setCurrentIndex(max(0, self.screen_combo.findData(p.get("screen", ""))))
            form.addRow("Экран:", self.screen_combo)
        elif k == "window":
            row = QHBoxLayout()
            self.window_combo = QComboBox()
            self.window_combo.setEditable(True)
            self.window_combo.addItems(window_titles())
            self.window_combo.setCurrentText(p.get("window", ""))
            ref = QPushButton("")
            set_icon(ref, "refresh", 16)
            ref.clicked.connect(lambda: (self.window_combo.clear(), self.window_combo.addItems(window_titles())))
            row.addWidget(self.window_combo, 1)
            row.addWidget(ref)
            form.addRow("Окно:", row)
        tabs.addTab(scrollable(main), "Источник")

        # ── положение ─────────────────────────────────────────────────────
        pos = QWidget()
        pf = QFormLayout(pos)
        self.geo = {}
        for key, label, lo, hi in (("x", "X", -20000, 20000), ("y", "Y", -20000, 20000),
                                   ("w", "Ширина", 1, 20000), ("h", "Высота", 1, 20000)):
            sp = QSpinBox()
            sp.setRange(lo, hi)
            sp.setSuffix(" px")
            sp.setValue(int(round(getattr(self.src, key))))
            pf.addRow(label + ":", sp)
            self.geo[key] = sp
        quick = QHBoxLayout()
        for text, fn in (("На весь экран", self._full), ("По центру", self._center)):
            b = QPushButton(text)
            b.clicked.connect(fn)
            quick.addWidget(b)
        pf.addRow(quick)
        self.rotation = QDoubleSpinBox()
        self.rotation.setRange(-360, 360)
        self.rotation.setSuffix(" °")
        self.rotation.setValue(self.src.rotation)
        pf.addRow("Поворот:", self.rotation)
        self.opacity = QSpinBox()
        self.opacity.setRange(0, 100)
        self.opacity.setSuffix(" %")
        self.opacity.setValue(int(round(self.src.opacity * 100)))
        pf.addRow("Непрозрачность:", self.opacity)
        self.fit = QComboBox()
        for key, text in (("fit", "Вписать (поля по краям)"), ("fill", "Заполнить (обрезать лишнее)"),
                          ("stretch", "Растянуть (исказить пропорции)")):
            self.fit.addItem(text, key)
        self.fit.setCurrentIndex(max(0, self.fit.findData(self.src.fit)))
        if k in ("video", "image", "slideshow", "camera", "screen", "window"):
            pf.addRow("Масштаб:", self.fit)
        self.locked = QCheckBox("Заблокировать (не двигать мышью)")
        self.locked.setChecked(self.src.locked)
        pf.addRow(self.locked)
        tabs.addTab(pos, "Положение")

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Apply | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.Apply).setText("Применить")
        buttons.button(QDialogButtonBox.Apply).clicked.connect(self._apply)
        root.addWidget(buttons)
        fit_to_screen(self, 640, 600)

    def _full(self) -> None:
        for key, v in (("x", 0), ("y", 0), ("w", self.project.canvas_w), ("h", self.project.canvas_h)):
            self.geo[key].setValue(v)

    def _center(self) -> None:
        self.geo["x"].setValue(int((self.project.canvas_w - self.geo["w"].value()) / 2))
        self.geo["y"].setValue(int((self.project.canvas_h - self.geo["h"].value()) / 2))

    def result_source(self) -> Source:
        s = self.src
        k = s.kind
        p = s.params
        s.name = self.name.text().strip() or s.name
        for key, wd in self.w.items():
            p[key] = wd.text().strip()
        if k == "video":
            s.sync, s.offset, s.loop = self.sync.isChecked(), self.offset.value(), self.loop.isChecked()
            s.volume, s.muted = self.volume.value() / 100.0, self.muted.isChecked()
            p["restart"] = self.restart.isChecked()
        elif k == "slideshow":
            p["interval"], p["shuffle"] = self.interval.value(), self.shuffle.isChecked()
        elif k == "text":
            p.update(text=self.text.toPlainText(), font=self.font.currentFont().family(), size=self.size.value(),
                     bold=self.bold.isChecked(), italic=self.italic.isChecked(), color=self.color.color,
                     align=self.align.currentData(), outline=self.outline.value(),
                     outline_color=self.outline_color.color, bg=self.bg.color)
        elif k == "color":
            p["color"] = self.color.color
        elif k == "camera":
            p["device"] = self.device.currentText()
            p["keep_on"] = self.keep_on.isChecked()
        elif k == "screen":
            p["screen"] = self.screen_combo.currentData()
        elif k == "window":
            p["window"] = self.window_combo.currentText()
        for key, sp in self.geo.items():
            setattr(s, key, float(sp.value()))
        s.rotation = self.rotation.value()
        s.opacity = self.opacity.value() / 100.0
        s.fit = self.fit.currentData()
        s.locked = self.locked.isChecked()
        return copy.deepcopy(s)

    def _apply(self) -> None:
        self.apply_cb(self.result_source())

    def _ok(self) -> None:
        self._apply()
        self.accept()


# ══════════════════════════════════════════════════════════════════════════════
# Привязка сцен к треку
# ══════════════════════════════════════════════════════════════════════════════
class BindingDialog(QDialog):
    def __init__(self, binding: Binding, project: Project, song: dict, make_video_scene: Callable[[], str],
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"Сцены для трека «{binding.song_name or song.get('name', '')}»")
        self.b = copy.deepcopy(binding)
        self.project = project
        self.song = song
        self.make_video_scene = make_video_scene
        lay = QVBoxLayout(self)
        info = QLabel("Когда в Concert DAW выбран и играет этот трек, на экранах — основная сцена. "
                      "Смена сцен по времени — в таблице (время от начала трека). Видео в сценах, "
                      "у которых включено «Идти за треком», играют синхронно с треком.")
        info.setWordWrap(True)
        lay.addWidget(info)
        form = QFormLayout()
        row = QHBoxLayout()
        self.scene = self._scene_combo(self.b.scene_id)
        row.addWidget(self.scene, 1)
        mk = QPushButton(" Видео на весь экран…")
        set_icon(mk, "plus", 15)
        mk.setToolTip("Выбрать видеофайл — будет создана сцена с этим видео (синхронно с треком)")
        mk.clicked.connect(self._new_video_scene)
        row.addWidget(mk)
        form.addRow("Основная сцена:", row)
        self.on_stop = QComboBox()
        for key, text in STOP_ACTIONS.items():
            self.on_stop.addItem(text, key)
        self.on_stop.setCurrentIndex(max(0, self.on_stop.findData(self.b.on_stop)))
        self.on_stop.setToolTip("Что на экране, пока трек выбран, но ещё не запущен (или после STOP)")
        form.addRow("До старта / после STOP:", self.on_stop)
        lay.addLayout(form)

        cap = QLabel("СМЕНА СЦЕН ПО ВРЕМЕНИ ТРЕКА")
        cap.setObjectName("caption")
        lay.addWidget(cap)
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["Время (мм:сс)", "Сцена"])
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        lay.addWidget(self.table, 1)
        for c in sorted(self.b.cues, key=lambda c: c.t):
            self._add_row(c.t, c.scene_id)
        btns = QHBoxLayout()
        for icon, text, fn in (("plus", " Добавить", lambda: self._add_row(0.0, self.scene.currentData() or "")),
                               ("trash", " Удалить", self._del_row),
                               ("marker", " Из маркеров трека", self._from_markers)):
            b = QPushButton(text)
            set_icon(b, icon, 15)
            b.clicked.connect(fn)
            btns.addWidget(b)
        btns.addStretch(1)
        lay.addLayout(btns)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        fit_to_screen(self, 640, 560)

    def _scene_combo(self, current: str) -> QComboBox:
        cb = QComboBox()
        cb.addItem("— нет —", "")
        for sc in self.project.scenes:
            cb.addItem(sc.name, sc.id)
        cb.setCurrentIndex(max(0, cb.findData(current)))
        return cb

    def _add_row(self, t: float, scene_id: str) -> None:
        r = self.table.rowCount()
        self.table.insertRow(r)
        self.table.setItem(r, 0, QTableWidgetItem(fmt_t(t)))
        self.table.setCellWidget(r, 1, self._scene_combo(scene_id))

    def _del_row(self) -> None:
        r = self.table.currentRow()
        if r >= 0:
            self.table.removeRow(r)

    def _from_markers(self) -> None:
        markers = self.song.get("markers") or []
        if not markers:
            QMessageBox.information(self, "Маркеры", "У этого трека в Concert DAW нет маркеров.")
            return
        for m in markers:
            self._add_row(float(m["t"]), self.scene.currentData() or "")

    def _new_video_scene(self) -> None:
        sid = self.make_video_scene()
        if sid:
            for cb in [self.scene] + [self.table.cellWidget(r, 1) for r in range(self.table.rowCount())]:
                if cb.findData(sid) < 0:
                    sc = self.project.scene(sid)
                    cb.addItem(sc.name if sc else sid, sid)
            self.scene.setCurrentIndex(self.scene.findData(sid))

    def result_binding(self) -> Binding:
        b = self.b
        b.scene_id = self.scene.currentData() or ""
        b.on_stop = self.on_stop.currentData()
        cues = []
        for r in range(self.table.rowCount()):
            t = parse_t(self.table.item(r, 0).text() if self.table.item(r, 0) else "")
            sid = self.table.cellWidget(r, 1).currentData()
            if t is not None and sid:
                cues.append(Cue(t, sid))
        b.cues = sorted(cues, key=lambda c: c.t)
        return b


# ══════════════════════════════════════════════════════════════════════════════
# Подключение к Concert DAW
# ══════════════════════════════════════════════════════════════════════════════
class ConnectDialog(QDialog):
    def __init__(self, ip: str, port: int, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Подключение к Concert DAW")
        lay = QVBoxLayout(self)
        info = QLabel("Concert DAW должен быть запущен на компьютере со звуком (Настройки → Видео → "
                      "«Разрешить подключение»). Оба компьютера — в одной сети.")
        info.setWordWrap(True)
        lay.addWidget(info)
        self.list = QListWidget()
        self.list.itemDoubleClicked.connect(lambda _i: self._take())
        self.list.itemClicked.connect(lambda _i: self._take(False))
        lay.addWidget(self.list, 1)
        self.search = QPushButton(" Найти Concert DAW в сети")
        set_icon(self.search, "search", 16)
        self.search.clicked.connect(self._search)
        lay.addWidget(self.search)
        form = QFormLayout()
        self.ip = QLineEdit(ip)
        self.ip.setPlaceholderText("например 192.168.1.20")
        self.port = QSpinBox()
        self.port.setRange(1024, 65000)
        self.port.setValue(int(port or DEFAULT_PORT))
        form.addRow("IP Concert DAW:", self.ip)
        form.addRow("Порт:", self.port)
        lay.addLayout(form)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Подключить")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self._found: Optional[list] = None
        self._timer = QTimer(self)
        self._timer.setInterval(100)
        self._timer.timeout.connect(self._check)
        fit_to_screen(self, 480, 420)
        QTimer.singleShot(100, self._search)

    def _search(self) -> None:
        self.search.setEnabled(False)
        self.search.setText(" Ищу…")
        self.list.clear()
        self._found = None

        def run():
            self._found = discover(1.5)
        threading.Thread(target=run, daemon=True).start()
        self._timer.start()

    def _check(self) -> None:
        if self._found is None:
            return
        self._timer.stop()
        self.search.setEnabled(True)
        self.search.setText(" Найти Concert DAW в сети")
        for d in self._found:
            it = QListWidgetItem(f"{d['name']}  —  {d['ip']}:{d['port']}")
            it.setData(Qt.UserRole, d)
            self.list.addItem(it)
        if not self._found:
            self.list.addItem("Не найден. Проверьте, что Concert DAW запущен и компьютеры в одной сети, "
                              "или введите IP вручную.")
        elif len(self._found) == 1:
            self.list.setCurrentRow(0)
            self._take(False)

    def _take(self, accept: bool = True) -> None:
        it = self.list.currentItem()
        d = it.data(Qt.UserRole) if it is not None else None
        if d:
            self.ip.setText(d["ip"])
            self.port.setValue(int(d["port"]))
            if accept:
                self.accept()


# ══════════════════════════════════════════════════════════════════════════════
# Настройки
# ══════════════════════════════════════════════════════════════════════════════
class SettingsDialog(QDialog):
    def __init__(self, project: Project, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Настройки Concert Video")
        self.p = project
        lay = QVBoxLayout(self)
        tabs = QTabWidget()
        lay.addWidget(tabs, 1)

        # ── видео / холст ──────────────────────────────────────────────────
        w = QWidget()
        f = QFormLayout(w)
        self.canvas = QComboBox()
        for cw, ch in ((1920, 1080), (1280, 720), (3840, 2160), (1024, 768), (1080, 1920)):
            self.canvas.addItem(f"{cw}×{ch}", (cw, ch))
        idx = self.canvas.findData((project.canvas_w, project.canvas_h))
        if idx < 0:
            self.canvas.addItem(f"{project.canvas_w}×{project.canvas_h}", (project.canvas_w, project.canvas_h))
            idx = self.canvas.count() - 1
        self.canvas.setCurrentIndex(idx)
        f.addRow("Холст (разрешение сцен):", self.canvas)
        self.trans = QComboBox()
        for key, text in TRANSITIONS.items():
            self.trans.addItem(text, key)
        self.trans.setCurrentIndex(max(0, self.trans.findData(project.transition)))
        f.addRow("Переход по умолчанию:", self.trans)
        self.trans_ms = QSpinBox()
        self.trans_ms.setRange(0, 5000)
        self.trans_ms.setSingleStep(100)
        self.trans_ms.setSuffix(" мс")
        self.trans_ms.setValue(project.transition_ms)
        f.addRow("Длительность перехода:", self.trans_ms)
        self.gpu = QCheckBox("Эфир и проекторы рисует видеокарта (OpenGL) — меньше нагрузка на процессор")
        self.gpu.setChecked(project.gpu_render)
        self.gpu.setToolTip("Выключите, если на проекторе чёрный экран или мерцание. "
                            "Применяется после перезапуска программы")
        f.addRow(self.gpu)
        tabs.addTab(scrollable(w), "Видео")

        # ── звук ───────────────────────────────────────────────────────────
        w = QWidget()
        f = QFormLayout(w)
        info = QLabel("Звук видео (если он включён в микшере) идёт на выбранную звуковую карту. "
                      "Звук концерта при этом идёт из Concert DAW — это другой компьютер.")
        info.setWordWrap(True)
        f.addRow(info)
        self.audio_out = QComboBox()
        self.audio_out.addItem("Устройство Windows по умолчанию", "")
        try:
            from PySide6.QtMultimedia import QMediaDevices
            for d in QMediaDevices.audioOutputs():
                self.audio_out.addItem(d.description(), d.description())
        except Exception:
            pass
        if project.audio_out and self.audio_out.findData(project.audio_out) < 0:
            self.audio_out.addItem(f"{project.audio_out} (не подключена)", project.audio_out)
        self.audio_out.setCurrentIndex(max(0, self.audio_out.findData(project.audio_out)))
        f.addRow("Звуковая карта (выход):", self.audio_out)
        self.new_muted = QCheckBox("Новые сцены с видео — без звука (звук концерта идёт из Concert DAW)")
        self.new_muted.setChecked(project.new_video_muted)
        f.addRow(self.new_muted)
        note = QLabel("Сцены из расписания добавляются со звуком. Громкость — в микшере: "
                      "общий выход, сцена (запоминается для каждой сцены); отдельное видео — в его свойствах.")
        note.setWordWrap(True)
        note.setObjectName("caption")
        f.addRow(note)
        tabs.addTab(scrollable(w), "Звук")

        # ── Concert DAW ────────────────────────────────────────────────────
        w = QWidget()
        f = QFormLayout(w)
        self.ip = QLineEdit(project.daw_ip)
        self.port = QSpinBox()
        self.port.setRange(1024, 65000)
        self.port.setValue(project.daw_port)
        f.addRow("IP Concert DAW:", self.ip)
        f.addRow("Порт:", self.port)
        self.auto = QCheckBox("Подключаться при запуске")
        self.auto.setChecked(project.auto_connect)
        f.addRow(self.auto)
        self.delay = QSpinBox()
        self.delay.setRange(-2000, 2000)
        self.delay.setSuffix(" мс")
        self.delay.setValue(project.video_delay_ms)
        self.delay.setToolTip("Плюс — видео позже звука, минус — раньше. Проектор обычно показывает "
                              "с задержкой 30–100 мс: тогда ставят минус столько же")
        f.addRow("Сдвиг видео относительно звука:", self.delay)
        self.unbound = QComboBox()
        self.unbound.addItem("Ничего не менять на экране", "keep")
        self.unbound.addItem("Показать заставку", "standby")
        self.unbound.setCurrentIndex(max(0, self.unbound.findData(project.unbound_action)))
        f.addRow("Трек без привязки:", self.unbound)
        tabs.addTab(scrollable(w), "Concert DAW")

        # ── запись / трансляция ────────────────────────────────────────────
        w = QWidget()
        f = QFormLayout(w)
        self.out = QComboBox()
        for ow, oh in ((1280, 720), (1920, 1080), (854, 480), (1080, 1920)):
            self.out.addItem(f"{ow}×{oh}", (ow, oh))
        idx = self.out.findData((project.out_w, project.out_h))
        self.out.setCurrentIndex(max(0, idx))
        f.addRow("Разрешение записи/трансляции:", self.out)
        self.fps = QComboBox()
        for v in (25, 30, 50, 60):
            self.fps.addItem(f"{v} к/с", v)
        self.fps.setCurrentIndex(max(0, self.fps.findData(project.out_fps)))
        f.addRow("Частота кадров:", self.fps)
        row = QHBoxLayout()
        self.audio = QComboBox()
        self.audio.addItem("— без звука —", "")
        if project.audio_device:
            self.audio.addItem(project.audio_device, project.audio_device)
            self.audio.setCurrentIndex(1)
        ref = QPushButton(" Найти")
        set_icon(ref, "refresh", 15)
        ref.clicked.connect(self._fill_audio)
        row.addWidget(self.audio, 1)
        row.addWidget(ref)
        f.addRow("Звук (вход Windows):", row)
        rec = QGroupBox("Запись")
        rf = QFormLayout(rec)
        self.rec_folder = QLineEdit(project.rec_folder)
        self.rec_folder.setPlaceholderText("Видео / Concert Video")
        rf.addRow("Папка:", _file_row(self.rec_folder, "Папка для записей", "", folder=True))
        self.rec_fmt = QComboBox()
        self.rec_fmt.addItem("MKV — не портится при сбое (рекомендуется)", "mkv")
        self.rec_fmt.addItem("MP4", "mp4")
        self.rec_fmt.setCurrentIndex(max(0, self.rec_fmt.findData(project.rec_format)))
        rf.addRow("Формат:", self.rec_fmt)
        self.rec_br = QSpinBox()
        self.rec_br.setRange(500, 50000)
        self.rec_br.setSingleStep(500)
        self.rec_br.setSuffix(" кбит/с")
        self.rec_br.setValue(project.rec_bitrate)
        rf.addRow("Битрейт:", self.rec_br)
        f.addRow(rec)
        st = QGroupBox("Трансляция")
        sf = QFormLayout(st)
        self.service = QComboBox()
        for name, url in STREAM_SERVICES.items():
            self.service.addItem(name, url)
        self.service.currentIndexChanged.connect(self._service)
        sf.addRow("Сервис:", self.service)
        self.url = QLineEdit(project.stream_url)
        sf.addRow("Сервер (RTMP):", self.url)
        self.key = QLineEdit(project.stream_key)
        self.key.setEchoMode(QLineEdit.Password)
        sf.addRow("Ключ трансляции:", self.key)
        self.st_br = QSpinBox()
        self.st_br.setRange(500, 20000)
        self.st_br.setSingleStep(250)
        self.st_br.setSuffix(" кбит/с")
        self.st_br.setValue(project.stream_bitrate)
        sf.addRow("Битрейт:", self.st_br)
        f.addRow(st)
        tabs.addTab(scrollable(w), "Запись и трансляция")

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        fit_to_screen(self, 620, 620)

    def _service(self, _i: int) -> None:
        url = self.service.currentData()
        if url:
            self.url.setText(url)

    def _fill_audio(self) -> None:
        cur = self.audio.currentData()
        self.audio.clear()
        self.audio.addItem("— без звука —", "")
        devs = audio_devices()
        for d in devs:
            self.audio.addItem(d, d)
        self.audio.setCurrentIndex(max(0, self.audio.findData(cur)))
        if not devs:
            QMessageBox.information(self, "Звук", "Устройства записи не найдены (нужен Windows и ffmpeg).")

    def apply_to(self, p: Project) -> bool:
        """Записать в проект. True — изменился размер холста (нужно перестроить сцены)."""
        cw, ch = self.canvas.currentData()
        canvas_changed = (cw, ch) != (p.canvas_w, p.canvas_h)
        p.canvas_w, p.canvas_h = cw, ch
        p.transition, p.transition_ms = self.trans.currentData(), self.trans_ms.value()
        p.daw_ip, p.daw_port, p.auto_connect = self.ip.text().strip(), self.port.value(), self.auto.isChecked()
        p.video_delay_ms = self.delay.value()
        p.audio_out = self.audio_out.currentData() or ""
        p.gpu_render = self.gpu.isChecked()
        p.new_video_muted = self.new_muted.isChecked()
        p.unbound_action = self.unbound.currentData()
        p.out_w, p.out_h = self.out.currentData()
        p.out_fps = self.fps.currentData()
        p.audio_device = self.audio.currentData() or ""
        p.rec_folder, p.rec_format, p.rec_bitrate = (self.rec_folder.text().strip(), self.rec_fmt.currentData(),
                                                     self.rec_br.value())
        p.stream_url, p.stream_key, p.stream_bitrate = (self.url.text().strip(), self.key.text().strip(),
                                                        self.st_br.value())
        return canvas_changed


# ══════════════════════════════════════════════════════════════════════════════
# Расписание
# ══════════════════════════════════════════════════════════════════════════════
class ScheduleDialog(QDialog):
    """Пункт расписания: когда (время, дни) → что (видео или сцена) → что после видео."""

    def __init__(self, item, project: Project, make_video_scene: Callable[[str], str], parent=None):
        super().__init__(parent)
        from PySide6.QtCore import QDate, QTime
        from PySide6.QtWidgets import QDateEdit, QRadioButton, QTimeEdit
        from schedule import AFTER_ACTIONS, WEEKDAYS
        self.setWindowTitle("Пункт расписания")
        self.item = copy.deepcopy(item)
        self.project = project
        self.make_video_scene = make_video_scene
        lay = QVBoxLayout(self)

        when = QGroupBox("Когда")
        wf = QFormLayout(when)
        self.time = QTimeEdit()
        self.time.setDisplayFormat("HH:mm:ss")
        t = self.item.clock()
        self.time.setTime(QTime(t.hour, t.minute, t.second))
        self.time.setStyleSheet("font-size:22px;font-weight:800")
        wf.addRow("Время:", self.time)
        self.r_daily = QRadioButton("Каждый день")
        self.r_week = QRadioButton("По дням недели:")
        self.r_date = QRadioButton("Один раз, дата:")
        days = QHBoxLayout()
        self.day_cbs = []
        for i, name in enumerate(WEEKDAYS):
            cb = QCheckBox(name)
            cb.setChecked(i in self.item.weekdays)
            days.addWidget(cb)
            self.day_cbs.append(cb)
        days.addStretch(1)
        self.date = QDateEdit()
        self.date.setCalendarPopup(True)
        self.date.setDisplayFormat("dd.MM.yyyy")
        qd = QDate.fromString(self.item.date, "yyyy-MM-dd")
        self.date.setDate(qd if qd.isValid() else QDate.currentDate())
        wf.addRow(self.r_daily)
        wf.addRow(self.r_week, days)
        wf.addRow(self.r_date, self.date)
        {"weekdays": self.r_week, "date": self.r_date}.get(self.item.mode, self.r_daily).setChecked(True)
        lay.addWidget(when)

        what = QGroupBox("Что показать")
        qf = QFormLayout(what)
        row = QHBoxLayout()
        self.scene = QComboBox()
        for sc in project.scenes:
            self.scene.addItem(sc.name, sc.id)
        self.scene.setCurrentIndex(max(0, self.scene.findData(self.item.scene_id)))
        row.addWidget(self.scene, 1)
        vid = QPushButton(" Видеофайл…")
        set_icon(vid, "video", 15)
        vid.setToolTip("Выбрать видео: создаётся сцена «видео на весь экран» и ставится сюда")
        vid.clicked.connect(self._pick_video)
        row.addWidget(vid)
        qf.addRow("Сцена:", row)
        self.restart = QCheckBox("Видео в сцене — с начала")
        self.restart.setChecked(self.item.restart)
        qf.addRow(self.restart)
        self.after = QComboBox()
        for key, text in AFTER_ACTIONS.items():
            self.after.addItem(text, key)
        self.after.setCurrentIndex(max(0, self.after.findData(self.item.after)))
        self.after_scene = QComboBox()
        for sc in project.scenes:
            self.after_scene.addItem(sc.name, sc.id)
        self.after_scene.setCurrentIndex(max(0, self.after_scene.findData(self.item.after_scene)))
        self.after.currentIndexChanged.connect(lambda _i: self.after_scene.setEnabled(
            self.after.currentData() == "scene"))
        self.after_scene.setEnabled(self.item.after == "scene")
        qf.addRow("Когда видео закончится:", self.after)
        qf.addRow("", self.after_scene)
        self.enabled = QCheckBox("Пункт включён")
        self.enabled.setChecked(self.item.enabled)
        qf.addRow(self.enabled)
        lay.addWidget(what)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self._ok)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        fit_to_screen(self, 560, 470)

    def _pick_video(self) -> None:
        sid = self.make_video_scene("")
        if sid:
            sc = self.project.scene(sid)
            self.scene.addItem(sc.name, sid)
            self.after_scene.addItem(sc.name, sid)
            self.scene.setCurrentIndex(self.scene.count() - 1)

    def _ok(self) -> None:
        if not self.scene.currentData():
            QMessageBox.information(self, "Расписание", "Выберите сцену или видеофайл.")
            return
        if self.r_week.isChecked() and not any(cb.isChecked() for cb in self.day_cbs):
            QMessageBox.information(self, "Расписание", "Отметьте хотя бы один день недели.")
            return
        self.accept()

    def result_item(self):
        it = self.item
        old = (it.time, it.mode, it.date, list(it.weekdays))
        it.time = self.time.time().toString("HH:mm:ss")
        it.mode = "weekdays" if self.r_week.isChecked() else ("date" if self.r_date.isChecked() else "daily")
        it.weekdays = [i for i, cb in enumerate(self.day_cbs) if cb.isChecked()]
        it.date = self.date.date().toString("yyyy-MM-dd")
        it.scene_id = str(self.scene.currentData() or "")
        it.name = self.scene.currentText()
        it.restart = self.restart.isChecked()
        it.after = str(self.after.currentData())
        it.after_scene = str(self.after_scene.currentData() or "")
        it.enabled = self.enabled.isChecked()
        if (it.time, it.mode, it.date, list(it.weekdays)) != old:
            it.fired = ""                 # время поменяли — сегодня может сработать снова
        return it
