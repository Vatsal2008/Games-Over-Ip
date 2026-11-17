"""Main menu: Host, Join, Settings."""
from __future__ import annotations

from typing import Any

from .. import __version__, widgets
from ..games import GAME_IDS
from ..gfx import Image, with_alpha
from .base import Screen, game_icon


def paint_logo(img: Image, th: Any) -> None:
    s = float(img.w)
    if th.glow:
        img.soft_circle(s * 0.5, s * 0.55, s * 0.34, with_alpha(th.accent, 110), s * 0.34)
    die = Image(int(s * 0.62), int(s * 0.62))
    widgets.paint_die(die, 5, "#ffffff" if th.key == "neon" else th.board_a, th.seat("blue")[2], angle=-14)
    img.blit(die, int(s * 0.06), int(s * 0.14))
    pawn = Image(int(s * 0.5), int(s * 0.5))
    widgets.paint_token(pawn, *th.seat("red"), style="pawn")
    img.blit(pawn, int(s * 0.5), int(s * 0.36))


class MenuScreen(Screen):
    def build(self) -> None:
        ui, c, app = self.ui, self.canvas, self.app
        self.b_host = self.add(widgets.Button(ui, c, "Host a game", app.go_host, kind="primary", icon="▶"))
        self.b_join = self.add(widgets.Button(ui, c, "Join a game", app.go_join, kind="secondary"))
        self.b_set = self.add(widgets.Button(ui, c, "Settings", app.go_settings, kind="ghost", font_px=14))

    def layout(self, w: int, h: int) -> None:
        ui, th = self.ui, self.th
        x0, cw = self.column(w, 340)
        btn_h, gap = ui.px(50), ui.px(12)
        buttons_h = 2 * btn_h + ui.px(40) + 2 * gap
        logo = max(ui.px(64), min(ui.px(120), int(h * 0.2)))
        show_icons = h > ui.px(470)
        icon = ui.px(40)
        block = logo + ui.px(18) + ui.px(34) + ui.px(24) + (icon + ui.px(30) if show_icons else 0)
        top = max(ui.px(18), int((h - ui.px(30) - block - buttons_h - ui.px(28)) * 0.42))
        y = top
        photo = ui.sprites.get(("logo", th.key), logo, logo, lambda img: paint_logo(img, th))
        self.image("logo", photo, w / 2, y, anchor="n")
        y += logo + ui.px(18)
        self.text("title", w / 2, y, "Games over IP", 30, bold=True, display=True, anchor="n", fit=cw)
        y += ui.px(40)
        self.text("sub", w / 2, y, "Tiny LAN games for long lectures", 13, fill=th.text_dim, anchor="n", fit=cw)
        y += ui.px(28)
        if show_icons:
            n = len(GAME_IDS)
            step = icon + ui.px(14)
            gx = w / 2 - (n * step - ui.px(14)) / 2
            for i, gid in enumerate(GAME_IDS):
                self.image("icon_%s" % gid, game_icon(ui, gid, icon), gx + i * step, y)
            y += icon + ui.px(30)
        else:
            for gid in GAME_IDS:
                if "icon_%s" % gid in self.items:
                    self.canvas.itemconfigure(self.items["icon_%s" % gid], state="hidden")
        y = max(y, h - ui.px(30) - buttons_h - ui.px(10)) if h > ui.px(560) else y + ui.px(4)
        self.b_host.place(x0, y, cw, btn_h)
        y += btn_h + gap
        self.b_join.place(x0, y, cw, btn_h)
        y += btn_h + gap
        self.b_set.place(x0 + cw / 4, y, cw / 2, ui.px(40))
        self.text("foot", w / 2, h - ui.px(14), "Esc hides the window  ·  v%s" % __version__, 11,
                  fill=th.text_faint, anchor="s", fit=cw)

    def on_key(self, keysym: str) -> None:
        if keysym in ("h", "H"):
            self.app.go_host()
        elif keysym in ("j", "J"):
            self.app.go_join()
