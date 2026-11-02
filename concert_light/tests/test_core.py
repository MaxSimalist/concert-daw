"""Тесты ядра без Qt и без устройства: python -m tests.test_core (из папки concert_light)."""

import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from core.engine import Engine
from core.fixtures import load_library
from core.output import Output
from core.show import Show
from core.timeline import TimelineLogic, match_daw_song


class FakeOutput:
    def __init__(self):
        self.frames = []
        self.status = "test"

    def send(self, u):
        self.frames.append(bytes(u))

    def close(self):
        pass


def make_show():
    lib = load_library(os.path.join(ROOT, "fixtures"))
    assert len(lib) >= 7, lib.keys()
    show = Show(lib)
    show.add_fixture("Generic RGB PAR 7ch", "PAR 1", 1)       # 1..7
    show.add_fixture("Generic RGB PAR 7ch", "PAR 2", 8)       # 8..14
    show.add_fixture("Generic Spot Moving Head 11ch", "Голова 1", 20)  # 20..30
    return show


def test_patch():
    show = make_show()
    assert show.next_free_address(7) == 31
    assert not show.conflicts()
    show.add_fixture("Generic Dimmer 1ch", "Конфликт", 5)
    assert show.conflicts() == {1, 4}
    print("patch ok")


def test_scene_fade_and_render():
    show = make_show()
    eng = Engine(show, FakeOutput())
    eng.set_programmer([1, 2], "dimmer", 255)
    eng.set_programmer([1, 2], "red", 255)
    eng.set_programmer([3], "pan", 100.5)
    eng.set_programmer([3], "dimmer", 200)
    assert eng.record_scene("Красный", fade=1.0)
    eng.clear_programmer()

    u = eng.compute()
    assert u[0] == 0 and u[19] == 128, (u[0], u[19])   # по умолчанию: темно, pan по центру

    t0 = time.monotonic()
    eng.go_scene("Красный")
    u = eng.compute(t0 + 0.5)
    assert 120 <= u[0] <= 135, u[0]                      # середина фейда диммера
    u = eng.compute(t0 + 1.1)
    assert u[0] == 255 and u[1] == 255 and u[7] == 255 and u[8] == 255
    assert u[19] == 100 and u[20] == 128, (u[19], u[20])  # 16-бит pan: 100.5 -> 100 / 128
    assert u[26] == 200                                   # диммер головы (8-й канал)

    eng.gm = 0.5
    u = eng.compute(t0 + 2)
    assert u[0] == 127
    eng.blackout = True
    assert eng.compute(t0 + 2)[0] == 0
    print("scenes ok")


def test_tracking_and_chase():
    show = make_show()
    eng = Engine(show, FakeOutput())
    show.scenes["Синий"] = {"fade": 0, "values": {1: {"dimmer": 255, "blue": 255}}}
    show.scenes["Зелёный"] = {"fade": 0, "values": {1: {"blue": 0, "green": 255}}}
    eng.go_scene("Синий")
    eng.go_scene("Зелёный")
    u = eng.compute()
    assert u[0] == 255 and u[2] == 255 and u[3] == 0, list(u[:4])  # диммер «протрекался»

    show.chases["Мигалка"] = {"steps": ["Синий", "Зелёный"], "step_time": 0.5, "fade": 0}
    eng.start_chase("Мигалка")
    t = time.monotonic()
    eng.compute(t)
    assert eng.current_scene == "Синий"
    eng.compute(t + 0.55)
    assert eng.current_scene == "Зелёный"
    eng.compute(t + 1.05)
    assert eng.current_scene == "Синий"
    print("chase ok")


def test_scene_beats_leftover_programmer():
    """Забытые в программере значения не должны мешать включить сцену."""
    show = make_show()
    eng = Engine(show, FakeOutput())
    show.scenes["Тьма"] = {"fade": 0, "values": {1: {"dimmer": 0, "red": 0}}}
    show.scenes["Свет"] = {"fade": 0, "values": {1: {"dimmer": 255, "red": 255}}}
    eng.load_scene_to_programmer("Тьма")          # «В программер (для правки)» и забыли
    eng.go_scene("Свет")
    u = eng.compute()
    assert u[0] == 255 and u[1] == 255, list(u[:3])
    assert 1 not in eng.programmer
    eng.set_programmer([1], "blue", 200)          # то, чего нет в сцене, программер держит
    eng.go_scene("Свет")
    assert eng.compute()[3] == 200
    print("сцена главнее программера ok")


def test_record_full_fixture_state():
    """Запись «все каналы»: в сцену попадает и то, что не трогали (диммер)."""
    show = make_show()
    eng = Engine(show, FakeOutput())
    eng.set_programmer([1], "dimmer", 255)
    eng.record_scene("База", fade=0)
    eng.clear_programmer()
    eng.go_scene("База")
    eng.set_programmer([1], "green", 255)         # тронули только цвет
    assert eng.record_scene("Зелёный", fade=0, full=True)
    vals = show.scenes["Зелёный"]["values"][1]
    assert vals["dimmer"] == 255 and vals["green"] == 255 and "strobe" in vals, vals
    assert eng.record_scene("Слой", fade=0, full=False)
    assert show.scenes["Слой"]["values"][1] == {"green": 255.0}
    eng.clear_programmer()
    eng.release(0)
    eng.go_scene("Зелёный")
    u = eng.compute()
    assert u[0] == 255 and u[2] == 255, list(u[:4])  # горит, хотя после release диммер был 0
    print("запись всех каналов ok")


def test_manual_patch():
    """Свой тип с каналами вручную, raw-каналы, проверка каналов, замена типа в патче."""
    from core.fixtures import FixtureType, attr_label, is_raw
    show = make_show()
    eng = Engine(show, FakeOutput())
    ft = FixtureType({"name": "Мой PAR", "channels": [
        {"attr": "red", "default": 0}, {"attr": "green", "default": 0}, {"attr": "blue", "default": 0},
        {"attr": "dimmer", "default": 0}, {"attr": "raw5", "default": 0}, {"attr": "strobe", "default": 0}]})
    assert is_raw("raw5") and attr_label("raw5") == "Канал 5" and "raw5" in ft.attributes
    show.library[ft.name] = ft
    fx = show.add_fixture("Мой PAR", "PAR X", 40)
    eng.rebuild_patch()
    eng.set_programmer([fx.id], "dimmer", 255)
    eng.set_programmer([fx.id], "raw5", 77)
    u = eng.compute()
    assert u[42] == 255 and u[43] == 77            # адрес 40: диммер — 4-й канал (43), raw5 — 44
    eng.test_channels[100] = 33                     # проверка канала — поверх всего
    assert eng.compute()[99] == 33 and eng.used_channels >= 100
    eng.test_channels.clear()
    # поменяли раскладку: диммер стал первым каналом
    ft2 = FixtureType({"name": "Мой PAR", "channels": [
        {"attr": "dimmer"}, {"attr": "red"}, {"attr": "green"}, {"attr": "blue"}]})
    show.replace_type("Мой PAR", ft2)
    eng.rebuild_patch()
    u = eng.compute()
    assert fx.type is ft2 and fx.last_channel == 43 and u[39] == 255, list(u[39:46])
    print("ручной патч ok")


def test_effects():
    show = make_show()
    eng = Engine(show, FakeOutput())
    show.effects["Радуга"] = {"kind": "rainbow", "fixtures": [1, 2], "speed": 1.0, "size": 0, "spread": 0.5}
    show.effects["Круг"] = {"kind": "circle", "fixtures": [3], "speed": 0.25, "size": 40, "spread": 0}
    eng.set_programmer([1, 2], "dimmer", 255)
    eng.start_effect("Радуга")
    eng.start_effect("Круг")
    start = eng.effects["Радуга"][1]
    u = eng.compute(start)
    assert u[1] == 255 and u[2] == 0          # PAR 1: hue 0 = красный
    assert 120 <= u[8] <= 130 and u[9] == 255  # PAR 2: hue 0.25 — жёлто-зелёный
    cstart = eng.effects["Круг"][1]
    u = eng.compute(cstart)
    assert u[19] == 168                       # pan 128 + 40*cos(0)
    u = eng.compute(cstart + 1.0)             # четверть круга
    assert abs(u[19] - 128) <= 1 and abs(u[21] - 168) <= 1, (u[19], u[21])
    print("effects ok")


def test_timeline():
    show = make_show()
    eng = Engine(show, FakeOutput())
    show.scenes["Куплет"] = {"fade": 0, "values": {1: {"dimmer": 100}}}
    show.scenes["Припев"] = {"fade": 0, "values": {1: {"dimmer": 255}}}
    show.effects["Волна"] = {"kind": "wave", "fixtures": [1, 2], "speed": 1, "size": 50, "spread": 1}
    show.songs["Песня"] = {"audio": "", "cues": [
        {"t": 0.0, "action": "scene", "target": "Куплет", "fade": 0},
        {"t": 10.0, "action": "scene", "target": "Припев", "fade": 0},
        {"t": 10.0, "action": "effect_on", "target": "Волна"},
        {"t": 20.0, "action": "effect_off", "target": "Волна"},
    ]}
    tl = TimelineLogic(show, eng)
    tl.set_song("Песня")
    tl.update(0.0, True)
    assert eng.current_scene == "Куплет"
    pos = 0.0
    while pos < 10.5:
        pos += 0.02
        tl.update(pos, True)
    assert eng.current_scene == "Припев" and "Волна" in eng.effects
    tl.update(10.45, True)                    # мелкий откат — повторов нет
    tl.update(25.0, True)                     # перемотка вперёд
    assert eng.current_scene == "Припев" and "Волна" not in eng.effects
    tl.update(3.0, True)                      # перемотка назад
    assert eng.current_scene == "Куплет"
    print("timeline ok")


def test_save_load():
    show = make_show()
    show.scenes["A"] = {"fade": 1.5, "values": {1: {"dimmer": 10.0}}}
    show.songs["S"] = {"audio": "", "cues": [{"t": 1, "action": "scene", "target": "A"}]}
    path = os.path.join(tempfile.mkdtemp(), "show.json")
    show.save(path)
    s2 = Show.load(path, show.library)
    assert [f.name for f in s2.patch] == ["PAR 1", "PAR 2", "Голова 1"]
    assert s2.scenes["A"]["values"][1]["dimmer"] == 10.0
    print("save/load ok")


def test_output_without_device():
    o = Output()
    o.send(bytearray(512))        # не должно падать без pyusb/устройства
    print("output ok:", o.status)


def test_engine_thread():
    show = make_show()
    out = FakeOutput()
    eng = Engine(show, out)
    eng.start()
    time.sleep(0.3)
    eng.stop()
    assert 8 <= len(out.frames) <= 16, len(out.frames)
    print("thread ok:", len(out.frames), "кадров за 0.3 с")


def test_daw_to_light_end_to_end():
    """Настоящий сервер Concert DAW (sync_server.py) -> SyncClient -> TimelineLogic -> Engine."""
    sys.path.insert(0, os.path.join(ROOT, "tests"))
    from daw_sync_server import SyncServer
    from sync_client import SyncClient, discover

    show = make_show()
    eng = Engine(show, FakeOutput())
    show.scenes["Интро"] = {"fade": 0, "values": {1: {"dimmer": 50}}}
    show.scenes["Взрыв"] = {"fade": 0, "values": {1: {"dimmer": 255}}}
    # привязка по id трека DAW (название в шоу другое)
    show.songs["Хит (свет)"] = {"audio": "", "daw_id": "trk-42", "cues": [
        {"t": 0.0, "action": "scene", "target": "Интро"},
        {"t": 0.6, "action": "scene", "target": "Взрыв"},
    ]}
    tl = TimelineLogic(show, eng)

    server = SyncServer("Тестовый DAW")
    assert server.start(47890), server.error
    server.set_songs([{"name": "Концерт", "songs": [{"id": "trk-42", "name": "Хит", "duration": 180.0}]}])
    client = SyncClient("test")
    client.connect("127.0.0.1", 47890)
    for _ in range(100):
        if client.connected and client.clock_ok and client.songs:
            break
        time.sleep(0.02)
    assert client.connected and client.clock_ok, "клиент не подключился"
    assert client.all_songs()[0][1]["id"] == "trk-42"

    t0 = time.monotonic()
    seen = []
    end = t0 + 1.2
    while time.monotonic() < end:
        server.publish_state("trk-42", "Хит", 180.0, time.monotonic() - t0, True)   # DAW играет
        st = client.state
        name = match_daw_song(show.songs, st.song_id, st.song_name)
        if name and tl.song != name:
            tl.set_song(name)
        tl.update(client.position(), st.playing)
        client.poll()
        if eng.current_scene and (not seen or seen[-1] != eng.current_scene):
            seen.append(eng.current_scene)
        time.sleep(0.02)
    pos_err = abs(client.position() - (time.monotonic() - t0))
    server.publish_state("trk-42", "Хит", 180.0, 1.2, False)
    time.sleep(0.15)
    assert not client.state.playing
    assert seen == ["Интро", "Взрыв"], seen
    assert pos_err < 0.05, pos_err
    print(f"DAW -> свет ok: {seen}, расхождение позиции {pos_err * 1000:.1f} мс")

    found = discover(0.6)
    client.disconnect()
    server.stop()
    print("автопоиск DAW:", found or "широковещание недоступно в этой среде")


def test_match_song():
    songs = {"Песня": {"daw_id": ""}, "Другая": {"daw_id": "id-7"}}
    assert match_daw_song(songs, "id-7", "что угодно") == "Другая"
    assert match_daw_song(songs, "", "песня ") == "Песня"
    assert match_daw_song(songs, "x", "нет такой") is None
    print("match ok")


def test_updater_layout():
    """updater.py найдёт программу в архиве репозитория и не тронет .venv."""
    import shutil
    import updater
    from version import APP_DIR, VERSION
    assert APP_DIR == "concert_light" and updater.parse_version(VERSION) >= (0, 9, 0)
    assert updater.is_newer("1.0.0", "0.9.1-beta")      # стабильная 1.0.0 придёт как обновление беты
    assert not updater.is_newer("0.9.1-beta", "0.9.1-beta")
    tmp = tempfile.mkdtemp()
    repo = os.path.join(tmp, "MaxSimalist-concert-daw-abc123")
    for d in ("concert_daw", "concert_video"):
        os.makedirs(os.path.join(repo, d))
        open(os.path.join(repo, d, "main.py"), "w").close()
        open(os.path.join(repo, d, "version.py"), "w").close()
    shutil.copytree(ROOT, os.path.join(repo, APP_DIR), ignore=shutil.ignore_patterns("__pycache__"))
    from pathlib import Path
    found = updater.find_app_dir(Path(tmp))
    assert found and found.name == APP_DIR, found
    dst = Path(tmp) / "installed"
    (dst / ".venv").mkdir(parents=True)
    (dst / ".venv" / "keep.txt").write_text("x")
    files = updater.copy_tree(found, dst)
    assert (dst / "core" / "engine.py").exists() and (dst / ".venv" / "keep.txt").read_text() == "x"
    print(f"updater ok: {len(files)} файлов")


if __name__ == "__main__":
    test_patch()
    test_scene_fade_and_render()
    test_tracking_and_chase()
    test_scene_beats_leftover_programmer()
    test_record_full_fixture_state()
    test_manual_patch()
    test_effects()
    test_timeline()
    test_save_load()
    test_output_without_device()
    test_engine_thread()
    test_match_song()
    test_updater_layout()
    test_daw_to_light_end_to_end()
    print("ВСЕ ТЕСТЫ ПРОЙДЕНЫ")
