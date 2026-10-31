"""gui_song_editor.py — Редактор песни: обрезка по волне, темп, тон, фейды, экспорт.

Все правки неразрушающие: исходные WAV не меняются. Параметры сохраняются
в конфиге песни (Song.edit), а при загрузке песни дорожки пересчитываются
(audio_edit.render_track) с кэшем на диске. «Экспорт WAV» записывает
обработанную версию в новую папку — её можно добавить в плейлист как песню.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Callable, Optional

import numpy as np
from PySide6.QtCore import QObject, QRunnable, QRectF, Qt, QThreadPool, Signal
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog, QFormLayout, QSpinBox,
                               QGroupBox, QHBoxLayout, QHeaderView, QLabel, QMessageBox,
                               QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)

from audio_edit import STRETCH_AVAILABLE, compute_overview, detect_silence, export_song
from gui_icons import set_icon
from gui_layout import fit_to_screen
from key_detect import ALL_KEYS, detect_song_key, transpose_key
from model import Song, SongEdit, is_click_like


def fmt_precise(sec: float) -> str:
    sec = max(0.0, sec)
    return f"{int(sec // 60):02d}:{sec % 60:05.2f}"


class _Signals(QObject):
    overview = Signal(object, float)  # пики, длительность
    key = Signal(str, float)          # тональность, уверенность
    silence = Signal(float, float, float)  # тишина в начале, в конце, длительность
    progress = Signal(str)
    exported = Signal(str, str)       # папка, текст ошибки


class _OverviewTask(QRunnable):
    def __init__(self, song: Song, signals: _Signals):
        super().__init__()
        self.song, self.signals = song, signals

    def run(self) -> None:
        try:
            peaks, dur = compute_overview(self.song)
        except Exception:
            peaks, dur = np.zeros(10, np.float32), 0.0
        self.signals.overview.emit(peaks, dur)


class _SilenceTask(QRunnable):
    def __init__(self, song: Song, signals):
        super().__init__()
        self.song, self.signals = song, signals

    def run(self) -> None:
        try:
            lead, trail, dur = detect_silence([t.file for t in self.song.tracks])
        except Exception:
            lead, trail, dur = 0.0, 0.0, 0.0
        self.signals.silence.emit(lead, trail, dur)


class _KeyTask(QRunnable):
    def __init__(self, song: Song, signals: _Signals):
        super().__init__()
        self.song, self.signals = song, signals

    def run(self) -> None:
        try:
            key, conf = detect_song_key(self.song)
        except Exception:
            key, conf = "?", 0.0
        self.signals.key.emit(key, conf)


class _ExportTask(QRunnable):
    def __init__(self, song, edit, transposes, samplerate, folder, signals):
        super().__init__()
        self.args = (song, edit, transposes, samplerate, folder)
        self.signals = signals

    def run(self) -> None:
        song, edit, transposes, sr, folder = self.args
        try:
            out = export_song(song, edit, transposes, sr, folder, self.signals.progress.emit)
            self.signals.exported.emit(out, "")
        except Exception as exc:
            self.signals.exported.emit("", str(exc))


# ──────────────────────────────────────────────────────────────────────────────
# Волна с маркерами начала/конца
# ──────────────────────────────────────────────────────────────────────────────
class WaveformView(QWidget):
    """Тяните зелёный маркер — начало, красный — конец. Клик ставит ближайший маркер."""
    markers_changed = Signal(float, float)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setMinimumHeight(110)
        self.setMouseTracking(True)
        self.peaks: Optional[np.ndarray] = None
        self.duration = 0.0
        self.start = 0.0
        self.end = 0.0
        self._drag: Optional[str] = None

    def set_data(self, peaks: np.ndarray, duration: float) -> None:
        self.peaks, self.duration = peaks, duration
        if self.end <= 0 or self.end > duration:
            self.end = duration
        self.update()

    def set_markers(self, start: float, end: float) -> None:
        self.start, self.end = start, end
        self.update()

    def _x(self, t: float) -> float:
        return t / self.duration * self.width() if self.duration else 0.0

    def _t(self, x: float) -> float:
        return min(max(x / max(1, self.width()) * self.duration, 0.0), self.duration)

    def mousePressEvent(self, e) -> None:
        if not self.duration:
            return
        x = e.position().x()
        self._drag = "start" if abs(x - self._x(self.start)) <= abs(x - self._x(self.end)) else "end"
        self._move(x)

    def mouseMoveEvent(self, e) -> None:
        x = e.position().x()
        if self._drag:
            self._move(x)
        elif self.duration:
            near = min(abs(x - self._x(self.start)), abs(x - self._x(self.end))) < 8
            self.setCursor(Qt.SizeHorCursor if near else Qt.ArrowCursor)

    def mouseReleaseEvent(self, _e) -> None:
        self._drag = None

    def _move(self, x: float) -> None:
        t = self._t(x)
        if self._drag == "start":
            self.start = min(t, max(0.0, self.end - 0.5))
        else:
            self.end = max(t, self.start + 0.5)
        self.update()
        self.markers_changed.emit(self.start, self.end)

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        w, h = self.width(), self.height()
        p.fillRect(self.rect(), QColor("#0b0d10"))
        if self.peaks is None:
            p.setPen(QColor("#7f8b97"))
            p.drawText(self.rect(), Qt.AlignCenter, "Загрузка волны…")
            return
        mid = h / 2
        n = len(self.peaks)
        p.setPen(QPen(QColor("#36d399"), 1))
        if n:
            cols = self.peaks[(np.arange(w) * n // max(1, w)).clip(0, n - 1)]
            for x in range(w):
                a = float(cols[x]) * mid * 0.92
                p.drawLine(x, int(mid - a), x, int(mid + a))
        # затемняем то, что отрезается
        xs, xe = self._x(self.start), self._x(self.end)
        shade = QColor(0, 0, 0, 170)
        p.fillRect(QRectF(0, 0, xs, h), shade)
        p.fillRect(QRectF(xe, 0, w - xe, h), shade)
        # маркеры
        for x, color, label, align in ((xs, "#36d399", fmt_precise(self.start), Qt.AlignLeft),
                                       (xe, "#ef4b55", fmt_precise(self.end), Qt.AlignRight)):
            p.setPen(QPen(QColor(color), 2))
            p.drawLine(int(x), 0, int(x), h)
            p.fillRect(QRectF(x - 6, 0, 12, 12), QColor(color))
            box = QRectF(x + 8, 14, 110, 20) if align == Qt.AlignLeft else QRectF(x - 118, 14, 110, 20)
            p.setPen(QColor("#e6eaee"))
            p.drawText(box, align | Qt.AlignVCenter, label)
        p.end()


# ──────────────────────────────────────────────────────────────────────────────
# Диалог редактора
# ──────────────────────────────────────────────────────────────────────────────
class SongEditorDialog(QDialog):
    def __init__(self, song: Song, samplerate: int, on_exported: Callable[[str], None], parent=None):
        super().__init__(parent)
        self.song = song
        self.samplerate = samplerate
        self.on_exported = on_exported
        self.edit = deepcopy(song.edit)
        self.setWindowTitle(f"Редактор трека — {song.name}")
        fit_to_screen(self, 980, 760)

        self.signals = _Signals()
        self.signals.overview.connect(self._on_overview)
        self.signals.progress.connect(lambda t: self.status.setText(t))
        self.signals.exported.connect(self._on_exported)
        self.signals.key.connect(self._on_key_detected)
        self.signals.silence.connect(self._on_silence)
        self.key_manual = song.key_manual

        lay = QVBoxLayout(self)
        self.wave = WaveformView()
        self.wave.markers_changed.connect(self._on_markers)
        lay.addWidget(self.wave)

        # ── обрезка ─────────────────────────────────────────────────────────
        trim = QGroupBox("Обрезка и фейды")
        tl = QHBoxLayout(trim)
        self.start_spin = self._spin(0, 36000, 0.01, " с")
        self.end_spin = self._spin(0, 36000, 0.01, " с")
        self.fade_in = self._spin(0, 20000, 10, " мс", decimals=0)
        self.fade_out = self._spin(0, 20000, 10, " мс", decimals=0)
        reset_trim = QPushButton("Без обрезки")
        reset_trim.clicked.connect(self._reset_trim)
        for label, wdg in (("Начало:", self.start_spin), ("Конец:", self.end_spin),
                           ("Fade in:", self.fade_in), ("Fade out:", self.fade_out)):
            tl.addWidget(QLabel(label))
            tl.addWidget(wdg)
        tl.addWidget(reset_trim)
        self.silence_btn = QPushButton(" Тишина")
        set_icon(self.silence_btn, "scissors")
        self.silence_btn.setToolTip("Поставить начало и конец по найденной тишине (дальше можно "
                                    "подвинуть маркеры мышью). Порог: −54 dBFS в начале, −60 dBFS в конце")
        self.silence_btn.clicked.connect(self._find_silence)
        tl.addWidget(self.silence_btn)
        lay.addWidget(trim)

        # ── темп и тон ──────────────────────────────────────────────────────
        tp = QGroupBox("Темп и тональность (меняются независимо друг от друга)")
        form = QFormLayout(tp)
        self.speed_spin = self._spin(50, 200, 1, " %", decimals=1)
        self.bpm_spin = self._spin(0, 400, 1, " BPM", decimals=1)
        self.bpm_spin.setSpecialValueText("не указан")
        self.bpm_label = QLabel()
        bpm_row = QHBoxLayout()
        bpm_row.addWidget(self.speed_spin)
        bpm_row.addWidget(QLabel("   исходный темп:"))
        bpm_row.addWidget(self.bpm_spin)
        bpm_row.addWidget(self.bpm_label)
        bpm_row.addStretch(1)
        form.addRow("Скорость:", bpm_row)
        self.pitch_spin = self._spin(-12, 12, 0.5, " пт", decimals=1)
        pitch_row = QHBoxLayout()
        pitch_row.addWidget(self.pitch_spin)
        pitch_row.addWidget(QLabel("   (+1 = на полтона выше, +2 = на тон)"))
        pitch_row.addStretch(1)
        form.addRow("Сдвиг тона:", pitch_row)

        key_row = QHBoxLayout()
        self.key_combo = QComboBox()
        self.key_combo.addItem("не определена", "")
        for k in ALL_KEYS:
            self.key_combo.addItem(k, k)
        self.key_combo.setCurrentIndex(max(0, self.key_combo.findData(song.key)))
        self.key_combo.activated.connect(self._on_key_manual)
        self.detect_btn = QPushButton("Определить")
        self.detect_btn.setToolTip("Анализ гармонии трека (без клика и гайда). Точность ~70–85 %")
        self.detect_btn.clicked.connect(self._detect_key)
        self.key_result = QLabel()
        self.key_result.setStyleSheet("color:#f7c948;font-weight:700")
        key_row.addWidget(self.key_combo)
        key_row.addWidget(self.detect_btn)
        key_row.addWidget(self.key_result)
        key_row.addStretch(1)
        form.addRow("Тональность исходника:", key_row)
        if not STRETCH_AVAILABLE:
            warn = QLabel("⚠ Нужен pedalboard 0.9.12+: pip install -U pedalboard")
            warn.setStyleSheet("color:#f7c948")
            form.addRow(warn)
            self.speed_spin.setEnabled(False)
            self.pitch_spin.setEnabled(False)
        lay.addWidget(tp)

        # ── что делать, когда песня доиграла ────────────────────────────────
        endg = QGroupBox("Окончание трека")
        el = QHBoxLayout(endg)
        self.end_combo = QComboBox()
        self.end_combo.addItem("Как в настройках плейлиста", "default")
        self.end_combo.addItem("Стоп — встать на начало следующей и ждать PLAY", "stop")
        self.end_combo.addItem("Играть следующий трек", "next")
        self.end_combo.setCurrentIndex(max(0, self.end_combo.findData(song.end_action)))
        self.gap_spin = self._spin(0, 60, 0.5, " с пауза", decimals=1)
        self.gap_spin.setValue(song.end_gap)
        self.end_combo.currentIndexChanged.connect(
            lambda: self.gap_spin.setEnabled(self.end_combo.currentData() == "next"))
        self.gap_spin.setEnabled(song.end_action == "next")
        el.addWidget(self.end_combo, 1)
        el.addWidget(self.gap_spin)
        lay.addWidget(endg)

        # ── пульт X32 и громкость ───────────────────────────────────────────
        xg = QGroupBox("Пульт X32 и громкость")
        xl = QHBoxLayout(xg)
        self.scene_spin = QSpinBox()
        self.scene_spin.setRange(-1, 99)
        self.scene_spin.setSpecialValueText("не менять")
        self.scene_spin.setValue(song.x32_scene)
        self.snip_spin = QSpinBox()
        self.snip_spin.setRange(-1, 99)
        self.snip_spin.setSpecialValueText("не менять")
        self.snip_spin.setValue(song.x32_snippet)
        self.norm_ex = QCheckBox("не выравнивать громкость")
        self.norm_ex.setChecked(song.norm_exclude)
        if song.lufs and song.lufs > -90:
            loud = f"измерено: {song.lufs:.1f} LUFS"
        elif song.lufs == -99:
            loud = "громкость не измерена (ошибка)"
        else:
            loud = "громкость измеряется в фоне…"
        for wdg in (QLabel("Сцена:"), self.scene_spin, QLabel("Сниппет:"), self.snip_spin,
                    QLabel("   "), self.norm_ex, QLabel(loud)):
            xl.addWidget(wdg)
        xl.addStretch(1)
        lay.addWidget(xg)

        # ── дорожки: кого транспонировать ───────────────────────────────────
        self.table = QTableWidget(len(song.tracks), 3)
        self.table.setHorizontalHeaderLabels(["Дорожка", "Выход", "Менять тон"])
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.tr_checks: list[QCheckBox] = []
        for i, t in enumerate(song.tracks):
            self.table.setItem(i, 0, QTableWidgetItem(t.name))
            self.table.setItem(i, 1, QTableWidgetItem(f"Out {t.output}"))
            cb = QCheckBox()
            cb.setChecked(t.transposes)
            cb.setToolTip("Клик и гайд обычно не транспонируют")
            holder = QWidget()
            hl = QHBoxLayout(holder)
            hl.setContentsMargins(0, 0, 0, 0)
            hl.setAlignment(Qt.AlignCenter)
            hl.addWidget(cb)
            self.table.setCellWidget(i, 2, holder)
            self.tr_checks.append(cb)
        lay.addWidget(self.table, 1)

        # ── низ ─────────────────────────────────────────────────────────────
        self.result_label = QLabel()
        self.result_label.setStyleSheet("font-size:16px;font-weight:700")
        self.status = QLabel("")
        self.status.setStyleSheet("color:#f7c948")
        lay.addWidget(self.result_label)
        lay.addWidget(self.status)

        btns = QHBoxLayout()
        reset_all = QPushButton("Сбросить всё")
        reset_all.clicked.connect(self._reset_all)
        self.export_btn = QPushButton("Экспорт WAV…")
        self.export_btn.setToolTip("Записать обработанную версию в новую папку (24 бит)")
        self.export_btn.clicked.connect(self._export)
        cancel = QPushButton("Отмена")
        cancel.clicked.connect(self.reject)
        apply = QPushButton("Применить")
        apply.setDefault(True)
        apply.clicked.connect(self._apply)
        btns.addWidget(reset_all)
        btns.addWidget(self.export_btn)
        btns.addStretch(1)
        btns.addWidget(cancel)
        btns.addWidget(apply)
        lay.addLayout(btns)

        self._load_values()
        for s in (self.start_spin, self.end_spin, self.fade_in, self.fade_out,
                  self.speed_spin, self.bpm_spin, self.pitch_spin):
            s.valueChanged.connect(self._on_values)
        QThreadPool.globalInstance().start(_OverviewTask(song, self.signals))

    @staticmethod
    def _spin(lo, hi, step, suffix, decimals=2) -> QDoubleSpinBox:
        s = QDoubleSpinBox()
        s.setRange(lo, hi)
        s.setSingleStep(step)
        s.setDecimals(decimals)
        s.setSuffix(suffix)
        s.setMinimumWidth(110)
        return s

    def _source_duration(self) -> float:
        return self.wave.duration or self.song.duration

    def _load_values(self) -> None:
        e = self.edit
        dur = self._source_duration()
        self._blocked(True)
        self.start_spin.setValue(e.start)
        self.end_spin.setValue(e.end if e.end > 0 else dur)
        self.fade_in.setValue(e.fade_in)
        self.fade_out.setValue(e.fade_out)
        self.speed_spin.setValue(e.speed * 100)
        self.bpm_spin.setValue(e.original_bpm)
        self.pitch_spin.setValue(e.pitch)
        self._blocked(False)
        self.wave.set_markers(e.start, e.end if e.end > 0 else dur)
        self._update_result()

    def _blocked(self, on: bool) -> None:
        for s in (self.start_spin, self.end_spin, self.fade_in, self.fade_out,
                  self.speed_spin, self.bpm_spin, self.pitch_spin):
            s.blockSignals(on)

    def _on_overview(self, peaks, duration: float) -> None:
        if duration and not self.song.duration:
            self.song.duration = duration
        self.wave.set_data(peaks, duration)
        self.start_spin.setMaximum(duration)
        self.end_spin.setMaximum(duration)
        self._load_values()

    def _on_markers(self, start: float, end: float) -> None:
        self._blocked(True)
        self.start_spin.setValue(start)
        self.end_spin.setValue(end)
        self._blocked(False)
        self._collect()

    def _on_values(self) -> None:
        self._collect()
        self.wave.set_markers(self.edit.start, self.end_spin.value())

    def _collect(self) -> None:
        dur = self._source_duration()
        e = self.edit
        e.start = self.start_spin.value()
        end = self.end_spin.value()
        e.end = 0.0 if dur and end >= dur - 0.005 else end  # «до конца» храним как 0
        e.fade_in = self.fade_in.value()
        e.fade_out = self.fade_out.value()
        e.speed = round(self.speed_spin.value() / 100, 4)
        e.original_bpm = self.bpm_spin.value()
        e.pitch = self.pitch_spin.value()
        self._update_result()
        self._update_key_result()

    def _update_result(self) -> None:
        e = self.edit
        dur = self._source_duration()
        if e.original_bpm:
            self.bpm_label.setText(f"→ новый темп {e.original_bpm * e.speed:.1f} BPM")
        else:
            self.bpm_label.setText("")
        out = e.output_duration(dur) if dur else 0.0
        self.result_label.setText(
            f"Итоговая длина: {fmt_precise(out)}   (исходник {fmt_precise(dur)})"
            + (f"   •   {e.summary()}" if e.summary() else ""))

    def _update_key_result(self) -> None:
        key = self.key_combo.currentData()
        if key and self.edit.pitch:
            self.key_result.setText(f"→ после сдвига: {transpose_key(key, self.edit.pitch)}")
        else:
            self.key_result.setText("")

    def _on_key_manual(self) -> None:
        self.key_manual = True
        self._update_key_result()

    def _detect_key(self) -> None:
        self.detect_btn.setEnabled(False)
        self.key_result.setText("анализ…")
        QThreadPool.globalInstance().start(_KeyTask(self.song, self.signals))

    def _on_key_detected(self, key: str, conf: float) -> None:
        self.detect_btn.setEnabled(True)
        if key == "?":
            self.key_result.setText("не удалось определить")
            return
        self.key_combo.setCurrentIndex(max(0, self.key_combo.findData(key)))
        self.key_manual = False
        self._update_key_result()
        if not self.edit.pitch:
            self.key_result.setText(f"уверенность {conf * 100:.0f} %")

    def _find_silence(self) -> None:
        if self.song.silence_lead >= 0:
            self._on_silence(self.song.silence_lead, self.song.silence_trail, self.song.duration)
            return
        self.silence_btn.setEnabled(False)
        self.silence_btn.setText(" ищу…")
        QThreadPool.globalInstance().start(_SilenceTask(self.song, self.signals))

    def _on_silence(self, lead: float, trail: float, duration: float) -> None:
        self.silence_btn.setEnabled(True)
        self.silence_btn.setText(" Тишина")
        dur = duration or self._source_duration()
        if not dur or (lead < 0.05 and trail < 0.05):
            self.status.setText("Тишины в начале и в конце не найдено")
            return
        self.song.silence_lead, self.song.silence_trail = lead, trail
        self._blocked(True)
        self.start_spin.setValue(lead)
        self.end_spin.setValue(dur - trail)
        if self.fade_in.value() < 10:
            self.fade_in.setValue(10)
        if trail > 0 and self.fade_out.value() < 50:
            self.fade_out.setValue(50)
        self._blocked(False)
        self._on_values()
        self.status.setText(f"Тишина: {lead:.2f} с в начале, {trail:.2f} с в конце — маркеры поставлены. "
                            "Можно подвинуть их мышью на волне.")

    def _reset_trim(self) -> None:
        self.edit.start = self.edit.end = 0.0
        self._load_values()

    def _reset_all(self) -> None:
        self.edit = SongEdit(original_bpm=self.edit.original_bpm)
        for cb, t in zip(self.tr_checks, self.song.tracks):
            cb.setChecked(not is_click_like(t.name))  # по умолчанию: клик/гайд не транспонируются
        self._load_values()

    def _transposes(self) -> dict[int, bool]:
        return {i: cb.isChecked() for i, cb in enumerate(self.tr_checks)}

    def _apply(self) -> None:
        self._collect()
        if self.edit.end and self.edit.end - self.edit.start < 0.5:
            QMessageBox.warning(self, "Обрезка", "После обрезки трек короче полсекунды.")
            return
        self.song.edit = deepcopy(self.edit)
        for t, cb in zip(self.song.tracks, self.tr_checks):
            t.transpose = cb.isChecked()
        self.song.key = self.key_combo.currentData() or ""
        self.song.key_manual = self.key_manual and bool(self.song.key)
        self.song.end_action = str(self.end_combo.currentData())
        self.song.end_gap = float(self.gap_spin.value())
        self.song.x32_scene = int(self.scene_spin.value())
        self.song.x32_snippet = int(self.snip_spin.value())
        self.song.norm_exclude = self.norm_ex.isChecked()
        self.accept()

    def _export(self) -> None:
        self._collect()
        folder = QFileDialog.getExistingDirectory(self, "Куда сохранить обработанную версию",
                                                  self.song.folder)
        if not folder:
            return
        self.export_btn.setEnabled(False)
        self.status.setText("Экспорт…")
        QThreadPool.globalInstance().start(
            _ExportTask(self.song, deepcopy(self.edit), self._transposes(),
                        self.samplerate, folder, self.signals))

    def _on_exported(self, folder: str, err: str) -> None:
        self.export_btn.setEnabled(True)
        if err:
            self.status.setText("")
            QMessageBox.critical(self, "Экспорт", err)
            return
        self.status.setText(f"Сохранено: {folder}")
        if QMessageBox.question(self, "Экспорт готов",
                                f"Файлы сохранены в:\n{folder}\n\nДобавить эту версию в плейлист "
                                "как отдельный трек?") == QMessageBox.StandardButton.Yes:
            self.on_exported(folder)
