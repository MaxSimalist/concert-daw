"""Вкладка «Программер»: ручное управление приборами и запись сцен."""

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QDoubleSpinBox, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QListWidget, QListWidgetItem, QMessageBox, QPushButton, QScrollArea, QSplitter,
    QVBoxLayout, QWidget,
)

from core.fixtures import ATTR_GROUPS, OTHER_GROUP, attr_group, attr_label

from .widgets import AttrSlider, XYPad, clear_layout

COLOR_PRESETS = [
    ("Красный", (255, 0, 0)), ("Оранжевый", (255, 110, 0)), ("Жёлтый", (255, 210, 0)),
    ("Зелёный", (0, 255, 0)), ("Голубой", (0, 200, 255)), ("Синий", (0, 0, 255)),
    ("Фиолетовый", (150, 0, 255)), ("Розовый", (255, 0, 140)), ("Белый", (255, 255, 255)),
]


class ProgrammerTab(QWidget):
    def __init__(self, show, engine, hub):
        super().__init__()
        self.show_, self.engine, self.hub = show, engine, hub
        self.sliders = {}
        self.pad = None

        split = QSplitter(Qt.Horizontal)

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.addWidget(QLabel("Приборы (Ctrl/Shift — несколько):"))
        self.fx_list = QListWidget()
        self.fx_list.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.fx_list.itemSelectionChanged.connect(self._rebuild)
        ll.addWidget(self.fx_list, 1)
        row = QHBoxLayout()
        b_all = QPushButton("Все")
        b_all.clicked.connect(self.fx_list.selectAll)
        b_none = QPushButton("Снять")
        b_none.clicked.connect(self.fx_list.clearSelection)
        row.addWidget(b_all)
        row.addWidget(b_none)
        ll.addLayout(row)
        split.addWidget(left)

        right = QWidget()
        rl = QVBoxLayout(right)
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(True)
        self.ctrl = QWidget()
        self.ctrl_lay = QVBoxLayout(self.ctrl)
        self.scroll.setWidget(self.ctrl)
        rl.addWidget(self.scroll, 1)

        quick = QHBoxLayout()
        for text, slot in (("Полный (диммер 100%)", self._full), ("Очистить выбранные", self._clear_sel),
                           ("Очистить программер", self._clear_all)):
            b = QPushButton(text)
            b.clicked.connect(slot)
            quick.addWidget(b)
        rl.addLayout(quick)

        rec = QGroupBox("Записать сцену из программера")
        rg = QHBoxLayout(rec)
        self.scene_name = QLineEdit()
        self.scene_name.setPlaceholderText("Название сцены")
        self.fade = QDoubleSpinBox()
        self.fade.setRange(0, 60)
        self.fade.setValue(1.0)
        self.fade.setSuffix(" с фейд")
        self.full = QCheckBox("все каналы приборов")
        self.full.setChecked(True)
        self.full.setToolTip("Включено: записывается всё, что сейчас видно на выбранных приборах "
                             "(диммер, цвет, позиция…), даже то, что вы не трогали.\n"
                             "Выключено: только тронутые ползунки — для сцен-слоёв "
                             "(отдельно цвет, отдельно позиция).")
        self.clear_after = QCheckBox("очистить программер")
        self.clear_after.setChecked(True)
        b_rec = QPushButton("● Записать")
        b_rec.clicked.connect(self._record)
        for w in (self.scene_name, self.fade, self.full, self.clear_after, b_rec):
            rg.addWidget(w)
        rl.addWidget(rec)
        split.addWidget(right)
        split.setSizes([260, 800])

        lay = QVBoxLayout(self)
        lay.addWidget(split)
        hub.changed.connect(self._on_changed)
        self.refresh_fixtures()

    def _on_changed(self, kind):
        if kind in ("patch", "all"):
            self.refresh_fixtures()
        elif kind == "programmer":
            self._rebuild()

    # --- выбор приборов ---------------------------------------------------

    def refresh_fixtures(self):
        sel = set(self.selected_ids())
        self.fx_list.blockSignals(True)
        self.fx_list.clear()
        for fx in sorted(self.show_.patch, key=lambda f: f.id):
            it = QListWidgetItem(f"{fx.id}. {fx.name}  [{fx.address}]")
            it.setData(Qt.UserRole, fx.id)
            self.fx_list.addItem(it)
            it.setSelected(fx.id in sel)
        self.fx_list.blockSignals(False)
        self._rebuild()

    def selected_ids(self):
        return [it.data(Qt.UserRole) for it in self.fx_list.selectedItems()]

    # --- построение контролов ----------------------------------------------

    def _rebuild(self):
        clear_layout(self.ctrl_lay)
        self.sliders = {}
        self.pad = None
        fids = self.selected_ids()
        if not fids:
            self.ctrl_lay.addWidget(QLabel("Выберите приборы слева"))
            self.ctrl_lay.addStretch()
            return
        fxs = [self.show_.fixture(f) for f in fids]
        attrs = []
        for fx in fxs:
            for a in fx.type.attributes:
                if a not in attrs:
                    attrs.append(a)
        order = [g for g, _ in ATTR_GROUPS] + [OTHER_GROUP]
        for group in order:
            alist = [a for a in attrs if attr_group(a) == group]
            if not alist:
                continue
            box = QGroupBox(group)
            v = QVBoxLayout(box)
            if group == "Позиция" and "pan" in alist and "tilt" in alist:
                self.pad = XYPad()
                self.pad.set_values(self._value("pan"), self._value("tilt"))
                self.pad.moved.connect(self._pad_moved)
                v.addWidget(self.pad, 0, Qt.AlignLeft)
            if group == "Цвет" and any(a in alist for a in ("red", "green", "blue")):
                grid = QGridLayout()
                for i, (name, rgb) in enumerate(COLOR_PRESETS):
                    b = QPushButton(name)
                    fg = "black" if sum(rgb) > 380 else "white"
                    b.setStyleSheet(f"background: rgb{rgb}; color: {fg}; padding: 6px;")
                    b.clicked.connect(lambda _=False, n=name, c=rgb: self._preset(n, c))
                    grid.addWidget(b, i // 5, i % 5)
                v.addLayout(grid)
            for a in alist:
                s = AttrSlider(a, attr_label(a), self._value(a))
                s.changed.connect(self._attr_changed)
                self.sliders[a] = s
                v.addWidget(s)
            self.ctrl_lay.addWidget(box)
        self.ctrl_lay.addStretch()

    def _value(self, attr):
        for fid in self.selected_ids():
            fx = self.show_.fixture(fid)
            if fx and fx.type.has(attr):
                return self.engine.programmer_value(fid, attr)
        return 0.0

    # --- изменения ---------------------------------------------------------

    def _attr_changed(self, attr, value):
        self.engine.set_programmer(self.selected_ids(), attr, value)
        if self.pad and attr in ("pan", "tilt"):
            self.pad.set_values(self._value("pan"), self._value("tilt"))

    def _pad_moved(self, x, y):
        fids = self.selected_ids()
        self.engine.set_programmer(fids, "pan", x)
        self.engine.set_programmer(fids, "tilt", y)
        for a, v in (("pan", x), ("tilt", y)):
            if a in self.sliders:
                self.sliders[a].set_value(v)

    def _preset(self, name, rgb):
        fids = self.selected_ids()
        vals = {"red": rgb[0], "green": rgb[1], "blue": rgb[2], "white": 255 if name == "Белый" else 0,
                "amber": 0, "uv": 0}
        for a, v in vals.items():
            self.engine.set_programmer(fids, a, v)
            if a in self.sliders:
                self.sliders[a].set_value(v)

    def _full(self):
        fids = self.selected_ids()
        self.engine.set_programmer(fids, "dimmer", 255)
        if "dimmer" in self.sliders:
            self.sliders["dimmer"].set_value(255)

    def _clear_sel(self):
        self.engine.clear_programmer(self.selected_ids())
        self._rebuild()

    def _clear_all(self):
        self.engine.clear_programmer()
        self._rebuild()

    def _record(self):
        name = self.scene_name.text().strip()
        if not name:
            QMessageBox.warning(self, "Сцена", "Введите название сцены")
            return
        if name in self.show_.scenes and QMessageBox.question(
                self, "Сцена", f"Сцена «{name}» уже есть. Перезаписать?") != QMessageBox.Yes:
            return
        if not self.engine.record_scene(name, self.fade.value(), full=self.full.isChecked()):
            QMessageBox.warning(self, "Сцена", "Программер пуст — сначала выставьте значения")
            return
        sc = self.show_.scenes[name]["values"]
        dark = [self.show_.fixture(fid).name for fid, v in sc.items()
                if self.show_.fixture(fid) and v.get("dimmer", 1) == 0]
        if dark:
            QMessageBox.information(
                self, "Сцена", f"Сцена «{name}» записана, но у приборов {', '.join(dark[:5])}"
                f"{' и др.' if len(dark) > 5 else ''} диммер = 0 — они будут тёмными.\n"
                "Поднимите «Диммер» (или кнопку «Полный») и запишите сцену ещё раз.")
        if self.clear_after.isChecked():
            self.engine.clear_programmer()
            self._rebuild()
        self.scene_name.clear()
        self.hub.changed.emit("scenes")
