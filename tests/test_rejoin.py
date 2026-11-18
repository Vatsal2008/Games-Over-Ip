"""Rejoining mid-game gives the player their seat (and pieces) back; Snakes boards change per game."""
from __future__ import annotations

import random
import time
import unittest
from typing import Any, Dict, List, Optional

from goip import protocol as P
from goip.client import Client
from goip.games.snakes.logic import CLASSIC_LADDERS, CLASSIC_SNAKES, Logic as SnakesLogic
from goip.games.testing import make_game
from goip.server import RoomServer


def drain(client: Client, until: str, seconds: float = 6.0) -> List[Dict[str, Any]]:
    got: List[Dict[str, Any]] = []
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        msgs = client.poll()
        got.extend(msgs)
        if any(m.get("t") == until for m in msgs):
            return got
        time.sleep(0.01)
    return got


def last_state(msgs: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    states = [m["s"] for m in msgs if m.get("t") == P.STATE]
    return states[-1] if states else None


class RejoinTest(unittest.TestCase):
    def setUp(self) -> None:
        self.clients: List[Client] = []
        self.server: Optional[RoomServer] = None

    def tearDown(self) -> None:
        for c in self.clients:
            c.close()
        if self.server is not None:
            self.server.stop()

    def open_room(self, game: str, options: Dict[str, Any]) -> None:
        self.server = RoomServer("127.0.0.1", 0, "Host", game, 2, 0, options, discovery=False, seed=11)
        self.server.start()
        self.host = self.client("Host", token=self.server.host_token)

    def client(self, name: str, token: Optional[str] = None) -> Client:
        c = Client(*self.server.address, name, token=token)  # type: ignore[union-attr]
        c.connect(timeout=5)
        self.clients.append(c)
        return c

    def test_leave_and_rejoin_gets_same_seat_and_board(self) -> None:
        self.open_room("bingo", {"turn_timer": 15})
        friend = self.client("Ravi")
        self.host.send({"t": P.START})
        first = drain(friend, P.STATE)
        seat = [m for m in first if m.get("t") == P.BEGIN][0]["seat"]
        board_before = last_state(first)["board"]
        friend.close()                                   # Ravi walks away; a bot takes the seat
        notices = drain(self.host, P.NOTICE)
        self.assertTrue(any("bot took over" in m.get("msg", "") for m in notices if m.get("t") == P.NOTICE))
        back = self.client("ravi")                       # name match is case-insensitive
        msgs = drain(back, P.STATE)
        begins = [m for m in msgs if m.get("t") == P.BEGIN]
        self.assertTrue(begins, "rejoining player got no begin")
        self.assertEqual(begins[0]["seat"], seat, "rejoined into a different seat")
        self.assertEqual(last_state(msgs)["board"], board_before, "Bingo board was not given back")
        self.assertFalse(begins[0]["seats"][seat]["bot"], "seat still marked as a bot")

    def test_rejoin_while_old_connection_still_looks_alive(self) -> None:
        """Wi-Fi drop: the host hasn't noticed yet, the player reconnects with the same name."""
        self.open_room("snakes", {"turn_timer": 15})
        friend = self.client("Ravi")
        self.host.send({"t": P.START})
        seat = [m for m in drain(friend, P.STATE) if m.get("t") == P.BEGIN][0]["seat"]
        back = self.client("Ravi")                       # old socket is still open
        msgs = drain(back, P.STATE)
        begins = [m for m in msgs if m.get("t") == P.BEGIN]
        self.assertTrue(begins, "same-name rejoin was rejected")
        self.assertEqual(begins[0]["seat"], seat)
        host_view = last_state(drain(self.host, P.STATE, 1.0)) or {}
        mine = last_state(msgs)
        self.assertEqual(mine["pos"], host_view.get("pos", mine["pos"]), "positions differ from the host's")
        gone = drain(friend, P.L_DISCONNECTED, 4)
        self.assertTrue(any(m.get("t") == P.L_DISCONNECTED for m in gone), "stale connection was not closed")

    def test_other_names_still_rejected_mid_game(self) -> None:
        self.open_room("tictactoe", {})
        self.client("Ravi")
        self.host.send({"t": P.START})
        time.sleep(0.3)
        with self.assertRaises(ConnectionError):
            self.client("Stranger")


class SnakesBoardTest(unittest.TestCase):
    def test_new_random_board_each_game_by_default(self) -> None:
        self.assertEqual(SnakesLogic.clean_options({})["layout"], "random")
        boards = set()
        for seed in range(6):
            host, game = make_game(SnakesLogic, 2, seed=seed)
            game.start()
            b = host.views[0]["board"]
            boards.add((tuple(map(tuple, b["ladders"])), tuple(map(tuple, b["snakes"]))))
            game.stop()
        self.assertEqual(len(boards), 6, "boards repeat between games")
        classic = (tuple(map(tuple, CLASSIC_LADDERS)), tuple(map(tuple, CLASSIC_SNAKES)))
        self.assertNotIn(classic, boards)

    def test_classic_still_available(self) -> None:
        host, game = make_game(SnakesLogic, 2, options={"layout": "classic"}, seed=1)
        game.start()
        self.assertEqual([tuple(x) for x in host.views[0]["board"]["ladders"]], [tuple(x) for x in CLASSIC_LADDERS])
        game.stop()


if __name__ == "__main__":
    unittest.main()
