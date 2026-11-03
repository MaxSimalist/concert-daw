"""
udmx.py — драйвер-обёртка для uDMX (Anyma и китайские клоны)
USB ID: VID 0x16C0, PID 0x05DC

Зависимости:
    pip install pyusb libusb-package

Драйвер устройства ставится через Zadig: libusb-win32 (рекомендуется) или WinUSB.

Протокол uDMX: vendor control transfer (bmRequestType = 0x40)
    request 1 (SetSingleChannel): wValue = значение 0..255, wIndex = канал (0..511)
    request 2 (SetChannelRange):  wValue = кол-во каналов, wIndex = первый канал (0..511),
                                  data   = значения каналов
Устройство само непрерывно шлёт DMX-кадры с последними значениями,
поэтому отправлять нужно только изменения.

uDMX — low-speed USB-устройство (программный USB на микроконтроллере): большие посылки
идут медленно, а клоны иногда отвечают ошибкой. Поэтому:
  * длинные диапазоны режутся на куски по CHUNK каналов;
  * буфер обновляется только после успешной посылки — несошедшее уйдёт в следующем кадре;
  * библиотека USB подбирается под установленный драйвер (libusb0 для libusb-win32).
"""

import time

import usb.core
import usb.util

VID = 0x16C0
PID = 0x05DC

CMD_SET_SINGLE_CHANNEL = 1
CMD_SET_CHANNEL_RANGE = 2

REQ_TYPE_OUT = (
    usb.util.CTRL_OUT | usb.util.CTRL_TYPE_VENDOR | usb.util.CTRL_RECIPIENT_DEVICE
)  # 0x40

DMX_CHANNELS = 512
CHUNK = 128            # каналов в одной USB-посылке


class UDMXError(Exception):
    pass


def _backends():
    """Библиотеки USB по порядку надёжности:
    libusb0 — родная для драйвера libusb-win32 (Zadig кладёт libusb0.dll в систему);
    libusb-1.0 из пакета libusb-package — для WinUSB; затем то, что найдёт pyusb сам."""
    out = []
    try:
        import usb.backend.libusb0 as l0
        be = l0.get_backend()
        if be is not None:
            out.append(("libusb0", be))
    except Exception:
        pass
    try:
        import libusb_package
        be = libusb_package.get_libusb1_backend()
        if be is not None:
            out.append(("libusb-1.0", be))
    except Exception:
        pass
    out.append(("pyusb по умолчанию", None))
    return out


def fresh_usb_context():
    """Новый контекст libusb-1.0 (как при перезапуске программы).

    pyusb держит один контекст на всё время работы. После того как uDMX «отвалился»
    и заново появился в системе (Errno 2 Entity not found), старый контекст на Windows
    может его больше не увидеть — помогал только перезапуск программы."""
    try:
        import usb.backend.libusb1 as l1
        l1._lib_object = None
        return True
    except Exception:
        return False


def driver_hint(backend_name):
    """Подсказка по драйверу: uDMX сделан под libusb-win32."""
    if backend_name != "libusb-1.0":   # libusb0 — нужный драйвер; «по умолчанию» — не знаем, не пугаем
        return ""
    return ("Драйвер uDMX сейчас WinUSB/libusbK. Для uDMX надёжнее родной драйвер libusb-win32: "
            "Zadig → Options → List All Devices → uDMX → libusb-win32 → Replace Driver.")


def _is_udmx(dev):
    """ID 16C0:05DC общий для V-USB устройств — по возможности проверяем строку продукта."""
    try:
        product = usb.util.get_string(dev, dev.iProduct) or ""
    except Exception:
        return True  # у некоторых клонов строка не читается — всё равно берём
    return not product or "udmx" in product.lower()


def find_devices(with_backend=False):
    """Все подключённые uDMX (через первую библиотеку USB, которая их видит)."""
    for name, be in _backends():
        try:
            devs = [d for d in usb.core.find(find_all=True, idVendor=VID, idProduct=PID, backend=be)
                    if _is_udmx(d)]
        except Exception:
            continue
        if devs:
            return [(d, name) for d in devs] if with_backend else devs
    return []


class UDMX:
    def __init__(self, index=0, timeout_ms=1000):
        devices = find_devices(with_backend=True)
        if not devices:
            raise UDMXError(
                "uDMX не найден. Проверьте подключение и драйвер (Zadig: libusb-win32 / WinUSB)."
            )
        if index >= len(devices):
            raise UDMXError(f"Найдено устройств: {len(devices)}, индекс {index} недоступен")
        self.dev, self.backend = devices[index]
        self.timeout = timeout_ms
        try:
            self.dev.get_active_configuration()
        except usb.core.USBError:
            try:
                self.dev.set_configuration()  # только если устройство ещё не сконфигурировано
            except usb.core.USBError:
                pass
        self.universe = bytearray(DMX_CHANNELS)
        self.chunk = CHUNK          # каналов в одной посылке (бережный режим — меньше)
        self.gap = 0.0              # пауза между кусками одной посылки, с

    # --- низкий уровень -------------------------------------------------

    def set_channel(self, channel, value):
        """channel: 1..512, value: 0..255"""
        self._check_channel(channel)
        value = max(0, min(255, int(value)))
        self.dev.ctrl_transfer(
            REQ_TYPE_OUT, CMD_SET_SINGLE_CHANNEL, value, channel - 1, None, self.timeout
        )
        self.universe[channel - 1] = value

    def set_range(self, start_channel, values):
        """Записать подряд несколько каналов начиная с start_channel (1..512)."""
        self._check_channel(start_channel)
        data = bytes(max(0, min(255, int(v))) for v in values)
        if start_channel - 1 + len(data) > DMX_CHANNELS:
            raise UDMXError("Выход за пределы 512 каналов")
        for off in range(0, len(data), self.chunk):
            if off and self.gap:
                time.sleep(self.gap)
            part = data[off:off + self.chunk]
            first = start_channel - 1 + off
            sent = self.dev.ctrl_transfer(
                REQ_TYPE_OUT, CMD_SET_CHANNEL_RANGE, len(part), first, part, self.timeout
            )
            if sent is not None and sent != len(part):
                raise UDMXError(f"uDMX принял {sent} из {len(part)} байт")
            self.universe[first:first + len(part)] = part

    # --- работа с буфером вселенной ------------------------------------

    def update(self, new_universe, limit=DMX_CHANNELS):
        """Отправить только изменившийся диапазон каналов (экономит USB-трафик).
        limit — сколько каналов вообще используется (до последнего прибора в патче)."""
        new = bytes(new_universe[:DMX_CHANNELS]).ljust(DMX_CHANNELS, b"\x00")
        diff = [i for i in range(min(limit, DMX_CHANNELS)) if new[i] != self.universe[i]]
        if not diff:
            return 0
        lo, hi = diff[0], diff[-1]
        self.set_range(lo + 1, new[lo: hi + 1])
        return hi - lo + 1

    def invalidate(self, limit=DMX_CHANNELS):
        """Считать, что устройство в неизвестном состоянии: следующий update перешлёт всё."""
        for i in range(min(limit, DMX_CHANNELS)):
            self.universe[i] = (self.universe[i] + 1) % 256

    def blackout(self):
        self.set_range(1, bytes(DMX_CHANNELS))

    def reset(self):
        """Сброс USB-порта uDMX — выводит прошивку из «зависания» (Errno 32 Pipe error)."""
        self.dev.reset()

    def close(self):
        usb.util.dispose_resources(self.dev)

    @staticmethod
    def _check_channel(channel):
        if not 1 <= channel <= DMX_CHANNELS:
            raise UDMXError(f"Канал {channel} вне диапазона 1..512")


if __name__ == "__main__":
    # Тест: плавно поднимаем и опускаем канал 1 (диммер), затем blackout
    dmx = UDMX()
    print(f"uDMX найден (библиотека USB: {dmx.backend}), тест канала 1...")
    try:
        for v in list(range(0, 256, 5)) + list(range(255, -1, -5)):
            dmx.set_channel(1, v)
            time.sleep(0.02)
    finally:
        dmx.blackout()
        dmx.close()
    print("Готово")
