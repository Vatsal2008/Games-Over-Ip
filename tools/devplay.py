"""Play or watch one game in a window, without the app screens.

    python tools/devplay.py ludo --humans 1 --bots 3            # you are seat 0
    python tools/devplay.py snakes --bots 3 --autoplay           # everyone is a bot
    python tools/devplay.py bingo --autoplay --shot shots/bingo.png --seconds 4 --count 3
    python tools/devplay.py tictactoe --theme paper --size S --options bot_level=hard,match=3

Runs a real RoomServer on 127.0.0.1 (random port) plus one Client, then mounts the
game's View on a canvas. Extra human seats (--humans > 1) are filled by silent
headless clients, so the idle timer plays for them.
"""
from __future__ import annotations

import argparse
import logging
import os
import sys
import tkinter as tk
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from goip import BASE_SIZE, WINDOW_SIZES  # noqa: E402
from goip import protocol as P  # noqa: E402
from goip.client import Client  # noqa: E402
from goip.games import logic_class, view_class  # noqa: E402
from goip.games.viewbase import ViewContext  # noqa: E402
from goip.server import RoomServer  # noqa: E402
from goip.theme import get_theme  # noqa: E402
from goip import widgets  # noqa: E402

HEADER = 34


def parse_options(text: str) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    for part in filter(None, (p.strip() for p in (text or "").split(","))):
        key, _, value = part.partition("=")
        if value.lower() in ("true", "false", "on", "off"):
            out[key] = value.lower() in ("true", "on")
        else:
            try:
                out[key] = int(value)
            except ValueError:
                out[key] = value
    return out


def dpi_aware() -> None:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass


class DevPlay:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        dpi_aware()
        self.root = tk.Tk()
        self.dpi = max(1.0, self.root.winfo_fpixels("1i") / 96.0)
        theme = get_theme(args.theme)
        w, h = WINDOW_SIZES[args.size]
        self.root.geometry("%dx%d+40+40" % (int(w * self.dpi), int(h * self.dpi)))
        self.root.title("devplay - %s" % args.game)
        self.ui = widgets.UI(self.root, theme)
        self.canvas = tk.Canvas(self.root, highlightthickness=0, bd=0, bg=theme.bg_top)
        self.canvas.pack(fill="both", expand=True)
        self.toaster = widgets.Toaster(self.ui, self.canvas)
        self.view: Optional[Any] = None
        self.view_canvas: Optional[tk.Canvas] = None
        self.extra: List[Client] = []
        self.shots_taken = 0
        self._resize_job: Optional[str] = None

        resolver = None
        if args.autoplay:
            base = logic_class(args.game)

            class Auto(base):  # type: ignore[misc, valid-type]
                def __init__(self, host, seats, options=None, rng=None):
                    super().__init__(host, seats, options, rng)
                    for seat in self.seats:
                        seat.is_bot = True

            resolver = lambda _gid: Auto  # noqa: E731

        self.server = RoomServer("127.0.0.1", 0, "You", args.game, args.humans, args.bots,
                                 parse_options(args.options), discovery=False,
                                 resolver=resolver, seed=args.seed)
        self.server.start()
        ip, port = self.server.address
        self.client = Client(ip, port, "You", token=self.server.host_token)
        self.client.connect(timeout=5)
        for i in range(1, args.humans):
            friend = Client(ip, port, "Friend %d" % (i + 1))
            friend.connect(timeout=5)
            self.extra.append(friend)
        self.client.send({"t": P.START})

        self.canvas.bind("<Configure>", self._on_configure)
        self.root.bind("<Key>", self._on_key)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self.root.after(15, self._pump)
        if args.shot:
            self.root.after(int(args.seconds * 1000), self._shot)

    # ------------------------------------------------------------------ layout
    def _size(self):
        return max(1, self.canvas.winfo_width()), max(1, self.canvas.winfo_height())

    def _on_configure(self, _event=None) -> None:
        if self._resize_job:
            self.root.after_cancel(self._resize_job)
        self._resize_job = self.root.after(30, self._layout)

    def _layout(self) -> None:
        self._resize_job = None
        w, h = self._size()
        bw, bh = BASE_SIZE
        self.ui.scale = max(0.8, min(1.6, min(w / (bw * self.dpi), h / (bh * self.dpi)) * self.dpi))
        widgets.draw_background(self.canvas, self.ui, w, h)
        hh = self.ui.px(HEADER)
        self.canvas.delete("hdr")
        self.canvas.create_text(self.ui.px(14), hh // 2, anchor="w", tags="hdr",
                                text="devplay - %s" % self.args.game,
                                font=self.ui.font(13, bold=True), fill=self.ui.theme.text_dim)
        if self.view_canvas is not None:
            self.view_canvas.place(x=0, y=hh, width=w, height=max(1, h - hh))
            self.view_canvas.update_idletasks()
            if self.view is not None:
                self.view.on_resize(max(1, w), max(1, h - hh))

    # -------------------------------------------------------------- networking
    def _pump(self) -> None:
        for friend in self.extra:
            friend.poll()
        for msg in self.client.poll():
            try:
                self._route(msg)
            except Exception:
                logging.exception("devplay: error handling %s", msg.get("t"))
        self.root.after(15, self._pump)

    def _route(self, msg: Dict[str, Any]) -> None:
        t = msg.get("t")
        if t == P.BEGIN:
            self._open_view(msg)
        elif t == P.STATE and self.view is not None:
            self.view.on_state(msg["s"])
        elif t == P.EVENT and self.view is not None:
            self.view.on_event(msg["e"])
        elif t == P.SEATS and self.view is not None:
            self.view.on_seats(msg["seats"])
        elif t == P.OVER:
            text = msg.get("result", {}).get("text", "Game over")
            self.toaster.show(text, kind="good", seconds=4)
            if self.args.loop:
                self.root.after(3500, lambda: self.client.send({"t": P.START}))
        elif t in (P.ERROR, P.NOTICE):
            self.toaster.show(msg.get("msg", ""), kind="bad" if t == P.ERROR else "info")
        elif t in (P.L_DISCONNECTED, P.BYE):
            logging.warning("devplay: disconnected: %s", msg.get("reason"))

    def _open_view(self, begin: Dict[str, Any]) -> None:
        if self.view is not None:
            self.view.destroy()
        if self.view_canvas is not None:
            self.view_canvas.destroy()
        self.view_canvas = tk.Canvas(self.canvas, highlightthickness=0, bd=0, bg=self.ui.theme.bg_bottom)
        ctx = ViewContext(
            ui=self.ui, my_seat=begin.get("seat"), seats=begin.get("seats", []),
            options=begin.get("options", {}), is_host=True,
            send_action=self.client.act,
            toast=lambda text: self.toaster.show(text),
        )
        self.view = view_class(self.args.game)(self.view_canvas, ctx)
        self._layout()

    def _on_key(self, event: tk.Event) -> None:
        if event.keysym == "Escape":
            self.quit()
        elif self.view is not None:
            self.view.on_key(event.keysym)

    # ----------------------------------------------------------- screenshots
    def _shot(self) -> None:
        from tools.snapshot import capture
        self.shots_taken += 1
        path = self.args.shot
        if self.args.count > 1:
            stem, ext = os.path.splitext(path)
            path = "%s_%d%s" % (stem, self.shots_taken, ext or ".png")
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        capture(self.root, path)
        print("saved", path, flush=True)
        if self.shots_taken < self.args.count:
            self.root.after(int(self.args.seconds * 1000), self._shot)
        else:
            self.root.after(200, self.quit)

    def quit(self) -> None:
        for c in [self.client] + self.extra:
            try:
                c.close()
            except Exception:
                pass
        try:
            self.server.stop()
        except Exception:
            pass
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def run(self) -> None:
        self.root.mainloop()


def main(argv: Optional[List[str]] = None) -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("game")
    ap.add_argument("--humans", type=int, default=1)
    ap.add_argument("--bots", type=int, default=1)
    ap.add_argument("--autoplay", action="store_true", help="every seat (including yours) is a bot")
    ap.add_argument("--loop", action="store_true", help="start a rematch after each game")
    ap.add_argument("--options", default="", help="comma list, e.g. bot_level=hard,turn_timer=0")
    ap.add_argument("--theme", default="neon", choices=["neon", "dim", "paper"])
    ap.add_argument("--size", default="M", choices=sorted(WINDOW_SIZES))
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--shot", default="", help="save a PNG screenshot to this path, then exit")
    ap.add_argument("--seconds", type=float, default=5.0, help="delay before (each) screenshot")
    ap.add_argument("--count", type=int, default=1, help="number of screenshots, one every --seconds")
    args = ap.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    DevPlay(args).run()


if __name__ == "__main__":
    main()
