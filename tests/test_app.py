"""Smoke tests for the real App: every screen, theme and size, boss key, and a hosted game."""
from __future__ import annotations

import os
import shutil
import tempfile
import time
import tkinter as tk
import unittest
from typing import Any, Callable, List

from goip import WINDOW_SIZES
from goip.client import Client
from goip.theme import THEMES


class AppTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.mkdtemp()
        os.environ["GOIP_SETTINGS"] = os.path.join(self.tmp, "settings.json")
        from goip.app import App
        try:
            self.app = App(dpi_aware=False)
        except tk.TclError as exc:  # pragma: no cover - headless CI
            raise unittest.SkipTest("no display: %s" % exc)
        self.errors: List[Any] = []
        self.app.root.report_callback_exception = lambda *exc: self.errors.append(exc)  # type: ignore[assignment]
        self.friends: List[Client] = []

    def tearDown(self) -> None:
        for f in self.friends:
            f.close()
        self.app.quit()
        os.environ.pop("GOIP_SETTINGS", None)
        shutil.rmtree(self.tmp, ignore_errors=True)
        self.assertEqual(self.errors, [], "Tk callback errors: %r" % (self.errors[:3],))

    def spin(self, seconds: float) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.app.root.update()
            time.sleep(0.005)

    def wait_for(self, cond: Callable[[], bool], seconds: float = 8.0) -> bool:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            self.app.root.update()
            if cond():
                return True
            time.sleep(0.005)
        return False

    def test_every_screen_theme_and_size(self) -> None:
        a = self.app
        for theme in THEMES:
            a.set_theme(theme)
            for go in (a.go_menu, a.go_host, a.go_join, a.go_settings):
                go()
                for size in WINDOW_SIZES:
                    a.apply_size(size)
                    self.spin(0.08)
        a.set_theme("neon")
        self.assertEqual(a.root.title(), THEMES["neon"].title)
        a.set_theme("paper")
        self.assertEqual(a.root.title(), "Notes")

    def test_boss_key_notes_and_minimise(self) -> None:
        a = self.app
        a.settings["boss"] = "notes"
        a.boss_key()
        self.spin(0.1)
        self.assertTrue(a.notes is not None and a.notes.visible)
        self.assertEqual(a.root.title(), "Notes")
        a.notes.text.insert("end", "photosynthesis")
        a.boss_key()
        self.spin(0.1)
        self.assertFalse(a.notes.visible)
        self.assertIn("photosynthesis", a.settings["notes"])
        a.settings["boss"] = "minimize"
        a.boss_key()
        self.spin(0.2)
        self.assertEqual(a.root.state(), "iconic")
        a.root.deiconify()
        self.spin(0.1)

    def test_overlay_stays_above_game_redraws(self) -> None:
        from goip.screens.base import Overlay
        a = self.app
        c = tk.Canvas(a.root, width=300, height=400)
        c.place(x=0, y=0)
        self.spin(0.1)
        ov = Overlay(a.ui, c, "Shared win!", "Asha and Ravi tie!", [("Rematch", "primary", lambda: None)],
                     allow_above=("toast",))
        late = c.create_text(150, 200, text="16", tags=("bgo_num",))      # a game redraw lands on top
        toast = c.create_rectangle(0, 0, 10, 10, tags=("toast",))        # allowed above the card
        self.spin(0.3)
        order = c.find_all()
        top = c.find_withtag(ov.buttons[-1].tag)[-1]
        self.assertLess(order.index(late), order.index(top), "game item still above the overlay")
        self.assertGreater(order.index(toast), order.index(top), "toast pushed under the overlay")
        ov.destroy()
        c.destroy()

    def test_joiner_leaves_and_rejoins_same_seat(self) -> None:
        from goip.screens import JoinScreen, PlayScreen
        from goip.server import RoomServer
        a = self.app
        server = RoomServer("127.0.0.1", 0, "Kabir", "ludo", 2, 0, {"turn_timer": 15}, discovery=False)
        server.start()
        try:
            host = Client(*server.address, "Kabir", token=server.host_token)
            host.connect(timeout=5)
            self.friends.append(host)
            a.settings["name"] = "Ravi"
            a.go_join()
            scr = a.screen
            scr.f_ip.value, scr.f_port.value = "127.0.0.1", str(server.address[1])
            scr.connect()
            self.assertTrue(self.wait_for(lambda: a.welcome is not None))
            host.send({"t": "start"})
            self.assertTrue(self.wait_for(lambda: isinstance(a.screen, PlayScreen)))
            seat = a.begin["seat"]
            a.leave()                                         # Ravi closes the game
            self.spin(0.5)
            a.go_join()
            self.assertIsInstance(a.screen, JoinScreen)
            self.assertEqual(a.screen.f_ip.value, "127.0.0.1", "last host IP not remembered")
            a.screen.connect()
            self.assertTrue(self.wait_for(lambda: isinstance(a.screen, PlayScreen)), "rejoin did not resume")
            self.assertEqual(a.begin["seat"], seat, "rejoined into another seat")
            self.assertTrue(self.wait_for(lambda: a.screen.view.state is not None))
            self.assertEqual(a.screen.view.state["tokens"], server.logic.view_for(seat)["tokens"])
        finally:
            server.stop()

    def test_host_play_and_leave(self) -> None:
        from goip.screens import HostGameScreen, LobbyScreen, MenuScreen, PlayScreen
        a = self.app
        a.host_net = ("Asha", "127.0.0.1", 0)
        a.show(HostGameScreen, mode="create")
        scr = a.screen
        scr.pick("tictactoe")
        scr.people = 2
        scr._normalise_seats()
        scr.go()
        self.assertTrue(self.wait_for(lambda: isinstance(a.screen, LobbyScreen)), "lobby did not open")
        ip, port = a.server.address
        friend = Client(ip, port, "Ravi")
        friend.connect(timeout=5)
        self.friends.append(friend)
        self.assertTrue(self.wait_for(lambda: sum(1 for s in a.room["seats"] if s["kind"] == "human") == 2))
        a.go_change_game()
        self.spin(0.2)
        a.screen.pick("bingo")
        a.screen.go()        # configure -> back to lobby
        self.assertTrue(self.wait_for(lambda: (a.room or {}).get("game") == "bingo"), "configure ignored")
        a.screen.start()
        self.assertTrue(self.wait_for(lambda: isinstance(a.screen, PlayScreen)), "play did not open")
        self.assertTrue(self.wait_for(lambda: a.screen.view is not None and a.screen.view.state is not None))
        a.screen.view.on_key("s")            # shuffle my board
        self.spin(0.5)
        a.leave()
        self.assertIsInstance(a.screen, MenuScreen)
        got_bye = False
        end = time.monotonic() + 5
        while time.monotonic() < end and not got_bye:
            got_bye = any(m.get("t") == "_disconnected" for m in friend.poll())
            time.sleep(0.02)
        self.assertTrue(got_bye, "friend was not told the room closed")


if __name__ == "__main__":
    unittest.main()
