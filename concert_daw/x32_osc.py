"""
x32_osc.py — Связь с пультом Behringer X32 по сети (OSC через UDP, порт 10023).

Что умеет
---------
* Проверить связь: /info — пульт отвечает моделью и версией прошивки.
* Вызвать сцену / сниппет: /-action/goscene N, /-action/gosnippet N.
* Двусторонняя синхронизация фейдеров:
    - запрос значения: адрес без аргументов, например /ch/01/mix/fader → пульт
      отвечает тем же адресом с текущим значением;
    - подписка /xremote (повторять каждые < 10 с): пульт сам присылает все
      изменения, сделанные на нём (двигаете фейдер на пульте — приходит сообщение);
    - установка: /ch/01/mix/fader 0.75 (float 0..1).

Как устроено
------------
Один UDP-сокет: через него отправляем команды и на него же пульт присылает ответы
и обновления. Приём — в отдельном потоке, который только складывает сообщения
в очередь; интерфейс забирает их таймером (никаких вызовов Qt из чужого потока).

Компьютер и пульт — в одной сети (разъём Remote/Ethernet пульта или через роутер).
IP пульта: Setup → Network. Брандмауэр Windows ответы пульта обычно пропускает;
если связи нет — разрешите Python в «Брандмауэр Защитника Windows → Разрешить приложение».
"""
from __future__ import annotations

import math
import queue
import re
import socket
import struct
import sys
import threading
import time
from typing import Optional

X32_PORT = 10023


# ══════════════════════════════════════════════════════════════════════════════
# OSC: сборка и разбор сообщений (big-endian, выравнивание по 4 байта)
# ══════════════════════════════════════════════════════════════════════════════
def _pad(b: bytes) -> bytes:
    b += b"\0"
    return b + b"\0" * ((4 - len(b) % 4) % 4)


def osc_message(address: str, *args) -> bytes:
    tags = ","
    payload = b""
    for a in args:
        if isinstance(a, bool):
            a = int(a)
        if isinstance(a, int):
            tags += "i"
            payload += struct.pack(">i", a)
        elif isinstance(a, float):
            tags += "f"
            payload += struct.pack(">f", a)
        else:
            tags += "s"
            payload += _pad(str(a).encode("utf-8"))
    return _pad(address.encode("ascii")) + _pad(tags.encode("ascii")) + payload


def parse_osc(data: bytes) -> tuple[str, list]:
    """Разбор сообщения: строки, int, float (blob и прочее — пропускаем)."""
    def read_str(pos: int) -> tuple[str, int]:
        end = data.index(b"\0", pos)
        return data[pos:end].decode("utf-8", "replace"), (end + 4) & ~3
    address, pos = read_str(0)
    if pos >= len(data) or data[pos:pos + 1] != b",":
        return address, []
    tags, pos = read_str(pos)
    args: list = []
    for t in tags[1:]:
        if t == "s":
            s, pos = read_str(pos)
            args.append(s)
        elif t == "i":
            args.append(struct.unpack(">i", data[pos:pos + 4])[0])
            pos += 4
        elif t == "f":
            args.append(struct.unpack(">f", data[pos:pos + 4])[0])
            pos += 4
        elif t == "b":                      # blob: длина (big-endian) + байты, выравнивание по 4
            size = struct.unpack(">i", data[pos:pos + 4])[0]
            args.append(data[pos + 4:pos + 4 + size])
            pos += 4 + ((size + 3) & ~3)
        else:
            break
    return address, args


# ══════════════════════════════════════════════════════════════════════════════
# Фейдеры пульта: адреса, названия, шкала
# ══════════════════════════════════════════════════════════════════════════════
def _range(prefix: str, n: int, two_digits: bool = True) -> list[str]:
    return [f"{prefix}/{i:02d}" if two_digits else f"{prefix}/{i}" for i in range(1, n + 1)]


CONSOLE_GROUPS: list[tuple[str, list[str]]] = [
    ("Мастер", ["main/st", "main/m"]),
    ("Каналы 1–16", _range("ch", 32)[:16]),
    ("Каналы 17–32", _range("ch", 32)[16:]),
    ("Aux In 1–8", _range("auxin", 8)),
    ("FX Return 1–8", _range("fxrtn", 8)),
    ("Шины (Mix Bus) 1–16", _range("bus", 16)),
    ("Матрицы 1–6", _range("mtx", 6)),
    ("DCA 1–8", _range("dca", 8, two_digits=False)),
]
ALL_KEYS: list[str] = [k for _, keys in CONSOLE_GROUPS for k in keys]
MASTER_KEY = "main/st"


def default_name(key: str) -> str:
    if key == "main/st":
        return "MAIN LR"
    if key == "main/m":
        return "M/C"
    kind, num = key.split("/")
    label = {"ch": "Ch", "auxin": "Aux", "fxrtn": "FX", "bus": "Bus", "mtx": "Mtx", "dca": "DCA"}[kind]
    return f"{label} {int(num)}"


def paths_for(key: str) -> tuple[str, str, str]:
    """(адрес фейдера, адрес «вкл» (не mute), адрес названия)."""
    if key.startswith("dca/"):
        return f"/{key}/fader", f"/{key}/on", f"/{key}/config/name"
    return f"/{key}/mix/fader", f"/{key}/mix/on", f"/{key}/config/name"


def fader_to_db(f: float) -> float:
    """Положение фейдера X32 (0..1) → dB (шкала пульта: 0.75 = 0 dB, 1.0 = +10 dB)."""
    if f >= 0.5:
        return f * 40.0 - 30.0
    if f >= 0.25:
        return f * 80.0 - 50.0
    if f >= 0.0625:
        return f * 160.0 - 70.0
    return f * 480.0 - 90.0


def db_to_fader(db: float) -> float:
    if db < -60.0:
        f = (db + 90.0) / 480.0
    elif db < -30.0:
        f = (db + 70.0) / 160.0
    elif db < -10.0:
        f = (db + 50.0) / 80.0
    else:
        f = (db + 30.0) / 40.0
    return min(1.0, max(0.0, f))


def format_db(f: float) -> str:
    if f <= 0.0005:
        return "−∞"
    db = fader_to_db(f)
    return f"{db:+.1f}" if db > -10 else f"{db:.0f}"


# ── Индикаторы уровня с пульта ───────────────────────────────────────────────
# Подписка: «/meters ,s /meters/N» (действует 10 с, продлевается вместе с /xremote).
# Пульт присылает blob: число значений (int32, little-endian) и сами значения
# (float32, little-endian, линейные: 1.0 = 0 dBFS).
#   /meters/0 — 70 значений: каналы 1–32, Aux In 1–8, FX Return 1–8, шины 1–16, матрицы 1–6
#   /meters/2 — 49 значений: шины 1–16, матрицы 1–6, MAIN L, MAIN R, M/C, …
METER_SOURCES = ("/meters/0", "/meters/2")


# Источники, которые можно назначить в DCA (у них есть /grp/dca — битовая маска DCA 1–8).
DCA_SOURCES = [k for k in ALL_KEYS if k.split("/")[0] in ("ch", "auxin", "fxrtn", "bus")]


def meter_channels(key: str) -> int:
    """Сколько столбиков у индикатора фейдера: MAIN LR — 2 (L/R), остальные — 1."""
    return 2 if key == "main/st" else 1


def meter_slots(key: str) -> list[tuple[str, int]]:
    """Откуда брать уровень фейдера: [(адрес /meters/N, номер значения)].
    У DCA своего уровня у пульта нет — он считается по входящим в DCA каналам (ConsoleMirror.levels)."""
    if key == "main/st":
        return [("/meters/2", 22), ("/meters/2", 23)]
    if key == "main/m":
        return [("/meters/2", 24)]
    kind, num = key.split("/")
    base = {"ch": 0, "auxin": 32, "fxrtn": 40, "bus": 48, "mtx": 64}.get(kind)
    if base is None:
        return []
    return [("/meters/0", base + int(num) - 1)]


def decode_meters(blob: bytes) -> list[float]:
    if not isinstance(blob, (bytes, bytearray)) or len(blob) < 4:
        return []
    n = struct.unpack("<i", blob[:4])[0]
    n = max(0, min(n, (len(blob) - 4) // 4))
    return list(struct.unpack(f"<{n}f", blob[4:4 + 4 * n]))


class ConsoleMirror:
    """Состояние фейдеров пульта по входящим сообщениям (без Qt — проверяется тестами)."""

    def __init__(self) -> None:
        self.fader: dict[str, float] = {}
        self.on: dict[str, int] = {}
        self.names: dict[str, str] = {}
        self.meters: dict[str, list[float]] = {}     # /meters/N → последние значения
        self.meters_at = 0.0
        self.dca_bits: dict[str, int] = {}          # источник → маска DCA (бит 0 = DCA 1)
        self._map: dict[str, tuple[str, str]] = {}
        for key in DCA_SOURCES:
            self._map[f"/{key}/grp/dca"] = (key, "dca")
        for key in ALL_KEYS:
            f, o, n = paths_for(key)
            self._map[f] = (key, "fader")
            self._map[o] = (key, "on")
            self._map[n] = (key, "name")

    def apply(self, address: str, args: list) -> Optional[tuple[str, str]]:
        if address in METER_SOURCES:
            if args:
                values = decode_meters(args[0])
                if values:
                    self.meters[address] = values
                    self.meters_at = time.monotonic()
                    return ("", "meters")
            return None
        hit = self._map.get(address)
        if hit is None or not args:
            return None
        key, kind = hit
        value = args[0]
        try:
            if kind == "dca":
                self.dca_bits[key] = int(value)
            elif kind == "fader":
                self.fader[key] = min(1.0, max(0.0, float(value)))
            elif kind == "on":
                self.on[key] = int(value)
            else:
                self.names[key] = str(value).strip()
        except (TypeError, ValueError):
            return None
        return hit

    def name(self, key: str) -> str:
        return self.names.get(key) or default_name(key)

    def levels(self, key: str) -> list[float]:
        """Текущий уровень фейдера пульта (1 значение, у MAIN LR — 2); [] — нет данных."""
        if time.monotonic() - self.meters_at > 1.0:
            return []                                  # данные устарели — индикатор гаснет
        if key.startswith("dca/"):
            # уровень DCA = самый громкий из назначенных в неё каналов/шин
            bit = 1 << (int(key.split("/")[1]) - 1)
            members = [k for k, bits in self.dca_bits.items() if bits & bit]
            levels = [lv for k in members for lv in self.levels(k)]
            return [max(levels)] if levels else [0.0]
        out = []
        for src, i in meter_slots(key):
            values = self.meters.get(src, [])
            if i < len(values):
                out.append(max(0.0, float(values[i])))
        return out

    @staticmethod
    def dca_queries() -> list[str]:
        """Запросы состава DCA: какие каналы/шины в какие DCA назначены."""
        return [f"/{k}/grp/dca" for k in DCA_SOURCES]

    @staticmethod
    def queries(keys) -> list[str]:
        """Адреса для запроса текущего состояния фейдеров keys."""
        out = []
        for k in keys:
            out.extend(paths_for(k))
        return out


# ══════════════════════════════════════════════════════════════════════════════
# Сетевое подключение
# ══════════════════════════════════════════════════════════════════════════════
class X32Osc:
    ALIVE_SECONDS = 12.0   # нет ответов дольше — считаем, что связи нет

    def __init__(self) -> None:
        self.ip = ""
        self.sock: Optional[socket.socket] = None
        self.last_error = ""
        self.last_rx = 0.0
        self._q: "queue.SimpleQueue[tuple[str, list]]" = queue.SimpleQueue()
        self._waiters: dict[str, tuple[threading.Event, list]] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None

    # ── подключение ─────────────────────────────────────────────────────────
    def configure(self, ip: str) -> None:
        ip = (ip or "").strip()
        if ip == self.ip and self.sock is not None:
            return
        self.close()
        self.ip = ip
        if not ip:
            return
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            if sys.platform == "win32":
                # Windows: иначе после «порт недоступен» recvfrom падает с WSAECONNRESET
                try:
                    sock.ioctl(socket.SIO_UDP_CONNRESET, False)
                except (AttributeError, OSError):
                    pass
            sock.bind(("", 0))
            sock.settimeout(0.3)
        except OSError as exc:
            self.last_error = str(exc)
            return
        self.sock = sock
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._rx_loop, args=(sock, self._stop),
                                        daemon=True, name="x32-osc-rx")
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        if self.sock is not None:
            try:
                self.sock.close()
            except OSError:
                pass
        self.sock = None
        self.last_rx = 0.0

    @property
    def connected(self) -> bool:
        return self.sock is not None and time.monotonic() - self.last_rx < self.ALIVE_SECONDS

    # ── приём (фоновый поток) ───────────────────────────────────────────────
    def _rx_loop(self, sock: socket.socket, stop: threading.Event) -> None:
        while not stop.is_set():
            try:
                data, _ = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except (ConnectionResetError, ConnectionRefusedError):
                continue
            except OSError:
                if stop.is_set():
                    break
                time.sleep(0.1)
                continue
            self.last_rx = time.monotonic()
            try:
                address, args = parse_osc(data)
            except Exception:
                continue
            with self._lock:
                waiter = self._waiters.get(address)
            if waiter is not None:
                waiter[1].append(args)
                waiter[0].set()
            self._q.put((address, args))

    def poll(self) -> list[tuple[str, list]]:
        """Забрать все пришедшие сообщения (вызывать из интерфейса по таймеру)."""
        out = []
        while True:
            try:
                out.append(self._q.get_nowait())
            except queue.Empty:
                return out

    # ── отправка ────────────────────────────────────────────────────────────
    def send(self, address: str, *args) -> bool:
        if self.sock is None or not self.ip:
            self.last_error = "IP пульта не задан"
            return False
        try:
            self.sock.sendto(osc_message(address, *args), (self.ip, X32_PORT))
            self.last_error = ""
            return True
        except OSError as exc:
            self.last_error = str(exc)
            return False

    def request(self, address: str, timeout: float = 1.0) -> Optional[list]:
        """Отправить запрос и дождаться ответа (блокирует — только для «Проверить связь»)."""
        waiter = (threading.Event(), [])
        with self._lock:
            self._waiters[address] = waiter
        try:
            if not self.send(address):
                return None
            if waiter[0].wait(timeout):
                return waiter[1][0]
            self.last_error = "пульт не ответил — проверьте IP и сетевой кабель"
            return None
        finally:
            with self._lock:
                self._waiters.pop(address, None)

    def ping(self) -> Optional[str]:
        """Описание пульта («V2.07 … X32 4.06») или None, если пульт не ответил."""
        args = self.request("/info")
        if args is None:
            return None
        return " ".join(str(a) for a in args) or "ответ получен"

    def subscribe(self) -> bool:
        """Подписка на изменения с пульта; повторять каждые < 10 секунд."""
        return self.send("/xremote")

    def recall_scene(self, n: int) -> bool:
        return self.send("/-action/goscene", int(n))

    def recall_snippet(self, n: int) -> bool:
        return self.send("/-action/gosnippet", int(n))


# ══════════════════════════════════════════════════════════════════════════════
# Поиск пульта в сети
# ══════════════════════════════════════════════════════════════════════════════
def local_ipv4s() -> list[str]:
    """IPv4-адреса этого компьютера (кроме 127.x)."""
    ips: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:  # адрес «основного» сетевого интерфейса (пакеты при этом не отправляются)
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127.") and ip != "0.0.0.0")


def discover(timeout: float = 1.5, port: Optional[int] = None) -> list[dict]:
    """Найти пульты X32/M32 в локальной сети.

    Отправляет /xinfo широковещательно и на каждый адрес своих подсетей /24
    (на случай, если роутер не пропускает broadcast). Пульт отвечает
    «/xinfo IP имя модель прошивка». Возвращает [{"ip", "name", "model", "version"}]."""
    port = port or X32_PORT
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        if sys.platform == "win32":
            try:
                sock.ioctl(socket.SIO_UDP_CONNRESET, False)
            except (AttributeError, OSError):
                pass
        sock.bind(("", 0))
        sock.settimeout(0.15)
        msg = osc_message("/xinfo")
        targets = ["255.255.255.255"]
        for ip in local_ipv4s():
            a, b, c, _ = ip.split(".")
            net = f"{a}.{b}.{c}"
            targets.append(f"{net}.255")
            if not ip.startswith("169.254."):
                targets.extend(f"{net}.{h}" for h in range(1, 255) if f"{net}.{h}" != ip)
        for t in dict.fromkeys(targets):
            try:
                sock.sendto(msg, (t, port))
            except OSError:
                pass
        found: dict[str, dict] = {}
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                data, addr = sock.recvfrom(2048)
            except (socket.timeout, ConnectionResetError, ConnectionRefusedError):
                continue
            except OSError:
                break
            try:
                address, args = parse_osc(data)
            except Exception:
                continue
            if address != "/xinfo":
                continue
            args = [str(a) for a in args] + ["", "", "", ""]
            found[addr[0]] = {"ip": addr[0], "name": args[1], "model": args[2] or "X32",
                              "version": args[3]}
        return list(found.values())
    finally:
        sock.close()


# ══════════════════════════════════════════════════════════════════════════════
# Темп эффектов пульта (FX 1–4) под трек
# ══════════════════════════════════════════════════════════════════════════════
# Типы эффектов слотов FX 1–4 (/fx/N/type) — по «Unofficial X32/M32 OSC Remote Protocol».
FX_TYPES = ["HALL", "AMBI", "RPLT", "ROOM", "CHAM", "PLAT", "VREV", "VRM", "GATE", "RVRS",
            "DLY", "3TAP", "4TAP", "CRS", "FLNG", "PHAS", "DIMC", "FILT", "ROTA", "PAN",
            "SUB", "D/RV", "CR/R", "FL/R", "D/CR", "D/FL", "MODD", "GEQ2", "GEQ", "TEQ2",
            "TEQ", "DES2", "DES", "P1A", "P1A2", "PQ5", "PQ5S", "WAVD", "LIM", "CMB",
            "CMB2", "FAC", "FAC1M", "FAC2", "LEC", "LEC2", "ULC", "ULC2", "ENH2", "ENH",
            "EXC2", "EXC", "IMG", "EDI", "SON", "AMP2", "AMP", "DRV2", "DRV", "PIT2", "PIT"]
FX_TITLES = {"DLY": "Stereo Delay", "3TAP": "3-Tap Delay", "4TAP": "4-Tap Delay", "HALL": "Hall Reverb",
             "PLAT": "Plate Reverb", "ROOM": "Room Reverb", "CRS": "Chorus", "FLNG": "Flanger",
             "PHAS": "Phaser", "D/RV": "Delay/Reverb", "MODD": "Modulation Delay"}
# Эффекты, у которых программа меняет время: тип → (номер параметра времени, мин мс, макс мс).
# Stereo Delay: par/02 = Time, 1…3000 мс. Результат каждый раз проверяется по ответу
# пульта в миллисекундах (/node), поэтому неверная шкала сразу будет замечена и исправлена.
TEMPO_FX = {"DLY": (2, 1.0, 3000.0)}

DIVISIONS: dict[str, tuple[str, float]] = {
    "1/4": ("1/4 — четверть", 1.0),
    "1/8": ("1/8 — восьмая", 0.5),
    "1/8.": ("1/8 с точкой", 0.75),
    "1/4.": ("1/4 с точкой", 1.5),
    "1/4T": ("1/4 триоль", 2.0 / 3.0),
    "1/8T": ("1/8 триоль", 1.0 / 3.0),
    "1/16": ("1/16", 0.25),
    "1/2": ("1/2 — половинная", 2.0),
}


def fx_type_name(index: Optional[int]) -> str:
    if index is None or not (0 <= index < len(FX_TYPES)):
        return "?"
    code = FX_TYPES[index]
    return FX_TITLES.get(code, code)


def delay_ms(bpm: float, division: str) -> float:
    """Время задержки для темпа: четверть = 60000 / BPM."""
    factor = DIVISIONS.get(division, ("", 1.0))[1]
    return 60000.0 / bpm * factor


def time_to_float(ms: float, lo: float, hi: float, scale: str) -> float:
    ms = min(hi, max(lo, ms))
    if scale == "log":
        return math.log(ms / lo) / math.log(hi / lo)
    return (ms - lo) / (hi - lo)


def float_to_time(f: float, lo: float, hi: float, scale: str) -> float:
    if scale == "log":
        return lo * (hi / lo) ** f
    return lo + f * (hi - lo)


class FxTempo:
    """Выставляет время задержки эффектов пульта под темп трека.

    1. Запрашивает тип эффекта в слоте (/fx/N/type).
    2. Если это задержка с известным параметром времени — отправляет время.
    3. Проверяет: запрашивает /node fx/N/par — пульт отвечает значениями в мс.
       Не совпало — пробует другую шкалу (линейная/логарифмическая) и запоминает рабочую.
    Всё асинхронно: ответы пульта приходят через handle() из основного цикла программы."""

    def __init__(self, osc: X32Osc, scale: str = "lin") -> None:
        self.osc = osc
        self.scale = scale if scale in ("lin", "log") else "lin"
        self.types: dict[int, int] = {}
        self._target: dict[int, float] = {}       # слот → нужное время, мс
        self._tried: dict[int, set] = {}
        self._sent_scale: dict[int, str] = {}     # слот → какой шкалой отправлено последнее значение
        self.reports: list[str] = []              # сообщения для строки состояния
        self.scale_changed = False

    def apply(self, bpm: float, slots: dict, quiet: set = frozenset()) -> None:
        """slots: {"2": "1/4", …}. bpm — темп, который звучит.
        quiet — слоты из авто-режима: если там не задержка, пропускаем молча."""
        if bpm <= 0 or not slots:
            return
        self._quiet = {int(k) for k in quiet if str(k).isdigit()}
        for key, division in slots.items():
            try:
                n = int(key)
            except ValueError:
                continue
            if not 1 <= n <= 4:
                continue
            self._target[n] = delay_ms(bpm, division)
            self._tried[n] = set()
            self.osc.send(f"/fx/{n}/type")         # ответ → handle → отправка времени

    def handle(self, address: str, args: list) -> None:
        if address.startswith("/fx/") and address.endswith("/type") and args:
            try:
                n = int(address.split("/")[2])
                self.types[n] = int(args[0])
            except (ValueError, IndexError):
                return
            if n in self._target:
                self._send_time(n)
        elif address.lstrip("/") == "node" and args:
            self._verify(str(args[0]))

    def _send_time(self, n: int, scale: Optional[str] = None) -> None:
        code = FX_TYPES[self.types[n]] if 0 <= self.types.get(n, -1) < len(FX_TYPES) else "?"
        ms = self._target.get(n)
        if ms is None:
            return
        if code not in TEMPO_FX:
            self._target.pop(n, None)
            if n in getattr(self, "_quiet", set()):
                return                                   # авто-режим: не задержка — не трогаем
            self.reports.append(f"FX{n}: «{fx_type_name(self.types.get(n))}» — у этого эффекта "
                                "нет времени задержки, пропущен")
            return
        par, lo, hi = TEMPO_FX[code]
        scale = scale or self.scale
        self._tried.setdefault(n, set()).add(scale)
        self._sent_scale[n] = scale
        self.osc.send(f"/fx/{n}/par/{par:02d}", float(time_to_float(ms, lo, hi, scale)))
        self.osc.send("/node", f"fx/{n}/par")       # проверка: пульт вернёт значения в мс

    def _verify(self, text: str) -> None:
        line = text.strip().splitlines()[0] if text.strip() else ""
        m = re.match(r"/fx/(\d)/par\s+(.*)", line)
        if not m:
            return
        n = int(m.group(1))
        ms = self._target.get(n)
        code = FX_TYPES[self.types[n]] if 0 <= self.types.get(n, -1) < len(FX_TYPES) else "?"
        if ms is None or code not in TEMPO_FX:
            return
        par, lo, hi = TEMPO_FX[code]
        tokens = m.group(2).split()
        if len(tokens) < par:
            return
        num = re.search(r"-?\d+(?:\.\d+)?", tokens[par - 1])
        if not num:
            return
        got = float(num.group(0))
        expected = min(hi, max(lo, ms))
        if abs(got - expected) <= max(2.0, expected * 0.01):
            self._target.pop(n, None)
            note = " (больше 3 с — ограничено)" if ms > hi else ""
            self.reports.append(f"FX{n} {fx_type_name(self.types[n])}: {got:.0f} мс ✔{note}")
            return
        sent = self._sent_scale.get(n, self.scale)
        other = "log" if sent == "lin" else "lin"
        if other not in self._tried.get(n, set()):
            if self.scale != other:
                self.scale = other                   # пульт кодирует время по другой шкале
                self.scale_changed = True
            self._send_time(n, other)
        else:
            self._target.pop(n, None)
            self.reports.append(f"FX{n}: пульт показывает {got:.0f} мс вместо {expected:.0f} — "
                                "проверьте эффект на пульте")

    def take_reports(self) -> list[str]:
        out, self.reports = self.reports, []
        return out
