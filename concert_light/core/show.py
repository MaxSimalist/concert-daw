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


MIN_STEP = 0.05     # самый короткий шаг, с


def scene_steps(sc):
    """Шаги сцены: [{"values", "fade", "hold"}]. Сцена без шагов — один шаг."""
    steps = sc.get("steps")
    if steps:
        return steps
    return [{"values": sc["values"], "fade": float(sc.get("fade", 0.0)), "hold": 0.0}]


def set_scene_steps(sc, steps):
    """Записать шаги; values/fade сцены = первый шаг (для кнопки, таймлайна и старых версий)."""
    steps = [s for s in steps if s.get("values") is not None]
    if not steps:
        return
    sc["values"] = steps[0]["values"]
    sc["fade"] = float(steps[0].get("fade", 0.0))
    if len(steps) > 1:
        sc["steps"] = steps
        sc.setdefault("loop", True)
    else:
        sc.pop("steps", None)


def step_time(step):
    """Сколько длится шаг: фейд + пауза (не меньше MIN_STEP)."""
    return max(MIN_STEP, float(step.get("fade", 0.0)) + float(step.get("hold", 0.0)))


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
        # name -> {"values": {fid: {attr: val}}, "fade": сек,
        #          "steps": [{"values", "fade", "hold"}] (если шагов больше одного), "loop": bool}
        self.scenes = {}
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
            for st in sc.get("steps", []):
                st["values"].pop(fid, None)
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
            "scenes": {name: self._scene_to_dict(sc) for name, sc in self.scenes.items()},
            "chases": copy.deepcopy(self.chases),
            "effects": copy.deepcopy(self.effects),
            "songs": copy.deepcopy(self.songs),
        }

    @staticmethod
    def _values_to_dict(values):
        return {str(fid): v for fid, v in values.items()}

    @staticmethod
    def _values_from_dict(d):
        return {int(k): {a: float(x) for a, x in v.items()} for k, v in (d or {}).items()}

    def _scene_to_dict(self, sc):
        out = {"fade": sc.get("fade", 0.0), "values": self._values_to_dict(sc["values"])}
        if sc.get("steps"):
            out["loop"] = bool(sc.get("loop", True))
            out["steps"] = [{"fade": float(st.get("fade", 0.0)), "hold": float(st.get("hold", 0.0)),
                             "values": self._values_to_dict(st["values"])} for st in sc["steps"]]
        return out

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
            scene = {"fade": float(sc.get("fade", 0.0)), "values": cls._values_from_dict(sc.get("values"))}
            if sc.get("steps"):
                scene["loop"] = bool(sc.get("loop", True))
                set_scene_steps(scene, [{"fade": float(st.get("fade", 0.0)), "hold": float(st.get("hold", 0.0)),
                                         "values": cls._values_from_dict(st.get("values"))}
                                        for st in sc["steps"]])
            show.scenes[name] = scene
        show.chases = d.get("chases", {})
        show.effects = d.get("effects", {})
        show.songs = d.get("songs", {})
        for s in show.songs.values():
            s.setdefault("cues", [])
            s["cues"].sort(key=lambda c: c["t"])
        show.path = path
        return show
