"""Widget gallery: every widget and painter of the UI kit in one window.

    python tools/gallery.py --theme neon --size M
    python tools/gallery.py --theme paper --size S --shot shots/gallery_paper_S.png

Two phone-sized columns side by side (each one the width of the chosen window preset),
so what you see is what the app shows at that size. --shot saves a PNG after --delay
seconds (default 1.5) and exits.
"""
from __future__ import annotations

import argparse
import os
import sys
import tkinter as tk
from typing import Any, List

if __package__ in (None, ""):
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from goip import BASE_SIZE, WINDOW_SIZES  # noqa: E402
from goip import gfx, widgets as W  # noqa: E402
from goip.theme import THEMES  # noqa: E402


def _dpi_aware() -> None:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass


class Gallery:
    def __init__(self, root: tk.Tk, theme_key: str, size: str) -> None:
        self.root = root
        dpi = root.winfo_fpixels("1i") / 96.0
        pw, ph = WINDOW_SIZES.get(size, WINDOW_SIZES["M"])
        self.colw = int(round(pw * dpi))
        self.ui = W.UI(root, THEMES.get(theme_key, THEMES["neon"]), scale=pw / float(BASE_SIZE[0]) * dpi)
        self.widgets: List[Any] = []
        ui = self.ui
        self.colh = max(int(round(ph * dpi)), ui.px(770))
        root.geometry("%dx%d" % (self.colw * 2, self.colh))
        root.title("Gallery - %s %s" % (ui.theme.label, size))
        root.configure(bg=ui.theme.bg_top)
        self.c1 = tk.Canvas(root, width=self.colw, height=self.colh, highlightthickness=0, bd=0,
                            bg=ui.theme.bg_top)
        self.c2 = tk.Canvas(root, width=self.colw, height=self.colh, highlightthickness=0, bd=0,
                            bg=ui.theme.bg_top)
        self.c1.place(x=0, y=0)
        self.c2.place(x=self.colw, y=0)
        for c in (self.c1, self.c2):
            W.draw_background(c, ui, self.colw, self.colh)
        self.build_left()
        self.build_right()

    def label(self, c: tk.Canvas, x: float, y: float, text: str) -> float:
        ui = self.ui
        c.create_text(x, y, text=text, anchor="nw", font=ui.font(12, bold=True), fill=ui.theme.text_faint)
        return y + ui.px(22)

    def add(self, w: Any) -> Any:
        self.widgets.append(w)
        return w

    # ------------------------------------------------------------------ left
    def build_left(self) -> None:
        ui, c, th = self.ui, self.c1, self.ui.theme
        m = ui.px(16)
        x, wd = m, self.colw - 2 * m
        half = (wd - ui.px(8)) / 2.0
        bh = ui.px(46)
        c.create_text(x, m, text="UI kit", anchor="nw", font=ui.font(26, bold=True, display=True), fill=th.text)
        c.create_text(x + wd, m + ui.px(10), text="%s theme" % th.label, anchor="ne", font=ui.font(12),
                      fill=th.text_dim)
        y = self.label(c, x, m + ui.px(46), "Buttons")
        self.add(W.Button(ui, c, "Host a game", kind="primary", icon="▶")).place(x, y, wd, bh)
        y += bh + ui.px(10)
        self.add(W.Button(ui, c, "Join a game", kind="secondary")).place(x, y, wd, bh)
        y += bh + ui.px(10)
        self.add(W.Button(ui, c, "Settings", kind="ghost")).place(x, y, half, bh)
        self.add(W.Button(ui, c, "Leave", kind="danger")).place(x + half + ui.px(8), y, half, bh)
        y += bh + ui.px(10)
        hov = self.add(W.Button(ui, c, "Hover", kind="primary")).place(x, y, half, bh)
        hov._state = "hover"
        hov.refresh()
        dis = self.add(W.Button(ui, c, "Disabled", kind="primary")).place(x + half + ui.px(8), y, half, bh)
        dis.set_enabled(False)
        y += bh + ui.px(16)

        y = self.label(c, x, y, "Panels")
        pw = (wd - 2 * ui.px(10)) / 3.0
        phh = ui.px(64)
        for i, kind in enumerate(("card", "inset", "glass")):
            px = x + i * (pw + ui.px(10))
            self.add(W.Panel(ui, c, kind=kind)).place(px, y, pw, phh)
            c.create_text(px + pw / 2, y + phh / 2, text=kind.capitalize(), font=ui.font(14, bold=True),
                          fill=th.text)
        y += phh + ui.px(20)

        y = self.label(c, x, y, "Text field")
        fh = ui.px(44)
        tf = self.add(W.TextField(ui, c, value="192.168.1", placeholder="Host IPv4"))
        tf.place(x, y, wd, fh)
        tf.set_error("That doesn't look like an IPv4 address")
        y += fh + ui.px(30)
        self.add(W.TextField(ui, c, placeholder="Your name")).place(x, y, wd, fh)
        y += fh + ui.px(16)

        y = self.label(c, x, y, "Segmented and dropdown")
        seg = self.add(W.Segmented(ui, c, [("easy", "Easy"), ("normal", "Normal"), ("hard", "Hard")], "normal"))
        seg.place(x, y, wd, ui.px(38))
        y += ui.px(38) + ui.px(10)
        dd = self.add(W.Dropdown(ui, c, [("a", "192.168.1.5  (Wi-Fi)"), ("b", "10.0.0.7  (Ethernet)")], "a"))
        dd.place(x, y, wd, ui.px(42))

    # ------------------------------------------------------------------ right
    def build_right(self) -> None:
        ui, c, th = self.ui, self.c2, self.ui.theme
        m = ui.px(16)
        x, wd = m, self.colw - 2 * m
        self.toaster = self.add(W.Toaster(ui, c))
        y = ui.px(170)
        y = self.label(c, x, y, "Dice")
        n = 6
        gap = ui.px(6)
        ds = int(min(ui.px(46), (wd - (n - 1) * gap) / n))
        for i in range(n):
            d = self.add(W.Dice(ui, c, ds))
            d.place(x + ds / 2 + i * (ds + gap), y + ds / 2)
            d.show(i + 1)
        y += ds + ui.px(18)
        big = ui.px(64)
        self.roller = self.add(W.Dice(ui, c, big))
        self.roller.place(x + big * 0.75, y + big / 2 + ui.px(6))
        self.roller.set_color("red")
        self.roller.set_active(True)
        still = self.add(W.Dice(ui, c, big))
        still.place(x + big * 2.4, y + big / 2 + ui.px(6))
        still.set_color("blue")
        still.show(6)
        c.create_text(x + big * 3.4, y + big / 2 + ui.px(6), text="Rolling and\nyour turn", anchor="w",
                      font=ui.font(12), fill=th.text_dim)
        y += big + ui.px(22)

        y = self.label(c, x, y, "Players")
        ch = ui.px(56)
        a = self.add(W.PlayerChip(ui, c)).place(x, y, wd, ch)
        a.update("Asha", "red", active=True, subtitle="Rolling…", timer=19, me=True, timer_total=30)
        y += ch + ui.px(10)
        b = self.add(W.PlayerChip(ui, c)).place(x, y, wd, ch)
        b.update("Bot Mia", "green", bot=True, subtitle="2 tokens home")
        y += ch + ui.px(10)
        cc = self.add(W.PlayerChip(ui, c)).place(x, y, wd, ch)
        cc.update("Ravi", "yellow", active=True, timer=4, timer_total=30, subtitle="Hurry up")
        y += ch + ui.px(18)

        y = self.label(c, x, y, "Tokens")
        bh = ui.px(118)
        board = self.add(W.Panel(ui, c, kind="card"))
        board.place(x, y, wd, bh)
        ts = int(min(ui.px(50), (wd - ui.px(24)) / 4.0))
        img_h = ts

        def surface(img: gfx.Image) -> None:
            img.fill_rrect(0, 0, img.w, img.h, ui.px(12), th.board_a, grad_to=th.board_b)

        self._board = ui.sprites.get(("gal_board", th.key), wd - ui.px(12), bh - ui.px(12), surface)
        c.create_image(x + ui.px(6), y + ui.px(6), image=self._board, anchor="nw")
        self._tok = []
        for row, style in enumerate(("pawn", "disc")):
            for i, col in enumerate(("red", "green", "yellow", "blue")):
                main, light, dark = th.seat(col)
                ph = ui.sprites.get(("tok", style, col, th.key), ts, img_h,
                                    lambda img, s=style, m_=main, l_=light, d_=dark: W.paint_token(img, m_, l_, d_, s))
                self._tok.append(ph)
                cx = x + ui.px(12) + (wd - ui.px(24)) * (i + 0.5) / 4.0
                c.create_image(cx, y + ui.px(10) + row * (bh - ui.px(20)) / 2.0 + (bh - ui.px(20)) / 4.0,
                               image=ph, anchor="center")
        self.conf_at = (self.colw / 2.0, y - ui.px(40))

    # ------------------------------------------------------------------ demo
    def start(self) -> None:
        r = self.root
        r.after(150, lambda: self.toaster.show("Ravi joined the room", "info", 30))
        r.after(250, lambda: self.toaster.show("You captured a token", "good", 30))
        r.after(350, lambda: self.toaster.show("Not your turn yet", "bad", 30))
        r.after(1100, lambda: W.confetti(self.ui, self.c2, self.conf_at[0], self.conf_at[1], 70))
        r.after(1250, lambda: self.roller.roll(5))


def main(argv: Any = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--theme", default="neon", choices=sorted(THEMES))
    ap.add_argument("--size", default="M", choices=sorted(WINDOW_SIZES))
    ap.add_argument("--shot", default=None, help="save a PNG of the window and exit")
    ap.add_argument("--delay", type=float, default=1.5, help="seconds before --shot")
    args = ap.parse_args(argv)
    _dpi_aware()
    root = tk.Tk()
    g = Gallery(root, args.theme, args.size)
    g.start()
    if args.shot:
        def shoot() -> None:
            from tools.snapshot import capture
            try:
                capture(root, args.shot)
            finally:
                root.destroy()
        root.after(int(args.delay * 1000), shoot)
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
