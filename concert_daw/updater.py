"""
updater.py — Обновление программы из репозитория GitHub (без Qt: работает и из update.bat).

Как устроено
------------
* Версия программы и адрес репозитория — в version.py (VERSION, REPO, APP_DIR).
* Проверка: GitHub API «последний релиз» (releases/latest). Тег вида v1.2.0.
* Скачивание: архив программы из релиза (файл «<APP_DIR>-<версия>.zip»), а если его нет —
  архив исходников релиза (в нём папка программы ищется сама).
* Установка — ПОСЛЕ закрытия программы, отдельным процессом:
    1) ждём, пока старая программа закроется;
    2) копируем новые файлы поверх (папка .venv, настройки и ваши файлы не трогаются);
    3) ставим/обновляем библиотеки из requirements.txt;
    4) запускаем программу снова.
  Если что-то пошло не так — старые файлы возвращаются из резервной копии (папка _backup).
* Только HTTPS и только github.com — никаких других адресов.

Командная строка (это использует update.bat):
    python updater.py --check            — показать, есть ли новая версия
    python updater.py --update           — скачать и установить (программа должна быть закрыта)
    python updater.py --apply DIR PID    — внутренний шаг: поставить скачанное из DIR после выхода PID
"""
from __future__ import annotations

import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path
from typing import Optional

from version import APP_DIR, APP_NAME, REPO, VERSION

HERE = Path(__file__).resolve().parent
API = "https://api.github.com"
KEEP = {".venv", "_backup", "__pycache__", ".git"}        # это при обновлении не трогаем
TIMEOUT = 15


def parse_version(text: str) -> tuple:
    """«v1.2.0», «1.2», «V1.0» → (1, 2, 0). Непонятное → (0,)."""
    m = re.search(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?", str(text or ""))
    if not m:
        return (0,)
    return tuple(int(x or 0) for x in m.groups())


def is_newer(remote: str, local: str = VERSION) -> bool:
    return parse_version(remote) > parse_version(local)


def _request(url: str, accept: str = "application/vnd.github+json"):
    if not url.startswith("https://") or not re.match(r"https://([a-z0-9-]+\.)*github(usercontent)?\.com/", url):
        raise ValueError(f"недопустимый адрес: {url}")
    req = urllib.request.Request(url, headers={"Accept": accept, "User-Agent": f"{APP_NAME}-updater"})
    return urllib.request.urlopen(req, timeout=TIMEOUT, context=ssl.create_default_context())


class UpdateInfo:
    def __init__(self, version: str, notes: str, url: str, page: str, asset: bool):
        self.version, self.notes, self.url, self.page, self.asset = version, notes, url, page, asset

    @property
    def newer(self) -> bool:
        return is_newer(self.version)


def check(repo: str = REPO) -> UpdateInfo:
    """Последний релиз репозитория. Ошибка сети/нет релизов — исключение с понятным текстом."""
    if not repo or "/" not in repo:
        raise RuntimeError("адрес репозитория не задан (version.py → REPO)")
    try:
        with _request(f"{API}/repos/{repo}/releases/latest") as r:
            rel = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return _from_tags(repo)            # релизов нет — берём самую новую метку версии (тег)
        raise RuntimeError(f"GitHub ответил ошибкой {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f"нет связи с GitHub ({getattr(exc, 'reason', exc)})") from None
    tag = str(rel.get("tag_name", ""))
    asset_url = ""
    for a in rel.get("assets") or []:
        name = str(a.get("name", ""))
        if name.lower().startswith(APP_DIR.lower()) and name.lower().endswith(".zip"):
            asset_url = str(a.get("browser_download_url", ""))
            break
    url = asset_url or str(rel.get("zipball_url", ""))
    return UpdateInfo(tag.lstrip("vV"), str(rel.get("body") or ""), url, str(rel.get("html_url", "")),
                      bool(asset_url))


def _from_tags(repo: str) -> UpdateInfo:
    """Версии по меткам (тегам) vX.Y.Z — если на GitHub не оформлен «релиз»."""
    try:
        with _request(f"{API}/repos/{repo}/tags?per_page=100") as r:
            tags = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        raise RuntimeError(f"GitHub ответил ошибкой {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f"нет связи с GitHub ({getattr(exc, 'reason', exc)})") from None
    names = [str(t.get("name", "")) for t in tags if re.match(r"^[vV]?\d+(\.\d+)*$", str(t.get("name", "")))]
    if not names:
        return _from_branch(repo)              # ни релизов, ни меток — версия из основной ветки
    best = max(names, key=parse_version)
    return UpdateInfo(best.lstrip("vV"), "", f"{API}/repos/{repo}/zipball/{best}",
                      f"https://github.com/{repo}/tree/{best}", False)


def _raw(repo: str, path: str, branch: str = "main") -> str:
    with _request(f"https://raw.githubusercontent.com/{repo}/{branch}/{path}", accept="text/plain") as r:
        return r.read().decode("utf-8", "replace")


def _from_branch(repo: str, branch: str = "main") -> UpdateInfo:
    """Версия из version.py в основной ветке репозитория + описание из CHANGELOG.md."""
    try:
        text = _raw(repo, f"{APP_DIR}/version.py", branch)
    except urllib.error.HTTPError as exc:
        raise RuntimeError("в репозитории ещё нет опубликованных версий" if exc.code == 404
                           else f"GitHub ответил ошибкой {exc.code}") from None
    except (urllib.error.URLError, OSError) as exc:
        raise RuntimeError(f"нет связи с GitHub ({getattr(exc, 'reason', exc)})") from None
    m = re.search(r'^VERSION\s*=\s*["\']([^"\']+)["\']', text, re.M)
    if not m:
        raise RuntimeError("не удалось прочитать версию в репозитории")
    notes = ""
    try:
        log = _raw(repo, "CHANGELOG.md", branch)
        part = re.split(r"(?m)^## ", log)
        notes = ("## " + part[1]).strip() if len(part) > 1 else ""
    except Exception:
        pass
    return UpdateInfo(m.group(1), notes, f"{API}/repos/{repo}/zipball/{branch}",
                      f"https://github.com/{repo}", False)


def download(info: UpdateInfo, progress=None) -> Path:
    """Скачать и распаковать во временную папку. Возвращает папку с файлами программы."""
    work = Path(tempfile.mkdtemp(prefix=f"{APP_DIR}_update_"))
    zpath = work / "update.zip"
    with _request(info.url, accept="application/octet-stream") as r, open(zpath, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        done = 0
        while True:
            chunk = r.read(65536)
            if not chunk:
                break
            f.write(chunk)
            done += len(chunk)
            if progress:
                progress(done, total)
    out = work / "unpacked"
    with zipfile.ZipFile(zpath) as z:
        for name in z.namelist():                      # защита от путей вида ../../
            target = (out / name).resolve()
            if not str(target).startswith(str(out.resolve())):
                raise RuntimeError("архив обновления повреждён")
        z.extractall(out)
    src = find_app_dir(out)
    if src is None:
        raise RuntimeError(f"в архиве нет папки {APP_DIR}")
    return src


def find_app_dir(root: Path) -> Optional[Path]:
    """Папка программы в распакованном архиве: <root>/<APP_DIR>, <root>/*/<APP_DIR> или сам root."""
    for cand in [root / APP_DIR] + [p / APP_DIR for p in root.iterdir() if p.is_dir()] + [root]:
        if (cand / "main.py").exists() and (cand / "version.py").exists():
            return cand
    return None


def copy_tree(src: Path, dst: Path, backup: Optional[Path] = None) -> list[str]:
    """Новые файлы поверх старых (кроме KEEP). Заменённые — в backup. Возвращает список файлов."""
    changed = []
    for path in src.rglob("*"):
        rel = path.relative_to(src)
        if any(part in KEEP for part in rel.parts):
            continue
        target = dst / rel
        if path.is_dir():
            target.mkdir(parents=True, exist_ok=True)
            continue
        if backup is not None and target.exists():
            (backup / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(target, backup / rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        changed.append(str(rel))
    return changed


def _venv_python(console: bool = True) -> str:
    name = ("python.exe" if console else "pythonw.exe") if sys.platform == "win32" else "python3"
    exe = HERE / ".venv" / ("Scripts" if sys.platform == "win32" else "bin") / name
    return str(exe) if exe.exists() else sys.executable


def _no_window() -> dict:
    return {"creationflags": 0x08000000} if sys.platform == "win32" else {}


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if sys.platform == "win32":
        try:
            out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"], capture_output=True, text=True,
                                 timeout=5, **_no_window()).stdout
            return str(pid) in out
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def apply(src: Path, wait_pid: int = 0, restart: bool = True, log=print) -> bool:
    """Поставить скачанное обновление (вызывается отдельным процессом после выхода программы)."""
    for _ in range(120):                                  # до 60 с ждём, пока программа закроется
        if not _pid_alive(wait_pid):
            break
        time.sleep(0.5)
    backup = HERE / "_backup"
    if backup.exists():
        shutil.rmtree(backup, ignore_errors=True)
    backup.mkdir()
    try:
        files = copy_tree(src, HERE, backup)
        log(f"Обновлено файлов: {len(files)}")
    except Exception as exc:
        log(f"Ошибка копирования: {exc} — возвращаю прежнюю версию")
        copy_tree(backup, HERE)
        return False
    req = HERE / "requirements.txt"
    if req.exists():
        log("Обновляю библиотеки...")
        subprocess.run([_venv_python(), "-m", "pip", "install", "-r", str(req), "-q",
                        "--disable-pip-version-check"], **_no_window())
    for parent in src.parents:                            # временная папка загрузки больше не нужна
        if parent.name.startswith(f"{APP_DIR}_update_"):
            shutil.rmtree(parent, ignore_errors=True)
            break
    if restart:
        subprocess.Popen([_venv_python(console=False), str(HERE / "main.py")], cwd=str(HERE),
                         **_no_window())
    return True


def launch_apply(src: Path) -> None:
    """Из программы: запустить установку отдельным процессом и сразу выйти из программы."""
    subprocess.Popen([_venv_python(console=False), str(HERE / "updater.py"), "--apply", str(src),
                      str(os.getpid())], cwd=str(HERE), **_no_window())


def main(argv: list[str]) -> int:
    if len(argv) >= 2 and argv[1] == "--apply":
        src = Path(argv[2])
        pid = int(argv[3]) if len(argv) > 3 else 0
        log_path = HERE / "update.log"

        def log(msg):
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}\n")
        return 0 if apply(src, pid, restart=True, log=log) else 1
    print(f"{APP_NAME} {VERSION}  (репозиторий {REPO})")
    try:
        info = check()
    except Exception as exc:
        print(f"Проверка не удалась: {exc}")
        return 1
    if not info.newer:
        print(f"Установлена последняя версия ({VERSION}).")
        return 0
    print(f"Доступна версия {info.version}.\n{info.notes}\n")
    if len(argv) >= 2 and argv[1] == "--update":
        print("Скачиваю...")
        src = download(info, lambda d, t: None)
        ok = apply(src, 0, restart=False)
        print("Готово! Запускайте программу." if ok else "Обновление не удалось, оставлена прежняя версия.")
        return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
