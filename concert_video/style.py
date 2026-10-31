"""style.py — Тёмная сценическая тема «Graphite».

Плоские кнопки без лишних рамок, глубокий графитовый фон, один акцентный цвет
(мятно-зелёный) для всего «включённого», жёлтый — для внимания, красный — для
записи/ошибок. Крупные шрифты и контраст: читается и в тёмном закулисье, и под
прямым светом пушки.

Все цвета интерфейса — здесь. Значки (gui_icons.py) и отрисовка дорожек/индикаторов
используют те же оттенки.
"""

BG = "#121518"        # фон окна
PANEL = "#191d22"     # панели
STRIP = "#20252b"     # кнопки, полосы микшера
HOVER = "#272d34"
BORDER = "#2c333b"
DEEP = "#0b0d10"      # «колодцы»: шкалы, прогресс, фейдер
TEXT = "#e6eaee"
DIM = "#7f8b97"
ACCENT = "#36d399"    # мятно-зелёный: играет, включено, выбрано
ACCENT_BG = "#173a2e"
ORANGE = "#ff9f43"
YELLOW = "#f7c948"
RED = "#ef4b55"
SELECT = "#22303a"

STYLESHEET = f"""
QWidget {{ background: {BG}; color: {TEXT}; font-size: 14px; font-family: "Segoe UI", "Inter", sans-serif; }}
QToolTip {{ background: {STRIP}; color: {TEXT}; border: 1px solid {BORDER}; border-radius: 4px; padding: 4px 6px; }}

/* ── панели ───────────────────────────────────────────────────────────── */
#transport {{ background: {PANEL}; border-bottom: 1px solid {BORDER}; }}
#leftPanel, #mixerPanel {{ background: {PANEL}; }}
QLabel#caption {{ color: {DIM}; font-size: 11px; font-weight: 700; letter-spacing: 1.5px; background: transparent; }}
QSplitter::handle {{ background: {BG}; }}
QSplitter::handle:hover {{ background: {ACCENT_BG}; }}
QSplitter::handle:horizontal {{ width: 5px; }}
QSplitter::handle:vertical {{ height: 5px; }}

/* ── кнопки: плоские, рамка появляется при наведении ──────────────────── */
QPushButton {{ background: {STRIP}; border: 1px solid transparent; border-radius: 8px; padding: 6px 12px; }}
QPushButton:hover {{ background: {HOVER}; border-color: {BORDER}; }}
QPushButton:pressed {{ background: {DEEP}; }}
QPushButton:checked {{ background: {ACCENT_BG}; color: {ACCENT}; border-color: {ACCENT}; }}
QPushButton:disabled {{ color: #4a535c; background: {PANEL}; }}
QPushButton#iconBtn, #timelinePanel QPushButton#iconBtn {{ padding: 0px; min-height: 0px; font-size: 12px;
                                                          border-radius: 5px; background: transparent; }}
QPushButton#iconBtn:hover {{ background: {HOVER}; }}

/* ── транспорт ────────────────────────────────────────────────────────── */
QPushButton#playBtn {{ background: {ACCENT_BG}; color: #eafff6; font-size: 22px; font-weight: 800;
                       min-width: 120px; min-height: 60px; border: 1px solid {ACCENT}; border-radius: 14px; }}
QPushButton#playBtn:hover {{ background: #1d4a3a; }}
QPushButton#playBtn[active="true"] {{ background: #1fa776; border-color: #5ee6b0; color: white; }}
QPushButton#pauseBtn {{ background: {STRIP}; color: {TEXT}; font-size: 22px; font-weight: 800;
                        min-width: 110px; min-height: 60px; border: 1px solid {BORDER}; border-radius: 14px; }}
QPushButton#pauseBtn:hover {{ background: {HOVER}; }}
QPushButton#pauseBtn[active="true"] {{ background: #5c4610; border-color: {YELLOW}; color: #ffe9a8; }}
QPushButton#stopBtn {{ background: #33161a; color: white; font-size: 22px; font-weight: 800;
                       min-width: 110px; min-height: 60px; border: 1px solid {RED}; border-radius: 14px; }}
QPushButton#stopBtn:hover {{ background: #45191f; }}
QPushButton#navBtn {{ font-size: 22px; min-width: 44px; min-height: 60px; border-radius: 14px; }}
QPushButton#recBtn {{ background: #2a1316; color: {RED}; font-size: 16px; font-weight: 800;
                      min-width: 70px; min-height: 60px; border: 1px solid #5a2228; border-radius: 14px; }}
QPushButton#recBtn:checked {{ background: {RED}; color: white; border-color: #ff7d85; }}
QPushButton#autoCueBtn {{ font-size: 15px; font-weight: 700; min-height: 40px; }}
QPushButton#autoCueBtn:checked {{ background: #3d2a12; color: {ORANGE}; border-color: {ORANGE}; }}
QPushButton#metroBtn {{ font-size: 15px; font-weight: 700; min-height: 34px; padding: 2px 12px; }}
QPushButton#metroBtn:checked {{ background: {YELLOW}; color: #1b1705; border-color: {YELLOW}; }}

QLabel#songTitle {{ font-size: 24px; font-weight: 800; background: transparent; }}
QLabel#keyLabel {{ font-size: 18px; font-weight: 800; color: {YELLOW}; background: transparent; padding-left: 10px; }}
QLabel#nextLabel {{ font-size: 16px; color: {ORANGE}; background: transparent; }}
QLabel#timeLabel {{ font-size: 38px; font-weight: 600; font-family: "Consolas", "JetBrains Mono", monospace; background: transparent; }}
QLabel#remainLabel {{ font-size: 20px; color: {DIM}; font-family: "Consolas", "JetBrains Mono", monospace; background: transparent; }}
QLabel#loadingLabel {{ color: {YELLOW}; font-size: 16px; background: transparent; }}
QLabel#recLabel {{ color: {RED}; font-size: 15px; font-weight: 700; background: transparent; }}

QProgressBar#songProgress {{ background: {DEEP}; border: none; border-radius: 4px; min-height: 8px; max-height: 8px; }}
QProgressBar#songProgress::chunk {{ background: {ACCENT}; border-radius: 4px; }}

/* ── списки плейлистов и треков ───────────────────────────────────────── */
QListWidget {{ background: {BG}; border: 1px solid {BORDER}; border-radius: 8px; font-size: 16px; outline: 0; padding: 2px; }}
QListWidget::item {{ padding: 8px 6px; border: none; border-left: 3px solid transparent; border-radius: 4px; margin: 1px 0; }}
QListWidget::item:hover {{ background: #191e23; }}
QListWidget::item:selected {{ background: {SELECT}; color: white; border-left: 3px solid {ACCENT}; }}
QListWidget#songList {{ font-size: 17px; }}

/* ── полосы прокрутки: тонкие ─────────────────────────────────────────── */
QScrollArea {{ border: none; background: {PANEL}; }}
QScrollBar:horizontal {{ height: 10px; background: transparent; margin: 1px; }}
QScrollBar:vertical {{ width: 10px; background: transparent; margin: 1px; }}
QScrollBar::handle:horizontal {{ background: {BORDER}; border-radius: 4px; min-width: 40px; }}
QScrollBar::handle:vertical {{ background: {BORDER}; border-radius: 4px; min-height: 40px; }}
QScrollBar::handle:hover {{ background: #48525d; }}
QScrollBar::add-line, QScrollBar::sub-line {{ width: 0px; height: 0px; }}
QScrollBar::add-page, QScrollBar::sub-page {{ background: transparent; }}

/* ── микшер ───────────────────────────────────────────────────────────── */
#trackStrip {{ background: {STRIP}; border: 1px solid {BORDER}; border-radius: 10px; }}
#trackStrip QLabel {{ background: transparent; }}
#trackStrip QPushButton {{ background: {PANEL}; }}
#trackStrip QPushButton:checked {{ background: {ACCENT_BG}; }}
QLabel#trackName {{ font-size: 13px; font-weight: 700; }}
#trackStrip QComboBox {{ font-size: 12px; padding: 2px 4px; }}
#trackStrip QLabel#caption {{ font-size: 10px; }}
QLabel#trackName[missing="true"] {{ color: {RED}; }}
QLabel#dbLabel {{ font-family: "Consolas", monospace; color: {DIM}; }}

QPushButton#insertBtn {{ font-size: 12px; text-align: left; padding: 5px 6px; }}
QPushButton#insertBtn[state="active"] {{ border-color: {ACCENT}; color: {ACCENT}; }}
QPushButton#insertBtn[state="bypass"] {{ color: {DIM}; border: 1px dashed {BORDER}; }}
QPushButton#insertBtn[state="error"] {{ border-color: {RED}; color: {RED}; }}
QPushButton#fxBtn {{ font-size: 11px; font-weight: 800; padding: 3px 2px; color: {DIM}; letter-spacing: 0.5px; }}
#trackStrip QPushButton#fxBtn[on="true"] {{ background: {ACCENT_BG}; color: {ACCENT}; border-color: {ACCENT}; }}

QPushButton#muteBtn, QPushButton#soloBtn {{ font-size: 15px; font-weight: 800; min-height: 28px; padding: 2px; }}
#trackStrip QPushButton#muteBtn:checked, #consoleStrip QPushButton#muteBtn:checked,
QPushButton#muteBtn:checked {{ background: {ORANGE}; color: #1b1205; border-color: {ORANGE}; }}
#trackStrip QPushButton#soloBtn:checked, QPushButton#soloBtn:checked {{ background: {YELLOW}; color: #1b1705; border-color: {YELLOW}; }}

/* фейдеры пульта X32 */
#consoleStrip {{ background: #171d1b; border: 1px solid #24352e; border-radius: 10px; }}
#consoleStrip QLabel {{ background: transparent; }}
#consoleStrip[master="true"] {{ border: 1px solid {ACCENT}; }}
#consoleStrip[offline="true"] {{ background: {PANEL}; border-color: {BORDER}; }}
QLabel#consoleStatus[ok="true"] {{ color: {ACCENT}; font-weight: 700; }}
QLabel#consoleStatus[ok="false"] {{ color: {DIM}; }}

QSlider::groove:vertical {{ background: {DEEP}; width: 6px; border-radius: 3px; }}
QSlider::handle:vertical {{ background: #dfe4e8; border: 1px solid #8b96a0; height: 18px; margin: 0 -5px; border-radius: 4px; }}
QSlider::handle:vertical:hover {{ background: white; border-color: {ACCENT}; }}
QSlider::add-page:vertical {{ background: #1f7a58; border-radius: 3px; }}
QSlider::groove:horizontal {{ background: {DEEP}; height: 6px; border-radius: 3px; }}
QSlider::handle:horizontal {{ background: #dfe4e8; border: 1px solid #8b96a0; width: 14px; margin: -5px 0; border-radius: 4px; }}
QSlider::sub-page:horizontal {{ background: #1f7a58; border-radius: 3px; }}

/* ── таймлайн: компактная шапка ───────────────────────────────────────── */
#timelinePanel QPushButton {{ padding: 2px 8px; min-height: 20px; font-size: 13px; border-radius: 6px; }}
#timelinePanel QComboBox {{ padding: 1px 6px; font-size: 13px; }}
#timelinePanel QLabel {{ font-size: 12px; }}
#trimBanner {{ background: #2e2814; border: 1px solid #6b5a1c; border-radius: 8px; }}
QLabel#trimText {{ color: {YELLOW}; font-weight: 700; background: transparent; }}

/* ── поля ввода ───────────────────────────────────────────────────────── */
QComboBox {{ background: {DEEP}; border: 1px solid {BORDER}; border-radius: 6px; padding: 4px 8px; font-size: 15px; font-weight: 700; }}
QComboBox:hover {{ border-color: #48525d; }}
QComboBox[warn="true"] {{ border: 1px solid {RED}; color: {RED}; }}
QComboBox QAbstractItemView {{ background: {STRIP}; border: 1px solid {BORDER}; selection-background-color: {SELECT}; outline: 0; }}
QLineEdit {{ background: {DEEP}; border: 1px solid {BORDER}; border-radius: 6px; padding: 4px 6px; }}
QLineEdit:focus {{ border-color: {ACCENT}; }}
QCheckBox, QRadioButton {{ background: transparent; spacing: 8px; }}
QCheckBox::indicator, QRadioButton::indicator {{ width: 16px; height: 16px; border: 1px solid #48525d; background: {DEEP}; }}
QCheckBox::indicator {{ border-radius: 4px; }}
QRadioButton::indicator {{ border-radius: 8px; }}
QCheckBox::indicator:checked, QRadioButton::indicator:checked {{ background: {ACCENT}; border-color: {ACCENT}; }}
QGroupBox {{ border: 1px solid {BORDER}; border-radius: 8px; margin-top: 14px; padding-top: 8px; }}
QGroupBox::title {{ subcontrol-origin: margin; left: 10px; padding: 0 4px; color: {DIM}; font-weight: 700; }}

/* ── вкладки настроек ─────────────────────────────────────────────────── */
QTabWidget::pane {{ border: 1px solid {BORDER}; border-radius: 8px; top: -1px; }}
QTabBar::tab {{ background: transparent; color: {DIM}; padding: 8px 14px; border-bottom: 2px solid transparent; }}
QTabBar::tab:hover {{ color: {TEXT}; }}
QTabBar::tab:selected {{ color: {TEXT}; border-bottom: 2px solid {ACCENT}; }}

QStatusBar {{ background: {PANEL}; color: {DIM}; font-size: 13px; border-top: 1px solid {BORDER}; }}
QStatusBar QLabel {{ background: transparent; }}
QLabel#statusMsg[warn="true"] {{ color: {YELLOW}; }}
QLabel#audioInfo[warn="true"] {{ color: {RED}; font-weight: 700; }}

QMenu {{ background: {STRIP}; border: 1px solid {BORDER}; border-radius: 8px; padding: 4px; font-size: 14px; }}
QMenu::item {{ padding: 7px 22px; border-radius: 5px; }}
QMenu::item:selected {{ background: {SELECT}; }}
QMenu::separator {{ height: 1px; background: {BORDER}; margin: 4px 8px; }}
QDialog {{ background: {PANEL}; }}
"""


def repolish(widget) -> None:
    """Применить стиль заново после смены динамического свойства (setProperty)."""
    widget.style().unpolish(widget)
    widget.style().polish(widget)
