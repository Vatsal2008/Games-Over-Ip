"""Replay complete recorded games (events + states, in wire order) into every game View.

Seat 0 is a human driven by the idle timer, so 'your turn' paths are exercised too.
Any exception inside a Tk callback fails the test.
"""
from __future__ import annotations

import logging
import random
import time
import tkinter as tk
import unittest
from typing import Any, Dict, List, Optional, Tuple

from goip import widgets
from goip.games import GAME_IDS, logic_class, view_class, view_module
from goip.games.base import Seat
from goip.games.testing import FakeHost, wire
from goip.games.viewbase import ViewContext
from goip.gfx import Image
from goip.theme import THEMES

SIZES = {"S": (360, 474), "M": (420, 554), "L": (520, 694)}


class RecordingHost(FakeHost):
    def __init__(self, watch: int) -> None:
        super().__init__()
        self.watch = watch
        self.stream: List[Tuple[str, Any]] = []

    def push_state(self) -> None:
        super().push_state()
        self.stream.append(("state", self.views[self.watch]))

    def send_event(self, event: Dict[str, Any], to_seat: Optional[int] = None) -> None:
        super().send_event(event, to_seat)
        if to_seat is None or to_seat == self.watch:
            self.stream.append(("event", wire(event)))


def record(game_id: str, n: int, seed: int) -> Tuple[List[Tuple[str, Any]], List[Dict[str, Any]], Dict[str, Any]]:
    cls = logic_class(game_id)
    host = RecordingHost(watch=0)
    colors = cls.seat_colors(n)
    seats = [Seat(i, "Player %d" % i, is_bot=(i != 0), color=colors[i]) for i in range(n)]
    logic = cls(host, seats, {"turn_timer": 15}, random.Random(seed))
    host.attach(logic)
    logic.start()
    result = host.run_until_over()
    assert result is not None, "recorded %s game did not finish" % game_id
    return host.stream, [s.public() for s in seats], dict(logic.options)


class ViewReplayTest(unittest.TestCase):
    root: Any = None

    @classmethod
    def setUpClass(cls) -> None:
        try:
            cls.root = tk.Tk()
        except tk.TclError as exc:  # pragma: no cover - headless CI
            raise unittest.SkipTest("no display: %s" % exc)
        cls.root.withdraw()

    @classmethod
    def tearDownClass(cls) -> None:
        if cls.root is not None:
            cls.root.destroy()

    def setUp(self) -> None:
        self.errors: List[Any] = []
        self.root.report_callback_exception = lambda *exc: self.errors.append(exc)  # type: ignore[assignment]
        errors = self.errors

        class Catch(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                errors.append(record.getMessage())

        self.handler = Catch(level=logging.ERROR)
        logging.getLogger("goip").addHandler(self.handler)

    def tearDown(self) -> None:
        logging.getLogger("goip").removeHandler(self.handler)

    def pump(self, seconds: float = 0.0) -> None:
        end = time.monotonic() + seconds
        while True:
            self.root.update()
            if time.monotonic() >= end:
                break
            time.sleep(0.005)

    def replay(self, game_id: str, n: int, theme_key: str, size: str, my_seat: Optional[int],
               limit: Optional[int] = None, seed: int = 3) -> None:
        stream, seats, options = record(game_id, n, seed)
        ui = widgets.UI(self.root, THEMES[theme_key])
        w, h = SIZES[size]
        ui.set_scale(min(w / 420.0, h / 554.0))
        canvas = tk.Canvas(self.root, width=w, height=h, highlightthickness=0)
        canvas.pack()
        toasts: List[str] = []
        sent: List[Dict[str, Any]] = []
        ctx = ViewContext(ui=ui, my_seat=my_seat, seats=seats, options=options, is_host=True,
                          send_action=sent.append, toast=toasts.append)
        view = view_class(game_id)(canvas, ctx)
        try:
            view.on_resize(w, h)
            for i, (kind, payload) in enumerate(stream[:limit] if limit else stream):
                if kind == "state":
                    view.on_state(payload)
                else:
                    view.on_event(payload)
                if i % 7 == 0:
                    self.pump()
                if i == 10:
                    view.on_resize(w - 30, h - 20)      # resize mid-game
                    seats[1] = dict(seats[1], bot=True, name="Bot Mia")
                    view.on_seats(list(seats))
                if i == 14:
                    for key in ("space", "1", "5", "Return", "s", "KP_7"):
                        view.on_key(key)
            self.pump(1.2)                                # let animations finish
            self.assertGreater(len(canvas.find_all()), 5, "view drew nothing")
        finally:
            view.destroy()
            self.pump(0.05)
            self.assertFalse(ui.anim.busy(view), "tweens left running after destroy")
            canvas.destroy()
        self.assertEqual(self.errors, [], "Tk callback errors: %r" % (self.errors[:3],))

    def test_full_games_neon_M(self) -> None:
        for gid in GAME_IDS:
            n = 2 if gid == "tictactoe" else 4
            with self.subTest(game=gid):
                self.replay(gid, n, "neon", "M", my_seat=0)

    def test_other_themes_and_sizes(self) -> None:
        for gid in GAME_IDS:
            for theme, size, n in (("paper", "S", 2), ("dim", "L", 3 if gid != "tictactoe" else 2)):
                with self.subTest(game=gid, theme=theme, size=size):
                    self.replay(gid, n, theme, size, my_seat=0, limit=80, seed=5)

    def test_spectator(self) -> None:
        for gid in GAME_IDS:
            with self.subTest(game=gid):
                self.replay(gid, 2, "neon", "S", my_seat=None, limit=60, seed=9)

    def test_paint_icons(self) -> None:
        for gid in GAME_IDS:
            for theme in THEMES.values():
                img = Image(64, 64)
                view_module(gid).paint_icon(img, theme)
                opaque = sum(1 for i in range(3, len(img.data), 4) if img.data[i] > 0) if hasattr(img, "data") else 1
                self.assertGreater(opaque, 0, "%s icon is empty" % gid)


if __name__ == "__main__":
    unittest.main()
