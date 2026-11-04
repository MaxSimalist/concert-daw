"""
main.py — Точка входа Concert Stage (табло для сцены: трек, остаток, очередь, секундомер, пульт).

Запуск:  python main.py   (или ярлык / start.bat после install.bat)
"""
import sys


def install_error_log() -> None:
    """Запуск без консоли (pythonw) — необработанные ошибки пишутся в лог и показываются
    окном (одна и та же ошибка — один раз за запуск). Лог: %APPDATA%/ConcertStage/error.log"""
    import datetime
    import traceback
    from stage_model import app_data_dir
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
        seen[key] = seen.get(key, 0) + 1
        if seen[key] <= 3:
            write_log(text)
        if seen[key] == 1 and not state["dialog_open"]:
            from PySide6.QtWidgets import QApplication
            if QApplication.instance() is not None:
                state["dialog_open"] = True
                try:
                    show_error_dialog("Concert Stage", exc_type, exc, text, log_path)
                finally:
                    state["dialog_open"] = False
        sys.__excepthook__(exc_type, exc, tb)

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


def main() -> int:
    install_error_log()
    from PySide6.QtWidgets import QApplication

    from gui_main import MainWindow
    from stage_model import SettingsStore

    app = QApplication(sys.argv)
    app.setApplicationName("Concert Stage")
    app.setStyle("Fusion")
    win = MainWindow(SettingsStore())
    win.setMinimumSize(640, 400)
    if win.settings.start_fullscreen:
        win.show()
        win.go_fullscreen()
    else:
        win.showMaximized()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
