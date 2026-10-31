"""
gui_console.py — Фейдеры пульта X32 в программе (двусторонняя синхронизация по сети).

* Подключение — кнопкой «подключить»: IP вручную или «Найти пульт в сети» (автопоиск).
* По умолчанию — мастер MAIN LR. Любые фейдеры (каналы 1–32, Aux In, FX Return,
  шины, матрицы, DCA, M/C, мастер) добавляются «+» и убираются «×».
  Порядок меняется перетаскиванием мышью за название фейдера.
* Двигаете фейдер на пульте → он двигается в программе (подписка /xremote).
  Двигаете в программе → двигается на пульте. Кнопка M — mute канала пульта.
* При запуске и после восстановления связи программа считывает с пульта текущие
  положения фейдеров, mute и названия каналов.
* Пока вы держите фейдер мышью, сообщения с пульта для него не применяются —
  иначе фейдер «дёргался» бы между вашим положением и запоздалым ответом пульта.
"""
from __future__ import annotations

import threading
import time
from typing import Callable, Optional

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QColor, QPainter
from PySide6.QtWidgets import (QApplication, QDialog, QDialogButtonBox, QFrame, QHBoxLayout, QLabel,
                               QLineEdit, QListWidget, QListWidgetItem, QMenu, QPushButton,
                               QScrollArea, QSlider, QVBoxLayout, QWidget)

from gui_icons import set_icon
from gui_layout import DragStripMixin, ElidedLabel, FlowLayout, ReorderHost, move_in_list
from model import AppConfig
from style import repolish
from gui_widgets import LevelMeter
from x32_osc import (ALL_KEYS, CONSOLE_GROUPS, METER_SOURCES, MASTER_KEY, ConsoleMirror, X32Osc,
                     default_name, discover, format_db, local_ipv4s, meter_channels, meter_slots,
                     paths_for)

STEPS = 1023                       # разрешение фейдера X32 — 1024 положения
ZERO_DB = round(0.75 * STEPS)      # 0 dB на шкале пульта
STRIP_W = 88
KEEPALIVE_MS = 8000                # /xremote действует 10 с — продлеваем каждые 8
LOCAL_HOLD = 0.4                   # сек: после движения мышью ответы пульта для фейдера игнорируются


class ConsoleFader(QSlider):
    """Фейдер пульта: 0..1023 как на X32; колёсико/двойной клик — 0 dB; прилипание к 0 dB."""

    def __init__(self, parent=None):
        super().__init__(Qt.Vertical, parent)
        self.setRange(0, STEPS)
        self.setPageStep(32)
        self.setMinimumHeight(80)
        self.setMaximumHeight(210)
        self.setFixedWidth(20)          # узкий фейдер
        self.setFocusPolicy(Qt.NoFocus)
        self.setToolTip("Двойной клик или колёсико мыши (нажатие) — 0 dB")
        self.sliderMoved.connect(self._snap)

    def _snap(self, pos: int) -> None:
        zone = max(8, int(6 * STEPS / max(1, self.height())))
        if pos != ZERO_DB and abs(pos - ZERO_DB) <= zone:
            self.setValue(ZERO_DB)

    def mousePressEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.MiddleButton:
            self.setValue(ZERO_DB)
            return
        super().mousePressEvent(e)

    def mouseDoubleClickEvent(self, _e) -> None:  # noqa: N802
        self.setValue(ZERO_DB)

    def paintEvent(self, e) -> None:  # noqa: N802
        super().paintEvent(e)
        p = QPainter(self)
        handle = 18   # высота ручки из стиля (QSlider::handle:vertical)
        y = int(handle / 2 + (self.height() - handle) * (STEPS - ZERO_DB) / STEPS)
        p.setPen(QColor("#f7c948"))
        p.drawLine(0, y, 4, y)
        p.drawLine(self.width() - 5, y, self.width() - 1, y)
        p.end()


class ConsoleStrip(DragStripMixin, QFrame):
    drag_group = "console"
    fader_changed = Signal(str, float)
    mute_changed = Signal(str, bool)
    remove_requested = Signal(str)

    def __init__(self, key: str, name: str, parent=None):
        super().__init__(parent)
        self.key = key
        self.last_local = 0.0
        self.setObjectName("consoleStrip")
        self.setProperty("master", key == MASTER_KEY)
        self.setFixedWidth(STRIP_W)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(4, 5, 4, 5)
        lay.setSpacing(3)

        top = QHBoxLayout()
        top.setSpacing(2)
        self.name_label = ElidedLabel(name)
        self.name_label.setObjectName("trackName")
        self.name_label.setAlignment(Qt.AlignCenter)
        top.addWidget(self.name_label, 1)
        rm = QPushButton("")
        rm.setFixedSize(18, 18)
        rm.setObjectName("iconBtn")
        set_icon(rm, "close", 10)
        rm.setFocusPolicy(Qt.NoFocus)
        rm.setToolTip("Убрать фейдер из программы (на пульте ничего не меняется).\n"
                      "Вернуть — кнопкой «+»")
        rm.clicked.connect(lambda: self.remove_requested.emit(self.key))
        top.addWidget(rm)
        lay.addLayout(top)

        self.mute_btn = QPushButton("M")
        self.mute_btn.setObjectName("muteBtn")
        self.mute_btn.setCheckable(True)
        self.mute_btn.setFocusPolicy(Qt.NoFocus)
        self.mute_btn.setToolTip("Mute на пульте")
        self.mute_btn.toggled.connect(lambda on: self.mute_changed.emit(self.key, on))
        lay.addWidget(self.mute_btn)

        row = QHBoxLayout()
        row.setSpacing(2)
        row.addStretch(1)
        self.fader = ConsoleFader()
        self.fader.valueChanged.connect(self._on_value)
        row.addWidget(self.fader)
        # индикатор уровня с пульта: у MAIN LR — два столбика (L/R); у DCA — по её каналам
        self.meter: Optional[LevelMeter] = LevelMeter(meter_channels(key))
        row.addWidget(self.meter)
        row.addStretch(1)
        lay.addLayout(row, 1)

        self.db_label = ElidedLabel("—")
        self.db_label.setObjectName("dbLabel")
        self.db_label.setAlignment(Qt.AlignCenter)
        lay.addWidget(self.db_label)
        self.set_online(False)

    def drag_key(self) -> str:
        return self.key

    # ── от пользователя ─────────────────────────────────────────────────────
    def _on_value(self, v: int) -> None:
        self.last_local = time.monotonic()
        self.db_label.setText(format_db(v / STEPS))
        self.fader_changed.emit(self.key, v / STEPS)

    # ── с пульта ────────────────────────────────────────────────────────────
    def busy(self) -> bool:
        return self.fader.isSliderDown() or time.monotonic() - self.last_local < LOCAL_HOLD

    def set_remote_fader(self, f: float) -> None:
        if self.busy():
            return
        v = round(f * STEPS)
        if v != self.fader.value():
            self.fader.blockSignals(True)
            self.fader.setValue(v)
            self.fader.blockSignals(False)
        self.db_label.setText(format_db(f))

    def set_remote_on(self, on: int) -> None:
        self.mute_btn.blockSignals(True)
        self.mute_btn.setChecked(not on)
        self.mute_btn.blockSignals(False)

    def set_name(self, name: str) -> None:
        self.name_label.setText(name)
        self.setToolTip(f"{name} — /{self.key}\nПотяните за название, чтобы переставить фейдер")

    def push_levels(self, levels: list) -> None:
        if self.meter is not None:
            self.meter.push_peaks(levels or [0.0, 0.0])

    def set_online(self, online: bool) -> None:
        self.fader.setEnabled(online)
        self.mute_btn.setEnabled(online)
        if not online:
            self.db_label.setText("—")
        self.setProperty("offline", not online)
        repolish(self)


class ClickLabel(ElidedLabel):
    clicked = Signal()

    def mousePressEvent(self, e) -> None:  # noqa: N802
        if e.button() == Qt.LeftButton:
            self.clicked.emit()
        super().mousePressEvent(e)


class ConnectDialog(QDialog):
    """Подключение к пульту: IP вручную или автопоиск в сети, проверка связи."""

    def __init__(self, osc: X32Osc, ip: str, parent=None):
        super().__init__(parent)
        self.osc = osc
        self.setWindowTitle("Подключение к пульту X32")
        self.setMinimumWidth(460)
        self._found: list[dict] = []
        self._search_done = False

        lay = QVBoxLayout(self)
        hint = QLabel("Компьютер и пульт должны быть в одной сети: кабель в разъём <b>Remote</b> "
                      "(Ethernet) на задней панели пульта — напрямую или через роутер/свитч.")
        hint.setWordWrap(True)
        lay.addWidget(hint)

        self.search_btn = QPushButton(" Найти пульт в сети")
        set_icon(self.search_btn, "search")
        self.search_btn.clicked.connect(self._search)
        lay.addWidget(self.search_btn)
        self.found_list = QListWidget()
        self.found_list.setMaximumHeight(110)
        self.found_list.itemClicked.connect(lambda it: self.ip_edit.setText(it.data(Qt.UserRole)))
        self.found_list.itemDoubleClicked.connect(lambda it: (self.ip_edit.setText(it.data(Qt.UserRole)),
                                                              self._connect()))
        self.found_list.hide()
        lay.addWidget(self.found_list)

        row = QHBoxLayout()
        row.addWidget(QLabel("IP пульта:"))
        self.ip_edit = QLineEdit(ip)
        self.ip_edit.setPlaceholderText("например 192.168.1.50 (на пульте: Setup → Network)")
        self.ip_edit.returnPressed.connect(self._connect)
        row.addWidget(self.ip_edit, 1)
        lay.addLayout(row)

        self.result = QLabel("")
        self.result.setWordWrap(True)
        lay.addWidget(self.result)

        buttons = QDialogButtonBox()
        self.connect_btn = buttons.addButton("Подключить", QDialogButtonBox.AcceptRole)
        buttons.addButton("Отмена", QDialogButtonBox.RejectRole)
        self.connect_btn.clicked.connect(self._connect)
        buttons.rejected.connect(self.reject)
        lay.addWidget(buttons)

        self._poll = QTimer(self)
        self._poll.setInterval(100)
        self._poll.timeout.connect(self._check_search)
        if not ip:
            QTimer.singleShot(200, self._search)   # IP ещё не задан — сразу ищем

    @property
    def ip(self) -> str:
        return self.ip_edit.text().strip()

    # ── автопоиск в фоне (окно не замирает) ────────────────────────────────
    def _search(self) -> None:
        self.search_btn.setEnabled(False)
        self.search_btn.setText(" Ищу пульт в сети…")
        self.result.setText("")
        self._search_done = False

        def run():
            try:
                self._found = discover(timeout=1.5)
            except Exception:
                self._found = []
            self._search_done = True

        threading.Thread(target=run, daemon=True).start()
        self._poll.start()

    def _check_search(self) -> None:
        if not self._search_done:
            return
        self._poll.stop()
        self.search_btn.setEnabled(True)
        self.search_btn.setText(" Найти пульт в сети")
        self.found_list.clear()
        if not self._found:
            nets = ", ".join(local_ipv4s()) or "нет сетевых подключений"
            self.result.setText(
                "Пульт не найден. Проверьте:\n"
                "• кабель в разъёме Remote/Ethernet пульта и в ноутбуке (или в том же роутере);\n"
                "• на пульте Setup → Network: IP в той же сети, что и компьютер "
                f"(у компьютера: {nets});\n"
                "• если ноутбук подключён по Wi-Fi к другой сети — подключите его к сети пульта.\n"
                "Можно ввести IP вручную.")
            return
        for c in self._found:
            item = QListWidgetItem(f"{c['model']}  {c['ip']}   {c['name']}  (прошивка {c['version']})")
            item.setData(Qt.UserRole, c["ip"])
            self.found_list.addItem(item)
        self.found_list.show()
        self.found_list.setCurrentRow(0)
        self.ip_edit.setText(self._found[0]["ip"])
        self.result.setText(f"Найдено пультов: {len(self._found)}. Нажмите «Подключить».")

    # ── проверка и подключение ──────────────────────────────────────────────
    def _connect(self) -> None:
        ip = self.ip
        if not ip:
            self.result.setText("Введите IP пульта или нажмите «Найти пульт в сети».")
            return
        self.result.setText("Проверяю связь…")
        QApplication.setOverrideCursor(Qt.WaitCursor)
        try:
            self.osc.configure(ip)
            answer = self.osc.ping()
        finally:
            QApplication.restoreOverrideCursor()
        if answer:
            self.accept()
            return
        self.result.setText(
            f"✖ Пульт по адресу {ip} не отвечает.\n"
            "• проверьте IP на пульте: Setup → Network;\n"
            "• компьютер и пульт должны быть в одной сети (сетевой кабель в разъём Remote);\n"
            "• брандмауэр Windows: разрешите Python («Разрешить взаимодействие с приложением»).")


class ConsolePanel(QFrame):
    """Секция «ПУЛЬТ X32» рядом с микшером."""
    changed = Signal()   # список фейдеров изменился — сохранить конфиг

    def __init__(self, cfg: AppConfig, osc: X32Osc, parent=None):
        super().__init__(parent)
        self.cfg = cfg
        self.osc = osc
        self.mirror = ConsoleMirror()
        self.strips: dict[str, ConsoleStrip] = {}
        self._dirty: dict[str, float] = {}
        self._online = False
        self._names_requested = False
        self.setObjectName("mixerPanel")

        lay = QVBoxLayout(self)
        lay.setContentsMargins(6, 4, 6, 6)
        lay.setSpacing(4)
        head = FlowLayout()
        cap = ElidedLabel("ПУЛЬТ X32")
        cap.setObjectName("caption")
        head.addWidget(cap)
        self.status = ClickLabel("нет связи")
        self.status.setObjectName("consoleStatus")
        self.status.setCursor(Qt.PointingHandCursor)
        self.status.setToolTip("Нажмите, чтобы подключить пульт")
        self.status.clicked.connect(self.open_connect)
        head.addWidget(self.status)
        self.add_btn = QPushButton("")
        set_icon(self.add_btn, "plus")
        self.add_btn.setToolTip("Добавить фейдер пульта: каналы, Aux, FX, шины, матрицы, DCA")
        self.add_btn.clicked.connect(self._show_add_menu)
        sync_btn = QPushButton("")
        set_icon(sync_btn, "refresh")
        sync_btn.setToolTip("Считать положения фейдеров с пульта")
        sync_btn.clicked.connect(self.full_sync)
        cfg_btn = QPushButton("")
        set_icon(cfg_btn, "plug")
        cfg_btn.setToolTip("Подключение к пульту: найти в сети или ввести IP")
        cfg_btn.clicked.connect(self.open_connect)
        for b in (self.add_btn, sync_btn, cfg_btn):
            b.setFocusPolicy(Qt.NoFocus)
            head.addWidget(b)
        lay.addLayout(head)

        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.NoFrame)
        host = ReorderHost("console")            # фейдеры переставляются мышью
        host.moved.connect(self._on_moved)
        self.row = QHBoxLayout(host)
        self.row.setContentsMargins(0, 0, 0, 0)
        self.row.setSpacing(6)
        self.row.addStretch(1)
        self.scroll.setWidget(host)
        lay.addWidget(self.scroll, 1)
        self.connect_big = QPushButton(" Подключить пульт X32")
        set_icon(self.connect_big, "plug", 18)
        self.connect_big.setToolTip("Найти пульт в сети или ввести его IP")
        self.connect_big.clicked.connect(self.open_connect)
        self.connect_big.setFocusPolicy(Qt.NoFocus)
        lay.addWidget(self.connect_big)

        self._flush_timer = QTimer(self)        # отправка движений фейдеров не чаще 40 раз/с
        self._flush_timer.setInterval(25)
        self._flush_timer.timeout.connect(self._flush)
        self._keepalive = QTimer(self)
        self._keepalive.setInterval(KEEPALIVE_MS)
        self._keepalive.timeout.connect(self._ping)
        self._watch = QTimer(self)               # состояние связи
        self._watch.setInterval(1000)
        self._watch.timeout.connect(self._check_link)
        self.rebuild()

    # ── включение ───────────────────────────────────────────────────────────
    @property
    def enabled(self) -> bool:
        return bool(self.cfg.osc_ip and self.cfg.osc_faders)

    def apply_config(self) -> None:
        """После загрузки программы или изменения настроек пульта."""
        self.rebuild()
        if self.enabled:
            self._keepalive.start()
            self._watch.start()
            self._flush_timer.start()
            self._ping()                         # ответ на /info → связь есть → full_sync
        else:
            self._keepalive.stop()
            self._watch.stop()
            self._flush_timer.stop()
            self._set_online(False)
        self._update_status()

    def keys(self) -> list[str]:
        return [k for k in dict.fromkeys(self.cfg.console_strips) if k in ALL_KEYS]

    def rebuild(self) -> None:
        for strip in self.strips.values():
            strip.setParent(None)
            strip.deleteLater()
        self.strips.clear()
        while self.row.count():
            item = self.row.takeAt(0)
            w = item.widget() if item is not None else None
            if w is not None:
                w.setParent(None)
                w.deleteLater()
        for key in self.keys():
            strip = ConsoleStrip(key, self.mirror.name(key))
            strip.fader_changed.connect(self._on_local_fader)
            strip.mute_changed.connect(self._on_local_mute)
            strip.remove_requested.connect(self.remove_fader)
            self.row.addWidget(strip)
            self.strips[key] = strip
            self._apply_known(key)
            strip.set_online(self._online)
        if not self.strips:
            hint = ElidedLabel("Нажмите +, чтобы добавить фейдер")
            hint.setObjectName("caption")
            self.row.addWidget(hint)
            self.strips_hint = hint
        self.row.addStretch(1)

    def _apply_known(self, key: str) -> None:
        strip = self.strips.get(key)
        if strip is None:
            return
        strip.set_name(self.mirror.name(key))
        if key in self.mirror.fader:
            strip.set_remote_fader(self.mirror.fader[key])
        if key in self.mirror.on:
            strip.set_remote_on(self.mirror.on[key])

    # ── связь ───────────────────────────────────────────────────────────────
    def _ping(self) -> None:
        if self.enabled:
            self.osc.subscribe()                 # продлеваем подписку на изменения с пульта
            self.osc.send("/info")               # ответ подтверждает, что пульт на связи
            self._subscribe_meters()

    def _subscribe_meters(self) -> None:
        """Уровни сигнала с пульта — только те блоки, что нужны добавленным фейдерам."""
        needed = {src for key in self.strips for src, _ in meter_slots(key)}
        if any(k.startswith("dca/") for k in self.strips):
            needed.add("/meters/0")              # каналы и шины, из которых складывается DCA
        for src in METER_SOURCES:
            if src in needed:
                self.osc.send("/meters", src)

    def tick_meters(self) -> None:
        """Вызывается таймером главного окна ~30 раз в секунду."""
        for key, strip in self.strips.items():
            strip.push_levels(self.mirror.levels(key) if self._online else [])

    def _check_link(self) -> None:
        online = self.enabled and self.osc.connected
        if online != self._online:
            self._set_online(online)
            if online:
                self.full_sync()                 # связь появилась — считываем всё с пульта
        self._update_status()

    def _set_online(self, online: bool) -> None:
        self._online = online
        for strip in self.strips.values():
            strip.set_online(online)
            if online:
                self._apply_known(strip.key)

    def full_sync(self) -> None:
        """Считать с пульта положения фейдеров, mute и названия (запросы без аргументов)."""
        if not self.enabled:
            return
        self.osc.subscribe()
        self._subscribe_meters()
        if any(k.startswith("dca/") for k in self.strips):
            for address in self.mirror.dca_queries():
                self.osc.send(address)
        for address in self.mirror.queries(self.strips):
            self.osc.send(address)
        if not self._names_requested:
            # названия всех каналов — чтобы в меню «＋» было видно «Ch 05 — Гитара»
            for key in ALL_KEYS:
                self.osc.send(paths_for(key)[2])
            self._names_requested = True

    def _update_status(self) -> None:
        self.connect_big.setVisible(not (self.enabled and self._online))
        if not self.cfg.osc_ip:
            text, ok = "не подключён", False
        elif not self.cfg.osc_faders:
            text, ok = "синхронизация фейдеров выключена", False
        elif self._online:
            text, ok = f"● связь  {self.cfg.osc_ip}", True
        else:
            text, ok = f"○ нет связи с {self.cfg.osc_ip}", False
        if self.status.text() != text:
            self.status.setText(text)
            self.status.setProperty("ok", ok)
            repolish(self.status)

    # ── сообщения с пульта (вызывается из главного окна по таймеру) ─────────
    def handle(self, address: str, args: list) -> None:
        hit = self.mirror.apply(address, args)
        if hit is None or hit[1] in ("meters", "dca"):
            return                               # уровни показывает tick_meters
        key, kind = hit
        strip = self.strips.get(key)
        if strip is None:
            return
        if kind == "fader":
            strip.set_remote_fader(self.mirror.fader[key])
        elif kind == "on":
            strip.set_remote_on(self.mirror.on[key])
        else:
            strip.set_name(self.mirror.name(key))

    # ── действия пользователя → пульт ──────────────────────────────────────
    def _on_local_fader(self, key: str, f: float) -> None:
        self.mirror.fader[key] = f
        self._dirty[key] = f                     # отправит таймер — не чаще раза в 25 мс

    def _flush(self) -> None:
        if not self._dirty:
            return
        dirty, self._dirty = self._dirty, {}
        for key, f in dirty.items():
            self.osc.send(paths_for(key)[0], float(f))

    def _on_local_mute(self, key: str, muted: bool) -> None:
        self.mirror.on[key] = 0 if muted else 1
        self.osc.send(paths_for(key)[1], 0 if muted else 1)

    # ── добавление / удаление фейдеров ─────────────────────────────────────
    def _show_add_menu(self) -> None:
        menu = QMenu(self)
        present = set(self.strips)
        for title, keys in CONSOLE_GROUPS:
            sub = menu.addMenu(title)
            for key in keys:
                name = self.mirror.names.get(key, "")
                label = default_name(key) + (f" — {name}" if name else "")
                act = sub.addAction(label)
                act.setCheckable(True)
                act.setChecked(key in present)
                act.setData(key)
        if not self.enabled:
            menu.addSeparator()
            menu.addAction("Пульт не подключён: фейдеры оживут после подключения").setEnabled(False)
        chosen = menu.exec(self.add_btn.mapToGlobal(self.add_btn.rect().bottomLeft()))
        if chosen is None or chosen.data() is None:
            return
        key = chosen.data()
        if key in present:
            self.remove_fader(key)
        else:
            self.add_fader(key)

    def add_fader(self, key: str) -> None:
        if key in self.cfg.console_strips:
            return
        self.cfg.console_strips.append(key)
        self.rebuild()
        if self.enabled:
            queries = self.mirror.queries([key])
            if key.startswith("dca/"):
                queries += self.mirror.dca_queries()
            for address in queries:
                self.osc.send(address)
            self._subscribe_meters()
        self.changed.emit()

    def remove_fader(self, key: str) -> None:
        if key in self.cfg.console_strips:
            self.cfg.console_strips.remove(key)
            self.rebuild()
            self.changed.emit()

    def _on_moved(self, key: str, index: int) -> None:
        order = self.keys()
        if key not in order:
            return
        new = move_in_list(order, order.index(key), index)
        if new != order:
            self.cfg.console_strips = new
            self.rebuild()
            self.changed.emit()

    def open_connect(self) -> None:
        dlg = ConnectDialog(self.osc, self.cfg.osc_ip, self)
        if dlg.exec():
            self.cfg.osc_ip = dlg.ip
            self.cfg.osc_faders = True
            self.cfg.osc_on = True
            self.apply_config()
            self.osc.last_rx = time.monotonic()   # пульт только что ответил
            self._check_link()
            self.changed.emit()
        else:
            self.osc.configure(self.cfg.osc_ip)   # отмена после пробы другого IP — вернуть прежний

    def shutdown(self) -> None:
        self._flush()
        for t in (self._flush_timer, self._keepalive, self._watch):
            t.stop()
