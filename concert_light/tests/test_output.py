"""Тест вывода на uDMX без устройства: поддельный pyusb + «капризный» uDMX и управляемые часы.

python tests/test_output.py
"""

import os
import sys
import tempfile
import types
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)


# ---------- поддельный pyusb ------------------------------------------------

class USBError(IOError):
    def __init__(self, msg, code=None, errno=None):
        super().__init__(msg)
        self.backend_error_code = code
        self.errno = errno


class FakeDev:
    idVendor, idProduct, iProduct = 0x16C0, 0x05DC, 2

    def __init__(self):
        self.transfers = []      # (first, data)
        self.fail_next = 0       # сколько следующих посылок «уронить» (таймаут)
        self.stalled = False     # прошивка «зависла»: каждая посылка — Errno 32, лечится сбросом
        self.gone = False
        self.resets = 0
        self.dmx = bytearray(512)

    def get_active_configuration(self):
        return 1

    def reset(self):
        if self.gone:
            raise USBError("Entity not found", code=-5, errno=2)
        self.resets += 1
        self.stalled = False
        self.dmx = bytearray(512)     # после сброса uDMX всё забыл

    def ctrl_transfer(self, rt, req, wvalue, windex, data, timeout):
        if self.gone:
            raise USBError("[Errno 2] Entity not found", code=-5, errno=2)
        if self.stalled:
            raise USBError("[Errno 32] Pipe error", code=-9, errno=32)
        if self.fail_next:
            self.fail_next -= 1
            raise USBError("[Errno 10060] Operation timed out", code=-7)
        assert rt == 0x40 and req == 2 and wvalue == len(data) <= 128
        self.dmx[windex:windex + len(data)] = data
        self.transfers.append((windex, bytes(data)))
        return len(data)


DEVICES = []
STATE = {"stale_ctx": False}       # «старый контекст libusb не видит вновь появившийся uDMX»
usb = types.ModuleType("usb")
usb.core = types.ModuleType("usb.core")
usb.util = types.ModuleType("usb.util")
usb.backend = types.ModuleType("usb.backend")
usb.backend.libusb1 = types.ModuleType("usb.backend.libusb1")
usb.backend.libusb1._lib_object = "ctx-1"
usb.core.USBError = USBError


def _find(find_all=True, **kw):
    if STATE["stale_ctx"] and usb.backend.libusb1._lib_object is not None:
        return []
    STATE["stale_ctx"] = False
    usb.backend.libusb1._lib_object = "ctx-new"
    return list(DEVICES)


usb.core.find = _find
usb.util.CTRL_OUT, usb.util.CTRL_TYPE_VENDOR, usb.util.CTRL_RECIPIENT_DEVICE = 0, 0x40, 0
usb.util.get_string = lambda dev, idx: "uDMX"
usb.util.dispose_resources = lambda dev: None
sys.modules.update({"usb": usb, "usb.core": usb.core, "usb.util": usb.util, "usb.backend": usb.backend,
                    "usb.backend.libusb1": usb.backend.libusb1})

from core import output as output_mod  # noqa: E402
from core.output import Output  # noqa: E402

assert output_mod._USB_OK, output_mod._USB_ERR


class Clock:
    t = 1000.0

    def __call__(self):
        return self.t

    def tick(self, dt):
        self.t += dt


CLOCK = Clock()
output_mod.time.monotonic = CLOCK      # часы вывода — управляемые


def make():
    dev = FakeDev()
    DEVICES[:] = [dev]
    STATE["stale_ctx"] = False
    log = Path(tempfile.mkdtemp()) / "udmx.log"
    out = Output(rate=0, log_path=log)   # rate=0 — без ограничения частоты для теста
    out.limit = 300
    return dev, out, log


def run(out, u, seconds, step=0.05, change=None):
    """Крутить вывод seconds секунд по 50 мс (как движок), меняя канал change."""
    for i in range(int(seconds / step)):
        if change is not None:
            u[change] = (u[change] + 1) % 256
        out.send(u)
        CLOCK.tick(step)


def test_connect_and_chunks():
    dev, out, _ = make()
    u = bytearray(512)
    u[0], u[299] = 255, 7
    out.send(u)
    assert out.connected, out.status
    assert dev.dmx[0] == 255 and dev.dmx[299] == 7
    assert all(len(d) <= 128 for _, d in dev.transfers)
    assert max(f + len(d) for f, d in dev.transfers) <= 300   # каналы за патчем не гоняем
    n = len(dev.transfers)
    out.send(u)                                             # ничего не изменилось — тишина
    assert len(dev.transfers) == n
    u[10] = 99
    out.send(u)
    assert dev.transfers[-1] == (10, bytes([99]))           # только изменение
    print("подключение и куски ok:", out.status)


def test_transient_errors_do_not_disconnect():
    dev, out, log = make()
    u = bytearray(512)
    out.send(u)
    for step in range(1, 11):
        u[5] = step * 20
        dev.fail_next = 1 if step % 2 else 0                # каждая вторая посылка падает
        out.send(u)
        CLOCK.tick(0.25)                                    # больше паузы после ошибки
    assert out.dev is not None and out.stats["reconnects"] == 0, out.stats
    u[5] = 255
    out.send(u)
    assert dev.dmx[5] == 255                               # несошедшее догнало
    assert out.stats["errors"] == 5
    assert "ошибка посылки" in log.read_text("utf-8")
    print("одиночные сбои не рвут связь ok:", out.stats["errors"], "ошибок")


def test_backoff_after_error():
    dev, out, _ = make()
    u = bytearray(512)
    out.send(u)
    dev.fail_next = 1
    u[1] = 1
    out.send(u)                                            # ошибка
    n = len(dev.transfers)
    for _ in range(3):                                     # сразу после ошибки — не «добиваем»
        u[1] += 1
        out.send(u)
        CLOCK.tick(0.05)
    assert len(dev.transfers) == n
    CLOCK.tick(0.1)
    out.send(u)
    assert len(dev.transfers) == n + 1 and dev.dmx[1] == u[1]
    print("пауза после ошибки ok")


def test_pipe_error_resets_port():
    """Как в журнале: прошивка зависла — Errno 32 на каждую посылку. Лечим сбросом порта."""
    dev, out, log = make()
    u = bytearray(512)
    u[100] = 42
    run(out, u, 1, change=0)
    dev.stalled = True
    run(out, u, 2, change=0)
    assert dev.resets == 1 and out.stats["resets"] == 1 and out.stats["reconnects"] == 0, out.stats
    assert out.connected and dev.dmx[100] == 42             # после сброса всё переслано
    assert "сброс USB-порта" in log.read_text("utf-8")
    print("Pipe error -> сброс USB-порта ok")


def test_entity_not_found_needs_fresh_context():
    """Как в журнале: uDMX пропал (Entity not found) и вернулся, но старый контекст его не видит."""
    dev, out, log = make()
    u = bytearray(512)
    u[7] = 77
    run(out, u, 0.5)
    dev.gone = True
    STATE["stale_ctx"] = True
    run(out, u, 0.3, change=0)
    assert out.dev is None and out.stats["reconnects"] == 1
    new = FakeDev()                                         # uDMX снова появился в системе
    DEVICES[:] = [new]
    run(out, u, 3)
    assert out.dev is None, "старый контекст ещё не видит"
    run(out, u, 4)                                          # после 5 с — новый контекст libusb
    assert out.connected and new.dmx[7] == 77, out.status
    text = log.read_text("utf-8")
    assert "новый контекст libusb" in text and "снова найден" in text and "держалась" in text
    print("Entity not found -> новый контекст libusb ok")


def test_unplug_and_plug_back():
    dev, out, _ = make()
    u = bytearray(512)
    out.send(u)
    dev.gone = True
    DEVICES.clear()
    u[0] = 1
    out.send(u)
    assert out.dev is None and "потеряна" in out.status      # выдернули — сразу видно
    new = FakeDev()
    DEVICES[:] = [new]
    u[0] = 200
    run(out, u, 2.5)
    assert out.connected and new.dmx[0] == 200
    print("выдернули/вставили ok")


def test_long_failure_reconnects():
    dev, out, _ = make()
    u = bytearray(512)
    u[100] = 42
    out.send(u)
    dev.fail_next = 1000                                     # таймауты, не Pipe error
    run(out, u, 3, change=1)
    assert out.stats["reconnects"] >= 1
    dev.fail_next = 0
    dev.dmx = bytearray(512)
    run(out, u, 3)
    assert out.connected and dev.dmx[100] == 42
    print("долгий сбой -> переподключение и полная пересылка ok")


# ошибки из настоящего журнала (libusb0 на русской Windows)
LOG_GEN = ("[Errno None] b'libusb0-dll:err [control_msg] sending control message failed, win error: "
           "\\xcf\\xf0\\xe8\\xf1\\xee\\xe5\\xe4\\xe8\\xed\\xe5\\xed\\xed\\xee\\xe5 \\xea \\xf1\\xe8\\xf1\\xf2\\xe5\\xec\\xe5 "
           "\\xf3\\xf1\\xf2\\xf0\\xee\\xe9\\xf1\\xf2\\xe2\\xee \\xed\\xe5 \\xf0\\xe0\\xe1\\xee\\xf2\\xe0\\xe5\\xf2.\\r\\n\\n'")
LOG_GONE = ("[Errno None] b'libusb0-dll:err [control_msg] sending control message failed, win error: "
            "\\xd3\\xea\\xe0\\xe7\\xe0\\xed\\xee \\xed\\xe5\\xf1\\xf3\\xf9\\xe5\\xf1\\xf2\\xe2\\xf3\\xfe\\xf9\\xe5\\xe5 "
            "\\xf3\\xf1\\xf2\\xf0\\xee\\xe9\\xf1\\xf2\\xe2\\xee.\\r\\n\\n'")


class LibusbError(IOError):
    def __init__(self, text, code=-5):
        super().__init__(text)
        self.backend_error_code = code
        self.errno = None

    def __str__(self):
        return self.args[0]


def test_readable_libusb0_errors():
    from core.output import readable_error
    a = readable_error(LibusbError(LOG_GEN))
    b = readable_error(LibusbError(LOG_GONE))
    assert "Присоединенное к системе устройство не работает" in a and "\\x" not in a, a
    assert "Указано несуществующее устройство" in b, b
    assert Output._is_gone(LibusbError(LOG_GONE), "libusb0")
    assert not Output._is_gone(LibusbError(LOG_GEN), "libusb0")     # -5 у libusb0 — не «пропал»
    assert Output._is_pipe(LibusbError(LOG_GEN))                   # «не работает» — сброс порта
    print("ошибки libusb0 по-русски ok:", a)


def test_gentle_mode():
    dev, out, log = make()
    out.rate = 20
    out.set_gentle(True)
    u = bytearray(512)
    u[0], u[299] = 1, 2
    out.send(u)
    assert out.dev.chunk == 32 and all(len(d) <= 32 for _, d in dev.transfers)
    n = len(dev.transfers)
    sent = 0
    for i in range(20):                 # 1 секунда по 50 мс, меняем канал каждый кадр
        u[0] = i + 2
        before = len(dev.transfers)
        out.send(u)
        sent += len(dev.transfers) > before
        CLOCK.tick(0.05)
    assert sent <= 11, sent             # не чаще 10 раз/с
    out.set_gentle(False)
    assert out.dev.chunk == 128
    assert "бережный режим" in log.read_text("utf-8")
    print("бережный режим ok:", sent, "посылок/с")


def test_fast_recovery_from_stall():
    """Как в часовом журнале: прошивка виснет раз в 20–40 с. Каждое зависание — как можно короче."""
    dev, out, log = make()
    u = bytearray(512)
    run(out, u, 1, change=0)
    dev.stalled = True
    t0 = CLOCK.t
    while dev.stalled:                           # до сброса — одна ошибка, без «трёх попыток»
        u[0] = (u[0] + 1) % 256
        out.send(u)
        CLOCK.tick(0.05)
    freeze = CLOCK.t - t0
    n = len(dev.transfers)
    run(out, u, 0.3, change=0)
    assert dev.resets == 1 and out.stats["errors"] == 1 and len(dev.transfers) > n
    assert freeze <= 0.1, freeze
    assert not out.gentle                        # бережный режим сам больше не включается
    print("быстрый выход из зависания ok:", out.stats["errors"], "ошибка на сбой")


def test_fast_retry_after_loss_and_summary():
    dev, out, log = make()
    u = bytearray(512)
    run(out, u, 0.5)
    dev.gone = True
    DEVICES.clear()
    run(out, u, 0.2, change=0)
    assert out.dev is None
    DEVICES[:] = [FakeDev()]
    run(out, u, 0.7)                             # вернулся через ~1 с — найден за полсекунды
    assert out.connected, out.status
    for _ in range(3):                           # ещё сбои, затем 10 минут работы
        out.dev.dev.stalled = True               # out.dev — UDMX, out.dev.dev — поддельный USB
        run(out, u, 1, change=0)
    CLOCK.tick(Output.SUMMARY_SEC)
    out.send(u)
    assert "за 10 мин: сбросов порта" in log.read_text("utf-8"), log.read_text("utf-8")
    print("быстрый поиск после потери и сводка за 10 минут ok")


def test_libusb0_reset_reopens():
    """libusb-win32: после сброса uDMX появляется в системе заново — переоткрываем, а не шлём в старый."""
    dev, out, log = make()
    u = bytearray(512)
    u[9] = 99
    run(out, u, 0.5)
    out.dev.backend = "libusb0"
    dev.stalled = True
    u[0] = 1
    CLOCK.tick(0.1)
    out.send(u)                                  # ошибка -> сброс -> закрыли
    assert out.dev is None and dev.resets == 1, (out.status, dev.resets)
    new = FakeDev()
    DEVICES[:] = [new]
    run(out, u, 0.5)                             # через 0,3 с переоткрыли и переслали всё
    assert out.connected and new.dmx[9] == 99 and out.stats["reconnects"] == 0
    assert "переоткрываю устройство" in log.read_text("utf-8")
    print("libusb0: сброс -> переоткрытие ok")


def test_rate_limit_and_driver_hint():
    dev, out, _ = make()
    out.rate = 20
    u = bytearray(512)
    out.send(u)
    n = len(dev.transfers)
    for i in range(10):          # 10 кадров подряд мгновенно — пройдёт не больше одного
        u[3] = i + 1
        out.send(u)
    assert len(dev.transfers) - n <= 1
    from core.udmx import driver_hint
    assert "libusb-win32" in driver_hint("libusb-1.0")       # открыто через libusb-1.0 (WinUSB)
    assert driver_hint("libusb0") == "" and driver_hint("pyusb по умолчанию") == ""
    assert Output().rate == 20
    print("ограничение частоты и подсказка о драйвере ok")


if __name__ == "__main__":
    test_connect_and_chunks()
    test_transient_errors_do_not_disconnect()
    test_backoff_after_error()
    test_pipe_error_resets_port()
    test_entity_not_found_needs_fresh_context()
    test_unplug_and_plug_back()
    test_long_failure_reconnects()
    test_rate_limit_and_driver_hint()
    test_readable_libusb0_errors()
    test_gentle_mode()
    test_fast_recovery_from_stall()
    test_fast_retry_after_loss_and_summary()
    test_libusb0_reset_reopens()
    print("ВСЕ ТЕСТЫ ВЫВОДА ПРОЙДЕНЫ")
