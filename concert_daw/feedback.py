"""
feedback.py — Идеи и сообщения об ошибках от пользователей (без Qt).

Никакой скрытой слежки: программа ничего не отправляет сама. Человек пишет текст,
видит ВЕСЬ отчёт целиком (что именно уйдёт) и сам выбирает, как отправить:
* GitHub — открывается страница «новое обращение» (issue) в репозитории программы с уже
  заполненным текстом; нужен бесплатный аккаунт GitHub;
* e-mail — открывается почтовая программа с готовым письмом (если адрес указан в version.py);
* скопировать — текст в буфер обмена (отправить в мессенджер, по почте вручную и т.п.).

В отчёт по желанию добавляются: версия программы, Windows, Python/PySide6 и последние
ошибки из журнала error.log. Имя компьютера и пути к вашим файлам вырезаются.
"""
from __future__ import annotations

import os
import platform
import re
import sys
import urllib.parse
from pathlib import Path

from version import APP_NAME, REPO, VERSION

try:
    from version import FEEDBACK_EMAIL
except ImportError:  # старый version.py
    FEEDBACK_EMAIL = ""

KINDS = {"bug": "Ошибка", "idea": "Идея"}
MAX_URL = 7500             # браузеры и GitHub не любят адреса длиннее ~8 КБ


def system_info() -> str:
    lines = [f"Программа: {APP_NAME} {VERSION}",
             f"Windows: {platform.platform()}" if sys.platform == "win32" else f"Система: {platform.platform()}",
             f"Python: {platform.python_version()} ({platform.architecture()[0]})"]
    try:
        import PySide6
        lines.append(f"PySide6: {PySide6.__version__}")
    except Exception:
        pass
    return "\n".join(lines)


def anonymize(text: str) -> str:
    """Убрать личное: имя пользователя Windows и компьютера, пути к домашней папке."""
    home = str(Path.home())
    user = os.environ.get("USERNAME") or os.environ.get("USER") or ""
    host = platform.node()
    out = text.replace(home, "~")
    for secret, mask in ((user, "<пользователь>"), (host, "<компьютер>")):
        if secret and len(secret) >= 3:
            out = re.sub(re.escape(secret), mask, out, flags=re.IGNORECASE)
    return out


def recent_errors(log_path: Path, limit: int = 3000) -> str:
    """Последние записи журнала ошибок (хвост файла)."""
    try:
        text = Path(log_path).read_text("utf-8", errors="replace")
    except OSError:
        return ""
    blocks = text.split("\n===== ")
    tail = "\n===== ".join(blocks[-3:]).strip()
    return tail[-limit:]


def build_report(kind: str, title: str, text: str, with_system: bool = True, log: str = "") -> tuple[str, str]:
    """(заголовок, текст) обращения в Markdown."""
    label = KINDS.get(kind, "Отзыв")
    first = text.strip().splitlines()[0][:80] if text.strip() else ""
    head = f"[{label}] {title.strip() or first or label}"
    parts = [text.strip() or "(без описания)"]
    if with_system:
        parts.append("---\n**Система**\n```\n" + system_info() + "\n```")
    if log.strip():
        parts.append("**Журнал ошибок (последние записи)**\n```\n" + log.strip() + "\n```")
    return anonymize(head), anonymize("\n\n".join(parts))


def issue_url(title: str, body: str) -> str:
    """Страница «новое обращение» на GitHub с заполненным текстом (длинный журнал — укорачиваем)."""
    base = f"https://github.com/{REPO}/issues/new"
    while True:
        url = base + "?" + urllib.parse.urlencode({"title": title, "body": body})
        if len(url) <= MAX_URL or len(body) < 200:
            return url
        body = body[: int(len(body) * 0.8)] + "\n…(обрезано — полный текст скопирован в буфер обмена)"


def mailto_url(title: str, body: str) -> str:
    if not FEEDBACK_EMAIL:
        return ""
    q = urllib.parse.urlencode({"subject": f"{APP_NAME}: {title}", "body": body}, quote_via=urllib.parse.quote)
    url = f"mailto:{FEEDBACK_EMAIL}?{q}"
    return url[:MAX_URL]
