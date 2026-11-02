"""Вывод DMX на uDMX: устойчив к сбоям USB, сам переподключается, ведёт журнал.

Как обрабатываются ошибки:
  * одиночная ошибка посылки — НЕ обрыв: несошедшие каналы уйдут в следующем кадре;
  * после ошибки — пауза BACKOFF: прошивке uDMX даём прийти в себя, а не «добиваем» её;
  * несколько Errno 32 (Pipe error — прошивка отвечает отказом) подряд — сброс USB-порта uDMX;
  * обрыв считается только после FAIL_LIMIT ошибок подряд —
    тогда устройство переоткрывается, и все каналы пересылаются заново;
  * uDMX пропал из системы и не находится — через 5 с пробуем новый контекст libusb
    (раньше в этом случае помогал только перезапуск программы);
  * частота отправки ограничена (rate, по умолчанию 30 Гц) — медленному USB uDMX так легче;
  * всё пишется в журнал %APPDATA%/ConcertLight/udmx.log — по нему видно причину обрывов.
"""

import datetime
import time

try:
    from .udmx import UDMX, driver_hint, fresh_usb_context
    _USB_OK = True
    _USB_ERR = ""
except Exception as e:  # нет pyusb / libusb
    UDMX = None
    _USB_OK = False
    _USB_ERR = str(e)


class Output:
    RETRY_SEC = 2.0
    FAIL_LIMIT = 10          # ошибок подряд до переподключения
    BACKOFF = 0.2            # пауза после ошибки посылки, с
    PIPE_RESET = 3           # столько Errno 32 подряд — сброс USB-порта
    FRESH_CTX_SEC = 5.0      # uDMX не находится столько секунд — новый контекст libusb
    FULL_REFRESH_SEC = 5.0   # раз в N секунд переслать всё (на случай сбоя внутри uDMX)

    def __init__(self, enabled=True, rate=20, log_path=None):
        self.enabled = enabled
        self.rate = rate
        self.log_path = log_path
        self.dev = None
        self.status = "Нет устройства"
        self.connected = False
        self.limit = 512              # сколько каналов занято патчем (меньше — быстрее)
        self.stats = {"sent": 0, "errors": 0, "reconnects": 0, "resets": 0, "last_error": "",
                      "backend": "", "hint": ""}
        self.hint = ""
        self._last_try = 0.0
        self._pipe = 0
        self._backoff_until = 0.0
        self._lost_at = None          # когда пропал uDMX (для нового контекста libusb)
        self._fresh_done = False
        self._connected_at = 0.0
        self._sent_since = 0
        self._last_send = 0.0
        self._last_full = 0.0
        self._fails = 0
        self._log_count = {}
        if not _USB_OK:
            self.status = f"pyusb недоступен: {_USB_ERR}"

    # ------------------------------------------------------------------

    def _log(self, msg):
        if not self.log_path:
            return
        n = self._log_count.get(msg, 0) + 1          # одинаковые ошибки — не чаще разумного
        self._log_count[msg] = n
        if n > 3 and n not in (10, 100, 1000, 10000):
            return
        try:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                rep = f" (повтор {n})" if n > 1 else ""
                f.write(f"{datetime.datetime.now():%Y-%m-%d %H:%M:%S}  {msg}{rep}\n")
        except OSError:
            pass

    def _connect(self):
        self._last_try = time.monotonic()
        if not _USB_OK or not self.enabled:
            return
        now = time.monotonic()
        if (self._lost_at is not None and not self._fresh_done
                and now - self._lost_at >= self.FRESH_CTX_SEC):
            self._fresh_done = True
            ok = fresh_usb_context()
            self._log("uDMX не находится — пробую новый контекст libusb" + ("" if ok else " (не удалось)"))
        try:
            self.dev = UDMX()
            self.dev.invalidate(self.limit)          # первая посылка — все каналы
            self.connected = True
            self._fails = 0
            self._pipe = 0
            self._last_full = now
            self._connected_at = now
            self._sent_since = 0
            if self._lost_at is not None:
                self._log(f"uDMX снова найден через {now - self._lost_at:.0f} с")
            self._lost_at = None
            self._fresh_done = False
            self.stats["backend"] = self.dev.backend
            self.hint = self.stats["hint"] = driver_hint(self.dev.backend)
            self.status = f"uDMX подключён ({self.dev.backend})"
            self._log(f"подключён, библиотека USB: {self.dev.backend}")
            if self.hint:
                self._log("подсказка: " + self.hint)
        except Exception as e:
            self.dev = None
            self.connected = False
            self.status = f"uDMX не найден ({e.__class__.__name__}: {e})"
            if self._lost_at is not None:
                self._log(f"переподключение не удалось: {e.__class__.__name__}: {e}")

    def _drop(self, reason):
        self.stats["reconnects"] += 1
        lived = time.monotonic() - self._connected_at if self._connected_at else 0
        self._log(f"переподключение: {reason} (связь держалась {lived:.0f} с, посылок {self._sent_since})")
        if self._lost_at is None:
            self._lost_at = time.monotonic()
        try:
            self.dev.close()
        except Exception:
            pass
        self.dev = None
        self.connected = False
        self.status = f"Связь с uDMX потеряна: {reason} — переподключаюсь…"
        self._last_try = 0.0                          # пробуем сразу, без паузы

    def reconnect(self):
        """Кнопка «Переподключить» в настройках."""
        if self.dev is not None:
            self._drop("по кнопке")
        else:
            self._last_try = 0.0

    # ------------------------------------------------------------------

    def send(self, universe):
        now = time.monotonic()
        if self.dev is None:
            if now - self._last_try >= self.RETRY_SEC:
                self._connect()
            if self.dev is None:
                return
        if self.rate and now - self._last_send < 1.0 / self.rate:
            return
        if now < self._backoff_until:
            return
        self._last_send = now
        if now - self._last_full >= self.FULL_REFRESH_SEC:
            self.dev.invalidate(self.limit)
            self._last_full = now
        try:
            if self.dev.update(universe, self.limit):
                self.stats["sent"] += 1
                self._sent_since += 1
            if self._fails:
                self._log(f"связь восстановилась после {self._fails} ошибок")
            self._fails = 0
            self._pipe = 0
            if not self.connected:
                self.connected = True
            self.status = f"uDMX подключён ({self.dev.backend})"
        except Exception as e:
            self._fails += 1
            self.stats["errors"] += 1
            err = f"{e.__class__.__name__}: {e}"
            self.stats["last_error"] = err
            self._log(f"ошибка посылки: {err}")
            self._backoff_until = now + self.BACKOFF
            if self._is_gone(e) or self._fails >= self.FAIL_LIMIT:
                self._drop(err)
                return
            self.status = f"uDMX: сбой посылки ({self._fails}) — {err}"
            if self._is_pipe(e):
                self._pipe += 1
                if self._pipe >= self.PIPE_RESET:
                    self._pipe = 0
                    self._reset_port()

    def _reset_port(self):
        """Сброс USB-порта uDMX: прошивка «зависла» и отвечает отказом на каждую посылку."""
        self.stats["resets"] += 1
        try:
            self.dev.reset()
            self.dev.invalidate(self.limit)
            self._backoff_until = time.monotonic() + 0.5   # даём uDMX переинициализироваться
            self._log("сброс USB-порта uDMX (повторяющийся Pipe error)")
        except Exception as e:
            self._log(f"сброс USB-порта не удался: {e.__class__.__name__}: {e}")
            self._drop(f"сброс не удался: {e}")

    @staticmethod
    def _is_pipe(exc):
        return getattr(exc, "errno", None) == 32 or getattr(exc, "backend_error_code", None) == -9 \
            or "pipe" in str(exc).lower()

    @staticmethod
    def _is_gone(exc):
        """Устройство выдернуто — переподключаться сразу, не ждать."""
        code = getattr(exc, "backend_error_code", None)
        errno = getattr(exc, "errno", None)
        text = str(exc).lower()
        return (code in (-4, -5) or errno in (2, 19) or "no such device" in text
                or "not found" in text)

    def close(self):
        if self.dev is not None:
            try:
                self.dev.blackout()
                self.dev.close()
            except Exception:
                pass
            self.dev = None
