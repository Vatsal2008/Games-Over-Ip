"""Settings: name, theme, window size, opacity, boss key action."""
from __future__ import annotations

from typing import Any

from .. import widgets
from ..protocol import clean_name
from ..theme import THEMES
from .base import Screen

SIZES = [("S", "Small"), ("M", "Medium"), ("L", "Large")]
OPACITY = [(100, "100%"), (90, "90%"), (80, "80%"), (70, "70%")]
BOSS = [("minimize", "Hide window"), ("notes", "Show notes")]


class PrefsScreen(Screen):
    def build(self) -> None:
        ui, c, s = self.ui, self.canvas, self.app.settings
        self.header("Settings", "Saved on this PC", back=self.back)
        self.f_name = self.add(widgets.TextField(ui, c, s["name"], "Your name", max_len=16,
                                                 on_enter=lambda v: self._save_name()))
        self.seg_theme = self.add(widgets.Segmented(ui, c, [(k, t.label) for k, t in THEMES.items()], s["theme"],
                                                    self._theme))
        self.seg_size = self.add(widgets.Segmented(ui, c, SIZES, s["size"], self._size))
        self.seg_alpha = self.add(widgets.Segmented(ui, c, OPACITY, s["opacity"], self._alpha))
        self.seg_boss = self.add(widgets.Segmented(ui, c, BOSS, s["boss"], self._boss))

    def layout(self, w: int, h: int) -> None:
        ui, th = self.ui, self.th
        y = self.layout_header(w)
        x0, cw = self.column(w)
        lab, fh, sh, gap = ui.px(20), ui.px(44), ui.px(36), ui.px(22)
        self.label("l_name", x0, y, "Your name", cw)
        self.f_name.place(x0, y + lab, cw, fh)
        y += lab + fh + gap
        rows = [("l_theme", "Theme", self.seg_theme,
                 "Paper looks like a plain notes app and renames the window to Notes. Dim is easy on the eyes."),
                ("l_size", "Window size", self.seg_size, ""),
                ("l_alpha", "Window opacity", self.seg_alpha, ""),
                ("l_boss", "Esc key", self.seg_boss, "Esc again brings the game back when notes are showing.")]
        for name, title, seg, hint in rows:
            self.label(name, x0, y, title, cw)
            seg.place(x0, y + lab, cw, sh)
            y += lab + sh
            if hint:
                self.text(name + "_hint", x0, y + ui.px(6), hint, 11, fill=th.text_faint, anchor="nw", width=cw,
                          justify="left")
                bbox = self.canvas.bbox(self.items[name + "_hint"])
                y = bbox[3] if bbox else y + ui.px(16)
            y += gap

    def _save_name(self) -> None:
        name = clean_name(self.f_name.value, "")
        if name:
            self.app.settings["name"] = name
            self.app.settings.save()

    def back(self) -> None:
        self._save_name()
        self.app.go_menu()

    def _theme(self, key: Any) -> None:
        self._save_name()
        self.app.set_theme(str(key))

    def _size(self, key: Any) -> None:
        self.app.settings["size"] = key
        self.app.settings.save()
        self.app.apply_size(str(key))

    def _alpha(self, value: Any) -> None:
        self.app.settings["opacity"] = int(value)
        self.app.settings.save()
        self.app.apply_opacity(int(value))

    def _boss(self, value: Any) -> None:
        self.app.settings["boss"] = value
        self.app.settings.save()
