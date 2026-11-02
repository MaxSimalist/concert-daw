"""Типы приборов (fixture types) и запатченные приборы."""

import glob
import json
import os

# Группы атрибутов для интерфейса программера (порядок = порядок в UI)
ATTR_GROUPS = [
    ("Яркость", ["dimmer"]),
    ("Цвет", ["red", "green", "blue", "white", "amber", "uv", "cto", "color_wheel", "color_macro"]),
    ("Позиция", ["pan", "tilt", "pt_speed"]),
    ("Луч", ["shutter", "strobe", "gobo", "gobo_rotation", "prism", "focus", "zoom", "iris", "frost"]),
]
OTHER_GROUP = "Прочее"

ATTR_LABELS = {
    "dimmer": "Диммер",
    "red": "Красный", "green": "Зелёный", "blue": "Синий", "white": "Белый",
    "amber": "Янтарный", "uv": "УФ", "cto": "CTO", "color_wheel": "Колесо цветов",
    "color_macro": "Цвет. макро",
    "pan": "Pan", "tilt": "Tilt", "pt_speed": "Скорость P/T",
    "shutter": "Шторка", "strobe": "Строб", "gobo": "Гобо", "gobo_rotation": "Вращ. гобо",
    "prism": "Призма", "focus": "Фокус", "zoom": "Зум", "iris": "Ирис", "frost": "Фрост",
    "mode": "Режим", "speed": "Скорость", "macro": "Макро", "reset": "Сброс",
}

# Атрибуты, которые не плавно переходят, а переключаются сразу
SNAP_ATTRS = {
    "color_wheel", "color_macro", "gobo", "gobo_rotation", "prism", "shutter", "strobe",
    "mode", "macro", "reset", "control", "speed", "pt_speed",
}
COLOR_ATTRS = ("red", "green", "blue", "white", "amber", "uv")


def attr_label(attr):
    return ATTR_LABELS.get(attr, attr)


def attr_group(attr):
    for group, attrs in ATTR_GROUPS:
        if attr in attrs:
            return group
    return OTHER_GROUP


class FixtureType:
    """Описание прибора: список DMX-каналов по порядку.

    Каждый канал: {"attr": "pan", "default": 0}. Канал "pan_fine" —
    младший байт 16-битного pan (значение берётся из дробной части pan).
    """

    def __init__(self, data):
        self.name = data["name"]
        self.manufacturer = data.get("manufacturer", "")
        self.channels = [dict(c) for c in data["channels"]]
        if not self.channels:
            raise ValueError(f"У прибора {self.name} нет каналов")

    @property
    def channel_count(self):
        return len(self.channels)

    @property
    def attributes(self):
        seen = []
        for ch in self.channels:
            a = ch["attr"]
            if a.endswith("_fine") or a in seen:
                continue
            seen.append(a)
        return seen

    @property
    def defaults(self):
        d = {}
        for ch in self.channels:
            a = ch["attr"]
            if not a.endswith("_fine"):
                d.setdefault(a, float(ch.get("default", 0)))
        return d

    def has(self, attr):
        return attr in self.attributes

    def to_dict(self):
        return {"name": self.name, "manufacturer": self.manufacturer, "channels": self.channels}


def load_library(folder):
    """Загрузить все *.json из папки библиотеки приборов."""
    lib = {}
    for path in sorted(glob.glob(os.path.join(folder, "*.json"))):
        try:
            with open(path, encoding="utf-8") as f:
                ft = FixtureType(json.load(f))
            lib[ft.name] = ft
        except Exception as e:  # битый файл не должен ронять программу
            print(f"[fixtures] пропущен {path}: {e}")
    return lib


def save_type_to_library(ftype, folder):
    os.makedirs(folder, exist_ok=True)
    safe = "".join(c if c.isalnum() or c in "-_ " else "_" for c in ftype.name).strip()
    path = os.path.join(folder, safe.replace(" ", "_") + ".json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(ftype.to_dict(), f, ensure_ascii=False, indent=2)
    return path


class Fixture:
    """Прибор в патче: тип + DMX-адрес."""

    def __init__(self, fid, name, ftype, address):
        self.id = int(fid)
        self.name = name
        self.type = ftype
        self.address = int(address)

    @property
    def last_channel(self):
        return self.address + self.type.channel_count - 1

    def to_dict(self):
        return {"id": self.id, "name": self.name, "type": self.type.name, "address": self.address}
