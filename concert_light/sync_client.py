"""
sync_client.py — Подключение к Concert DAW (мастеру) и «общие часы».

* Поиск мастера: UDP-запрос «CDAW_DISCOVER» на порт 47811 широковещательно —
  Concert DAW отвечает своим именем и портом.
* Подключение по TCP (порт 47810), строки JSON. Обрыв связи — переподключение
  каждые 2 секунды само, без участия оператора.
* Часы: раз в секунду ping → pong с временем мастера. Разница часов берётся по
  замеру с самой короткой задержкой из последних 20 (как в NTP) — в локальной
  сети точность ~1 мс.
* position(): где сейчас трек на мастере — последняя присланная позиция плюс
  прошедшее с того момента время по часам мастера (если играет).
Всё сетевое — в своём потоке; GUI забирает события через poll().
"""
from __future__ import annotations

import json
import queue
import socket
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

DEFAULT_PORT = 47810
DISCOVERY_PORT = 47811
DISCOVER_MAGIC = b"CDAW_DISCOVER"


def local_ipv4s() -> list[str]:
    ips: set[str] = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ips.add(info[4][0])
    except OSError:
        pass
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ips.add(s.getsockname()[0])
        s.close()
    except OSError:
        pass
    return sorted(ip for ip in ips if not ip.startswith("127.") and ip != "0.0.0.0")


def discover(timeout: float = 1.5, port: int = DISCOVERY_PORT, extra: tuple = ()) -> list[dict]:
    """Найти Concert DAW в локальной сети. [{"name", "ip", "port"}]"""
    found: dict[str, dict] = {}
    targets = {"255.255.255.255", "127.0.0.1", *extra}
    for ip in local_ipv4s():
        parts = ip.split(".")
        targets.add(".".join(parts[:3] + ["255"]))
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        s.settimeout(0.2)
    except OSError:
        return []
    try:
        for t in targets:
            try:
                s.sendto(DISCOVER_MAGIC, (t, port))
            except OSError:
                pass
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                data, addr = s.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            try:
                msg = json.loads(data.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if msg.get("t") == "here":
                found[addr[0]] = {"name": msg.get("name", addr[0]), "ip": addr[0],
                                  "port": int(msg.get("port", DEFAULT_PORT))}
    finally:
        s.close()
    # 127.0.0.1 и внешний адрес того же компьютера — один и тот же мастер
    if len(found) > 1 and "127.0.0.1" in found:
        found.pop("127.0.0.1")
    return sorted(found.values(), key=lambda d: d["name"])


@dataclass
class MasterState:
    song_id: str = ""
    song_name: str = ""
    duration: float = 0.0
    pos: float = 0.0
    playing: bool = False
    countin: float = 0.0
    rate: float = 1.0
    s: float = 0.0               # время мастера, к которому относится pos
    seq: int = 0
    received: float = 0.0        # местное время получения
    # дополнительные поля Concert DAW 1.3+ (у старых версий — пустые)
    next_id: str = ""
    next_name: str = ""
    paused: bool = False
    playlist: str = ""
    show_running: bool = False   # секундомер концерта идёт
    show_elapsed: float = 0.0    # его показание на момент s (время мастера)
    autocue: bool = False


@dataclass
class ClockSample:
    rtt: float
    offset: float


class SyncClient:
    PING_INTERVAL = 1.0
    RECONNECT = 2.0

    def __init__(self, name: str = "") -> None:
        self.name = name or socket.gethostname()
        self.ip = ""
        self.port = DEFAULT_PORT
        self.server_name = ""
        self.connected = False
        self.error = ""
        self.state = MasterState()
        self.songs: list = []              # [{"name": плейлист, "songs": [{"id","name","duration","markers"}]}]
        self.offset = 0.0                  # время мастера − наше время
        self.rtt = 0.0
        self._samples: list[ClockSample] = []
        self._events: "queue.SimpleQueue[tuple]" = queue.SimpleQueue()
        self._sock: Optional[socket.socket] = None
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._synced = False

    # ── часы ────────────────────────────────────────────────────────────────
    @staticmethod
    def now() -> float:
        return time.monotonic()

    def master_now(self, local: Optional[float] = None) -> float:
        return (self.now() if local is None else local) + self.offset

    def position(self, local: Optional[float] = None) -> float:
        """Позиция трека на мастере прямо сейчас (сек; во время отсчёта — отрицательная)."""
        st = self.state
        if not st.playing:
            return st.pos
        return st.pos + (self.master_now(local) - st.s) * st.rate

    def show_elapsed(self, local: Optional[float] = None) -> Optional[float]:
        """Секундомер концерта Concert DAW прямо сейчас (сек) или None, если он не запущен."""
        st = self.state
        if not self.connected or not st.show_running:
            return None
        return max(0.0, st.show_elapsed + (self.master_now(local) - st.s))

    @property
    def clock_ok(self) -> bool:
        return self._synced

    def _on_pong(self, c: float, s: float) -> None:
        now = self.now()
        rtt = max(0.0, now - c)
        sample = ClockSample(rtt=rtt, offset=s - (c + rtt / 2))
        self._samples.append(sample)
        self._samples = self._samples[-20:]
        best = min(self._samples, key=lambda x: x.rtt)
        self.offset, self.rtt = best.offset, best.rtt
        self._synced = True

    # ── подключение ─────────────────────────────────────────────────────────
    def connect(self, ip: str, port: int = DEFAULT_PORT) -> None:
        self.disconnect()
        self.ip, self.port = ip.strip(), int(port)
        if not self.ip:
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, daemon=True, name="daw-sync")
        self._thread.start()

    def disconnect(self) -> None:
        self._stop.set()
        sock = self._sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                sock.close()
            except OSError:
                pass
        if self._thread is not None:
            self._thread.join(timeout=2.0)
        self._thread = None
        self._set_connected(False)

    def _set_connected(self, on: bool) -> None:
        if on != self.connected:
            self.connected = on
            if not on:
                self._synced = False
                self._samples = []
                self.state = MasterState()
            self._events.put(("connection", on))

    def _send(self, msg: dict) -> None:
        sock = self._sock
        if sock is None:
            return
        data = (json.dumps(msg, ensure_ascii=False) + "\n").encode("utf-8")
        with self._lock:
            try:
                sock.sendall(data)
            except OSError:
                pass

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                sock = socket.create_connection((self.ip, self.port), timeout=3.0)
            except OSError as exc:
                self.error = f"нет связи с {self.ip}:{self.port} ({exc.strerror or exc})"
                self._events.put(("error", self.error))
                if self._stop.wait(self.RECONNECT):
                    return
                continue
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(0.25)
            self._sock = sock
            self.error = ""
            self._send({"t": "hello", "app": "Concert Light", "name": self.name})
            for _ in range(5):                       # быстрые первые замеры часов
                self._send({"t": "ping", "c": self.now()})
            self._serve(sock)
            self._sock = None
            try:
                sock.close()
            except OSError:
                pass
            self._set_connected(False)
            if self._stop.wait(self.RECONNECT):
                return

    def _serve(self, sock: socket.socket) -> None:
        buf = b""
        next_ping = self.now() + 0.3
        while not self._stop.is_set():
            now = self.now()
            if now >= next_ping:
                self._send({"t": "ping", "c": now})
                next_ping = now + self.PING_INTERVAL
            try:
                chunk = sock.recv(65536)
            except socket.timeout:
                continue
            except OSError:
                return
            if not chunk:
                return
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                self._handle(msg)

    def _handle(self, msg: dict) -> None:
        t = msg.get("t")
        if t == "pong":
            try:
                self._on_pong(float(msg["c"]), float(msg["s"]))
            except (KeyError, TypeError, ValueError):
                pass
        elif t == "hello":
            self.server_name = str(msg.get("name", ""))
            self._set_connected(True)
        elif t == "songs":
            self.songs = msg.get("playlists") or []
            self._events.put(("songs", self.songs))
        elif t == "state":
            prev = self.state
            st = MasterState(song_id=str(msg.get("song_id", "")), song_name=str(msg.get("song_name", "")),
                             duration=float(msg.get("duration", 0.0)), pos=float(msg.get("pos", 0.0)),
                             playing=bool(msg.get("playing")), countin=float(msg.get("countin", 0.0)),
                             rate=float(msg.get("rate", 1.0)), s=float(msg.get("s", 0.0)),
                             seq=int(msg.get("seq", 0)), received=self.now(),
                             next_id=str(msg.get("next_id", "")), next_name=str(msg.get("next_name", "")),
                             paused=bool(msg.get("paused", False)), playlist=str(msg.get("playlist", "")),
                             show_running=bool(msg.get("show_running", False)),
                             show_elapsed=float(msg.get("show_elapsed", 0.0) or 0.0),
                             autocue=bool(msg.get("autocue", False)))
            self.state = st
            if st.seq != prev.seq or st.song_id != prev.song_id or st.playing != prev.playing:
                self._events.put(("transport", st))

    def poll(self) -> list[tuple]:
        out = []
        while True:
            try:
                out.append(self._events.get_nowait())
            except queue.Empty:
                return out

    # ── удобства для GUI ────────────────────────────────────────────────────
    def find_song(self, song_id: str) -> Optional[dict]:
        for pl in self.songs:
            for sg in pl.get("songs", []):
                if sg.get("id") == song_id:
                    return sg
        return None

    def all_songs(self) -> list[tuple[str, dict]]:
        """[(название плейлиста, трек)] — для списка треков."""
        return [(pl.get("name", ""), sg) for pl in self.songs for sg in pl.get("songs", [])]
