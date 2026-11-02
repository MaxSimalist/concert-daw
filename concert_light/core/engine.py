"""DMX-движок: считает итоговое состояние приборов 40 раз в секунду.

Слои (снизу вверх):
  1. Сцены (с кроссфейдом; чейз просто переключает сцены по шагам)
  2. Программер (ручные значения, перекрывают сцену)
  3. Эффекты (круг, волна, радуга) — поверх программера
  4. Гранд-мастер / Blackout
"""

import colorsys
import copy
import math
import threading
import time

from .fixtures import COLOR_ATTRS, SNAP_ATTRS

FPS = 40


class Engine:
    def __init__(self, show, output):
        self.show = show
        self.output = output
        self.lock = threading.RLock()

        self.programmer = {}          # fid -> {attr: value}
        self.gm = 1.0
        self.blackout = False

        self.current_scene = None
        self._from = {}
        self._to = {}
        self._fade_start = 0.0
        self._fade_time = 0.0

        self.chase = None             # активный чейз (копия данных + позиция)
        self.effects = {}             # name -> (params, start_time)

        self.state = {}               # итоговые значения (для монитора в UI)
        self.universe = bytearray(512)
        self.used_channels = 512

        self._thread = None
        self._running = False
        self.rebuild_patch()

    # ------------------------------------------------------------------
    # патч
    # ------------------------------------------------------------------

    def _defaults(self):
        return {f.id: f.type.defaults for f in self.show.patch}

    @staticmethod
    def _merge(base, layer):
        out = {fid: dict(attrs) for fid, attrs in base.items()}
        for fid, attrs in layer.items():
            if fid in out:
                for a, v in attrs.items():
                    if a in out[fid]:
                        out[fid][a] = float(v)
        return out

    def rebuild_patch(self):
        """Вызывать после любого изменения патча."""
        with self.lock:
            base = self._defaults()
            self._from = self._merge(base, self._from)
            self._to = self._merge(base, self._to)
            ids = set(base)
            self.programmer = {fid: v for fid, v in self.programmer.items() if fid in ids}

    # ------------------------------------------------------------------
    # сцены
    # ------------------------------------------------------------------

    def _scene_layer(self, now):
        if self._fade_time <= 0 or now >= self._fade_start + self._fade_time:
            return copy.deepcopy(self._to)
        k = (now - self._fade_start) / self._fade_time
        out = {}
        for fid, to_attrs in self._to.items():
            fr = self._from.get(fid, {})
            o = {}
            for a, b in to_attrs.items():
                av = fr.get(a, b)
                o[a] = b if a in SNAP_ATTRS else av + (b - av) * k
            out[fid] = o
        return out

    def go_scene(self, name, fade=None):
        sc = self.show.scenes.get(name)
        if sc is None:
            return False
        with self.lock:
            now = time.monotonic()
            cur = self._scene_layer(now)
            self._from = cur
            # «трекинг»: атрибуты, которых нет в сцене, остаются как были
            self._to = self._merge(cur, sc["values"])
            self._fade_start = now
            self._fade_time = float(sc.get("fade", 0.0) if fade is None else fade)
            self.current_scene = name
        return True

    def release(self, fade=1.0):
        """Плавно вернуть слой сцен к значениям по умолчанию (всё погасить)."""
        with self.lock:
            now = time.monotonic()
            self._from = self._scene_layer(now)
            self._to = self._defaults()
            self._fade_start = now
            self._fade_time = float(fade)
            self.current_scene = None
            self.chase = None

    def release_all(self, fade=1.0):
        with self.lock:
            self.effects.clear()
            self.programmer.clear()
            self.release(fade)

    # ------------------------------------------------------------------
    # программер
    # ------------------------------------------------------------------

    def set_programmer(self, fids, attr, value):
        value = max(0.0, min(255.0, float(value)))
        with self.lock:
            for fid in fids:
                fx = self.show.fixture(fid)
                if fx and fx.type.has(attr):
                    self.programmer.setdefault(fid, {})[attr] = value

    def programmer_value(self, fid, attr):
        with self.lock:
            v = self.programmer.get(fid, {}).get(attr)
            if v is None:
                v = self.state.get(fid, {}).get(attr, 0.0)
            return v

    def clear_programmer(self, fids=None):
        with self.lock:
            if fids is None:
                self.programmer.clear()
            else:
                for fid in fids:
                    self.programmer.pop(fid, None)

    def record_scene(self, name, fade=1.0):
        """Записать содержимое программера как сцену. False, если программер пуст."""
        with self.lock:
            if not any(self.programmer.values()):
                return False
            self.show.scenes[name] = {"fade": float(fade), "values": copy.deepcopy(self.programmer)}
            return True

    def load_scene_to_programmer(self, name):
        sc = self.show.scenes.get(name)
        if sc is None:
            return
        with self.lock:
            self.programmer = copy.deepcopy(sc["values"])

    # ------------------------------------------------------------------
    # чейзы
    # ------------------------------------------------------------------

    def start_chase(self, name):
        ch = self.show.chases.get(name)
        if not ch or not ch.get("steps"):
            return False
        with self.lock:
            self.chase = {
                "name": name,
                "steps": list(ch["steps"]),
                "step_time": max(0.05, float(ch.get("step_time", 1.0))),
                "fade": max(0.0, min(1.0, float(ch.get("fade", 0.0)))),
                "index": -1,
                "next": time.monotonic(),
            }
        return True

    def stop_chase(self):
        with self.lock:
            self.chase = None

    def set_chase_step_time(self, name, step_time):
        with self.lock:
            if self.chase and self.chase["name"] == name:
                self.chase["step_time"] = max(0.05, float(step_time))

    def _tick_chase(self, now):
        ch = self.chase
        if not ch or now < ch["next"]:
            return
        ch["index"] = (ch["index"] + 1) % len(ch["steps"])
        self.go_scene(ch["steps"][ch["index"]], fade=ch["step_time"] * ch["fade"])
        ch["next"] += ch["step_time"]
        if now - ch["next"] > ch["step_time"]:  # отстали (например, после паузы)
            ch["next"] = now + ch["step_time"]

    # ------------------------------------------------------------------
    # эффекты
    # ------------------------------------------------------------------

    def start_effect(self, name):
        ef = self.show.effects.get(name)
        if not ef:
            return False
        with self.lock:
            self.effects[name] = (copy.deepcopy(ef), time.monotonic())
        return True

    def stop_effect(self, name):
        with self.lock:
            self.effects.pop(name, None)

    def stop_all_effects(self):
        with self.lock:
            self.effects.clear()

    def _apply_effects(self, state, now):
        for p, start in self.effects.values():
            fids = [f for f in p.get("fixtures", []) if f in state]
            n = len(fids)
            speed = float(p.get("speed", 0.5))
            size = float(p.get("size", 30))
            spread = float(p.get("spread", 1.0))
            kind = p.get("kind")
            for i, fid in enumerate(fids):
                st = state[fid]
                phase = (now - start) * speed + (i / n) * spread
                ang = 2 * math.pi * phase
                if kind == "circle":
                    if "pan" in st:
                        st["pan"] += size * math.cos(ang)
                    if "tilt" in st:
                        st["tilt"] += size * math.sin(ang)
                elif kind == "wave":
                    depth = max(0.0, min(1.0, size / 100.0))
                    k = 1.0 - depth * (0.5 - 0.5 * math.sin(ang))
                    if "dimmer" in st:
                        st["dimmer"] *= k
                    else:
                        for c in COLOR_ATTRS:
                            if c in st:
                                st[c] *= k
                elif kind == "rainbow":
                    r, g, b = colorsys.hsv_to_rgb(phase % 1.0, 1.0, 1.0)
                    for a, v in (("red", r), ("green", g), ("blue", b)):
                        if a in st:
                            st[a] = v * 255.0
                    if "white" in st:
                        st["white"] = 0.0

    # ------------------------------------------------------------------
    # расчёт кадра
    # ------------------------------------------------------------------

    def compute(self, now=None):
        now = time.monotonic() if now is None else now
        with self.lock:
            self._tick_chase(now)
            state = self._scene_layer(now)
            for fid, attrs in self.programmer.items():
                if fid in state:
                    state[fid].update(attrs)
            self._apply_effects(state, now)

            master = 0.0 if self.blackout else self.gm
            uni = bytearray(512)
            for fx in self.show.patch:
                st = state.get(fx.id)
                if st is None:
                    continue
                for a, v in st.items():
                    st[a] = max(0.0, min(255.0, v))
                if master < 1.0:
                    if "dimmer" in st:
                        st["dimmer"] *= master
                    else:
                        for c in COLOR_ATTRS:
                            if c in st:
                                st[c] *= master
                for i, ch in enumerate(fx.type.channels):
                    addr = fx.address - 1 + i
                    if not 0 <= addr < 512:
                        continue
                    a = ch["attr"]
                    if a.endswith("_fine"):
                        v = st.get(a[:-5], 0.0)
                        uni[addr] = int(round((v - int(v)) * 255))
                    else:
                        uni[addr] = int(st.get(a, 0.0))
            self.state = state
            self.universe = uni
            # сколько каналов реально занято — uDMX не гоняет по USB пустые
            self.used_channels = max((fx.last_channel for fx in self.show.patch), default=1)
            return uni

    # ------------------------------------------------------------------
    # поток вывода
    # ------------------------------------------------------------------

    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._loop, name="dmx-engine", daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False
        if self._thread:
            self._thread.join(timeout=1.0)
        self.output.close()

    def _loop(self):
        period = 1.0 / FPS
        nxt = time.monotonic()
        while self._running:
            try:
                uni = self.compute()
                self.output.limit = min(512, self.used_channels)
                self.output.send(uni)
            except Exception as e:
                print(f"[engine] ошибка кадра: {e}")
            nxt += period
            delay = nxt - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                nxt = time.monotonic()
