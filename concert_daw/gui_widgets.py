"""gui_widgets.py — Канальный стрип дорожки и индикатор уровня."""
from __future__ import annotations

import copy
import math
import sys

from PySide6.QtCore import QRect, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QLinearGradient, QPainter
from PySide6.QtWidgets import (QApplication, QComboBox, QFileDialog, QFrame, QHBoxLayout,
                               QInputDialog, QLabel, QMenu, QMessageBox, QPushButton, QSlider,
                               QVBoxLayout, QWidget)

from audio_engine import FADER_MIN_DB, TrackRuntime, db_to_gain
from model import MAX_OUTPUTS, PluginState
from plugin_host import PEDALBOARD_AVAILABLE, PEDALBOARD_ERROR, default_vst3_dir
import dsp
from gui_fx import CompDialog, EqDialog
from gui_layout import DragStripMixin
from style import repolish

STRIP_WIDTH = 96   # компактный стрип: больше дорожек на экране


def no_focus(*widgets: QWidget) -> None:
    """Кнопки не забирают фокус — иначе Пробел нажимал бы последнюю кликнутую кнопку
    вместо PLAY/STOP."""
    for w in widgets:
        w.setFocusPolicy(Qt.NoFocus)


class LevelMeter(QWidget):
    """Пиковый индикатор со шкалой dB. Моно-дорожка — один столбик, стерео — два (L/R).
    Цвета: зелёный → жёлтый (-12 dB) → красный (-3 dB). Сверху — лампа клипа."""

    SCALE = (0, -3, -6, -12, -18, -24, -36, -48, -60)
    BAR_W = 7
    SCALE_W = 22

    def __init__(self, channels: int = 1, parent=None):
        super().__init__(parent)
        self.channels = max(1, min(2, channels))
        self.setFixedWidth(self.SCALE_W + self.channels * (self.BAR_W + 2) + 1)
        self.setMinimumHeight(80)
        self._levels = [0.0] * self.channels
        self._drawn = None
        self._holds = [0.0] * self.channels      # пиковое удержание (тонкая черта)
        self._hold_ttl = [0] * self.channels
        self._clip = [0] * self.channels

    @staticmethod
    def _norm(peak: float) -> float:
        if peak <= 1e-5:
            return 0.0
        return min(max((20 * math.log10(peak) - FADER_MIN_DB) / -FADER_MIN_DB, 0.0), 1.0)

    def push_peaks(self, peaks) -> None:
        for i in range(self.channels):
            pk = float(peaks[i]) if i < len(peaks) else float(peaks[-1])
            norm = self._norm(pk)
            # мгновенный подъём, плавный спад
            self._levels[i] = norm if norm > self._levels[i] else max(norm, self._levels[i] - 0.025)
            if norm >= self._holds[i]:
                self._holds[i], self._hold_ttl[i] = norm, 30
            elif self._hold_ttl[i]:
                self._hold_ttl[i] -= 1
            else:
                self._holds[i] = max(0.0, self._holds[i] - 0.01)
            if pk >= 0.999:
                self._clip[i] = 45  # ~1.5 с при 30 кадрах/с
            elif self._clip[i]:
                self._clip[i] -= 1
        # перерисовка только если картинка изменилась: тишина/стоп — ноль работы для GUI
        state = (tuple(round(v, 3) for v in self._levels), tuple(round(v, 3) for v in self._holds),
                 tuple(bool(c) for c in self._clip))
        if state != self._drawn:
            self._drawn = state
            self.update()

    def push_peak(self, peak: float) -> None:  # совместимость
        self.push_peaks([peak])

    def paintEvent(self, _event) -> None:
        p = QPainter(self)
        r = self.rect()
        clip_h = 6
        top = clip_h + 3
        h = r.height() - top - 1
        # шкала dB слева
        f = p.font()
        f.setPixelSize(9)
        p.setFont(f)
        for db in self.SCALE:
            y = top + int(h * (1 - (db - FADER_MIN_DB) / -FADER_MIN_DB))
            p.setPen(QColor("#56616c"))
            p.drawLine(self.SCALE_W - 4, y, self.SCALE_W - 1, y)
            p.setPen(QColor("#7f8b97"))
            label = "-∞" if db == FADER_MIN_DB else str(db)
            p.drawText(QRect(0, y - 6, self.SCALE_W - 5, 12), Qt.AlignRight | Qt.AlignVCenter, label)
        # столбики
        for i in range(self.channels):
            x = self.SCALE_W + i * (self.BAR_W + 2)
            p.fillRect(QRect(x, r.top() + 1, self.BAR_W, clip_h),
                       QColor("#ef4b55" if self._clip[i] else "#3a1519"))
            bar = QRect(x, top, self.BAR_W, h)
            p.fillRect(bar, QColor("#0b0d10"))
            lh = int(h * self._levels[i])
            if lh > 0:
                g = QLinearGradient(0, bar.bottom(), 0, bar.top())
                g.setColorAt(0.0, QColor("#36d399"))
                g.setColorAt(0.78, QColor("#36d399"))
                g.setColorAt(0.86, QColor("#f7c948"))
                g.setColorAt(1.0, QColor("#ef4b55"))
                p.fillRect(QRect(x, bar.bottom() - lh + 1, self.BAR_W, lh), QBrush(g))
            if self._holds[i] > 0.01:
                y = bar.bottom() - int(h * self._holds[i]) + 1
                p.fillRect(QRect(x, y, self.BAR_W, 2), QColor("#e6eaee"))
        p.end()


class FaderSlider(QSlider):
    """Вертикальный фейдер в десятых долях dB.
    * двойной клик или нажатие колёсиком — сразу 0 dB;
    * при перетаскивании возле 0 dB фейдер «прилипает» к нулю (как риска на пульте)."""

    SNAP_PX = 6  # ширина зоны прилипания в пикселях по обе стороны от 0 dB

    def __init__(self, parent=None):
        super().__init__(Qt.Vertical, parent)
        self.setRange(int(FADER_MIN_DB * 10), 60)  # -60.0 … +6.0 dB
        self.setPageStep(30)
        self.setMinimumHeight(80)
        self.setMaximumHeight(210)
        self.setFixedWidth(20)          # узкий фейдер
        self.setToolTip("Двойной клик или колёсико мыши (нажатие) — 0 dB")
        self.sliderMoved.connect(self._snap)

    def _snap_zone(self) -> int:
        """Зона прилипания в единицах фейдера: не меньше 1 dB и не меньше SNAP_PX пикселей."""
        span = self.maximum() - self.minimum()
        per_px = span / max(1, self.height())
        return max(10, int(self.SNAP_PX * per_px))

    def _snap(self, pos: int) -> None:
        if pos != 0 and abs(pos) <= self._snap_zone():
            self.setValue(0)

    def mousePressEvent(self, e) -> None:
        if e.button() == Qt.MiddleButton:
            self.setValue(0)
            e.accept()
            return
        super().mousePressEvent(e)

    def mouseDoubleClickEvent(self, _event) -> None:
        self.setValue(0)

    def paintEvent(self, e) -> None:
        super().paintEvent(e)
        # риска 0 dB слева от направляющей
        p = QPainter(self)
        span = self.maximum() - self.minimum()
        h = self.height()
        handle = 18   # высота ручки из стиля (QSlider::handle:vertical)  # высота ручки из стиля
        y = int(handle / 2 + (h - handle) * (self.maximum() - 0) / span)
        p.setPen(QColor("#f7c948"))
        p.drawLine(0, y, 4, y)
        p.drawLine(self.width() - 5, y, self.width() - 1, y)
        p.end()


class TrackStrip(DragStripMixin, QFrame):
    """Стрип дорожки: имя, выход X32, инсерт VST, Mute/Solo, фейдер, индикатор."""

    drag_group = "track"
    changed = Signal()                 # любое изменение → отложенное сохранение конфига
    routing_changed = Signal(str, int)  # имя дорожки, новый выход → кэш роутинга
    remove_requested = Signal(object)   # TrackConfig — убрать дорожку из песни
    reload_requested = Signal()         # изменились параметры обработки — перезагрузить песню

    def __init__(self, rt: TrackRuntime, num_outputs: int, samplerate: int, blocksize: int,
                 stereo_mix: bool = False, parent=None):
        super().__init__(parent)
        self.rt = rt
        self.cfg = rt.cfg
        self.num_outputs = num_outputs
        self.stereo_mix = stereo_mix
        self.samplerate = samplerate
        self.blocksize = blocksize
        self._last_insert_state = None
        self.setObjectName("trackStrip")
        self.setFixedWidth(STRIP_WIDTH)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(5, 6, 5, 6)
        lay.setSpacing(4)

        # ── имя ─────────────────────────────────────────────────────────────
        self.name_label = QLabel()
        self.name_label.setObjectName("trackName")
        self.name_label.setAlignment(Qt.AlignCenter)
        fm = self.name_label.fontMetrics()
        self.name_label.setText(fm.elidedText(self.cfg.name, Qt.ElideRight, STRIP_WIDTH - 20))
        self.name_label.setToolTip(self.cfg.file)
        if rt.length == 0:
            self.name_label.setProperty("missing", True)
            self.name_label.setToolTip(f"Файл не найден или пуст:\n{self.cfg.file}")
        # Правый клик по стрипу — меню дорожки
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_track_menu)
        lay.addWidget(self.name_label)

        # ── роутинг на физический выход X32 ─────────────────────────────────
        cap = QLabel("ВЫХОД X32")
        cap.setObjectName("caption")
        lay.addWidget(cap)
        self.out_combo = QComboBox()
        ch = max(1, rt.channels)
        for first in range(1, MAX_OUTPUTS - ch + 2):
            self.out_combo.addItem(f"Out {first}" if ch == 1 else f"Out {first}–{first + ch - 1}", first)
        idx = self.out_combo.findData(self.cfg.output)
        self.out_combo.setCurrentIndex(max(0, idx))
        self.out_combo.currentIndexChanged.connect(self._on_output)
        lay.addWidget(self.out_combo)

        # ── инсерт-слот VST ─────────────────────────────────────────────────
        cap2 = QLabel("INSERT")
        cap2.setObjectName("caption")
        lay.addWidget(cap2)
        self.insert_btn = QPushButton()
        self.insert_btn.setObjectName("insertBtn")
        self.insert_btn.clicked.connect(self._show_insert_menu)
        lay.addWidget(self.insert_btn)

        # ── встроенная обработка: эквалайзер и многополосный компрессор ─────
        fxr = QHBoxLayout()
        fxr.setSpacing(3)
        self.eq_btn = QPushButton("EQ")
        self.comp_btn = QPushButton("COMP")
        for b, part, tip in ((self.eq_btn, "eq", "Параметрический эквалайзер"),
                             (self.comp_btn, "comp", "Многополосный компрессор (3 полосы)")):
            b.setObjectName("fxBtn")
            b.setMinimumWidth(28)
            b.setToolTip(f"{tip}\nКлик — открыть, правый клик — включить/выключить")
            b.setContextMenuPolicy(Qt.CustomContextMenu)
            b.customContextMenuRequested.connect(lambda _p, pt=part: self._toggle_fx(pt))
            fxr.addWidget(b)
        self.eq_btn.clicked.connect(self.open_eq)
        self.comp_btn.clicked.connect(self.open_comp)
        self._fx_dialogs: dict = {}
        lay.addLayout(fxr)

        # ── Mute / Solo ─────────────────────────────────────────────────────
        ms = QHBoxLayout()
        self.mute_btn = QPushButton("M")
        self.mute_btn.setObjectName("muteBtn")
        self.mute_btn.setCheckable(True)
        self.mute_btn.setChecked(self.cfg.mute)
        self.mute_btn.toggled.connect(self._on_mute)
        self.solo_btn = QPushButton("S")
        self.solo_btn.setObjectName("soloBtn")
        self.solo_btn.setCheckable(True)
        self.solo_btn.setChecked(self.cfg.solo)
        self.solo_btn.toggled.connect(self._on_solo)
        ms.addWidget(self.mute_btn)
        ms.addWidget(self.solo_btn)
        lay.addLayout(ms)

        # ── фейдер + индикатор ──────────────────────────────────────────────
        fr = QHBoxLayout()
        fr.addStretch(1)
        self.fader = FaderSlider()
        self.meter = LevelMeter(rt.channels)
        fr.addWidget(self.fader)
        fr.addSpacing(2)
        fr.addWidget(self.meter)
        fr.addStretch(1)
        lay.addLayout(fr, 1)

        self.db_label = QLabel()
        self.db_label.setObjectName("dbLabel")
        self.db_label.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.db_label)

        self.fader.setValue(int(round(self.cfg.gain_db * 10)))
        self.fader.valueChanged.connect(self._on_fader)
        self._update_db_label(self.cfg.gain_db)

        no_focus(self.out_combo, self.insert_btn, self.eq_btn, self.comp_btn, self.mute_btn, self.solo_btn,
                 self.fader)
        # узкий стрип: разрешаем элементам сжиматься (иначе Qt держит их «по тексту» и они вылезают)
        for w, mn in ((self.mute_btn, 26), (self.solo_btn, 26), (self.out_combo, 40), (self.insert_btn, 40)):
            w.setMinimumWidth(mn)
        self._update_route_warning()
        self._update_insert_btn()
        self._update_fx_btns()

    def drag_key(self) -> str:
        return self.cfg.file           # путь уникален в пределах трека

    # ── обработчики ─────────────────────────────────────────────────────────
    def _on_output(self, idx: int) -> None:
        first = int(self.out_combo.itemData(idx))
        self.rt.output = first        # аудиопоток подхватит со следующего блока
        self.cfg.output = first
        self._update_route_warning()
        self.routing_changed.emit(self.cfg.name, first)
        self.changed.emit()

    def set_stereo_mix(self, on: bool) -> None:
        self.stereo_mix = on
        self._update_route_warning()

    def _update_route_warning(self) -> None:
        last = self.cfg.output + max(1, self.rt.channels) - 1
        bad = (not self.stereo_mix) and bool(self.num_outputs) and last > self.num_outputs
        self.out_combo.setProperty("warn", bad)
        if self.stereo_mix:
            tip = "Режим без пульта: дорожка звучит в колонках. Выход запомнится для работы с X32"
        elif bad:
            tip = f"У устройства только {self.num_outputs} выходов — дорожка не звучит!"
        else:
            tip = "Физический выход карты X-USB (Card Out) пульта"
        self.out_combo.setToolTip(tip)
        repolish(self.out_combo)

    def _show_track_menu(self, pos) -> None:
        m = QMenu(self)
        a_rename = m.addAction("Переименовать…")
        a_tr = m.addAction("Менять тон при транспонировании")
        a_tr.setCheckable(True)
        a_tr.setChecked(self.cfg.transposes)
        m.addSeparator()
        a_remove = m.addAction("Убрать дорожку из трека")
        chosen = m.exec(self.mapToGlobal(pos))
        if chosen is a_rename:
            name, ok = QInputDialog.getText(self, "Дорожка", "Название:", text=self.cfg.name)
            if ok and name.strip():
                self.cfg.name = name.strip()
                fm = self.name_label.fontMetrics()
                self.name_label.setText(fm.elidedText(self.cfg.name, Qt.ElideRight, STRIP_WIDTH - 20))
                self.changed.emit()
        elif chosen is a_tr:
            self.cfg.transpose = a_tr.isChecked()
            self.changed.emit()
            self.reload_requested.emit()
        elif chosen is a_remove:
            self.remove_requested.emit(self.cfg)

    def _on_mute(self, on: bool) -> None:
        self.rt.mute = self.cfg.mute = on
        self.changed.emit()

    def _on_solo(self, on: bool) -> None:
        self.rt.solo = self.cfg.solo = on
        self.changed.emit()

    def _on_fader(self, value: int) -> None:
        db = value / 10.0
        self.rt.target_gain = db_to_gain(db)
        self.cfg.gain_db = db
        self._update_db_label(db)
        self.changed.emit()

    def _update_db_label(self, db: float) -> None:
        self.db_label.setText("−∞ dB" if db <= FADER_MIN_DB else f"{db:+.1f} dB")

    def refresh_meter(self) -> None:
        """Вызывается таймером GUI ~30 раз/с."""
        peaks = self.rt.peaks.copy()
        self.rt.peaks[:] = 0.0
        self.meter.push_peaks(peaks)
        slot = self.rt.slot
        state = (slot.plugin is not None, slot.bypass, slot.failed, slot.load_error)
        if state != self._last_insert_state:
            self._update_insert_btn()

    # ── встроенные EQ / компрессор ──────────────────────────────────────────
    def _set_fx_part(self, part: str, values: dict) -> None:
        """Окно EQ или компрессора изменило свою часть настроек → дорожка и конфиг."""
        full = dsp.normalize(self.cfg.fx)
        full[part] = copy.deepcopy(values)
        self.cfg.fx = full
        if self.rt.fx is not None:
            self.rt.fx.update(full)          # аудиопоток подхватит со следующего блока
        self._update_fx_btns()
        self.changed.emit()

    def _toggle_fx(self, part: str) -> None:
        full = dsp.normalize(self.cfg.fx)
        full[part]["on"] = not full[part]["on"]
        self._set_fx_part(part, full[part])
        dlg = self._fx_dialogs.get(part)
        if dlg is not None:
            dlg.set_settings(self.cfg.fx)

    def _update_fx_btns(self) -> None:
        full = dsp.normalize(self.cfg.fx)
        for b, part in ((self.eq_btn, "eq"), (self.comp_btn, "comp")):
            on = bool(full[part]["on"])
            if b.property("on") != on:
                b.setProperty("on", on)
                repolish(b)

    def _show_fx_dialog(self, part: str) -> None:
        """Окно плагина открывается ВСЕГДА (даже если обработка недоступна — тогда
        в окне видно, почему, и есть кнопка установки)."""
        dlg = self._fx_dialogs.get(part)
        try:
            if dlg is None:
                cls = EqDialog if part == "eq" else CompDialog
                dlg = cls(self.cfg.name, self.cfg.fx, self.samplerate or 48000,
                          lambda v, pt=part: self._set_fx_part(pt, v), lambda: self.rt.fx, self)
                self._fx_dialogs[part] = dlg
            else:
                dlg.set_settings(self.cfg.fx)
            dlg.setWindowState(dlg.windowState() & ~Qt.WindowMinimized)
            dlg.show()
            dlg.raise_()
            dlg.activateWindow()
        except Exception as exc:
            import traceback
            self._fx_dialogs.pop(part, None)
            log_error_text = traceback.format_exc()
            print(log_error_text, file=sys.stderr)
            QMessageBox.critical(self, "Плагин", f"Не удалось открыть окно плагина:\n{exc}\n\n"
                                 f"{log_error_text[-1500:]}")

    def open_eq(self) -> None:
        self._show_fx_dialog("eq")

    def open_comp(self) -> None:
        self._show_fx_dialog("comp")

    # ── инсерт VST ──────────────────────────────────────────────────────────
    def _update_insert_btn(self) -> None:
        slot = self.rt.slot
        self._last_insert_state = (slot.plugin is not None, slot.bypass, slot.failed, slot.load_error)
        if slot.plugin is None:
            text, state = ("⚠ VST не загружен", "error") if slot.load_error else ("— пусто —", "empty")
            tip = slot.load_error or "Нажмите, чтобы загрузить VST3-эффект"
        elif slot.failed:
            text, state, tip = f"⚠ {slot.name}", "error", "Плагин упал при обработке и выключен (байпас)"
        elif slot.bypass:
            text, state, tip = f"(byp) {slot.name}", "bypass", slot.path
        else:
            text, state, tip = slot.name, "active", slot.path
        fm = self.insert_btn.fontMetrics()
        self.insert_btn.setText(fm.elidedText(text, Qt.ElideRight, STRIP_WIDTH - 32))
        self.insert_btn.setToolTip(tip)
        self.insert_btn.setProperty("state", state)
        repolish(self.insert_btn)

    def _show_insert_menu(self) -> None:
        slot = self.rt.slot
        has = slot.plugin is not None
        m = QMenu(self)
        a_file = m.addAction("Загрузить VST3 (файл .vst3)…")
        a_bundle = m.addAction("Загрузить VST3 (папка-бандл .vst3)…")
        if not PEDALBOARD_AVAILABLE:
            a_file.setEnabled(False)
            a_bundle.setEnabled(False)
            m.addAction(f"pedalboard не установлен: {PEDALBOARD_ERROR}").setEnabled(False)
        m.addSeparator()
        a_edit = m.addAction("Открыть окно плагина")
        a_edit.setEnabled(has)
        a_byp = m.addAction("Bypass")
        a_byp.setCheckable(True)
        a_byp.setChecked(slot.bypass)
        a_byp.setEnabled(has)
        a_retry = m.addAction("Снова включить после сбоя") if slot.failed else None
        a_remove = m.addAction("Удалить плагин")
        a_remove.setEnabled(has)

        chosen = m.exec(self.insert_btn.mapToGlobal(self.insert_btn.rect().bottomLeft()))
        if chosen is None:
            return
        if chosen is a_file:
            path, _ = QFileDialog.getOpenFileName(self, "VST3-плагин", default_vst3_dir(),
                                                  "VST3 (*.vst3);;Все файлы (*)")
            if path:
                self._load_plugin(path)
        elif chosen is a_bundle:
            path = QFileDialog.getExistingDirectory(self, "Папка-бандл VST3", default_vst3_dir())
            if path:
                self._load_plugin(path)
        elif chosen is a_edit:
            if not slot.open_editor():
                QMessageBox.information(self, "VST", "Окно плагина уже открыто.")
        elif chosen is a_byp:
            slot.bypass = a_byp.isChecked()
            if self.cfg.plugin:
                self.cfg.plugin.bypass = slot.bypass
            self.changed.emit()
        elif a_retry is not None and chosen is a_retry:
            slot.reset()
            slot.failed = False
        elif chosen is a_remove:
            slot.unload()
            self.cfg.plugin = None
            self.changed.emit()
        self._update_insert_btn()

    def _load_plugin(self, path: str) -> None:
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.rt.slot.load(path, "", self.samplerate, max(1, self.rt.channels), self.blocksize)
        except Exception as exc:
            QApplication.restoreOverrideCursor()
            QMessageBox.critical(self, "VST", f"Не удалось загрузить плагин:\n{path}\n\n{exc}")
            return
        QApplication.restoreOverrideCursor()
        self.cfg.plugin = PluginState(path=path)
        self._update_insert_btn()
        self.changed.emit()
