"""Папка данных программы (%APPDATA%\\ConcertLight) и настройки (settings.json)."""

import json
import os
import sys
from pathlib import Path


def app_data_dir() -> Path:
    base = os.environ.get("APPDATA") if sys.platform == "win32" else None
    root = Path(base) if base else Path.home() / ".config"
    return root / "ConcertLight"


def shows_dir() -> Path:
    """Папка для шоу по умолчанию: Документы\\Concert Light."""
    return Path.home() / "Documents" / "Concert Light"


class Settings:
    DEFAULTS = {
        "last_show": "",
        "update_checked": 0.0,
        "update_auto": True,
        "daw_ip": "",
        "daw_port": 47810,
        "daw_name": "",
        "auto_connect": True,
        "udmx_enabled": True,
        "udmx_rate": 20,
        "udmx_gentle": False,
    }

    def __init__(self, path: Path = None):
        self.path = Path(path) if path else app_data_dir() / "settings.json"
        self.data = dict(self.DEFAULTS)
        try:
            with open(self.path, encoding="utf-8") as f:
                stored = json.load(f)
            if isinstance(stored, dict):
                self.data.update({k: v for k, v in stored.items() if k in self.DEFAULTS})
        except (OSError, ValueError):
            pass

    def __getattr__(self, name):
        data = self.__dict__.get("data", {})
        if name in data:
            return data[name]
        raise AttributeError(name)

    def set(self, **kw):
        self.data.update(kw)
        self.save()

    def save(self):
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_suffix(".tmp")
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, ensure_ascii=False, indent=1)
            os.replace(tmp, self.path)
        except OSError as e:
            print(f"[settings] не сохранено: {e}")
