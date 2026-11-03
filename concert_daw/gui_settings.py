"""gui_settings.py — Окно настроек: Интерфейс, Звук, Плейлист."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QGroupBox,
                               QHeaderView, QSpinBox, QTableWidget, QTableWidgetItem,
                               QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
                               QMessageBox, QPushButton, QTabWidget, QVBoxLayout, QWidget)

from audio_edit import STRETCH_AVAILABLE, cache_size_mb, clear_cache
from audio_engine import (AudioEngine, DeviceInfo, asio_available, list_input_devices,
                          list_output_devices, rescan_devices)
from gui_icons import set_icon
from gui_layout import fit_to_screen, scrollable
from gui_rec_channels import RecChannelsDialog, summary_text
from midi_ctl import ACTIONS, MIDO_AVAILABLE, MIDO_ERROR, MidiController, describe
from model import AppConfig
from x32_osc import DIVISIONS, X32Osc, fx_type_name

SCALES = (0.75, 0.9, 1.0, 1.1, 1.25, 1.5, 1.75, 2.0)
OUTPUT_MODES = (
    ("auto", "Авто: пульт X32 — по выходам, обычная карта — в стерео"),
    ("routed", "Роутинг по выходам (Out 1–32 пульта / многоканальной карты)"),
    ("stereo", "Сведение в стерео (без пульта: колонки или наушники)"),
)


@dataclass
class SettingsResult:
    device: Optional[DeviceInfo]
    samplerate: int
    blocksize: int
    output_mode: str
    ui_scale: float
    start_maximized: bool
    auto_cue: bool
    auto_play_next: bool
    auto_play_delay: float
    preload_next: bool
    confirm_delete: bool
    songs_folder: str
    switch_while_playing: str
    rec_folder: str
    rec_channels: str
    rec_names: dict
    rec_chosen: bool
    rec_to_song: bool
    video_sync_on: bool
    video_sync_port: int
    rec_format: str
    rec_split: bool
    rec_mode: str
    rec_input_device: str
    metro_output: int
    metro_volume_db: float
    metro_accent: bool
    metro_beats: int
    countin_bars: int
    norm_on: bool
    norm_target: float
    norm_max_boost: float
    osc_on: bool
    osc_ip: str
    osc_when: str
    osc_faders: bool
    reset_layout: bool
    fx_tempo: dict
    fx_auto: bool
    fx_auto_div: str
    silence_offer: bool
    midi_port: str
    midi_map: dict
    midi_cc_any: bool
    update_auto: bool = True
    title_source: str = "file"


class SettingsDialog(QDialog):
    def __init__(self, cfg: AppConfig, engine: AudioEngine, current_device_name: str, parent=None,
                 osc: X32Osc = None, midi: MidiController = None, initial_tab: str = "",
                 fx_types: dict = None, console_names=None, video_clients: list = None,
                 update_button=None):
        super().__init__(parent)
        self.cfg = cfg
        self.engine = engine
        self.osc = osc or X32Osc()
        self.midi = midi or MidiController()
        self.midi_map = dict(cfg.midi_map)
        self.fx_types = dict(fx_types or {})
        self.video_clients = list(video_clients or [])
        self.console_names = console_names           # имена каналов с пульта (для выбора входов записи)
        self.rec_spec, self.rec_names = cfg.rec_channels, dict(cfg.rec_names)
        self.rec_chosen = cfg.rec_chosen
        self._learning: str = ""
        self.setWindowTitle("Настройки")
        fit_to_screen(self, 760, 640)
        self.devices: list[DeviceInfo] = []

        lay = QVBoxLayout(self)
        self.tabs = QTabWidget()
        self.tabs.addTab(scrollable(self._build_ui_tab()), "Интерфейс")
        self.tabs.addTab(scrollable(self._build_sound_tab()), "Звук")
        self.tabs.addTab(scrollable(self._build_playlist_tab()), "Плейлист")
        self.tabs.addTab(scrollable(self._build_rec_tab()), "Запись")
        self.tabs.addTab(scrollable(self._build_osc_tab()), "Пульт X32 (сеть)")
        self.tabs.addTab(scrollable(self._build_midi_tab()), "MIDI")
        self.tabs.addTab(scrollable(self._build_video_tab()), "Видео")
        from gui_feedback import build_updates_tab
        from model import app_data_dir
        upd_tab, self.update_auto_cb = build_updates_tab(self, update_button, cfg.update_auto,
                                                         app_data_dir() / "error.log")
        self.tabs.addTab(scrollable(upd_tab), "Обновления и отзывы")
        tab_index = {"ui": 0, "sound": 1, "playlist": 2, "rec": 3, "osc": 4, "midi": 5}
        if initial_tab in tab_index:
            self.tabs.setCurrentIndex(tab_index[initial_tab])
        lay.addWidget(self.tabs)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Ok).setText("Применить")
        buttons.button(QDialogButtonBox.Cancel).setText("Отмена")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)
        self._fill_devices(current_device_name)

    # ── Интерфейс ───────────────────────────────────────────────────────────
    def _build_ui_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.scale_combo = QComboBox()
        for s in SCALES:
            self.scale_combo.addItem(f"{s * 100:.0f} %", s)
        idx = min(range(len(SCALES)), key=lambda i: abs(SCALES[i] - self.cfg.ui_scale))
        self.scale_combo.setCurrentIndex(idx)
        form.addRow("Масштаб интерфейса:", self.scale_combo)
        form.addRow("", QLabel("Крупнее — удобнее на сцене и на сенсорном экране.\n"
                               "Масштаб применяется после перезапуска программы."))
        self.maximized_cb = QCheckBox("Открывать окно на весь экран")
        self.maximized_cb.setChecked(self.cfg.start_maximized)
        form.addRow("", self.maximized_cb)
        self._reset_layout = False
        reset = QPushButton("Сбросить расположение зон окна")
        reset.setToolTip("Если зоны стали слишком маленькими или «заклинили» — вернуть размеры по умолчанию")
        self.reset_note = QLabel("")
        reset.clicked.connect(self._on_reset_layout)
        form.addRow("", reset)
        form.addRow("", self.reset_note)
        return w

    def _on_reset_layout(self) -> None:
        self._reset_layout = True
        self.reset_note.setText("Зоны вернутся к размерам по умолчанию после «Применить».")

    # ── Звук ────────────────────────────────────────────────────────────────
    def _build_sound_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        self.info = QLabel()
        self.info.setWordWrap(True)
        lay.addWidget(self.info)

        form = QFormLayout()
        self.dev_combo = QComboBox()
        self.sr_combo = QComboBox()
        for sr in (44100, 48000, 96000):
            self.sr_combo.addItem(f"{sr} Гц", sr)
        self.sr_combo.setCurrentIndex(max(0, self.sr_combo.findData(self.cfg.samplerate)))
        self.bs_combo = QComboBox()
        for bs in (64, 128, 256, 512, 1024, 2048):
            self.bs_combo.addItem(f"{bs} сэмплов", bs)
        self.bs_combo.setCurrentIndex(max(0, self.bs_combo.findData(self.cfg.blocksize)))
        self.mode_combo = QComboBox()
        for key, text in OUTPUT_MODES:
            self.mode_combo.addItem(text, key)
        self.mode_combo.setCurrentIndex(max(0, self.mode_combo.findData(self.cfg.output_mode)))
        form.addRow("Устройство:", self.dev_combo)
        form.addRow("Частота (как на пульте!):", self.sr_combo)
        form.addRow("Буфер:", self.bs_combo)
        form.addRow("Вывод:", self.mode_combo)
        lay.addLayout(form)

        rescan = QPushButton("Пересканировать устройства (пульт подключили после запуска)")
        rescan.clicked.connect(self._rescan)
        lay.addWidget(rescan)

        metro = QGroupBox("Метроном (кнопка метронома рядом с тональностью, клавиша M)")
        mf = QFormLayout(metro)
        self.metro_out = QComboBox()
        self.metro_out.addItem("как у дорожки «Клик» (иначе Out 1)", 0)
        for i in range(1, 33):
            self.metro_out.addItem(f"Out {i}", i)
        self.metro_out.setCurrentIndex(max(0, self.metro_out.findData(self.cfg.metro_output)))
        self.metro_vol = QDoubleSpinBox()
        self.metro_vol.setRange(-40, 6)
        self.metro_vol.setSuffix(" dB")
        self.metro_vol.setValue(self.cfg.metro_volume_db)
        self.metro_accent = QCheckBox("Акцент на первую долю такта")
        self.metro_accent.setChecked(self.cfg.metro_accent)
        self.metro_beats = QSpinBox()
        self.metro_beats.setRange(1, 12)
        self.metro_beats.setValue(self.cfg.metro_beats)
        self.metro_beats.setSuffix(" долей в такте")
        self.countin_bars = QSpinBox()
        self.countin_bars.setRange(1, 4)
        self.countin_bars.setValue(self.cfg.countin_bars)
        self.countin_bars.setSuffix(" такт(а)")
        mf.addRow("Выход:", self.metro_out)
        mf.addRow("Громкость:", self.metro_vol)
        mf.addRow("Размер:", self.metro_beats)
        mf.addRow(self.metro_accent)
        mf.addRow("Отсчёт перед стартом:", self.countin_bars)
        mf.addRow("", QLabel("Отсчёт включается кнопкой «1-2-3-4» рядом с метрономом (клавиша C)."))
        lay.addWidget(metro)

        cache_row = QHBoxLayout()
        self.cache_label = QLabel()
        clear_btn = QPushButton("Очистить кэш")
        clear_btn.clicked.connect(self._clear_cache)
        cache_row.addWidget(self.cache_label, 1)
        cache_row.addWidget(clear_btn)
        lay.addLayout(cache_row)
        self._update_cache_label()
        if not STRETCH_AVAILABLE:
            warn = QLabel("⚠ Смена темпа и тона недоступна: обновите pedalboard (pip install -U pedalboard).")
            warn.setWordWrap(True)
            lay.addWidget(warn)
        lay.addStretch(1)
        return w

    def _fill_devices(self, select_name: str) -> None:
        self.dev_combo.clear()
        try:
            self.devices = list_output_devices()
        except Exception as exc:
            self.devices = []
            self.info.setText(f"Ошибка PortAudio: {exc}")
            return
        sel = 0
        for i, d in enumerate(self.devices):
            self.dev_combo.addItem(("★ " if d.is_x32 else "") + d.label)
            if d.name == select_name:
                sel = i
        self.dev_combo.setCurrentIndex(sel)
        if any(d.is_x32 for d in self.devices):
            msg = "✔ ASIO-драйвер Behringer X32 найден (отмечен ★)."
        elif asio_available():
            msg = ("Пульт X32 не найден. Можно работать без него: выберите звуковую карту "
                   "компьютера — дорожки будут сведены в стерео.")
        else:
            msg = ("Пульт X32 не найден, ASIO недоступен. Работа без пульта: выберите выход "
                   "[Windows WASAPI] — дорожки будут сведены в стерео.")
        self.info.setText(msg)

    def _rescan(self) -> None:
        idx = self.dev_combo.currentIndex()
        name = self.devices[idx].name if 0 <= idx < len(self.devices) else ""
        # PortAudio можно перезапустить только при закрытом потоке
        self.engine.close()
        rescan_devices()
        self._fill_devices(name)

    def _update_cache_label(self) -> None:
        self.cache_label.setText(f"Кэш обработки темпа/тона: {cache_size_mb():.0f} МБ")

    def _clear_cache(self) -> None:
        clear_cache()
        self._update_cache_label()

    # ── Плейлист ────────────────────────────────────────────────────────────
    def _build_playlist_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        self.autocue_cb = QCheckBox("Auto-cue: доиграть трек и встать на начало следующего")
        self.autocue_cb.setChecked(self.cfg.auto_cue)
        form.addRow(self.autocue_cb)

        row = QHBoxLayout()
        self.autoplay_cb = QCheckBox("Автоматически запускать следующий трек через")
        self.autoplay_cb.setChecked(self.cfg.auto_play_next)
        self.delay_spin = QDoubleSpinBox()
        self.delay_spin.setRange(0.0, 120.0)
        self.delay_spin.setSingleStep(0.5)
        self.delay_spin.setSuffix(" с")
        self.delay_spin.setValue(self.cfg.auto_play_delay)
        row.addWidget(self.autoplay_cb)
        row.addWidget(self.delay_spin)
        row.addStretch(1)
        form.addRow(row)
        self.autocue_cb.toggled.connect(self._sync_autoplay)
        self._sync_autoplay(self.autocue_cb.isChecked())

        self.silence_cb = QCheckBox("Предлагать обрезать тишину в начале и в конце трека")
        self.silence_cb.setChecked(self.cfg.silence_offer)
        form.addRow(self.silence_cb)
        self.preload_cb = QCheckBox("Заранее загружать следующий трек в память (мгновенный переход)")
        self.preload_cb.setChecked(self.cfg.preload_next)
        form.addRow(self.preload_cb)
        self.switch_combo = QComboBox()
        self.switch_combo.addItem("сразу играть выбранный трек", "play")
        self.switch_combo.addItem("остановить и встать на её начало", "cue")
        self.switch_combo.setCurrentIndex(max(0, self.switch_combo.findData(self.cfg.switch_while_playing)))
        form.addRow("Выбор другого трека во время игры:", self.switch_combo)
        form.addRow("", QLabel("Действие в конце конкретного трека задаётся в «Редакторе трека»."))
        self.title_combo = QComboBox()
        self.title_combo.addItem("из имени файла", "file")
        self.title_combo.addItem("из тегов файла (MP3 ID3 «Название»), иначе имя файла", "tags")
        self.title_combo.setCurrentIndex(max(0, self.title_combo.findData(self.cfg.title_source)))
        self.title_combo.setToolTip("Для новых треков. Уже добавленные: правый клик по треку → "
                                    "«Название из имени файла»")
        form.addRow("Название нового трека:", self.title_combo)
        self.confirm_cb = QCheckBox("Спрашивать подтверждение при удалении треков и плейлистов")
        self.confirm_cb.setChecked(self.cfg.confirm_delete)
        form.addRow(self.confirm_cb)

        norm = QGroupBox("Выравнивание громкости треков (LUFS)")
        nf = QFormLayout(norm)
        self.norm_cb = QCheckBox("Приводить все треки к одной громкости")
        self.norm_cb.setChecked(self.cfg.norm_on)
        self.norm_target = QDoubleSpinBox()
        self.norm_target.setRange(-30, -6)
        self.norm_target.setSuffix(" LUFS")
        self.norm_target.setValue(self.cfg.norm_target)
        self.norm_boost = QDoubleSpinBox()
        self.norm_boost.setRange(0, 20)
        self.norm_boost.setSuffix(" dB")
        self.norm_boost.setValue(self.cfg.norm_max_boost)
        nf.addRow(self.norm_cb)
        nf.addRow("Целевая громкость:", self.norm_target)
        nf.addRow("Тихие треки поднимать не больше чем на:", self.norm_boost)
        nf.addRow(QLabel("Громкость измеряется в фоне по музыкальным дорожкам. Клик и гайд\n"
                         "не меняются. Трек можно исключить в «Редакторе трека»."))
        form.addRow(norm)

        folder_row = QHBoxLayout()
        self.folder_edit = QLineEdit(self.cfg.songs_folder)
        self.folder_edit.setPlaceholderText("не задана")
        browse = QPushButton("Обзор…")
        browse.clicked.connect(self._browse_folder)
        folder_row.addWidget(self.folder_edit, 1)
        folder_row.addWidget(browse)
        form.addRow("Папка с треками:", folder_row)
        return w

    # ── Запись ──────────────────────────────────────────────────────────────
    def _build_rec_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        info = QLabel("С пультом X32 записываются входы карты X-USB (на пульте: Routing → Card Out — "
                      "что отправить в компьютер: каналы, мастер, мониторы). Без пульта — вход "
                      "звуковой карты.")
        info.setWordWrap(True)
        form.addRow(info)

        folder_row = QHBoxLayout()
        self.rec_folder_edit = QLineEdit(self.cfg.rec_folder)
        self.rec_folder_edit.setPlaceholderText("Музыка / ConcertDAW Записи")
        browse = QPushButton("Обзор…")
        browse.clicked.connect(lambda: self._pick_dir(self.rec_folder_edit, "Папка для записей"))
        folder_row.addWidget(self.rec_folder_edit, 1)
        folder_row.addWidget(browse)
        form.addRow("Папка записей:", folder_row)

        ch_row = QHBoxLayout()
        self.rec_channels_label = QLabel()
        self.rec_channels_label.setWordWrap(True)
        ch_btn = QPushButton(" Выбрать каналы…")
        set_icon(ch_btn, "list", 15)
        ch_btn.clicked.connect(self._pick_rec_channels)
        ch_row.addWidget(self.rec_channels_label, 1)
        ch_row.addWidget(ch_btn)
        form.addRow("Каналы для записи:", ch_row)
        self._show_rec_channels()

        self.rec_format_combo = QComboBox()
        for key, text in (("PCM_24", "WAV 24 бит (рекомендуется)"), ("PCM_16", "WAV 16 бит"),
                          ("FLOAT", "WAV 32 бит float")):
            self.rec_format_combo.addItem(text, key)
        self.rec_format_combo.setCurrentIndex(max(0, self.rec_format_combo.findData(self.cfg.rec_format)))
        form.addRow("Формат:", self.rec_format_combo)

        self.rec_split_cb = QCheckBox("Отдельный файл на каждый канал (Ch01.wav, Ch02.wav … — сразу в DAW)")
        self.rec_split_cb.setChecked(self.cfg.rec_split)
        form.addRow(self.rec_split_cb)

        self.rec_song_cb = QCheckBox("После записи добавлять каналы в трек: фейдер с выходом на пульт, "
                                     "эквалайзер и компрессор для голоса")
        self.rec_song_cb.setToolTip("Каналы выравниваются под трек. Новый дубль того же канала заменяет "
                                    "запись на фейдере, настройки фейдера сохраняются")
        self.rec_song_cb.setChecked(self.cfg.rec_to_song)
        form.addRow(self.rec_song_cb)

        self.rec_mode_combo = QComboBox()
        self.rec_mode_combo.addItem("Вручную — кнопкой ● REC (Ctrl+R)", "manual")
        self.rec_mode_combo.addItem("Автоматически с каждым PLAY — отдельная папка на трек", "auto")
        self.rec_mode_combo.setCurrentIndex(max(0, self.rec_mode_combo.findData(self.cfg.rec_mode)))
        form.addRow("Запуск записи:", self.rec_mode_combo)

        self.rec_dev_combo = QComboBox()
        self.rec_dev_combo.addItem("По умолчанию (системный микрофон / линейный вход)", "")
        try:
            for d in list_input_devices():
                self.rec_dev_combo.addItem(f"[{d.hostapi}] {d.name} — {d.inputs} вх.", d.name)
        except Exception:
            pass
        self.rec_dev_combo.setCurrentIndex(max(0, self.rec_dev_combo.findData(self.cfg.rec_input_device)))
        form.addRow("Вход без пульта:", self.rec_dev_combo)
        return w

    # ── Пульт X32 по сети ──────────────────────────────────────────────────
    def _build_video_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        info = QLabel("Concert Video — программа для экранов сцены на другом компьютере. Она подключается "
                      "к Concert DAW по сети и запускает видео синхронно с треками: PLAY, пауза, перемотка "
                      "и стоп повторяются на экранах. Оба компьютера должны быть в одной сети "
                      "(кабель или Wi-Fi). В Concert Video нажмите «Найти Concert DAW».")
        info.setWordWrap(True)
        form.addRow(info)
        self.video_cb = QCheckBox("Разрешить подключение видео-программы (мастер-часы)")
        self.video_cb.setChecked(self.cfg.video_sync_on)
        form.addRow(self.video_cb)
        self.video_port = QSpinBox()
        self.video_port.setRange(1024, 65000)
        self.video_port.setValue(int(self.cfg.video_sync_port))
        form.addRow("Порт:", self.video_port)
        from x32_osc import local_ipv4s
        ips = ", ".join(local_ipv4s()) or "нет сетевых подключений"
        form.addRow("IP этого компьютера:", QLabel(ips))
        form.addRow("Подключено:", QLabel(", ".join(self.video_clients) or "никого"))
        note = QLabel("При первом запуске Windows может спросить разрешение для сети — нажмите «Разрешить».")
        note.setWordWrap(True)
        note.setObjectName("caption")
        form.addRow(note)
        return w

    def _build_osc_tab(self) -> QWidget:
        w = QWidget()
        form = QFormLayout(w)
        info = QLabel("Связь с пультом X32 по сети: фейдеры пульта в программе и переключение сцен/сниппетов под каждый трек "
                      "(номер задаётся в «Редакторе трека»). Компьютер и пульт — в одной сети: "
                      "кабель в разъём Remote/Ethernet пульта или через роутер. IP пульта: "
                      "Setup → Network.\n\nВнимание: сцена меняет на пульте всё, что не защищено "
                      "Scene Safes. Для смены только эффектов и мьютов удобнее сниппеты.")
        info.setWordWrap(True)
        form.addRow(info)
        ip_row = QHBoxLayout()
        self.osc_ip = QLineEdit(self.cfg.osc_ip)
        self.osc_ip.setPlaceholderText("например 192.168.1.50 — или «Найти пульт»")
        find = QPushButton(" Найти пульт")
        set_icon(find, "search")
        find.setToolTip("Автопоиск пульта X32 в сети")
        find.clicked.connect(self._osc_find)
        test = QPushButton("Проверить связь")
        test.clicked.connect(self._osc_test)
        ip_row.addWidget(self.osc_ip, 1)
        ip_row.addWidget(find)
        ip_row.addWidget(test)
        form.addRow("IP пульта:", ip_row)
        self.osc_result = QLabel("")
        form.addRow("", self.osc_result)
        self.osc_when = QComboBox()
        self.osc_when.addItem("при выборе трека (сцена готова заранее)", "cue")
        self.osc_when.addItem("при нажатии PLAY", "play")
        self.osc_when.setCurrentIndex(max(0, self.osc_when.findData(self.cfg.osc_when)))
        form.addRow("Когда переключать сцены:", self.osc_when)
        self.osc_faders_cb = QCheckBox("Фейдеры пульта в программе (синхронизация в обе стороны)")
        self.osc_faders_cb.setChecked(self.cfg.osc_faders)
        self.osc_faders_cb.setToolTip("Мастер MAIN LR и добавленные кнопкой «+» фейдеры двигаются вместе с пультом.\n"
                                      "При запуске положения считываются с пульта.")
        form.addRow(self.osc_faders_cb)

        fxg = QGroupBox("Темп эффектов пульта (FX 1–4)")
        fxf = QFormLayout(fxg)
        fx_info = QLabel("Время задержки эффекта Stereo Delay выставляется под темп каждого трека "
                         "(с учётом скорости из редактора) — при выборе трека, вместе со сценой. "
                         "Результат проверяется по значению на пульте в мс. Множители Factor L/R на "
                         "пульте продолжают работать от этого времени.")
        fx_info.setWordWrap(True)
        fxf.addRow(fx_info)
        auto_row = QHBoxLayout()
        self.fx_auto_cb = QCheckBox("Авто: все Stereo Delay на пульте (FX 1–4), доля")
        self.fx_auto_cb.setChecked(self.cfg.fx_auto)
        self.fx_auto_cb.setToolTip("При выборе трека программа сама проверит FX 1–4 и выставит время всем "
                                   "Stereo Delay. Остальные эффекты не трогаются.")
        self.fx_auto_div = QComboBox()
        for div, (label, _) in DIVISIONS.items():
            self.fx_auto_div.addItem(label, div)
        self.fx_auto_div.setCurrentIndex(max(0, self.fx_auto_div.findData(self.cfg.fx_auto_div)))
        auto_row.addWidget(self.fx_auto_cb)
        auto_row.addWidget(self.fx_auto_div)
        auto_row.addStretch(1)
        fxf.addRow(auto_row)
        fxf.addRow(QLabel("Своя доля для отдельного слота (важнее авто):"))
        self.fx_rows: dict[str, tuple[QCheckBox, QComboBox]] = {}
        for n in range(1, 5):
            key = str(n)
            cb = QCheckBox(f"FX {n}")
            cb.setChecked(key in self.cfg.fx_tempo)
            combo = QComboBox()
            for div, (label, _) in DIVISIONS.items():
                combo.addItem(label, div)
            combo.setCurrentIndex(max(0, combo.findData(self.cfg.fx_tempo.get(key, "1/4"))))
            kind = fx_type_name(self.fx_types.get(n)) if n in self.fx_types else "тип узнается при подключении"
            row = QHBoxLayout()
            row.addWidget(cb)
            row.addWidget(combo)
            row.addWidget(QLabel(f"на пульте: {kind}"), 1)
            fxf.addRow(row)
            self.fx_rows[key] = (cb, combo)
        form.addRow(fxg)
        return w

    def _osc_find(self) -> None:
        from gui_console import ConnectDialog
        dlg = ConnectDialog(self.osc, self.osc_ip.text().strip(), self)
        if dlg.exec():
            self.osc_ip.setText(dlg.ip)
            self.osc_result.setText("✔ Пульт на связи")

    def _osc_test(self) -> None:
        self.osc.configure(self.osc_ip.text())
        self.osc_result.setText("проверяю…")
        self.osc_result.repaint()
        answer = self.osc.ping()
        self.osc_result.setText(f"✔ Пульт ответил: {answer}" if answer
                                else f"✖ Нет ответа: {self.osc.last_error}")

    # ── MIDI ────────────────────────────────────────────────────────────────
    def _build_midi_tab(self) -> QWidget:
        w = QWidget()
        lay = QVBoxLayout(w)
        if not MIDO_AVAILABLE:
            lbl = QLabel("MIDI недоступно: установите библиотеки\n  pip install mido python-rtmidi\n\n"
                         f"({MIDO_ERROR})")
            lbl.setWordWrap(True)
            lay.addWidget(lbl)
            lay.addStretch(1)
            return w
        form = QFormLayout()
        self.midi_port = QComboBox()
        self.midi_port.addItem("— не использовать —", "")
        for name in MidiController.list_ports():
            self.midi_port.addItem(name, name)
        self.midi_port.setCurrentIndex(max(0, self.midi_port.findData(self.cfg.midi_port)))
        self.midi_port.currentIndexChanged.connect(
            lambda: self.midi.open(self.midi_port.currentData() or ""))
        form.addRow("MIDI-вход (педаль, контроллер):", self.midi_port)
        self.midi_cc_any = QCheckBox("CC-педаль с фиксацией: срабатывать на любое изменение значения")
        self.midi_cc_any.setChecked(self.cfg.midi_cc_any)
        form.addRow(self.midi_cc_any)
        lay.addLayout(form)

        self.midi_table = QTableWidget(len(ACTIONS), 3)
        self.midi_table.setHorizontalHeaderLabels(["Действие", "Кнопка / педаль", ""])
        self.midi_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.midi_table.verticalHeader().setVisible(False)
        self._midi_rows = list(ACTIONS)
        for row, action in enumerate(self._midi_rows):
            self.midi_table.setItem(row, 0, QTableWidgetItem(ACTIONS[action]))
            self.midi_table.setItem(row, 1, QTableWidgetItem(describe(self.midi_map.get(action, ""))))
            cell = QWidget()
            hl = QHBoxLayout(cell)
            hl.setContentsMargins(2, 0, 2, 0)
            learn = QPushButton("Обучить")
            learn.clicked.connect(lambda _=False, a=action: self._start_learn(a))
            clear = QPushButton("")
            set_icon(clear, "close", 12)
            clear.setFixedWidth(30)
            clear.clicked.connect(lambda _=False, a=action: self._set_midi(a, ""))
            hl.addWidget(learn)
            hl.addWidget(clear)
            self.midi_table.setCellWidget(row, 2, cell)
        lay.addWidget(self.midi_table, 1)
        self.learn_label = QLabel("«Обучить» → нажмите педаль или кнопку контроллера.")
        lay.addWidget(self.learn_label)
        self._learn_timer = QTimer(self)
        self._learn_timer.setInterval(30)
        self._learn_timer.timeout.connect(self._poll_learn)
        return w

    def _start_learn(self, action: str) -> None:
        if not self.midi.port_name:
            port = self.midi_port.currentData() or ""
            if not port or not self.midi.open(port):
                self.learn_label.setText("Сначала выберите MIDI-вход.")
                return
        self.midi.poll()                    # выбросить накопившееся
        self.midi.learning = True
        self._learning = action
        self.learn_label.setText(f"Жду нажатия для «{ACTIONS[action]}»…")
        self._learn_timer.start()

    def _poll_learn(self) -> None:
        self.midi.cc_any = self.midi_cc_any.isChecked()
        sigs = self.midi.poll()
        if sigs and self._learning:
            self._set_midi(self._learning, sigs[0])
            self.learn_label.setText(f"«{ACTIONS[self._learning]}» ← {describe(sigs[0])}")
            self._learning = ""
            self.midi.learning = False
            self._learn_timer.stop()

    def _set_midi(self, action: str, sig: str) -> None:
        # одна кнопка — одно действие
        for a, v in list(self.midi_map.items()):
            if sig and v == sig and a != action:
                self.midi_map.pop(a)
        if sig:
            self.midi_map[action] = sig
        else:
            self.midi_map.pop(action, None)
        for row, a in enumerate(self._midi_rows):
            self.midi_table.item(row, 1).setText(describe(self.midi_map.get(a, "")))

    def done(self, r: int) -> None:
        self.midi.learning = False
        super().done(r)

    def _pick_dir(self, edit: QLineEdit, title: str) -> None:
        d = QFileDialog.getExistingDirectory(self, title, edit.text())
        if d:
            edit.setText(d)

    def _sync_autoplay(self, on: bool) -> None:
        self.autoplay_cb.setEnabled(on)
        self.delay_spin.setEnabled(on)

    def _rec_inputs(self) -> int:
        """Сколько входов у устройства записи: X32 по ASIO — входы карты, иначе — выбранный вход."""
        if self.engine.duplex_inputs:
            return self.engine.duplex_inputs
        name = str(self.rec_dev_combo.currentData() or "")
        try:
            dev = next((d for d in list_input_devices() if d.name == name), None)
        except Exception:
            dev = None
        return dev.inputs if dev else 32

    def _show_rec_channels(self) -> None:
        self.rec_channels_label.setText(summary_text(self.rec_spec, self.rec_names))

    def _pick_rec_channels(self) -> None:
        dlg = RecChannelsDialog(self.rec_spec, self.rec_names, self._rec_inputs(),
                                self.engine.samplerate or 48000, str(self.rec_format_combo.currentData()),
                                self.console_names, self)
        if dlg.exec():
            self.rec_spec, self.rec_names, self.rec_chosen = dlg.channels_spec(), dlg.names(), True
            self._show_rec_channels()

    def _browse_folder(self) -> None:
        d = QFileDialog.getExistingDirectory(self, "Папка с треками", self.folder_edit.text())
        if d:
            self.folder_edit.setText(d)

    # ── результат ───────────────────────────────────────────────────────────
    def result_values(self) -> SettingsResult:
        idx = self.dev_combo.currentIndex()
        return SettingsResult(
            device=self.devices[idx] if 0 <= idx < len(self.devices) else None,
            samplerate=int(self.sr_combo.currentData()),
            blocksize=int(self.bs_combo.currentData()),
            output_mode=str(self.mode_combo.currentData()),
            ui_scale=float(self.scale_combo.currentData()),
            start_maximized=self.maximized_cb.isChecked(),
            auto_cue=self.autocue_cb.isChecked(),
            auto_play_next=self.autoplay_cb.isChecked() and self.autocue_cb.isChecked(),
            auto_play_delay=float(self.delay_spin.value()),
            preload_next=self.preload_cb.isChecked(),
            confirm_delete=self.confirm_cb.isChecked(),
            songs_folder=self.folder_edit.text().strip(),
            switch_while_playing=str(self.switch_combo.currentData()),
            rec_folder=self.rec_folder_edit.text().strip(),
            rec_channels=self.rec_spec or "1-32",
            rec_names=dict(self.rec_names),
            rec_chosen=self.rec_chosen,
            rec_to_song=self.rec_song_cb.isChecked(),
            video_sync_on=self.video_cb.isChecked(),
            video_sync_port=int(self.video_port.value()),
            rec_format=str(self.rec_format_combo.currentData()),
            rec_split=self.rec_split_cb.isChecked(),
            rec_mode=str(self.rec_mode_combo.currentData()),
            rec_input_device=str(self.rec_dev_combo.currentData()),
            metro_output=int(self.metro_out.currentData()),
            metro_volume_db=float(self.metro_vol.value()),
            metro_accent=self.metro_accent.isChecked(),
            metro_beats=int(self.metro_beats.value()),
            countin_bars=int(self.countin_bars.value()),
            norm_on=self.norm_cb.isChecked(),
            norm_target=float(self.norm_target.value()),
            norm_max_boost=float(self.norm_boost.value()),
            title_source=self.title_combo.currentData() or "file",
            osc_on=True,
            osc_ip=self.osc_ip.text().strip(),
            osc_when=str(self.osc_when.currentData()),
            osc_faders=self.osc_faders_cb.isChecked(),
            reset_layout=self._reset_layout,
            fx_tempo={k: str(combo.currentData()) for k, (cb, combo) in self.fx_rows.items() if cb.isChecked()},
            silence_offer=self.silence_cb.isChecked(),
            fx_auto=self.fx_auto_cb.isChecked(),
            fx_auto_div=str(self.fx_auto_div.currentData()),
            midi_port=(self.midi_port.currentData() or "") if MIDO_AVAILABLE else self.cfg.midi_port,
            midi_map=dict(self.midi_map),
            midi_cc_any=self.midi_cc_any.isChecked() if MIDO_AVAILABLE else self.cfg.midi_cc_any,
            update_auto=self.update_auto_cb.isChecked(),
        )
