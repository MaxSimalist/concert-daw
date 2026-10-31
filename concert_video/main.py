"""
main.py — Точка входа Concert Video (видео на экраны сцены, синхронно с Concert DAW).

Запуск:  python main.py   (или ярлык / start.bat после install.bat)
"""
import os
import sys

# Qt Multimedia: движок FFmpeg (Qt 6.5+) — играет почти любые видеофайлы
os.environ.setdefault("QT_MEDIA_BACKEND", "ffmpeg")


def install_error_log() -> None:
    """Запуск без консоли (pythonw) — необработанные ошибки пишутся в лог и показываются
    окном (одна и та же ошибка — один раз за запуск). Лог: %APPDATA%/ConcertVideo/error.log"""
    import datetime
    import traceback
    from vmodel import app_data_dir
    log_path = app_data_dir() / "error.log"
    seen: dict[str, int] = {}
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
            from PySide6.QtWidgets import QApplication, QMessageBox
            if QApplication.instance() is not None:
                state["dialog_open"] = True
                try:
                    QMessageBox.critical(None, "Concert Video — ошибка",
                                         f"{exc_type.__name__}: {exc}\n\nПрограмма продолжает работать. "
                                         f"Подробности в журнале:\n{log_path}")
                finally:
                    state["dialog_open"] = False
        except Exception:
            pass

    sys.excepthook = hook


def main() -> int:
    install_error_log()
    from PySide6.QtWidgets import QApplication

    from gui_main import MainWindow
    from style import STYLESHEET
    from vmodel import ProjectStore

    app = QApplication(sys.argv)
    app.setApplicationName("Concert Video")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)
    store = ProjectStore()
    try:                                     # ускорение видеокартой — до создания окон эфира
        import views
        views.GPU["on"] = bool(getattr(store.load(), "gpu_render", True))
    except Exception:
        pass
    win = MainWindow(store)
    show_maximized(win)
    return app.exec()


def show_maximized(win) -> None:
    """Развернуть на весь экран надёжно (Windows):
    * окно не требует больше места, чем есть на экране (иначе Windows не может его
      развернуть и сдвигает в сторону);
    * «обычный» размер — 85 % экрана по центру: кнопка «Развернуть/Восстановить»
      ведёт себя предсказуемо;
    * если с первого раза не развернулось (окно ещё не получило экран) — повтор."""
    from PySide6.QtCore import QTimer
    from PySide6.QtGui import QGuiApplication
    screen = QGuiApplication.primaryScreen()
    win.setMinimumSize(640, 400)            # явный минимум вместо «суммы всех панелей»
    if screen is not None:
        av = screen.availableGeometry()
        w, h = int(av.width() * 0.85), int(av.height() * 0.85)
        win.setGeometry(av.x() + (av.width() - w) // 2, av.y() + (av.height() - h) // 2, w, h)
    win.showMaximized()

    def again():
        if not win.isMaximized() and not win.isFullScreen():
            win.showNormal()
            win.showMaximized()
    QTimer.singleShot(200, again)


if __name__ == "__main__":
    sys.exit(main())
