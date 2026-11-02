"""Вкладка «Таймлайн»: кьюшки света по времени песни.

Источник времени:
  • «Своё аудио» — играет аудиофайл песни (или просто таймер, если файла нет);
  • «Concert DAW» — время трека берётся с сервера синхронизации Concert DAW
    (тот же, что у Concert Video: Настройки → Видео → «Разрешить подключение»).
    Песня таймлайна выбирается сама: по привязке к треку DAW или по совпадению названия.

Запись: включите «● Запись», запустите песню и нажимайте сцены/чейзы/эффекты —
кьюшки встанут в текущую позицию.
"""

import os
import time

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QButtonGroup, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog,
    QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QListWidget, QListWidgetItem,
    QMessageBox, QPushButton, QRadioButton, QSlider, QTableWidget, QTableWidgetItem, QVBoxLayout,
    QWidget,
)

from core.show import CUE_ACTIONS, CUE_NEEDS_TARGET
from core.timeline import TimelineLogic, match_daw_song

try:
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer
    _MEDIA = True
except Exception:
    _MEDIA = False


def fmt(t):
    sign = "−" if t < 0 else ""
    t = abs(t)
    return f"{sign}{int(t // 60)}:{t % 60:05.2f}"


class DawSongsDialog(QDialog):
    """Выбор треков Concert DAW, для которых создать песни таймлайна."""

    def __init__(self, client, existing, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Песни из Concert DAW")
        self.resize(460, 520)
        lay = QVBoxLayout(self)
        lay.addWidget(QLabel("Отметьте треки — для каждого появится песня с привязкой к треку DAW."))
        self.list = QListWidget()
        taken = {s.get("daw_id") for s in existing.values()}
        for playlist, sg in client.all_songs():
            sid = sg.get("id", "")
            dur = float(sg.get("duration") or 0)
            it = QListWidgetItem(f"{sg.get('name', '?')}   [{playlist}, {fmt(dur)}]")
            it.setData(Qt.UserRole, sg)
            it.setFlags(it.flags() | Qt.ItemIsUserCheckable)
            done = sid in taken
            it.setCheckState(Qt.Unchecked if done else Qt.Checked)
            if done:
                it.setText(it.text() + "  — уже есть")
            self.list.addItem(it)
        lay.addWidget(self.list, 1)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def chosen(self):
        return [self.list.item(i).data(Qt.UserRole) for i in range(self.list.count())
                if self.list.item(i).checkState() == Qt.Checked]


class TimelineTab(QWidget):
    def __init__(self, show, engine, hub, client):
        super().__init__()
        self.show_, self.engine, self.hub = show, engine, hub
        self.logic = TimelineLogic(show, engine)
        self.client = client          # SyncClient — подключение к Concert DAW (общее для программы)

        self._playing = False
        self._clock_pos = 0.0
        self._clock_start = 0.0
        self._seeking = False

        self.media = None
        if _MEDIA:
            try:
                self.media = QMediaPlayer(self)
                self.audio_out = QAudioOutput(self)
                self.media.setAudioOutput(self.audio_out)
            except Exception as e:
                print(f"[timeline] аудио недоступно: {e}")
                self.media = None

        lay = QVBoxLayout(self)

        # --- песня ---
        srow = QHBoxLayout()
        srow.addWidget(QLabel("Песня:"))
        self.song_combo = QComboBox()
        self.song_combo.setMinimumWidth(220)
        self.song_combo.currentTextChanged.connect(self._song_selected)
        srow.addWidget(self.song_combo, 1)
        for text, slot in (("Добавить", self._add_song), ("Из Concert DAW…", self._add_from_daw),
                           ("Переименовать", self._rename_song), ("Удалить", self._delete_song),
                           ("Аудиофайл…", self._choose_audio)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            srow.addWidget(b)
        lay.addLayout(srow)
        self.audio_label = QLabel()
        lay.addWidget(self.audio_label)

        # --- источник времени ---
        src = QGroupBox("Источник времени")
        sl = QHBoxLayout(src)
        self.rb_internal = QRadioButton("Своё аудио / таймер")
        self.rb_daw = QRadioButton("Concert DAW")
        self.rb_internal.setChecked(True)
        grp = QButtonGroup(self)
        grp.addButton(self.rb_internal)
        grp.addButton(self.rb_daw)
        self.rb_daw.toggled.connect(self._source_changed)
        self.sync_status = QLabel()
        for w in (self.rb_internal, self.rb_daw):
            sl.addWidget(w)
        sl.addWidget(self.sync_status, 1)
        lay.addWidget(src)

        # --- транспорт ---
        trow = QHBoxLayout()
        self.b_play = QPushButton("▶ Play")
        self.b_play.clicked.connect(self.play)
        self.b_pause = QPushButton("❚❚ Пауза")
        self.b_pause.clicked.connect(self.pause)
        self.b_stop = QPushButton("■ Стоп")
        self.b_stop.clicked.connect(self.stop)
        self.pos_label = QLabel("0:00.00")
        self.pos_label.setStyleSheet("font: bold 18px monospace; padding: 0 12px;")
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 3000)
        self.slider.sliderPressed.connect(lambda: setattr(self, "_seeking", True))
        self.slider.sliderReleased.connect(self._slider_released)
        self.rec = QCheckBox("● Запись кьюшек с пульта")
        self.rec.setStyleSheet("QCheckBox:checked { color: #ff5050; font-weight: bold; }")
        for w in (self.b_play, self.b_pause, self.b_stop, self.pos_label):
            trow.addWidget(w)
        trow.addWidget(self.slider, 1)
        trow.addWidget(self.rec)
        lay.addLayout(trow)

        # --- кьюшки ---
        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Время, с", "Действие", "Цель", "Фейд, с"])
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.table.setSelectionBehavior(QTableWidget.SelectRows)
        self.table.cellChanged.connect(self._cell_changed)
        self.table.cellDoubleClicked.connect(self._row_double_clicked)
        lay.addWidget(self.table, 1)

        crow = QHBoxLayout()
        self.action_combo = QComboBox()
        for k, label in CUE_ACTIONS.items():
            self.action_combo.addItem(label, k)
        self.action_combo.currentIndexChanged.connect(self._fill_targets)
        self.target_combo = QComboBox()
        self.target_combo.setMinimumWidth(160)
        self.cue_fade = QDoubleSpinBox()
        self.cue_fade.setRange(0, 60)
        self.cue_fade.setSpecialValueText("как в сцене")
        self.cue_fade.setSuffix(" с")
        b_add = QPushButton("+ Кью в текущей позиции")
        b_add.clicked.connect(self._add_cue_here)
        b_del = QPushButton("Удалить кью")
        b_del.clicked.connect(self._delete_cues)
        for w in (QLabel("Действие:"), self.action_combo, QLabel("Цель:"), self.target_combo,
                  QLabel("Фейд:"), self.cue_fade, b_add, b_del):
            crow.addWidget(w)
        crow.addStretch()
        lay.addLayout(crow)
        lay.addWidget(QLabel("Время и фейд правятся двойным щелчком по ячейке. "
                             "Двойной щелчок по строке — перейти к этой кьюшке."))

        hub.changed.connect(self._on_changed)
        hub.action.connect(self._on_console_action)
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._tick)
        self.timer.start(20)
        self.refresh()

    # ------------------------------------------------------------------
    # песни
    # ------------------------------------------------------------------

    def _on_changed(self, kind):
        if kind in ("songs", "all"):
            self.refresh()
        elif kind in ("scenes", "chases", "effects"):
            self._fill_targets()
            self._fill_table()

    def song(self):
        return self.show_.songs.get(self.song_combo.currentText())

    def refresh(self):
        cur = self.song_combo.currentText()
        self.song_combo.blockSignals(True)
        self.song_combo.clear()
        self.song_combo.addItems(list(self.show_.songs))
        i = self.song_combo.findText(cur)
        self.song_combo.setCurrentIndex(i if i >= 0 else (0 if self.song_combo.count() else -1))
        self.song_combo.blockSignals(False)
        self._song_selected(self.song_combo.currentText())
        self._fill_targets()

    def _song_selected(self, name):
        self.stop()
        self.logic.set_song(name)
        s = self.song()
        audio = s.get("audio", "") if s else ""
        if self.media is not None:
            self.media.setSource(QUrl.fromLocalFile(audio) if audio and os.path.exists(audio) else QUrl())
        if not s:
            self.audio_label.setText("Добавьте песню")
        elif audio and os.path.exists(audio):
            self.audio_label.setText(f"Аудио: {audio}")
        elif audio:
            self.audio_label.setText(f"⚠ Файл не найден: {audio} (работает таймер)")
        else:
            self.audio_label.setText("Без аудиофайла — время идёт по таймеру или от Concert DAW")
        self._fill_table()

    def _add_song(self):
        name, ok = QInputDialog.getText(self, "Новая песня",
                                        "Название (как в плейлисте Concert DAW — для автосинхры):")
        name = name.strip()
        if not ok or not name:
            return
        if name in self.show_.songs:
            QMessageBox.warning(self, "Ошибка", "Такая песня уже есть")
            return
        self.show_.songs[name] = {"audio": "", "cues": []}
        self.refresh()
        self.song_combo.setCurrentText(name)

    def _rename_song(self):
        old = self.song_combo.currentText()
        if not old:
            return
        new, ok = QInputDialog.getText(self, "Переименовать", "Новое название:", text=old)
        new = new.strip()
        if not ok or not new or new == old or new in self.show_.songs:
            return
        self.show_.songs = {(new if k == old else k): v for k, v in self.show_.songs.items()}
        self.song_combo.blockSignals(True)
        self.refresh()
        self.song_combo.setCurrentText(new)
        self.song_combo.blockSignals(False)
        self._song_selected(new)

    def _delete_song(self):
        name = self.song_combo.currentText()
        if not name or QMessageBox.question(self, "Удалить", f"Удалить песню «{name}» и её кьюшки?") != QMessageBox.Yes:
            return
        del self.show_.songs[name]
        self.refresh()

    def _choose_audio(self):
        s = self.song()
        if not s:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Аудиофайл песни", "",
                                              "Аудио (*.wav *.mp3 *.flac *.ogg *.m4a *.aac);;Все файлы (*)")
        if path:
            s["audio"] = path
            self._song_selected(self.song_combo.currentText())

    # ------------------------------------------------------------------
    # транспорт
    # ------------------------------------------------------------------

    def _use_media(self):
        return (self.media is not None and not self.rb_daw.isChecked()
                and not self.media.source().isEmpty())

    def position(self):
        if self.rb_daw.isChecked():
            return self.client.position() if self.client.connected else 0.0
        if self._use_media():
            return self.media.position() / 1000.0
        if self._playing:
            return self._clock_pos + (time.monotonic() - self._clock_start)
        return self._clock_pos

    def is_playing(self):
        if self.rb_daw.isChecked():
            return self.client.connected and self.client.state.playing
        if self._use_media():
            return self.media.playbackState() == QMediaPlayer.PlaybackState.PlayingState
        return self._playing

    def duration(self):
        if self.rb_daw.isChecked() and self.client.state.duration > 0:
            return self.client.state.duration
        if self._use_media() and self.media.duration() > 0:
            return self.media.duration() / 1000.0
        cues = self.logic.cues()
        return max(300.0, (cues[-1]["t"] + 30) if cues else 0)

    def play(self):
        if self.rb_daw.isChecked():
            return
        self.logic.reset()
        if self._use_media():
            self.media.play()
        elif not self._playing:
            self._clock_start = time.monotonic()
            self._playing = True

    def pause(self):
        if self.rb_daw.isChecked():
            return
        if self._use_media():
            self.media.pause()
        elif self._playing:
            self._clock_pos = self.position()
            self._playing = False

    def stop(self):
        if self.media is not None:
            self.media.stop()
        self._playing = False
        self._clock_pos = 0.0
        self.logic.reset()

    def seek(self, pos):
        pos = max(0.0, pos)
        if self.rb_daw.isChecked():
            return
        if self._use_media():
            self.media.setPosition(int(pos * 1000))
        else:
            self._clock_pos = pos
            self._clock_start = time.monotonic()
        if not self.is_playing():
            self.logic.apply_state_at(pos)  # предпросмотр света в этой точке

    def _slider_released(self):
        self._seeking = False
        self.seek(self.slider.value() / 3000.0 * self.duration())

    def _tick(self):
        self.client.poll()            # очередь событий клиента не должна копиться
        if self.rb_daw.isChecked():
            self._follow_daw_song()
        pos = self.position()
        playing = self.is_playing()
        fired = self.logic.update(pos, playing)
        self.pos_label.setText(fmt(pos))
        if not self._seeking:
            self.slider.blockSignals(True)
            self.slider.setValue(int(min(1.0, pos / self.duration()) * 3000))
            self.slider.blockSignals(False)
        if fired:
            self._highlight(pos)
        if self.rb_daw.isChecked():
            self.sync_status.setText(self._daw_status())

    # ------------------------------------------------------------------
    # синхронизация с DAW
    # ------------------------------------------------------------------

    def _source_changed(self, *_):
        self.stop()
        daw = self.rb_daw.isChecked()
        if not daw:
            self.sync_status.setText("")
        elif not self.client.ip:
            self.sync_status.setText("Не подключено — Настройки → Concert DAW")
        for b in (self.b_play, self.b_pause, self.b_stop):
            b.setEnabled(not daw)
        self._daw_track = None

    def _daw_status(self):
        c = self.client
        if not c.ip:
            return "Не подключено — Настройки → Concert DAW"
        if not c.connected:
            return f"Нет связи с {c.ip}:{c.port}" + (f" ({c.error})" if c.error else "") + " — переподключаюсь…"
        st = c.state
        song = st.song_name or "—"
        mapped = match_daw_song(self.show_.songs, st.song_id, st.song_name)
        note = "" if mapped or not st.song_name else "  ⚠ нет такой песни в шоу"
        clock = "" if c.clock_ok else ", синхронизирую часы…"
        return f"● {c.server_name or c.ip}: «{song}» {'▶' if st.playing else '■'}{note}{clock}"

    def _follow_daw_song(self):
        """DAW переключила трек — переключаем песню таймлайна."""
        st = self.client.state
        key = (st.song_id, st.song_name)
        if key == getattr(self, "_daw_track", None):
            return
        self._daw_track = key
        name = match_daw_song(self.show_.songs, st.song_id, st.song_name)
        if name and name != self.song_combo.currentText():
            self.song_combo.setCurrentText(name)
        elif not name:
            self.logic.set_song(None)

    def _add_from_daw(self):
        if not self.client.connected:
            QMessageBox.information(self, "Concert DAW",
                                    "Нет подключения к Concert DAW.\nНастройки → Concert DAW → «Найти в сети».")
            return
        if not self.client.all_songs():
            QMessageBox.information(self, "Concert DAW", "Concert DAW ещё не прислал список треков.")
            return
        d = DawSongsDialog(self.client, self.show_.songs, self)
        if not d.exec():
            return
        last = None
        for sg in d.chosen():
            name = base = str(sg.get("name") or "Трек").strip()
            n = 2
            while name in self.show_.songs:
                name = f"{base} ({n})"
                n += 1
            self.show_.songs[name] = {"audio": "", "cues": [], "daw_id": sg.get("id", "")}
            last = name
        self.refresh()
        if last:
            self.song_combo.setCurrentText(last)

    def shutdown(self):
        if self.media is not None:
            self.media.stop()

    # ------------------------------------------------------------------
    # кьюшки
    # ------------------------------------------------------------------

    def _fill_targets(self, *_):
        action = self.action_combo.currentData()
        kind = CUE_NEEDS_TARGET.get(action)
        self.target_combo.clear()
        if kind:
            self.target_combo.addItems(list(getattr(self.show_, kind)))
        self.target_combo.setEnabled(bool(kind))
        self.cue_fade.setEnabled(action in ("scene", "release"))

    def _fill_table(self):
        s = self.song()
        t = self.table
        t.blockSignals(True)
        cues = s["cues"] if s else []
        t.setRowCount(len(cues))
        for row, c in enumerate(cues):
            missing = c.get("target") and c["action"] in CUE_NEEDS_TARGET and \
                c["target"] not in getattr(self.show_, CUE_NEEDS_TARGET[c["action"]])
            vals = [f"{c['t']:.2f}", CUE_ACTIONS.get(c["action"], c["action"]), c.get("target") or "",
                    "" if c.get("fade") is None else f"{c['fade']:.2f}"]
            for col, v in enumerate(vals):
                it = QTableWidgetItem(v)
                if col in (1, 2):
                    it.setFlags(it.flags() & ~Qt.ItemIsEditable)
                if missing:
                    it.setForeground(QColor(255, 90, 90))
                t.setItem(row, col, it)
        t.blockSignals(False)

    def _highlight(self, pos):
        cues = self.logic.cues()
        last = -1
        for i, c in enumerate(cues):
            if c["t"] <= pos:
                last = i
        if last >= 0:
            self.table.selectRow(last)

    def _add_cue(self, t, action, target, fade):
        s = self.song()
        if not s:
            return
        cue = {"t": round(max(0.0, t), 2), "action": action, "target": target or None,
               "fade": fade if action in ("scene", "release") and fade else None}
        s["cues"].append(cue)
        s["cues"].sort(key=lambda c: c["t"])
        self._fill_table()

    def _add_cue_here(self):
        if not self.song():
            QMessageBox.warning(self, "Кью", "Сначала добавьте песню")
            return
        action = self.action_combo.currentData()
        target = self.target_combo.currentText() if self.target_combo.isEnabled() else None
        if action in CUE_NEEDS_TARGET and not target:
            QMessageBox.warning(self, "Кью", "Нет цели: сначала создайте сцену/чейз/эффект")
            return
        self._add_cue(self.position(), action, target, self.cue_fade.value())

    def _on_console_action(self, action, target, fade):
        if self.rec.isChecked() and self.song() and self.is_playing():
            self._add_cue(self.position(), action, target, fade if action == "release" else None)

    def _delete_cues(self):
        s = self.song()
        rows = sorted({i.row() for i in self.table.selectedItems()}, reverse=True)
        if not s or not rows:
            return
        for r in rows:
            del s["cues"][r]
        self._fill_table()

    def _cell_changed(self, row, col):
        s = self.song()
        if not s or row >= len(s["cues"]):
            return
        c = s["cues"][row]
        txt = self.table.item(row, col).text().replace(",", ".").strip()
        try:
            if col == 0:
                c["t"] = max(0.0, float(txt))
            elif col == 3:
                c["fade"] = float(txt) if txt else None
        except ValueError:
            pass
        s["cues"].sort(key=lambda x: x["t"])
        self._fill_table()

    def _row_double_clicked(self, row, col):
        if col in (1, 2):
            s = self.song()
            if s and row < len(s["cues"]):
                self.seek(s["cues"][row]["t"])
