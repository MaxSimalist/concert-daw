"""
stage.py — Живые сцены: для каждой сцены проекта — холст (QGraphicsScene)
с источниками. Один и тот же холст показывают сразу несколько окон (превью,
эфир, проекторы) — картинка считается один раз.
"""
from __future__ import annotations

from typing import Optional

from PySide6.QtCore import QObject, QRectF, Signal
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import QGraphicsScene

from sources import SourceRuntime, make_runtime
from vmodel import Project, Scene, Source


class Stage(QObject):
    changed = Signal()                 # состав/вид сцен изменился

    def __init__(self, project: Project):
        super().__init__()
        self.project = project
        self.canvases: dict[str, QGraphicsScene] = {}
        self.runtimes: dict[str, dict[str, SourceRuntime]] = {}
        self.program_id = ""
        self.preview_id = ""
        self.build_all()

    # ── построение ─────────────────────────────────────────────────────────
    def _rect(self) -> QRectF:
        return QRectF(0, 0, self.project.canvas_w, self.project.canvas_h)

    def build_all(self) -> None:
        for sid in list(self.canvases):
            self._release_scene(sid)
        for sc in self.project.scenes:
            self._build(sc)
        self._update_active()

    def _build(self, sc: Scene) -> None:
        canvas = self.canvases.get(sc.id)
        if canvas is None:
            canvas = QGraphicsScene(self._rect())
            canvas.setBackgroundBrush(QBrush(QColor("#000000")))
            self.canvases[sc.id] = canvas
        canvas.setSceneRect(self._rect())
        rts = {}
        for z, src in enumerate(sc.sources):
            rt = make_runtime(src)
            rt.item.setZValue(z)
            canvas.addItem(rt.item)
            rts[src.id] = rt
        self.runtimes[sc.id] = rts
        self.apply_audio(sc.id)

    def _release_scene(self, sid: str) -> None:
        for rt in self.runtimes.pop(sid, {}).values():
            rt.release()
            canvas = self.canvases.get(sid)
            if canvas is not None and rt.item.scene() is canvas:
                canvas.removeItem(rt.item)

    def rebuild(self, scene_id: str) -> None:
        sc = self.project.scene(scene_id)
        self._release_scene(scene_id)
        if sc is None:
            self.canvases.pop(scene_id, None)
        else:
            self._build(sc)
        self._update_active()
        self.changed.emit()

    def set_canvas_size(self) -> None:
        for canvas in self.canvases.values():
            canvas.setSceneRect(self._rect())
        self.changed.emit()

    # ── сцены ──────────────────────────────────────────────────────────────
    def add_scene(self, sc: Scene) -> None:
        self._build(sc)
        self.changed.emit()

    def remove_scene(self, scene_id: str) -> None:
        self._release_scene(scene_id)
        self.canvases.pop(scene_id, None)
        if self.program_id == scene_id:
            self.program_id = ""
        if self.preview_id == scene_id:
            self.preview_id = ""
        self.changed.emit()

    def canvas(self, scene_id: str) -> Optional[QGraphicsScene]:
        return self.canvases.get(scene_id)

    # ── источники ──────────────────────────────────────────────────────────
    def add_source(self, scene_id: str, src: Source) -> SourceRuntime:
        canvas = self.canvases[scene_id]
        rt = make_runtime(src)
        rt.item.setZValue(len(self.runtimes[scene_id]))
        canvas.addItem(rt.item)
        self.runtimes[scene_id][src.id] = rt
        self.apply_audio(scene_id)
        if scene_id in (self.program_id, self.preview_id):
            rt.activate()
        self.changed.emit()
        return rt

    def remove_source(self, scene_id: str, source_id: str) -> None:
        rt = self.runtimes.get(scene_id, {}).pop(source_id, None)
        if rt is not None:
            rt.release()
            if rt.item.scene() is not None:
                rt.item.scene().removeItem(rt.item)
        self.restack(scene_id)

    def update_source(self, scene_id: str, source_id: str) -> None:
        rt = self.runtime(scene_id, source_id)
        if rt is not None:
            rt.apply()

    def restack(self, scene_id: str) -> None:
        """Порядок слоёв = порядок источников в сцене (последний — сверху)."""
        sc = self.project.scene(scene_id)
        if sc is None:
            return
        rts = self.runtimes.get(scene_id, {})
        for z, src in enumerate(sc.sources):
            if src.id in rts:
                rts[src.id].item.setZValue(z)
        self.changed.emit()

    def runtime(self, scene_id: str, source_id: str) -> Optional[SourceRuntime]:
        return self.runtimes.get(scene_id, {}).get(source_id)

    # ── эфир / превью ──────────────────────────────────────────────────────
    def set_program(self, scene_id: str) -> None:
        self.program_id = scene_id
        self.project.program_scene = scene_id
        self._update_active()

    def set_preview(self, scene_id: str) -> None:
        self.preview_id = scene_id
        self._update_active()

    def _update_active(self) -> None:
        live = {self.program_id, self.preview_id}
        for sid, rts in self.runtimes.items():
            for rt in rts.values():
                if sid in live and not rt.active:
                    rt.activate()
                elif sid not in live and rt.active:
                    rt.deactivate()

    def tick(self, media_pos: float, playing: bool, connected: bool) -> None:
        for sid in {self.program_id, self.preview_id}:
            for rt in self.runtimes.get(sid, {}).values():
                if rt.active:
                    rt.tick(media_pos, playing, connected)

    # ── звук ───────────────────────────────────────────────────────────────
    def apply_audio(self, scene_id: str = "") -> None:
        """Громкость сцены, общий выход и звуковая карта → плееры (одной сцены или всех)."""
        p = self.project
        master = 0.0 if p.master_muted else 10 ** (p.master_db / 20.0)
        for sid in ([scene_id] if scene_id else list(self.runtimes)):
            sc = p.scene(sid)
            if sc is None:
                continue
            gain = 10 ** (sc.volume_db / 20.0)
            for rt in self.runtimes.get(sid, {}).values():
                if rt.has_audio:
                    rt.set_output_device(p.audio_out)
                    rt.set_mix(gain, master, sc.muted)

    def audio_runtimes(self) -> list[SourceRuntime]:
        return [rt for rt in self.runtimes.get(self.program_id, {}).values() if rt.has_audio]

    def release(self) -> None:
        for sid in list(self.runtimes):
            self._release_scene(sid)
