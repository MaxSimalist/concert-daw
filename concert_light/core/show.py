"""Шоу: патч, сцены, чейзы, эффекты, песни с таймлайнами. Сохраняется в JSON."""

import copy
import json

from .fixtures import Fixture, FixtureType

EFFECT_KINDS = {
    "circle": "Круг (pan/tilt)",
    "wave": "Волна (яркость)",
    "rainbow": "Радуга (RGB)",
}

CUE_ACTIONS = {
    "scene": "Сцена",
    "chase_start": "Старт чейза",
    "chase_stop": "Стоп чейза",
    "effect_on": "Эффект вкл",
    "effect_off": "Эффект выкл",
    "release": "Сброс (в ноль)",
    "blackout_on": "Blackout вкл",
    "blackout_off": "Blackout выкл",
}
CUE_NEEDS_TARGET = {"scene": "scenes", "chase_start": "chases", "effect_on": "effects", "effect_off": "effects"}


def scene_rgb(scene):
    """Цвет сцены для кнопки: средний RGB горящих приборов, ярко (None — сцена без цвета)."""
    rs = gs = bs = n = 0
    for attrs in scene["values"].values():
        if attrs.get("dimmer", 255) <= 0 or not any(c in attrs for c in ("red", "green", "blue", "white")):
            continue
        w = attrs.get("white", 0)
        rs += min(255, attrs.get("red", 0) + w)
        gs += min(255, attrs.get("green", 0) + w)
        bs += min(255, attrs.get("blue", 0) + w)
        n += 1
    if not n or rs + gs + bs < 30 * n:
        return None
    r, g, b = rs / n, gs / n, bs / n
    k = 255 / max(r, g, b)
    return round(r * k), round(g * k), round(b * k)


class Show:
    def __init__(self, library=None):
        self.library = library or {}
        self.types = {}      # типы, используемые в шоу (копия внутри файла шоу)
        self.patch = []      # [Fixture]
        self.scenes = {}     # name -> {"values": {fid: {attr: val}}, "fade": сек}
        self.chases = {}     # name -> {"steps": [scene], "step_time": сек, "fade": 0..1}
        self.effects = {}    # name -> {"kind", "fixtures": [fid], "speed", "size", "spread"}
        self.songs = {}      # name -> {"audio": path, "cues": [{"t","action","target","fade"}]}
        self.path = None

    # --- типы и патч ---------------------------------------------------

    def all_types(self):
        d = dict(self.library)
        d.update(self.types)
        return d

    def get_type(self, name):
        return self.types.get(name) or self.library.get(name)

    def fixture(self, fid):
        for f in self.patch:
            if f.id == fid:
                return f
        return None

    def next_id(self):
        return max((f.id for f in self.patch), default=0) + 1

    def next_free_address(self, count=1):
        used = set()
        for f in self.patch:
            used.update(range(f.address, f.last_channel + 1))
        for start in range(1, 513 - count + 1):
            if not any(a in used for a in range(start, start + count)):
                return start
        return 1

    def add_fixture(self, type_name, name, address):
        ftype = self.get_type(type_name)
        if ftype is None:
            raise KeyError(f"Нет типа прибора {type_name}")
        if address + ftype.channel_count - 1 > 512:
            raise ValueError(f"{name}: не помещается в 512 каналов (адрес {address})")
        self.types[ftype.name] = ftype
        fx = Fixture(self.next_id(), name, ftype, address)
        self.patch.append(fx)
        return fx

    def remove_fixture(self, fid):
        self.patch = [f for f in self.patch if f.id != fid]
        for sc in self.scenes.values():
            sc["values"].pop(fid, None)
        for ef in self.effects.values():
            ef["fixtures"] = [x for x in ef["fixtures"] if x != fid]

    def replace_type(self, old_name, new_type):
        """Тип прибора изменили в ручном патче — обновить все приборы этого типа."""
        for f in self.patch:
            if f.type.name == old_name:
                f.type = new_type
        self.types.pop(old_name, None)
        if any(f.type is new_type for f in self.patch):
            self.types[new_type.name] = new_type
        self.library[new_type.name] = new_type

    def conflicts(self):
        """id приборов, у которых пересекаются адреса."""
        bad = set()
        for i, a in enumerate(self.patch):
            for b in self.patch[i + 1:]:
                if a.address <= b.last_channel and b.address <= a.last_channel:
                    bad.update((a.id, b.id))
        return bad

    # --- сохранение ----------------------------------------------------

    def to_dict(self):
        used = {f.type.name for f in self.patch}
        return {
            "format": "concert-light-show",
            "version": 1,
            "types": [self.types[n].to_dict() for n in sorted(used) if n in self.types],
            "patch": [f.to_dict() for f in self.patch],
            "scenes": {
                name: {
                    "fade": sc.get("fade", 0.0),
                    "values": {str(fid): v for fid, v in sc["values"].items()},
                }
                for name, sc in self.scenes.items()
            },
            "chases": copy.deepcopy(self.chases),
            "effects": copy.deepcopy(self.effects),
            "songs": copy.deepcopy(self.songs),
        }

    def save(self, path=None):
        path = path or self.path
        if not path:
            raise ValueError("Не указан путь")
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=1)
        self.path = path

    @classmethod
    def load(cls, path, library):
        with open(path, encoding="utf-8") as f:
            d = json.load(f)
        show = cls(library)
        for td in d.get("types", []):
            ft = FixtureType(td)
            show.types[ft.name] = ft
        for fd in d.get("patch", []):
            ft = show.get_type(fd["type"])
            if ft is None:
                print(f"[show] неизвестный тип {fd['type']}, прибор {fd['name']} пропущен")
                continue
            show.patch.append(Fixture(fd["id"], fd["name"], ft, fd["address"]))
        for name, sc in d.get("scenes", {}).items():
            show.scenes[name] = {
                "fade": float(sc.get("fade", 0.0)),
                "values": {int(k): {a: float(x) for a, x in v.items()} for k, v in sc.get("values", {}).items()},
            }
        show.chases = d.get("chases", {})
        show.effects = d.get("effects", {})
        show.songs = d.get("songs", {})
        for s in show.songs.values():
            s.setdefault("cues", [])
            s["cues"].sort(key=lambda c: c["t"])
        show.path = path
        return show
