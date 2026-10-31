"""
output.py — Запись и трансляция эфира через ffmpeg (как «Начать запись» /
«Начать трансляцию» в OBS).

* Кадры эфира рисуются с холста сцены (плюс слои перехода/затемнения) в
  картинку нужного размера и отдаются ffmpeg по трубе (stdin) отдельным потоком —
  интерфейс не тормозит; если компьютер не успевает, лишние кадры пропускаются
  (счётчик «пропущено»).
* Видео: H.264 (libx264, preset veryfast), звук: устройство Windows (DirectShow) —
  например, линейный вход с пульта. Запись — MKV (не портится при сбое) или MP4,
  трансляция — RTMP (YouTube, VK Видео, Twitch, свой сервер).
* ffmpeg берётся из пакета imageio-ffmpeg (ставится install.bat) или из PATH.
"""
from __future__ import annotations

import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from typing import Optional

STREAM_SERVICES = {
    "YouTube": "rtmp://a.rtmp.youtube.com/live2",
    "VK Видео": "",                     # адрес и ключ — в VK Видео → «Начать трансляцию» → ПО для трансляций
    "Twitch": "rtmp://live.twitch.tv/app",
    "Свой сервер": "",
}


def ffmpeg_exe() -> Optional[str]:
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which("ffmpeg")


def _no_window() -> dict:
    if sys.platform == "win32":
        return {"creationflags": 0x08000000}       # CREATE_NO_WINDOW — без чёрного окна консоли
    return {}


def audio_devices() -> list[str]:
    """Устройства записи звука для ffmpeg (Windows / DirectShow)."""
    exe = ffmpeg_exe()
    if not exe or sys.platform != "win32":
        return []
    try:
        res = subprocess.run([exe, "-hide_banner", "-list_devices", "true", "-f", "dshow", "-i", "dummy"],
                             capture_output=True, timeout=10, **_no_window())
    except Exception:
        return []
    return parse_dshow(res.stderr.decode("utf-8", "replace"))


def parse_dshow(text: str) -> list[str]:
    """Разбор списка устройств ffmpeg: новый формат ("Имя" (audio)) и старый (раздел audio devices)."""
    found: list[str] = []
    section = ""
    for line in text.splitlines():
        if "DirectShow audio devices" in line:
            section = "audio"
            continue
        if "DirectShow video devices" in line:
            section = "video"
            continue
        if "Alternative name" in line:
            continue
        m = re.search(r'"([^"]+)"\s*\((audio|video)\)', line)
        if m:
            if m.group(2) == "audio" and m.group(1) not in found:
                found.append(m.group(1))
            continue
        m = re.search(r'\]\s+"([^"]+)"\s*$', line)
        if m and section == "audio" and m.group(1) not in found:
            found.append(m.group(1))
    return found


def build_command(exe: str, kind: str, target: str, w: int, h: int, fps: int, bitrate: int,
                  audio_device: str = "") -> list[str]:
    cmd = [exe, "-hide_banner", "-loglevel", "error", "-y",
           "-use_wallclock_as_timestamps", "1",
           "-f", "rawvideo", "-pix_fmt", "bgra", "-s", f"{w}x{h}", "-r", str(fps), "-i", "-"]
    if audio_device and sys.platform == "win32":
        cmd += ["-f", "dshow", "-i", f"audio={audio_device}"]
    cmd += ["-map", "0:v"]
    if audio_device and sys.platform == "win32":
        cmd += ["-map", "1:a"]
    cmd += ["-c:v", "libx264", "-preset", "veryfast", "-pix_fmt", "yuv420p", "-r", str(fps),
            "-b:v", f"{bitrate}k", "-maxrate", f"{bitrate}k", "-bufsize", f"{bitrate * 2}k",
            "-g", str(fps * 2)]
    if kind == "stream":
        cmd += ["-tune", "zerolatency"]
    if audio_device and sys.platform == "win32":
        cmd += ["-c:a", "aac", "-b:a", "160k", "-ar", "48000"]
    else:
        cmd += ["-an"]
    if kind == "stream":
        cmd += ["-f", "flv", target]
    else:
        if target.lower().endswith(".mp4"):
            cmd += ["-movflags", "+faststart"]
        cmd += [target]
    return cmd


class FfmpegOutput:
    """Один выход (запись или трансляция). Кадры — bytes в формате BGRA w×h."""

    def __init__(self, kind: str, target: str, w: int, h: int, fps: int, bitrate: int, audio_device: str = ""):
        self.kind, self.target = kind, target
        self.w, self.h, self.fps = w, h, fps
        self.bitrate, self.audio_device = bitrate, audio_device
        self.proc: Optional[subprocess.Popen] = None
        self.q: "queue.Queue[Optional[bytes]]" = queue.Queue(maxsize=max(4, fps // 2))
        self.frames = 0
        self.dropped = 0
        self.error = ""
        self.started = 0.0
        self._log: list[str] = []
        self._writer: Optional[threading.Thread] = None

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    @property
    def seconds(self) -> float:
        return time.monotonic() - self.started if self.started else 0.0

    def start(self) -> bool:
        exe = ffmpeg_exe()
        if not exe:
            self.error = "не найден ffmpeg — запустите install.bat (пакет imageio-ffmpeg)"
            return False
        cmd = build_command(exe, self.kind, self.target, self.w, self.h, self.fps, self.bitrate,
                            self.audio_device)
        try:
            self.proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                         stderr=subprocess.PIPE, **_no_window())
        except OSError as exc:
            self.error = f"ffmpeg не запустился: {exc}"
            return False
        self.started = time.monotonic()
        threading.Thread(target=self._read_log, daemon=True, name="ffmpeg-log").start()
        self._writer = threading.Thread(target=self._write, daemon=True, name="ffmpeg-in")
        self._writer.start()
        return True

    def push(self, frame: bytes) -> None:
        if not self.running:
            return
        try:
            self.q.put_nowait(frame)
        except queue.Full:
            self.dropped += 1

    def _write(self) -> None:
        proc = self.proc
        while proc is not None:
            data = self.q.get()
            if data is None:
                break
            try:
                proc.stdin.write(data)
                self.frames += 1
            except (OSError, ValueError):
                self.error = self.error or self.last_log() or "ffmpeg прервал работу"
                break
        try:
            proc.stdin.close()
        except Exception:
            pass

    def _read_log(self) -> None:
        proc = self.proc
        if proc is None or proc.stderr is None:
            return
        for raw in proc.stderr:
            line = raw.decode("utf-8", "replace").strip()
            if line:
                self._log.append(line)
                self._log = self._log[-30:]

    def last_log(self) -> str:
        return self._log[-1] if self._log else ""

    def stop(self) -> None:
        if self.proc is None:
            return
        try:
            self.q.put(None, timeout=1.0)
        except queue.Full:
            pass
        if self._writer is not None:
            self._writer.join(timeout=3.0)
        try:
            self.proc.wait(timeout=8.0)        # ffmpeg дописывает файл
        except subprocess.TimeoutExpired:
            self.proc.kill()
        if self.proc.returncode not in (0, None) and not self.error:
            self.error = self.last_log() or f"ffmpeg завершился с кодом {self.proc.returncode}"
        self.proc = None
