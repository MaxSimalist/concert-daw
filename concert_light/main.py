"""
main.py — Точка входа Concert Light (пульт света для uDMX, синхронно с Concert DAW).

Запуск:  python main.py   (или ярлык / start.bat после install.bat)
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)


def install_error_log() -> None:
    """Запуск без консоли (pythonw) — необработанные ошибки пишутся в лог и показываются
    окном (одна и та же ошибка — один раз за запуск). Лог: %APPDATA%/ConcertLight/error.log"""
    import datetime
    import traceback
    from core.appdata import app_data_dir
    log_path = app_data_dir() / "error.log"
    seen: dict = {}
    state = {"dialog_open": False}

    def write_log(text: str) -> None:
        try:
            log_path.parent.mkdir(parents=True, exist_ok=True)
            if log_path.exists() and log_path.stat().st_size > 1_000_000:
                tail = log_path.read_bytes()[-200_000:]
                log_path.write_bytes("...(старые записи обрезаны)...\n".encode("utf-8") + tail)
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n===== {datetime.datetime.now():%Y-%m-%d %H:%M:%S} =====\n{text}")
        except OSError:
            pass

    def hook(exc_type, exc, tb):
        text = "".join(traceback.format_exception(exc_type, exc, tb))
        frames = traceback.extract_tb(tb)
        where = f"{frames[-1].filename}:{frames[-1].lineno}" if frames else ""
        key = f"{exc_type.__name__}@{where}"
        count = seen.get(key, 0) + 1
        seen[key] = count
        if count == 1:
            write_log(text)
        elif count in (2, 10, 100, 1000):
            write_log(f"(повтор ошибки {key} — уже {count} раз)\n")
        if sys.__stderr__ is not None and count <= 3:
            sys.__stderr__.write(text)
        if count > 1 or state["dialog_open"]:
            return
        try:
            from PySide6.QtWidgets import QApplication
            if QApplication.instance() is not None:
                state["dialog_open"] = True
                try:
                    show_error_dialog("Concert Light", exc_type, exc, text, log_path)
                finally:
                    state["dialog_open"] = False
        except Exception:
            pass

    sys.excepthook = hook


def show_error_dialog(app_name: str, exc_type, exc, text: str, log_path) -> None:
    """Окно ошибки с кнопкой «Сообщить об ошибке» (готовый отчёт с текстом ошибки)."""
    from PySide6.QtWidgets import QMessageBox
    box = QMessageBox()
    box.setIcon(QMessageBox.Critical)
    box.setWindowTitle(f"{app_name} — ошибка")
    box.setText(f"{exc_type.__name__}: {exc}")
    box.setInformativeText(f"Программа продолжает работать. Подробности в журнале:\n{log_path}")
    report = box.addButton("Сообщить об ошибке…", QMessageBox.ActionRole)
    box.addButton("Закрыть", QMessageBox.RejectRole)
    box.exec()
    if box.clickedButton() is report:
        from gui_feedback import FeedbackDialog
        FeedbackDialog("bug", log_path, error_text=text).exec()


def dark_palette():
    from PySide6.QtGui import QColor, QPalette
    p = QPalette()
    base, alt, text = QColor(36, 36, 42), QColor(48, 48, 56), QColor(225, 225, 230)
    for role, c in (
        (QPalette.Window, base), (QPalette.WindowText, text), (QPalette.Base, QColor(26, 26, 30)),
        (QPalette.AlternateBase, alt), (QPalette.Text, text), (QPalette.Button, alt),
        (QPalette.ButtonText, text), (QPalette.Highlight, QColor(220, 130, 0)),
        (QPalette.HighlightedText, QColor(0, 0, 0)), (QPalette.ToolTipBase, alt),
        (QPalette.ToolTipText, text), (QPalette.Link, QColor(255, 170, 60)),
    ):
        p.setColor(role, c)
    p.setColor(QPalette.Disabled, QPalette.Text, QColor(120, 120, 120))
    p.setColor(QPalette.Disabled, QPalette.ButtonText, QColor(120, 120, 120))
    return p


def main() -> int:
    install_error_log()
    from PySide6.QtWidgets import QApplication

    from core.appdata import Settings, app_data_dir
    from core.fixtures import load_library
    from ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName("Concert Light")
    app.setStyle("Fusion")
    app.setPalette(dark_palette())
    # готовые типы — в папке программы, свои — в %APPDATA% (не затираются обновлением)
    library = load_library(os.path.join(HERE, "fixtures"))
    user_fixtures = str(app_data_dir() / "fixtures")
    library.update(load_library(user_fixtures))
    win = MainWindow(library, user_fixtures, Settings())
    win.resize(1280, 800)
    win.showMaximized()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
