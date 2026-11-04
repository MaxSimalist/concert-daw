"""
gui_main.py — Главное окно Concert Stage: табло для тех, кто на сцене.

Сверху — состояние (ИГРАЕТ / ПАУЗА / СТОП / ОТСЧЁТ), секундомер концерта и часы.
В центре — какой трек играет, крупно сколько осталось, текущая часть песни (маркер),
полоса прогресса и следующий трек. Справа — очередь треков плейлиста.
Снизу — каналы пульта X32: уровень сигнала, фейдер, MUTE (только показ, без управления).
Данные пульта собирает и присылает Concert DAW — табло само к пульту не подключается.

Клавиши: F11 — на весь экран / обратно, Esc — выйти из полноэкранного режима,
F2 — настройки. Правый клик — меню.
"""
from __future__ import annotations

import math
import threading
import time
from typing import Optional

from PySide6.QtCore import QEvent, QObject, Qt, QTimer
from PySide6.QtGui import QCursor, QGuiApplication, QKeySequence, QShortcut
from PySide6.QtWidgets import (QApplication, QBoxLayout, QHBoxLayout, QLabel, QMainWindow, QMenu, QPushButton, QSizePolicy,
                               QVBoxLayout, QWidget)

import sync_client
from gui_board import (ACCENT, ALERT, BG, BLUE, DIM, FAINT, TEXT, WARN, ChannelsPanel, ProgressStrip,
                       SetlistView)
from gui_update import UpdateButton
from stage_model import (RelayMirror, SettingsStore, StageSettings, app_data_dir, console_caption,
                         current_marker, find_setlist, fmt_remaining, fmt_time, next_marker, remaining_level)
from version import APP_NAME, VERSION



class _CursorHider(QObject):
    """В полноэкранном режиме прячет курсор мыши через 3 с без движения."""

    def __init__(self, win) -> None:
        super().__init__(win)
        self.win = win
        self.hidden = False
        self.last_move = time.monotonic()

    def eventFilter(self, obj, ev) -> bool:
        if ev.type() == QEvent.MouseMove:
            self.last_move = time.monotonic()
            if self.hidden:
                QApplication.restoreOverrideCursor()
                self.hidden = False
        return False

    def tick(self) -> None:
        want = self.win.isFullScreen() and time.monotonic() - self.last_move > 3.0
        if want and not self.hidden:
            QApplication.setOverrideCursor(QCursor(Qt.BlankCursor))
            self.hidden = True
        elif not want and self.hidden:
            QApplication.restoreOverrideCursor()
            self.hidden = False


def _label(obj: str = "", align=Qt.AlignLeft | Qt.AlignVCenter) -> QLabel:
    lb = QLabel("")
    if obj:
        lb.setObjectName(obj)
    lb.setAlignment(align)
    lb.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
    return lb


class MainWindow(QMainWindow):
    def __init__(self, store: Optional[SettingsStore] = None) -> None:
        super().__init__()
        self.store = store or SettingsStore()
        self.settings: StageSettings = self.store.load()
        self.setWindowTitle(f"{APP_NAME} {VERSION} — табло")
        # ── связь ───────────────────────────────────────────────────────────
        self.client = sync_client.SyncClient()
        self.mirror = RelayMirror()                 # каналы пульта — от Concert DAW
        self._console_at = 0.0
        self._console_live = False
        self._daw_found: Optional[list] = None
        self._daw_searching = False
        self._daw_last_search = 0.0
        self._daw_lost_since = 0.0
        self._blink = False
        self._last_layout_key = None
        self._build_ui()
        self.cursor_hider = _CursorHider(self)
        QApplication.instance().installEventFilter(self.cursor_hider)
        for keys, fn in (("F11", self.toggle_fullscreen), ("Esc", self.leave_fullscreen),
                         ("F2", self.open_settings), ("Ctrl+Q", self.close)):
            QShortcut(QKeySequence(keys), self, activated=fn)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.customContextMenuRequested.connect(self._menu)
        self.apply_settings()
        self.timer = QTimer(self)
        self.timer.setInterval(40)                 # 25 кадров/с — плавный остаток и индикаторы
        self.timer.timeout.connect(self._tick)
        self.timer.start()
        self.blink_timer = QTimer(self)
        self.blink_timer.setInterval(500)
        self.blink_timer.timeout.connect(self._toggle_blink)
        self.blink_timer.start()

    # ═════════════════════════════ интерфейс ═══════════════════════════════
    def _build_ui(self) -> None:
        root = QWidget()
        root.setObjectName("board")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(18, 12, 18, 12)
        outer.setSpacing(10)

        # ── верхняя строка ──────────────────────────────────────────────────
        top = QHBoxLayout()
        self.state_label = _label("statePill", Qt.AlignCenter)
        self.state_label.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Preferred)
        self.playlist_label = _label("caption")
        self.timer_caption = _label("caption", Qt.AlignRight | Qt.AlignVCenter)
        self.timer_caption.setText("КОНЦЕРТ")
        self.show_label = _label("showTimer", Qt.AlignRight | Qt.AlignVCenter)
        self.show_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        self.clock_label = _label("clock", Qt.AlignRight | Qt.AlignVCenter)
        self.clock_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        self.link_label = _label("links", Qt.AlignRight | Qt.AlignVCenter)
        self.link_label.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)
        self.gear = QPushButton("⚙")
        self.gear.setObjectName("gear")
        self.gear.setToolTip("Настройки (F2). F11 — на весь экран")
        self.gear.setFocusPolicy(Qt.NoFocus)
        self.gear.clicked.connect(self.open_settings)
        # секундомер — отдельным блоком: на горизонтальном экране он в верхней строке,
        # на вертикальном — своей крупной строкой под ней
        self.timer_w = QWidget()
        tl = QHBoxLayout(self.timer_w)
        tl.setContentsMargins(0, 0, 0, 0)
        tl.setSpacing(10)
        self.timer_layout = tl
        tl.addWidget(self.timer_caption)
        tl.addWidget(self.show_label)
        top.addWidget(self.state_label)
        top.addSpacing(14)
        top.addWidget(self.playlist_label, 1)
        top.addWidget(self.timer_w)
        top.addSpacing(24)
        top.addWidget(self.clock_label)
        top.addSpacing(16)
        top.addWidget(self.link_label)
        top.addWidget(self.gear)
        self.top_layout = top
        self.outer = outer
        outer.addLayout(top)
        self.portrait = False

        # ── середина ────────────────────────────────────────────────────────
        mid = QBoxLayout(QBoxLayout.LeftToRight)
        mid.setSpacing(24)
        self.mid_layout = mid
        left = QVBoxLayout()
        left.setSpacing(6)
        self.now_caption = _label("caption")
        self.song_label = _label("songName")
        self.song_label.setWordWrap(True)
        self.song_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.marker_label = _label("marker")
        self.progress = ProgressStrip()
        times = QBoxLayout(QBoxLayout.LeftToRight)
        self.times_layout = times
        self.elapsed_label = _label("elapsed", Qt.AlignLeft | Qt.AlignBottom)
        self.remain_label = _label("remain", Qt.AlignRight | Qt.AlignBottom)
        self.remain_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        times.addWidget(self.elapsed_label, 1)
        times.addWidget(self.remain_label, 2)
        self.next_caption = _label("caption")
        self.next_caption.setText("ДАЛЕЕ")
        self.next_label = _label("nextName")
        left.addWidget(self.now_caption)
        left.addWidget(self.song_label, 3)
        left.addWidget(self.marker_label)
        left.addSpacing(4)
        left.addWidget(self.progress)
        left.addLayout(times)
        left.addSpacing(6)
        left.addWidget(self.next_caption)
        left.addWidget(self.next_label)
        self.left_box = QWidget()
        self.left_box.setLayout(left)
        mid.addWidget(self.left_box, 3)

        right = QVBoxLayout()
        right.setSpacing(6)
        self.set_caption = _label("caption")
        self.setlist = SetlistView()
        right.addWidget(self.set_caption)
        right.addWidget(self.setlist, 1)
        self.right_box = QWidget()
        self.right_box.setLayout(right)
        mid.addWidget(self.right_box, 2)
        outer.addLayout(mid, 5)

        # ── каналы пульта ───────────────────────────────────────────────────
        self.ch_caption = _label("caption")
        self.channels = ChannelsPanel()
        self.ch_box = QWidget()
        cb = QVBoxLayout(self.ch_box)
        cb.setContentsMargins(0, 4, 0, 0)
        cb.setSpacing(4)
        cb.addWidget(self.ch_caption)
        cb.addWidget(self.channels, 1)
        outer.addWidget(self.ch_box, 2)

        self.update_btn = UpdateButton(self, lambda: "", lambda: float(self.settings.update_checked),
                                       self._set_update_checked, lambda: bool(self.settings.update_auto))
        self.update_btn.hide()               # на табло кнопки нет; проверка — в настройках

    def _set_update_checked(self, t: float) -> None:
        self.settings.update_checked = t
        self.save()

    def _styles(self, base: float, portrait: bool = False) -> str:
        song = 2.6 if portrait else 3.2
        remain = 3.8 if portrait else 4.2          # обратный отсчёт: крупно, но не во весь экран
        timer = 2.6 if portrait else 2.0
        return f"""
        QWidget#board {{ background: {BG}; }}
        QLabel {{ color: {TEXT}; }}
        QLabel#caption {{ color: {DIM}; font-size: {base * 0.85:.0f}px; font-weight: 700; letter-spacing: 1px; }}
        QLabel#songName {{ font-size: {base * song:.0f}px; font-weight: 800; }}
        QLabel#marker {{ color: {BLUE}; font-size: {base * 1.6:.0f}px; font-weight: 700; }}
        QLabel#elapsed {{ color: {DIM}; font-size: {base * 2.2:.0f}px; font-weight: 700; }}
        QLabel#remain {{ font-size: {base * remain:.0f}px; font-weight: 900; }}
        QLabel#nextName {{ color: {WARN}; font-size: {base * 2.0:.0f}px; font-weight: 800; }}
        QLabel#showTimer {{ color: {ACCENT}; font-size: {base * timer:.0f}px; font-weight: 900; }}
        QLabel#clock {{ font-size: {base * 2.0:.0f}px; font-weight: 800; }}
        QLabel#links {{ color: {DIM}; font-size: {base * 0.8:.0f}px; }}
        QLabel#statePill {{ font-size: {base * 1.3:.0f}px; font-weight: 900; border-radius: {base * 0.4:.0f}px;
                            padding: {base * 0.25:.0f}px {base * 0.8:.0f}px; }}
        QPushButton#gear {{ color: {DIM}; background: transparent; border: none; font-size: {base * 1.2:.0f}px; }}
        QPushButton#gear:hover {{ color: {TEXT}; }}
        """

    def _want_portrait(self) -> bool:
        mode = self.settings.orientation
        if mode == "portrait":
            return True
        if mode == "landscape":
            return False
        return self.height() > self.width() * 1.05          # вертикальный монитор / окно

    def _set_portrait(self, on: bool) -> None:
        """Вертикальный экран: всё друг под другом и крупнее —
        состояние и часы → секундомер → трек → ОСТАЛОСЬ (во всю ширину) → далее → очередь → каналы."""
        if on == self.portrait:
            return
        self.portrait = on
        self.mid_layout.setDirection(QBoxLayout.TopToBottom if on else QBoxLayout.LeftToRight)
        self.mid_layout.setStretchFactor(self.left_box, 3)
        self.mid_layout.setStretchFactor(self.right_box, 3 if on else 2)
        self.times_layout.setDirection(QBoxLayout.TopToBottom if on else QBoxLayout.LeftToRight)
        self.remain_label.setAlignment((Qt.AlignHCenter if on else Qt.AlignRight) | Qt.AlignBottom)
        self.elapsed_label.setAlignment((Qt.AlignHCenter if on else Qt.AlignLeft) | Qt.AlignBottom)
        if on:                                     # «осталось» — главное: выше «прошло»
            self.times_layout.removeWidget(self.elapsed_label)
            self.times_layout.addWidget(self.elapsed_label, 0)
        else:
            self.times_layout.removeWidget(self.remain_label)
            self.times_layout.addWidget(self.remain_label, 2)
        self.top_layout.removeWidget(self.timer_w)
        if on:
            self.timer_layout.setAlignment(Qt.AlignHCenter)
            self.outer.insertWidget(1, self.timer_w)
        else:
            self.outer.removeWidget(self.timer_w)
            self.top_layout.insertWidget(3, self.timer_w)
        self.playlist_label.setVisible(not on)     # на узком экране название плейлиста — над очередью
        self.channels.max_rows = 4 if on else 2
        self.outer.setStretchFactor(self.ch_box, 3 if on else 2)

    def _apply_fonts(self) -> None:
        if not hasattr(self, "ch_box"):
            return                                 # окно ещё строится
        portrait = self._want_portrait()
        self._set_portrait(portrait)
        if portrait:      # вертикальный: ширины мало, высоты много — шрифт от ширины, крупнее
            base = min(self.height() / 46.0, self.width() / 23.0)
        else:
            base = min(self.height() / 34.0, self.width() / 52.0)
        base = max(10.0, base * self.settings.font_scale)
        key = (round(base, 1), portrait)
        if key == self._last_layout_key:
            return
        self._last_layout_key = key
        self.centralWidget().setStyleSheet(self._styles(base, portrait))
        self.progress.setFixedHeight(int(max(10, base * 0.9)))
        self.setlist.set_font_px(base * (1.45 if portrait else 1.5))   # очередь — крупно
        self.channels.set_font_px(base * 0.85)

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._apply_fonts()

    def apply_settings(self) -> None:
        s = self.settings
        self.right_box.setVisible(s.show_setlist)
        self.timer_w.setVisible(s.show_timer)
        self.ch_box.setVisible(s.show_channels)
        self.show_label.setVisible(s.show_timer)
        self.timer_caption.setVisible(s.show_timer)
        self.clock_label.setVisible(s.show_clock)
        self.marker_label.setVisible(s.show_marker)
        self._last_layout_key = None
        self._apply_fonts()
        # Concert DAW
        if s.daw_ip:
            if (self.client.ip, self.client.port) != (s.daw_ip, s.daw_port) or not self.client._thread:
                self.client.connect(s.daw_ip, s.daw_port)
        else:
            self.client.disconnect()
            self._daw_last_search = 0.0
        # пульт — просим у Concert DAW нужные каналы (или «авто»: с названиями + MAIN)
        self.client.want_console((list(s.channels) or ["auto"]) if s.show_channels else None)
        self._update_channel_keys()

    def save(self) -> None:
        try:
            self.store.save(self.settings)
        except OSError:
            pass

    # ═════════════════════════════ полный экран ═════════════════════════════
    def toggle_fullscreen(self) -> None:
        if self.isFullScreen():
            self.leave_fullscreen()
        else:
            self.go_fullscreen()

    def go_fullscreen(self, screen_index: Optional[int] = None) -> None:
        idx = self.settings.screen if screen_index is None else screen_index
        screens = QGuiApplication.screens()
        if 0 <= idx < len(screens):
            if self.isFullScreen():
                self.showNormal()
            geo = screens[idx].geometry()
            self.setGeometry(geo.x() + 40, geo.y() + 40, max(640, geo.width() - 80), max(400, geo.height() - 80))
            if self.windowHandle() is not None:
                self.windowHandle().setScreen(screens[idx])
        self.showFullScreen()

    def leave_fullscreen(self) -> None:
        if self.isFullScreen():
            self.showNormal()
            self.showMaximized()

    def _menu(self, pos) -> None:
        m = QMenu(self)
        m.addAction("Настройки… (F2)", self.open_settings)
        fs = m.addAction("Обычное окно (Esc)" if self.isFullScreen() else "На весь экран (F11)")
        fs.triggered.connect(self.toggle_fullscreen)
        screens = QGuiApplication.screens()
        if len(screens) > 1:
            sm = m.addMenu("На весь экран на мониторе")
            for i, sc in enumerate(screens):
                g = sc.geometry()
                sm.addAction(f"{i + 1}: {sc.name()} ({g.width()}×{g.height()})",
                             lambda i=i: self._fullscreen_on(i))
        m.addSeparator()
        m.addAction("Выход", self.close)
        m.exec(self.mapToGlobal(pos))

    def _fullscreen_on(self, i: int) -> None:
        self.settings.screen = i
        self.save()
        self.go_fullscreen(i)

    def open_settings(self) -> None:
        from gui_settings import SettingsDialog
        was_full = self.isFullScreen()
        dlg = SettingsDialog(self.settings, self.mirror, self.update_btn, self)
        if dlg.exec():
            self.settings = dlg.result_settings()
            self.save()
            self.apply_settings()
            if was_full and self.settings.screen >= 0:
                self.go_fullscreen()

    # ═════════════════════════════ поиск в сети ═════════════════════════════
    def _daw_search(self) -> None:
        if self._daw_searching:
            return
        self._daw_searching = True
        self._daw_last_search = time.monotonic()

        def run():
            try:
                self._daw_found = sync_client.discover(1.5)
            except Exception:
                self._daw_found = []
        threading.Thread(target=run, daemon=True, name="daw-discover").start()

    def _daw_link(self) -> None:
        s, c, now = self.settings, self.client, time.monotonic()
        if self._daw_searching and self._daw_found is not None:
            found, self._daw_found, self._daw_searching = self._daw_found, None, False
            pick = next((d for d in found if s.daw_name and d["name"] == s.daw_name), None)
            if pick is None and found and (not s.daw_ip or not s.daw_name):
                pick = found[0]
            if pick is not None and ((pick["ip"], int(pick["port"])) != (c.ip, c.port) or not c.connected):
                s.daw_ip, s.daw_port, s.daw_name = pick["ip"], int(pick["port"]), pick["name"]
                self.save()
                c.connect(s.daw_ip, s.daw_port)
        if c.connected:
            self._daw_lost_since = 0.0
            if c.server_name and c.server_name != s.daw_name:
                s.daw_name = c.server_name
                self.save()
            return
        if not s.daw_ip:
            if now - self._daw_last_search > 5.0:
                self._daw_search()                   # ещё не нашли — ищем каждые 5 с
            return
        if not self._daw_lost_since:
            self._daw_lost_since = now
        elif s.daw_name and now - self._daw_lost_since > 10.0 and now - self._daw_last_search > 10.0:
            self._daw_search()                       # пропала связь — может, у компьютера новый IP

    def _update_channel_keys(self) -> None:
        self.channels.set_keys(self.mirror.keys_for(self.settings.channels))

    # ═════════════════════════════ кадр ═════════════════════════════════════
    def _toggle_blink(self) -> None:
        self._blink = not self._blink

    def _tick(self) -> None:
        self.cursor_hider.tick()
        self._daw_link()
        for _kind, _value in self.client.poll():
            pass                                     # всё нужное читаем из client.state
        c = self.client
        now = time.monotonic()
        if c.console_at and c.console_at != self._console_at:
            self._console_at = c.console_at
            self.mirror.update(c.console, now)
            self._update_channel_keys()
        self._tick_board()
        if self.settings.show_channels:
            cap, live = console_caption(c.connected, self.mirror, len(self.channels.keys), now)
            self._console_live = live
            self.channels.tick(self.mirror, live)
            self._set(self.ch_caption, cap)

    @staticmethod
    def _set(label: QLabel, text: str) -> None:
        if label.text() != text:
            label.setText(text)

    def _set_color(self, label: QLabel, color: str, extra: str = "") -> None:
        css = f"color:{color};{extra}"
        if label.property("_css") != css:
            label.setProperty("_css", css)
            label.setStyleSheet(css)

    def _tick_board(self) -> None:
        c, s = self.client, self.settings
        st = c.state
        self._set(self.clock_label, time.strftime("%H:%M"))
        el = c.show_elapsed()
        self._set(self.show_label, fmt_time(el, hours=True) if el is not None else "—:——:——")
        self._set_color(self.show_label, ACCENT if el is not None else FAINT)
        daw = ("● DAW" if c.connected else "○ DAW") + (("   ● X32" if self._console_live else "   ○ X32")
                                                     if s.show_channels else "")
        self._set(self.link_label, daw)

        if not c.connected:
            self._set(self.state_label, "НЕТ СВЯЗИ")
            self._set_color(self.state_label, TEXT, f"background:{FAINT};")
            self._set(self.now_caption, "CONCERT DAW")
            self._set(self.song_label, "Ищу Concert DAW в сети…" if not s.daw_ip else
                      f"Нет связи с Concert DAW ({s.daw_ip})")
            for lb in (self.marker_label, self.elapsed_label, self.remain_label, self.next_label,
                       self.playlist_label):
                self._set(lb, "")
            self.progress.set_state(0.0, FAINT, [])
            self.setlist.set_data([], -1, "", False)
            self._set(self.set_caption, "ПЛЕЙЛИСТ")
            return

        pos = c.position()
        dur = max(0.0, st.duration)
        counting = st.playing and pos < 0
        if counting:
            state, color = f"ОТСЧЁТ  {math.ceil(-pos)} с", WARN
        elif st.playing:
            state, color = "▶ ИГРАЕТ", ACCENT
        elif st.paused:
            state, color = "❚❚ ПАУЗА", BLUE
        else:
            state, color = "■ СТОП", FAINT
        self._set(self.state_label, state)
        self._set_color(self.state_label, BG if color != FAINT else TEXT, f"background:{color};")

        sl = find_setlist(c.songs, st.song_id, st.playlist)
        song = sl.current or c.find_song(st.song_id) or {}
        n_total = len(sl.songs)
        num = f"ТРЕК {sl.index + 1} ИЗ {n_total}" if sl.index >= 0 and n_total else "ТРЕК"
        self._set(self.now_caption, ("СЕЙЧАС · " if st.playing else "ГОТОВ К СТАРТУ · ") + num)
        self._set(self.song_label, st.song_name or "— трек не выбран —")
        self._set(self.playlist_label, f"«{sl.name}»" if sl.name else "")

        p = min(max(pos, 0.0), dur) if dur else 0.0
        markers = song.get("markers") or []
        if s.show_marker:
            cur = current_marker(markers, p)
            nm = next_marker(markers, p)
            text = cur
            if nm and st.playing:
                text = (f"{cur}   →  " if cur else "→  ") + f"{nm[0]} через {fmt_time(nm[1])}"
            self._set(self.marker_label, text)
        remaining = max(0.0, dur - p)
        level = remaining_level(remaining, s.warn_seconds, s.alert_seconds, st.playing and not counting)
        rcolor = {"normal": TEXT, "warn": WARN, "alert": ALERT if self._blink else "#7a1f26"}[level]
        self._set(self.remain_label, fmt_remaining(remaining) if dur else "")
        self._set_color(self.remain_label, rcolor)
        self._set(self.elapsed_label, fmt_time(p) + (f" / {fmt_time(dur)}" if dur else ""))
        ticks = [float(m.get("t", 0.0)) / dur for m in markers] if dur else []
        self.progress.set_state(p / dur if dur else 0.0, {"normal": ACCENT, "warn": WARN, "alert": ALERT}[level]
                                if st.playing else BLUE, ticks)

        nxt_name = st.next_name
        nxt_dur = 0.0
        if st.next_id:
            nx = c.find_song(st.next_id)
            if nx:
                nxt_name, nxt_dur = nx.get("name", nxt_name), float(nx.get("duration", 0.0) or 0.0)
        if st.autocue and nxt_name:
            nxt_name += "   (после остановки)"
        self._set(self.next_caption, "ДАЛЕЕ" + (f" · {fmt_time(nxt_dur)}" if nxt_dur else ""))
        self._set(self.next_label, nxt_name or "— конец плейлиста —")

        if s.show_setlist:
            self.setlist.set_data(sl.songs, sl.index, st.next_id, st.playing)
            left = sl.left_after_current() + remaining
            cap = f"ПЛЕЙЛИСТ · {n_total} тр." if n_total else "ПЛЕЙЛИСТ"
            if sl.index >= 0:
                cap += f" · до конца ≈ {fmt_time(left, hours=left >= 3600)}"
            self._set(self.set_caption, cap)

    def closeEvent(self, e) -> None:
        self.save()
        self.client.disconnect()
        super().closeEvent(e)


def log_path():
    return app_data_dir() / "error.log"
