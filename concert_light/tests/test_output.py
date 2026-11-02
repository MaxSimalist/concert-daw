"""Тест вывода на uDMX без устройства: поддельный pyusb + «капризный» uDMX.

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
    def __init__(self, msg, code=None):
        super().__init__(msg)
        self.backend_error_code = code


class FakeDev:
    idVendor, idProduct, iProduct = 0x16C0, 0x05DC, 2

    def __init__(self):
        self.transfers = []      # (first, data)
        self.fail_next = 0       # сколько следующих посылок «уронить»
        self.gone = False
        self.dmx = bytearray(512)

    def get_active_configuration(self):
        return 1

    def ctrl_transfer(self, rt, req, wvalue, windex, data, timeout):
        if self.gone:
            raise USBError("No such device (it may have been disconnected)", code=-4)
        if self.fail_next:
            self.fail_next -= 1
            raise USBError("[Errno 10060] Operation timed out", code=-7)
        assert rt == 0x40 and req == 2 and wvalue == len(data) <= 128
        self.dmx[windex:windex + len(data)] = data
        self.transfers.append((windex, bytes(data)))
        return len(data)


DEVICES = []
usb = types.ModuleType("usb")
usb.core = types.ModuleType("usb.core")
usb.util = types.ModuleType("usb.util")
usb.backend = types.ModuleType("usb.backend")
usb.core.USBError = USBError
usb.core.find = lambda find_all=True, **kw: list(DEVICES)
usb.util.CTRL_OUT, usb.util.CTRL_TYPE_VENDOR, usb.util.CTRL_RECIPIENT_DEVICE = 0, 0x40, 0
usb.util.get_string = lambda dev, idx: "uDMX"
usb.util.dispose_resources = lambda dev: None
sys.modules.update({"usb": usb, "usb.core": usb.core, "usb.util": usb.util, "usb.backend": usb.backend})

from core import output as output_mod  # noqa: E402
from core.output import Output  # noqa: E402

assert output_mod._USB_OK, output_mod._USB_ERR


def make():
    dev = FakeDev()
    DEVICES[:] = [dev]
    log = Path(tempfile.mkdtemp()) / "udmx.log"
    out = Output(rate=0, log_path=log)   # rate=0 — без ограничения частоты для теста
    out.limit = 300
    return dev, out, log


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
    assert out.dev is not None and out.stats["reconnects"] == 0, out.stats
    u[5] = 255
    out.send(u)
    assert dev.dmx[5] == 255                               # несошедшее догнало
    assert out.stats["errors"] == 5
    assert "ошибка посылки" in log.read_text("utf-8")
    print("одиночные сбои не рвут связь ok:", out.stats)


def test_long_failure_reconnects_and_resends_all():
    dev, out, log = make()
    u = bytearray(512)
    u[100] = 42
    out.send(u)
    dev.fail_next = Output.FAIL_LIMIT
    for i in range(Output.FAIL_LIMIT):
        u[1] = i + 1
        out.send(u)
    assert out.dev is None and out.stats["reconnects"] == 1
    dev.dmx = bytearray(512)                                 # uDMX «перезагрузился» и всё забыл
    out.send(u)                                              # переподключение сразу, без паузы
    assert out.connected and dev.dmx[100] == 42, "после переподключения переслано всё"
    print("долгий сбой -> переподключение и полная пересылка ok")


def test_unplug_and_plug_back():
    dev, out, _ = make()
    u = bytearray(512)
    out.send(u)
    dev.gone = True
    DEVICES.clear()
    u[0] = 1
    out.send(u)
    assert out.dev is None and "потеряна" in out.status      # выдернули — сразу видно
    out.send(u)
    assert out.dev is None                                   # устройства нет — ждём
    new = FakeDev()
    DEVICES[:] = [new]
    out._last_try = 0
    u[0] = 200
    out.send(u)
    assert out.connected and new.dmx[0] == 200
    print("выдернули/вставили ok")


def test_rate_limit():
    dev, out, _ = make()
    out.rate = 30
    u = bytearray(512)
    out.send(u)
    n = len(dev.transfers)
    for i in range(10):          # 10 кадров подряд мгновенно — пройдёт не больше одного
        u[3] = i + 1
        out.send(u)
    assert len(dev.transfers) - n <= 1
    print("ограничение частоты ok")


if __name__ == "__main__":
    test_connect_and_chunks()
    test_transient_errors_do_not_disconnect()
    test_long_failure_reconnects_and_resends_all()
    test_unplug_and_plug_back()
    test_rate_limit()
    print("ВСЕ ТЕСТЫ ВЫВОДА ПРОЙДЕНЫ")
