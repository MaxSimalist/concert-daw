"""
sync_server.py — «Мастер-часы» Concert DAW для видео-программы (Concert Video) по сети.

Как это работает
----------------
* Concert DAW — ведущий (мастер). Видео-программа на другом компьютере подключается
  по TCP (порт 47810) и получает:
    - список плейлистов и треков (id, название, длительность, маркеры) — чтобы
      привязать к трекам сцены и видео;
    - состояние транспорта 10 раз в секунду и сразу при любом изменении
      (PLAY / PAUSE / STOP / перемотка / смена трека): какой трек, играет ли,
      позиция В ТРЕКЕ (то, что сейчас слышно из колонок, с учётом задержки
      звуковой карты; во время отсчёта — отрицательная).
* Часы двух компьютеров разные, поэтому видео-программа периодически шлёт ping,
  мастер отвечает своим временем (pong) — по этим парам считается разница часов
  с точностью до миллисекунды в локальной сети (как NTP).
* Поиск: видео-программа рассылает по сети UDP-запрос на порт 47811, мастер
  отвечает — IP вводить не нужно.
* Формат: строки JSON (UTF-8), одно сообщение — одна строка.
* Сетевые операции — в своих потоках. Главное окно только вызывает
  publish_state()/set_songs(): это мгновенно (очередь), звук не страдает.
"""
from __future__ import annotations

import json
import queue
import socket
import threading
import time
from typing import Optional

PROTOCOL = 1
DEFAULT_PORT = 47810
DISCOVERY_PORT = 47811
DISCOVER_MAGIC = b"CDAW_DISCOVER"
STATE_INTERVAL = 0.1          # сек: как часто слать состояние, если ничего не менялось


def encode(msg: dict) -> bytes:
    return (json.dumps(msg, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")


class _Client:
    def __init__(self, sock: socket.socket, addr) -> None:
        self.sock = sock
        self.addr = addr
        self.name = f"{addr[0]}"
        self.q: "queue.Queue[Optional[bytes]]" = queue.Queue(maxsize=200)
        self.alive = True

    def send(self, data: bytes) -> None:
        if not self.alive:
            return
        try:
            self.q.put_nowait(data)
        except queue.Full:            # клиент завис и не читает — отключаем
            self.close()

    def close(self) -> None:
        if self.alive:
            self.alive = False
            try:
                self.q.put_nowait(None)
            except queue.Full:
                pass
            try:
                self.sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self.sock.close()
            except OSError:
                pass


class SyncServer:
    def __init__(self, name: str = "") -> None:
        self.name = name or socket.gethostname()
        self.port = DEFAULT_PORT
        self.error = ""
        self._clients: list[_Client] = []
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []
        self._listen: Optional[socket.socket] = None
        self._udp: Optional[socket.socket] = None
        self._songs_msg: Optional[bytes] = None
        self._last_state: dict = {}
        self._last_sent = 0.0
        self.seq = 0

    # ── запуск / остановка ─────────────────────────────────────────────────
    @property
    def running(self) -> bool:
        return self._listen is not None

    def start(self, port: int = DEFAULT_PORT) -> bool:
        self.stop()
        self.port = int(port)
        self._stop.clear()
        try:
            ls = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            ls.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            ls.bind(("0.0.0.0", self.port))
            ls.listen(8)
            ls.settimeout(0.5)
            self._listen = ls
        except OSError as exc:
            self.error = f"порт {self.port} занят или недоступен: {exc}"
            self._listen = None
            return False
        try:
            us = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            us.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            us.bind(("0.0.0.0", DISCOVERY_PORT))
            us.settimeout(0.5)
            self._udp = us
        except OSError:
            self._udp = None          # поиск не работает — подключиться можно по IP
        self.error = ""
        self._spawn(self._accept_loop, "sync-accept")
        if self._udp is not None:
            self._spawn(self._discovery_loop, "sync-discovery")
        return True

    def stop(self) -> None:
        self._stop.set()
        for s in (self._listen, self._udp):
            if s is not None:
                try:
                    s.close()
                except OSError:
                    pass
        self._listen = self._udp = None
        with self._lock:
            clients, self._clients = self._clients, []
        for c in clients:
            c.close()
        for t in self._threads:
            t.join(timeout=1.0)
        self._threads = []

    def _spawn(self, fn, name: str, *args) -> None:
        t = threading.Thread(target=fn, args=args, daemon=True, name=name)
        t.start()
        self._threads.append(t)

    # ── клиенты ────────────────────────────────────────────────────────────
    @property
    def clients(self) -> list[str]:
        with self._lock:
            return [c.name for c in self._clients if c.alive]

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            ls = self._listen
            if ls is None:
                return
            try:
                sock, addr = ls.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.settimeout(None)
            client = _Client(sock, addr)
            with self._lock:
                self._clients.append(client)
            threading.Thread(target=self._writer, args=(client,), daemon=True, name="sync-writer").start()
            threading.Thread(target=self._reader, args=(client,), daemon=True, name="sync-reader").start()
            client.send(encode({"t": "hello", "app": "Concert DAW", "protocol": PROTOCOL, "name": self.name}))
            if self._songs_msg:
                client.send(self._songs_msg)
            if self._last_state:
                client.send(encode(dict(self._last_state, s=time.monotonic())))

    def _writer(self, c: _Client) -> None:
        while c.alive:
            data = c.q.get()
            if data is None:
                break
            try:
                c.sock.sendall(data)
            except OSError:
                break
        self._drop(c)

    def _reader(self, c: _Client) -> None:
        buf = b""
        while c.alive and not self._stop.is_set():
            try:
                chunk = c.sock.recv(4096)
            except OSError:
                break
            if not chunk:
                break
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                try:
                    msg = json.loads(line.decode("utf-8"))
                except (ValueError, UnicodeDecodeError):
                    continue
                self._handle(c, msg)
            if len(buf) > 1_000_000:
                break
        self._drop(c)

    def _handle(self, c: _Client, msg: dict) -> None:
        t = msg.get("t")
        if t == "ping":
            # ответ — сразу, с текущим временем мастера (для расчёта разницы часов)
            c.send(encode({"t": "pong", "c": msg.get("c"), "s": time.monotonic()}))
        elif t == "hello":
            app = str(msg.get("app") or "").replace("Concert ", "")
            c.name = (f"{app}: " if app else "") + f"{msg.get('name') or c.addr[0]} ({c.addr[0]})"
        elif t == "get_songs" and self._songs_msg:
            c.send(self._songs_msg)

    def _drop(self, c: _Client) -> None:
        c.close()
        with self._lock:
            if c in self._clients:
                self._clients.remove(c)

    def _discovery_loop(self) -> None:
        while not self._stop.is_set():
            us = self._udp
            if us is None:
                return
            try:
                data, addr = us.recvfrom(1024)
            except socket.timeout:
                continue
            except OSError:
                return
            if data.strip() == DISCOVER_MAGIC:
                reply = {"t": "here", "app": "Concert DAW", "name": self.name, "port": self.port,
                         "protocol": PROTOCOL}
                try:
                    us.sendto(json.dumps(reply, ensure_ascii=False).encode("utf-8"), addr)
                except OSError:
                    pass

    # ── публикация (GUI-поток) ─────────────────────────────────────────────
    def _broadcast(self, data: bytes) -> None:
        with self._lock:
            clients = list(self._clients)
        for c in clients:
            c.send(data)

    def set_songs(self, playlists: list) -> None:
        """Список плейлистов/треков. Отправляется сразу и каждому новому клиенту."""
        data = encode({"t": "songs", "playlists": playlists})
        if data != self._songs_msg:
            self._songs_msg = data
            self._broadcast(data)

    def publish_state(self, song_id: str, song_name: str, duration: float, pos: float,
                      playing: bool, counting_in: float = 0.0, extra: Optional[dict] = None) -> None:
        """Вызывать часто (каждый кадр GUI). Отправка — при изменении сразу, иначе 10 раз/с.
        extra — дополнительные поля (следующий трек, пауза, секундомер концерта…): старые
        клиенты их просто не читают, поэтому версия протокола та же."""
        now = time.monotonic()
        state = {"t": "state", "song_id": song_id or "", "song_name": song_name or "",
                 "duration": round(float(duration), 4), "pos": round(float(pos), 4),
                 "playing": bool(playing), "countin": round(float(counting_in), 3), "rate": 1.0}
        if extra:
            state.update(extra)
        last = self._last_state
        changed = (not last or last.get("song_id") != state["song_id"] or last.get("playing") != playing
                   or last.get("duration") != state["duration"]
                   or any(last.get(k) != state.get(k) for k in ("next_id", "paused", "playlist",
                                                                  "show_running", "song_name")))
        if not changed and last:
            # перемотка: позиция ушла от ожидаемой больше чем на 0.15 с
            expect = last["pos"] + ((now - last["s"]) if last.get("playing") else 0.0)
            changed = abs(expect - state["pos"]) > 0.15
        if not changed and now - self._last_sent < STATE_INTERVAL:
            return
        if changed:
            self.seq += 1
        state["seq"] = self.seq
        state["s"] = now
        self._last_state = state
        self._last_sent = now
        if self._clients:
            self._broadcast(encode(state))
