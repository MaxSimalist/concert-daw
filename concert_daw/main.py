"""
main.py — Точка входа Concert DAW для Behringer X32.

Запуск:  python main.py
"""
import os
import sys

# ASIO-сборка PortAudio должна быть выбрана ДО любого import sounddevice (см. audio_engine.py)
os.environ.setdefault("SD_ENABLE_ASIO", "1")

import gc

from model import ConfigStore


def tune_for_realtime() -> None:
    """Настройки процесса, снижающие риск щелчков на живом выступлении."""
    # 1) Аудио-коллбэк — Python-код и ему нужен GIL. По умолчанию интерпретатор
    #    передаёт GIL между потоками раз в 5 мс — это почти весь бюджет блока
    #    в 256 сэмплов (5.3 мс). Уменьшаем интервал до 1 мс.
    sys.setswitchinterval(0.001)

    # 2) Windows: высокий приоритет процесса (HIGH_PRIORITY_CLASS = 0x80).
    #    REALTIME (0x100) не используем — он может «повесить» систему.
    if sys.platform == "win32":
        try:
            import ctypes
            k32 = ctypes.windll.kernel32
            k32.SetPriorityClass(k32.GetCurrentProcess(), 0x00000080)
        except Exception:
            pass


def install_error_log() -> None:
    """Запуск ярлыком/start.bat идёт без консоли (pythonw) — ошибку иначе не увидеть.
    Любое необработанное исключение пишется в лог и показывается окном.
    * одна и та же ошибка показывается окном только ОДИН раз за запуск
      (повторы — только в лог, со счётчиком), чтобы не заваливать экран;
    * одновременно открыто не больше одного окна ошибки;
    * лог ограничен ~1 МБ (старое обрезается).
    Лог: %APPDATA%/ConcertDAW/error.log"""
    import datetime
    import traceback
    from model import app_data_dir
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
        # «отпечаток» ошибки: тип + место, где она возникла
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
                    show_error_dialog("Concert DAW", exc_type, exc, text, log_path)
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

def main() -> int:
    install_error_log()
    # Процессы-работники (темп/тон, анализ тональности) на Windows запускаются
    # «с нуля» — freeze_support нужен, если программу соберут в .exe (PyInstaller).
    import multiprocessing
    multiprocessing.freeze_support()
    tune_for_realtime()

    # Масштаб интерфейса (⚙ Настройки → Интерфейс). Qt принимает его только
    # ДО создания QApplication, поэтому смена масштаба требует перезапуска.
    store = ConfigStore()
    cfg = store.load()
    if abs(cfg.ui_scale - 1.0) > 0.001:
        os.environ["QT_SCALE_FACTOR"] = f"{cfg.ui_scale:.2f}"

    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication

    from gui_main import MainWindow
    from style import STYLESHEET

    app = QApplication(sys.argv)
    app.setApplicationName("Concert DAW")
    app.setStyle("Fusion")
    app.setStyleSheet(STYLESHEET)

    win = MainWindow(store)
    if win.cfg.start_maximized:
        win.showMaximized()
    else:
        win.show()

    # 3) «Замораживаем» объекты, созданные при запуске: сборщик мусора больше
    #    не будет обходить их при каждой полной сборке → короче паузы GIL.
    gc.collect()
    gc.freeze()

    QTimer.singleShot(0, win.start_audio)  # открываем звук после показа окна
    # Через 3 с после старта заранее поднимаем процессы обработки темпа/тона,
    # чтобы первая смена тональности не ждала их запуска.
    import audio_edit
    QTimer.singleShot(3000, audio_edit.warm_up)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
