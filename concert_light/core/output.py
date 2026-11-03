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

import ast
import datetime
import re
import time

try:
    from .udmx import UDMX, driver_hint, fresh_usb_context
    _USB_OK = True
    _USB_ERR = ""
except Exception as e:  # нет pyusb / libusb
    UDMX = None
    _USB_OK = False
    _USB_ERR = str(e)


def readable_error(exc):
    """Текст ошибки USB по-человечески: libusb0 на русской Windows пишет текст байтами в cp1251
    (b'...\\xcf\\xf0...') — раскодируем и убираем служебную приставку."""
    text = f"{exc.__class__.__name__}: {exc}"
    m = re.search(r"b'(.*)'", text, re.S)
    if m:
        try:
            raw = ast.literal_eval("b'" + m.group(1) + "'")
            msg = raw.decode("cp1251", "replace")
        except Exception:
            msg = m.group(1)
        msg = re.sub(r"libusb0-dll:err \[\w+\]\s*", "", msg)
        msg = msg.replace("sending control message failed, win error:", "").strip()
        text = text[:m.start()] + msg + text[m.end():]
    return " ".join(text.replace("[Errno None]", "").split())


# что пишет Windows (libusb0) при сбоях uDMX
_GONE_WORDS = ("несуществующее устройство", "does not exist", "no such device", "not found",
               "не удается найти")
_STUCK_WORDS = ("pipe", "не опознает команду", "does not recognize the command",
                "устройство не работает", "is not functioning")


class Output:
    RETRY_SEC = 2.0
    FAST_RETRY_SEC = 0.5     # сразу после потери uDMX ищем его чаще (он возвращается за 1–2 с)
    FAST_RETRY_WINDOW = 10.0
    SUMMARY_SEC = 600.0      # раз в 10 минут — сводка сбоев в журнал (повторы там прореживаются)
    FAIL_LIMIT = 10          # ошибок подряд до переподключения
    BACKOFF = 0.2            # пауза после ошибки посылки, с
    PIPE_RESET = 1           # прошивка зависла — сброс порта сразу (сама она не отвисает)
    RESET_PAUSE = 0.15       # пауза после сброса порта, с
    FRESH_CTX_SEC = 5.0      # uDMX не находится столько секунд — новый контекст libusb
    FULL_REFRESH_SEC = 5.0   # раз в N секунд переслать всё (на случай сбоя внутри uDMX)
    GENTLE_RATE = 10         # бережный режим: не чаще 10 раз в секунду,
    GENTLE_CHUNK = 32        # кусками по 32 канала
    GENTLE_GAP = 0.003       # с паузой 3 мс между кусками

    def __init__(self, enabled=True, rate=20, log_path=None, gentle=False):
        self.enabled = enabled
        self.rate = rate
        self.gentle = gentle
        self._summary_at = 0.0
        self._summary_base = (0, 0)
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
            self._apply_gentle()
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
            self._log(f"подключён, библиотека USB: {self.dev.backend}"
                      + (", бережный режим" if self.gentle else ""))
            if self.hint:
                self._log("подсказка: " + self.hint)
        except Exception as e:
            self.dev = None
            self.connected = False
            self.status = f"uDMX не найден ({readable_error(e)})"
            if self._lost_at is not None:
                self._log(f"переподключение не удалось: {readable_error(e)}")

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

    def set_gentle(self, on):
        on = bool(on)
        if on != self.gentle:
            self._log(f"бережный режим {'включён' if on else 'выключен'} вручную")
        self.gentle = on
        self._apply_gentle()

    def _apply_gentle(self):
        if self.dev is not None:
            self.dev.chunk = self.GENTLE_CHUNK if self.gentle else 128
            self.dev.gap = self.GENTLE_GAP if self.gentle else 0.0

    def reconnect(self):
        """Кнопка «Переподключить» в настройках."""
        if self.dev is not None:
            self._drop("по кнопке")
        else:
            self._last_try = 0.0

    # ------------------------------------------------------------------

    def send(self, universe):
        now = time.monotonic()
        self._summary(now)
        if self.dev is None:
            fast = self._lost_at is not None and now - self._lost_at < self.FAST_RETRY_WINDOW
            if now - self._last_try >= (self.FAST_RETRY_SEC if fast else self.RETRY_SEC):
                self._connect()
            if self.dev is None:
                return
        rate = min(self.rate or self.GENTLE_RATE, self.GENTLE_RATE) if self.gentle else self.rate
        if rate and now - self._last_send < 1.0 / rate:
            return
        if now < self._backoff_until:
            return
        self._last_send = now
        if now - self._last_full >= self.FULL_REFRESH_SEC * (2 if self.gentle else 1):
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
            self.status = f"uDMX подключён ({self.dev.backend})" + (", бережный режим" if self.gentle else "")
        except Exception as e:
            self._fails += 1
            self.stats["errors"] += 1
            err = readable_error(e)
            self.stats["last_error"] = err
            self._log(f"ошибка посылки: {err}")
            self._backoff_until = now + self.BACKOFF
            if self._is_gone(e, self.dev.backend) or self._fails >= self.FAIL_LIMIT:
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
            self._backoff_until = time.monotonic() + self.RESET_PAUSE
            self._fails = 0
            self._log("сброс USB-порта uDMX (прошивка не отвечает на посылки)")
        except Exception as e:
            self._log(f"сброс USB-порта не удался: {readable_error(e)}")
            self._drop(f"сброс не удался: {readable_error(e)}")

    def _summary(self, now):
        """Сводка за 10 минут: в журнале одинаковые строки прореживаются, а так видно частоту сбоев."""
        if not self._summary_at:
            self._summary_at = now
            return
        if now - self._summary_at < self.SUMMARY_SEC:
            return
        resets = self.stats["resets"] - self._summary_base[0]
        drops = self.stats["reconnects"] - self._summary_base[1]
        if resets or drops:
            mins = (now - self._summary_at) / 60
            self._log(f"за {mins:.0f} мин: сбросов порта {resets}, переподключений {drops}"
                      + (f" (в среднем сбой раз в {(now - self._summary_at) / max(1, resets + drops):.0f} с)"))
        self._summary_at = now
        self._summary_base = (self.stats["resets"], self.stats["reconnects"])

    @staticmethod
    def _is_pipe(exc):
        """Прошивка «зависла»: отвечает отказом (Pipe error / «устройство не опознает команду»)."""
        text = readable_error(exc).lower()
        return (getattr(exc, "errno", None) == 32 or getattr(exc, "backend_error_code", None) == -9
                or any(w in text for w in _STUCK_WORDS))

    @staticmethod
    def _is_gone(exc, backend=""):
        """Устройство пропало из системы — переподключаться сразу, не ждать."""
        code = getattr(exc, "backend_error_code", None)
        errno = getattr(exc, "errno", None)
        text = readable_error(exc).lower()
        if any(w in text for w in _GONE_WORDS):
            return True
        if backend == "libusb0":         # у libusb0 свои коды (-5 там — просто ошибка ввода-вывода)
            return errno == 19
        return code in (-4, -5) or errno in (2, 19)

    def close(self):
        if self.dev is not None:
            try:
                self.dev.blackout()
                self.dev.close()
            except Exception:
                pass
            self.dev = None
