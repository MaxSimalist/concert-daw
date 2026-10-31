"""
vmodel.py — Проект Concert Video: сцены, источники, привязки к трекам Concert DAW,
настройки вывода. Хранится в JSON (%APPDATA%/ConcertVideo/project.json),
можно сохранить/открыть другой файл проекта.

Термины как в OBS:
* Сцена — набор источников (слоёв), показываемый на экране целиком.
* Источник — видео, картинка, текст, цвет, камера, захват экрана/окна, слайд-шоу.
  Положение и размер — в пикселях холста (по умолчанию 1920×1080).
* Привязка к треку (Binding) — какая сцена показывается для трека DAW и когда
  меняются сцены по ходу трека (cues: время → сцена).
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Optional

SOURCE_KINDS = {
    "video": "Видео / медиафайл",
    "image": "Изображение",
    "slideshow": "Слайд-шоу (папка картинок)",
    "text": "Текст",
    "color": "Цвет",
    "camera": "Камера / карта захвата",
    "screen": "Захват экрана",
    "window": "Захват окна",
}
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".avi", ".wmv", ".webm", ".m4v", ".mpg", ".mpeg", ".ts", ".flv", ".gif"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".webp", ".tif", ".tiff"}
TRANSITIONS = {"cut": "Мгновенно (Cut)", "fade": "Растворение (Fade)", "fade_black": "Через чёрное"}
STOP_ACTIONS = {"keep": "Оставить кадр (пауза)", "standby": "Показать заставку", "black": "Затемнить"}


def new_id() -> str:
    return uuid.uuid4().hex[:12]


def app_data_dir() -> Path:
    base = os.environ.get("APPDATA") if sys.platform == "win32" else None
    root = Path(base) if base else Path.home() / ".config"
    return root / "ConcertVideo"


def _from_dict(cls, d: dict):
    names = {f.name for f in fields(cls)}
    return cls(**{k: v for k, v in d.items() if k in names})


@dataclass
class Source:
    kind: str
    name: str
    id: str = field(default_factory=new_id)
    params: dict = field(default_factory=dict)   # файл, текст, цвет, устройство…
    x: float = 0.0
    y: float = 0.0
    w: float = 1920.0
    h: float = 1080.0
    rotation: float = 0.0
    opacity: float = 1.0
    visible: bool = True
    locked: bool = False
    fit: str = "fit"              # fit — вписать, fill — заполнить (обрезая), stretch — растянуть
    # видео
    sync: bool = True             # идти за треком Concert DAW (позиция = позиция трека + offset)
    offset: float = 0.0           # сек: видео начинается на столько позже начала трека (минус — раньше)
    loop: bool = False
    volume: float = 1.0
    muted: bool = True            # звук концерта идёт из DAW, у видео по умолчанию без звука

    @classmethod
    def from_dict(cls, d: dict) -> "Source":
        return _from_dict(cls, d)


@dataclass
class Scene:
    name: str
    id: str = field(default_factory=new_id)
    sources: list = field(default_factory=list)   # снизу вверх: последний — поверх всех
    volume_db: float = 0.0        # громкость сцены (запоминается для каждой сцены)
    muted: bool = False

    @classmethod
    def from_dict(cls, d: dict) -> "Scene":
        d = dict(d)
        srcs = [Source.from_dict(s) for s in d.pop("sources", [])]
        sc = _from_dict(cls, d)
        sc.sources = srcs
        return sc

    def source(self, sid: str) -> Optional[Source]:
        return next((s for s in self.sources if s.id == sid), None)


@dataclass
class Cue:
    t: float          # секунды от начала трека
    scene_id: str


@dataclass
class Binding:
    song_id: str
    song_name: str = ""
    scene_id: str = ""            # основная сцена трека
    cues: list = field(default_factory=list)       # смена сцен по времени: [Cue]
    on_stop: str = "keep"

    @classmethod
    def from_dict(cls, d: dict) -> "Binding":
        d = dict(d)
        cues = [Cue(float(c["t"]), str(c["scene_id"])) for c in d.pop("cues", [])]
        b = _from_dict(cls, d)
        b.cues = sorted(cues, key=lambda c: c.t)
        return b

    def scene_at(self, t: float) -> str:
        """Какая сцена должна быть на экране в момент t трека."""
        scene = self.scene_id
        for c in sorted(self.cues, key=lambda c: c.t):
            if c.t <= t + 1e-6:
                scene = c.scene_id or scene
            else:
                break
        return scene


@dataclass
class Project:
    canvas_w: int = 1920
    canvas_h: int = 1080
    scenes: list = field(default_factory=list)
    bindings: dict = field(default_factory=dict)      # song_id → Binding
    standby_scene: str = ""        # заставка: когда трек не привязан или остановлен
    unbound_action: str = "keep"   # трек без привязки: keep — не трогать экран, standby — заставка
    transition: str = "fade"
    transition_ms: int = 500
    studio_mode: bool = False
    follow_daw: bool = True        # автоматика: сцены и видео идут за Concert DAW
    program_scene: str = ""
    # подключение к Concert DAW
    daw_ip: str = ""
    daw_port: int = 47810
    daw_name: str = ""            # имя компьютера с DAW: если у него сменился IP — найдём заново
    auto_connect: bool = True
    video_delay_ms: int = 0        # + — видео позже (если проектор/экран «отстаёт», ставят минус)
    # проекторы: [{"screen": имя экрана, "what": "program"}]
    projectors: list = field(default_factory=list)
    # запись / трансляция
    out_w: int = 1280
    out_h: int = 720
    out_fps: int = 30
    rec_folder: str = ""
    rec_format: str = "mkv"
    rec_bitrate: int = 6000        # кбит/с
    stream_url: str = "rtmp://a.rtmp.youtube.com/live2"
    stream_key: str = ""
    stream_bitrate: int = 3500
    audio_device: str = ""         # звук для записи/трансляции (Windows: устройство DirectShow)
    ui_splitters: dict = field(default_factory=dict)
    # расписание: видео/сцены по времени (schedule.ScheduleItem)
    schedule: list = field(default_factory=list)
    schedule_on: bool = True
    # звук видео
    audio_out: str = ""            # звуковая карта для звука видео ("" — устройство Windows по умолчанию)
    master_db: float = 0.0         # общий выход звука видео
    master_muted: bool = False
    new_video_muted: bool = True   # новые видео без звука (звук концерта идёт из DAW)
    gpu_render: bool = True
    update_checked: float = 0.0    # когда последний раз проверяли обновления на GitHub        # эфир и проекторы рисует видеокарта (OpenGL)

    @classmethod
    def from_dict(cls, d: dict) -> "Project":
        d = dict(d)
        scenes = [Scene.from_dict(s) for s in d.pop("scenes", [])]
        binds = {k: Binding.from_dict(v) for k, v in d.pop("bindings", {}).items()}
        sched = d.pop("schedule", [])
        p = _from_dict(cls, d)
        p.scenes, p.bindings = scenes, binds
        from schedule import ScheduleItem          # здесь, чтобы не было циклического импорта
        p.schedule = [ScheduleItem.from_dict(x) for x in sched if isinstance(x, dict)]
        return p

    def to_dict(self) -> dict:
        return asdict(self)

    def scene(self, sid: str) -> Optional[Scene]:
        return next((s for s in self.scenes if s.id == sid), None)

    def scene_by_name(self, name: str) -> Optional[Scene]:
        return next((s for s in self.scenes if s.name == name), None)

    def unique_scene_name(self, base: str) -> str:
        names = {s.name for s in self.scenes}
        if base not in names:
            return base
        i = 2
        while f"{base} ({i})" in names:
            i += 1
        return f"{base} ({i})"


def default_project() -> Project:
    p = Project()
    logo = Scene("Заставка")
    logo.sources.append(Source("color", "Фон", params={"color": "#000000"}))
    logo.sources.append(Source("text", "Название", params={"text": "Добро пожаловать!", "size": 110,
                                                           "color": "#ffffff", "align": "center"},
                               x=160, y=380, w=1600, h=320))
    p.scenes.append(logo)
    p.standby_scene = logo.id
    p.program_scene = logo.id
    return p


def scene_for_video(project: Project, name: str, path: str, sync: bool = True,
                    muted: Optional[bool] = None) -> Scene:
    """Сцена «видео на весь экран» — самое частое для концерта."""
    sc = Scene(project.unique_scene_name(name))
    sc.sources.append(Source("color", "Фон", params={"color": "#000000"}))
    sc.sources.append(Source("video", Path(path).stem, params={"file": path}, sync=sync,
                             w=project.canvas_w, h=project.canvas_h, muted=False))
    # звук включается/выключается на уровне сцены (фейдер сцены в микшере)
    sc.muted = project.new_video_muted if muted is None else muted
    return sc


def match_video(song_name: str, files: list[str]) -> Optional[str]:
    """Автопривязка: видеофайл, в имени которого есть название трека (без учёта регистра,
    «ё»→«е», знаков препинания). Самое точное совпадение — самое короткое имя."""
    def norm(s: str) -> str:
        s = s.lower().replace("ё", "е")
        return " ".join("".join(ch if ch.isalnum() else " " for ch in s).split())
    key = norm(song_name)
    if not key:
        return None
    hits = [f for f in files if key in norm(Path(f).stem)]
    return min(hits, key=lambda f: len(Path(f).stem)) if hits else None


class ProjectStore:
    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else app_data_dir() / "project.json"

    def load(self) -> Project:
        for p in (self.path, self.path.with_suffix(".json.bak")):
            try:
                return Project.from_dict(json.loads(p.read_text("utf-8")))
            except FileNotFoundError:
                continue
            except Exception:
                continue
        return default_project()

    def save(self, project: Project) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(project.to_dict(), ensure_ascii=False, indent=1), "utf-8")
        if self.path.exists():
            try:
                bak = self.path.with_suffix(".json.bak")
                if bak.exists():
                    bak.unlink()
                self.path.replace(bak)
            except OSError:
                pass
        tmp.replace(self.path)
