"""Snakes and Ladders logic: board geometry, layouts, dice rules, pacing and full games."""
from __future__ import annotations

import heapq
import itertools
import random
import unittest
from typing import Any, Dict, Optional, Sequence

from goip.games.base import ActionError
from goip.games.testing import FakeHost, make_game
from goip.games.snakes.logic import (CLASSIC_LADDERS, CLASSIC_SNAKES, Logic, anim_time,
                                     random_layout, square_to_rowcol, validate_layout)

REQUIRED_KEYS = ("turn", "timer", "phase", "finished", "board", "pos", "last_roll", "streak", "winner")


class Dice(random.Random):
    """A Random whose d6 rolls come from a script (then fall back to real randomness)."""

    def __init__(self, rolls: Sequence[int]) -> None:
        super().__init__(0)
        self.rolls = list(rolls)

    def randint(self, a: int, b: int) -> int:
        if (a, b) == (1, 6) and self.rolls:
            return self.rolls.pop(0)
        return super().randint(a, b)


def advance(host: FakeHost, seconds: float) -> None:
    """FakeHost.advance that never runs a timer past the target (skips cancelled heads)."""
    target = host.t + seconds
    while True:
        while host._heap and host._heap[0][1] in host._cancelled:
            host._cancelled.discard(heapq.heappop(host._heap)[1])
        if not host._heap or host._heap[0][0] > target:
            break
        host.step()
    host.t = max(host.t, target)


def rigged(rolls: Sequence[int] = (), n: int = 2, humans: int = 2, **options: Any):
    options.setdefault("turn_timer", 0)
    host, game = make_game(Logic, n, humans=humans, options=options, seed=1)
    game.rng = Dice(rolls)
    return host, game


def last_move(host: FakeHost) -> Dict[str, Any]:
    return host.events_of("move")[-1]


class Checker:
    """Rule invariants on every push, plus a replay of the roll/move events at the end."""

    def __init__(self, tc: unittest.TestCase, host: FakeHost, game: Logic) -> None:
        self.tc, self.host, self.game = tc, host, game
        self.board: Optional[Dict[str, Any]] = None
        self.pushes = 0
        host.on_push = self

    def jumps(self) -> Dict[int, Any]:
        assert self.board is not None
        out = {a: ("ladder", b) for a, b in self.board["ladders"]}
        out.update({h: ("snake", t) for h, t in self.board["snakes"]})
        return out

    def __call__(self, host: FakeHost) -> None:
        tc, g = self.tc, self.game
        self.pushes += 1
        v = host.views[None]
        for key in REQUIRED_KEYS:
            tc.assertIn(key, v)
        n = len(g.seats)
        for seat in range(n):
            tc.assertEqual(host.views[seat], v, "snakes has no private state")
        if self.board is None:
            self.board = v["board"]
            for a, b in self.board["ladders"]:
                tc.assertLess(a, b)
            for h, t in self.board["snakes"]:
                tc.assertGreater(h, t)
            if g.layout == "random":
                tc.assertTrue(validate_layout(self.board["ladders"], self.board["snakes"]))
            else:
                tc.assertEqual(self.board["ladders"], [list(p) for p in CLASSIC_LADDERS])
                tc.assertEqual(self.board["snakes"], [list(p) for p in CLASSIC_SNAKES])
        tc.assertEqual(v["board"], self.board, "the board never changes")
        jumps = self.jumps()
        tc.assertEqual(v["phase"], "roll")
        tc.assertEqual(len(v["pos"]), n)
        for p in v["pos"]:
            tc.assertTrue(0 <= p <= 100)
            tc.assertNotIn(p, jumps, "a token never rests on a ladder foot or snake head")
        tc.assertLessEqual(v["pos"].count(100), 1)
        if v["last_roll"] is not None:
            tc.assertIn(v["last_roll"], range(1, 7))
        if v["winner"] is None:
            tc.assertFalse(v["finished"])
            tc.assertIn(v["turn"], range(n))
            tc.assertIn(v["streak"], (0, 1, 2))
            if v["streak"]:
                tc.assertTrue(g.six_again)
                tc.assertEqual(v["last_roll"], 6)
            tc.assertNotIn(100, v["pos"])
            seat = v["turn"]
            if g.is_bot(seat) or not g.options["turn_timer"]:
                tc.assertIsNone(v["timer"])
            else:
                tc.assertGreater(v["timer"], 0)
        else:
            tc.assertEqual(v["pos"][v["winner"]], 100)
            tc.assertIsNone(v["turn"])
            tc.assertIsNone(v["timer"])
            tc.assertEqual(v["ranking"][0], v["winner"])
        if v["finished"]:
            tc.assertIsNotNone(v["winner"])

    def check_game(self) -> Dict[str, Any]:
        """Replay every roll/move event against the rules and compare with the result."""
        tc, g = self.tc, self.game
        result = self.host.result
        tc.assertIsNotNone(result)
        assert result is not None
        n = len(g.seats)
        jumps = self.jumps()
        evs = [e for _to, e in self.host.events if e["kind"] in ("roll", "move")]
        tc.assertEqual(len(evs) % 2, 0)
        pos = [0] * n
        expected = 0
        sixes = 0
        winner = None
        for i in range(0, len(evs), 2):
            roll, move = evs[i], evs[i + 1]
            tc.assertIsNone(winner, "no rolls after someone reached 100")
            tc.assertEqual(roll["kind"], "roll")
            tc.assertEqual(move["kind"], "move")
            seat, value = roll["seat"], roll["value"]
            tc.assertEqual(seat, expected)
            tc.assertEqual(move["seat"], seat)
            tc.assertIn(value, range(1, 7))
            frm = pos[seat]
            tc.assertEqual(move["from"], frm)
            target = frm + value
            if target > 100 and g.exact_finish:
                tc.assertEqual(move["path"], [])
                tc.assertTrue(move["overshoot"])
                tc.assertEqual(move["to"], frm)
                tc.assertIsNone(move["via"])
            else:
                target = min(target, 100)
                tc.assertFalse(move["overshoot"])
                tc.assertEqual(move["path"], list(range(frm + 1, target + 1)))
                if target in jumps:
                    kind, dest = jumps[target]
                    tc.assertEqual(move["via"], {"type": kind, "from": target, "to": dest})
                    tc.assertEqual(move["to"], dest)
                else:
                    tc.assertIsNone(move["via"])
                    tc.assertEqual(move["to"], target)
            pos[seat] = move["to"]
            if pos[seat] == 100:
                winner = seat
                continue
            if value == 6 and g.six_again:
                sixes += 1
                if sixes < 3:
                    continue
            sixes = 0
            expected = (seat + 1) % n
        tc.assertIsNotNone(winner)
        tc.assertEqual(result["winners"], [winner])
        tc.assertEqual(result["pos"], pos)
        tc.assertEqual(result["ranking"][0], winner)
        tc.assertEqual(sorted(result["ranking"]), list(range(n)))
        others = [pos[s] for s in result["ranking"][1:]]
        tc.assertEqual(others, sorted(others, reverse=True))
        tc.assertIn(g.name(winner), result["text"])
        tc.assertTrue(self.host.views[None]["finished"])
        return result


class GeometryTests(unittest.TestCase):
    def test_corners(self):
        self.assertEqual(square_to_rowcol(1), (9, 0))
        self.assertEqual(square_to_rowcol(10), (9, 9))
        self.assertEqual(square_to_rowcol(11), (8, 9))
        self.assertEqual(square_to_rowcol(20), (8, 0))
        self.assertEqual(square_to_rowcol(21), (7, 0))
        self.assertEqual(square_to_rowcol(91), (0, 9))
        self.assertEqual(square_to_rowcol(100), (0, 0))
        self.assertEqual(square_to_rowcol(0), (9, -1))

    def test_every_square_unique_and_neighbours_adjacent(self):
        cells = [square_to_rowcol(n) for n in range(1, 101)]
        self.assertEqual(len(set(cells)), 100)
        for r, c in cells:
            self.assertTrue(0 <= r <= 9 and 0 <= c <= 9)
        for a, b in zip(cells, cells[1:]):
            self.assertEqual(abs(a[0] - b[0]) + abs(a[1] - b[1]), 1)

    def test_out_of_range(self):
        for bad in (-1, 101):
            with self.assertRaises(ValueError):
                square_to_rowcol(bad)


class LayoutTests(unittest.TestCase):
    def test_classic_layout_matches_spec(self):
        self.assertEqual(CLASSIC_LADDERS, ((1, 38), (4, 14), (9, 31), (21, 42), (28, 84), (36, 44),
                                           (51, 67), (71, 91), (80, 100)))
        self.assertEqual(CLASSIC_SNAKES, ((16, 6), (47, 26), (49, 11), (56, 53), (62, 19), (64, 60),
                                          (87, 24), (93, 73), (95, 75), (98, 78)))

    def test_random_layouts_are_valid_for_500_seeds(self):
        seen = set()
        for seed in range(500):
            ladders, snakes = random_layout(random.Random(seed))
            self.assertEqual(len(ladders), 8)
            self.assertEqual(len(snakes), 8)
            ends = []
            for a, b in ladders:
                self.assertGreaterEqual(b - a, 10)
                self.assertNotEqual(a, 1)
                self.assertNotEqual(b, 100)
                self.assertTrue(1 <= a < b <= 100)
                ends += [a, b]
            for h, t in snakes:
                self.assertGreaterEqual(h - t, 10)
                self.assertNotEqual(h, 100)
                self.assertTrue(1 <= t < h <= 100)
                ends += [h, t]
            self.assertEqual(len(set(ends)), 32, "all endpoints distinct")
            starts = {a for a, _ in ladders} | {h for h, _ in snakes}
            finishes = {b for _, b in ladders} | {t for _, t in snakes}
            self.assertFalse(starts & finishes, "no chains")
            self.assertTrue(validate_layout(ladders, snakes))
            seen.add((tuple(ladders), tuple(snakes)))
        self.assertGreater(len(seen), 490)

    def test_random_layout_is_deterministic(self):
        self.assertEqual(random_layout(random.Random(12)), random_layout(random.Random(12)))
        self.assertNotEqual(random_layout(random.Random(12)), random_layout(random.Random(13)))

    def test_validate_layout_rejects_bad_boards(self):
        ladders, snakes = random_layout(random.Random(4))
        self.assertTrue(validate_layout(ladders, snakes))
        self.assertFalse(validate_layout(ladders[:-1], snakes))
        self.assertFalse(validate_layout(ladders, snakes[:-1]))
        self.assertFalse(validate_layout(list(CLASSIC_LADDERS), list(CLASSIC_SNAKES)))

        def with_ladder(pair):
            return validate_layout(ladders[:-1] + [pair], snakes)

        used = {x for p in ladders[:-1] + snakes for x in p}
        free = [x for x in range(2, 100) if x not in used]
        a = free[0]
        b = next(x for x in free if x >= a + 10)
        self.assertTrue(with_ladder((a, b)))
        self.assertFalse(with_ladder((1, 40)), "ladder from 1")
        self.assertFalse(with_ladder((a, 100)), "ladder to 100")
        self.assertFalse(with_ladder((a, a + 9)), "too short")
        self.assertFalse(with_ladder((b, a)), "ladder going down")
        head = snakes[0][0]
        self.assertFalse(with_ladder((a, head)), "chain: ladder top is a snake head")
        bad_snakes = snakes[:-1] + [(100, 50)]
        self.assertFalse(validate_layout(ladders, bad_snakes), "snake head on 100")

    def test_random_option_uses_a_valid_random_board(self):
        boards = set()
        for seed in range(5):
            host, game = make_game(Logic, 2, humans=0, options={"layout": "random"}, seed=seed)
            game.start()
            b = host.views[0]["board"]
            self.assertTrue(validate_layout(b["ladders"], b["snakes"]))
            boards.add(repr(b))
        self.assertEqual(len(boards), 5)


class MetadataTests(unittest.TestCase):
    def test_class_attributes(self):
        self.assertEqual(Logic.ID, "snakes")
        self.assertEqual(Logic.NAME, "Snakes & Ladders")
        self.assertLessEqual(len(Logic.TAGLINE), 28)
        self.assertEqual(Logic.SEAT_COUNTS, (2, 3, 4))
        self.assertEqual(Logic.clean_options({}), {"layout": "classic", "six_again": True,
                                                   "exact_finish": True, "turn_timer": 30})
        self.assertEqual(Logic.clean_options({"layout": "random", "six_again": False,
                                              "exact_finish": 0, "turn_timer": 15}),
                         {"layout": "random", "six_again": False, "exact_finish": True,
                          "turn_timer": 15})
        self.assertNotIn("bot_level", [o.key for o in Logic.OPTIONS])

    def test_registry(self):
        from goip.games import logic_class
        self.assertIs(logic_class("snakes"), Logic)


class RuleTests(unittest.TestCase):
    def test_start_state(self):
        host, game = rigged()
        game.start()
        v = host.views[1]
        self.assertEqual(v["pos"], [0, 0])
        self.assertEqual(v["turn"], 0)
        self.assertEqual(v["streak"], 0)
        self.assertIsNone(v["last_roll"])
        self.assertIsNone(v["winner"])
        self.assertFalse(v["finished"])
        self.assertIsNone(v["timer"])

    def test_ladder_from_square_one(self):
        host, game = rigged([1])
        game.start()
        host.act(0, a="roll")
        self.assertEqual(host.events_of("roll")[-1], {"kind": "roll", "seat": 0, "value": 1})
        self.assertEqual(last_move(host), {"kind": "move", "seat": 0, "from": 0, "path": [1], "to": 38,
                                           "via": {"type": "ladder", "from": 1, "to": 38},
                                           "overshoot": False, "value": 1})
        v = host.views[0]
        self.assertEqual(v["pos"], [38, 0])
        self.assertEqual(v["turn"], 1)
        self.assertEqual(v["last_roll"], 1)
        self.assertEqual(v["last_move"]["to"], 38)
        # the roll event comes before the move event
        kinds = [e["kind"] for _to, e in host.events]
        self.assertEqual(kinds[-2:], ["roll", "move"])

    def test_snake_slides_down(self):
        host, game = rigged([3])
        game.start()
        game.pos[0] = 13
        host.act(0, a="roll")
        mv = last_move(host)
        self.assertEqual(mv["path"], [14, 15, 16])
        self.assertEqual(mv["via"], {"type": "snake", "from": 16, "to": 6})
        self.assertEqual(game.pos[0], 6)

    def test_passing_over_a_ladder_does_nothing(self):
        host, game = rigged([5])
        game.start()
        host.act(0, a="roll")                 # 0 -> 5 passes ladders at 1 and 4
        self.assertEqual(game.pos[0], 5)
        self.assertIsNone(last_move(host)["via"])

    def test_exact_finish_overshoot_stays(self):
        host, game = rigged([5])
        game.start()
        game.pos[0] = 97
        host.act(0, a="roll")
        mv = last_move(host)
        self.assertEqual((mv["path"], mv["to"], mv["overshoot"], mv["via"]), ([], 97, True, None))
        self.assertEqual(game.pos[0], 97)
        self.assertEqual(game.turn, 1)

    def test_exact_finish_hit_wins(self):
        host, game = rigged([6])
        checker = Checker(self, host, game)
        game.start()
        game.pos[0] = 94
        host.act(0, a="roll")
        self.assertEqual(game.pos[0], 100)
        self.assertEqual(host.views[0]["winner"], 0)
        self.assertIsNone(host.views[0]["turn"])
        self.assertIsNone(host.result, "finish waits for the move animation")
        with self.assertRaisesRegex(ActionError, "over"):
            host.act(1, a="roll")
        advance(host, anim_time(6, False) - 0.01)
        self.assertIsNone(host.result)
        advance(host, 0.02)
        self.assertEqual(host.result["winners"], [0])
        self.assertEqual(host.result["text"], "P0 wins!")
        self.assertEqual(host.result["ranking"], [0, 1])
        self.assertTrue(host.views[1]["finished"])
        with self.assertRaisesRegex(ActionError, "over"):
            host.act(1, a="roll")
        self.assertGreater(checker.pushes, 2)

    def test_no_exact_finish_overshoot_wins(self):
        host, game = rigged([5], exact_finish=False)
        game.start()
        game.pos[0] = 97
        host.act(0, a="roll")
        mv = last_move(host)
        self.assertEqual((mv["path"], mv["to"], mv["overshoot"]), ([98, 99, 100], 100, False))
        host.run_until_over()
        self.assertEqual(host.result["winners"], [0])

    def test_ladder_to_hundred_wins(self):
        host, game = rigged([4], n=3)
        game.start()
        game.pos[0] = 76
        game.pos[2] = 50
        host.act(0, a="roll")
        self.assertEqual(last_move(host)["via"], {"type": "ladder", "from": 80, "to": 100})
        host.run_until_over()
        self.assertEqual(host.result["winners"], [0])
        self.assertEqual(host.result["ranking"], [0, 2, 1])

    def test_six_rolls_again_at_most_three_times(self):
        host, game = rigged([6, 6, 6, 2, 6, 3])
        game.start()
        host.act(0, a="roll")
        self.assertEqual((game.turn, game.streak, game.pos[0]), (0, 1, 6))
        host.act(0, a="roll")
        self.assertEqual((game.turn, game.streak, game.pos[0]), (0, 2, 12))
        host.act(0, a="roll")
        self.assertEqual((game.turn, game.streak, game.pos[0]), (1, 0, 18), "third six ends the turn")
        host.act(1, a="roll")
        self.assertEqual((game.turn, game.streak, game.pos[1]), (0, 0, 2))
        host.act(0, a="roll")
        self.assertEqual((game.turn, game.streak), (0, 1))
        host.act(0, a="roll")
        self.assertEqual((game.turn, game.streak), (1, 0), "a non-six ends the streak")

    def test_six_again_off(self):
        host, game = rigged([6, 6], six_again=False)
        game.start()
        host.act(0, a="roll")
        self.assertEqual((game.turn, game.streak), (1, 0))

    def test_errors(self):
        host, game = rigged([2])
        game.start()
        with self.assertRaisesRegex(ActionError, "^Not your turn$"):
            host.act(1, a="roll")
        with self.assertRaises(ActionError):
            host.act(0, a="move")
        with self.assertRaises(ActionError):
            host.act(0)
        with self.assertRaises(ActionError):
            game.on_action(0, None)  # type: ignore[arg-type]
        self.assertEqual(game.pos, [0, 0])
        host.act(0, a="roll")
        with self.assertRaisesRegex(ActionError, "^Not your turn$"):
            host.act(0, a="roll")


class PacingTests(unittest.TestCase):
    def test_bot_waits_for_the_animation(self):
        host, game = rigged([3, 2], humans=1)
        game.start()
        host.act(0, a="roll")                     # 0 -> 3, three steps, no jump
        wait = anim_time(3, False) + Logic.BOT_DELAY
        advance(host, wait - 0.01)
        self.assertEqual(len(host.events_of("roll")), 1)
        advance(host, 0.02)
        self.assertEqual(len(host.events_of("roll")), 2)
        self.assertEqual(host.events_of("roll")[-1]["seat"], 1)

    def test_jump_adds_time(self):
        self.assertAlmostEqual(anim_time(0, False), 0.6)
        self.assertAlmostEqual(anim_time(5, False), 0.6 + 0.8)
        self.assertAlmostEqual(anim_time(5, True), 0.6 + 0.8 + 0.7)
        host, game = rigged([1, 2], humans=1)
        game.start()
        host.act(0, a="roll")                     # ladder 1 -> 38
        wait = anim_time(1, True) + Logic.BOT_DELAY
        advance(host, wait - 0.01)
        self.assertEqual(len(host.events_of("roll")), 1)
        advance(host, 0.02)
        self.assertEqual(len(host.events_of("roll")), 2)

    def test_first_bot_roll_waits_a_moment(self):
        host, game = rigged([2], humans=0)
        game.start()
        advance(host, Logic.START_DELAY + Logic.BOT_DELAY - 0.01)
        self.assertEqual(host.events_of("roll"), [])
        advance(host, 0.02)
        self.assertEqual(len(host.events_of("roll")), 1)

    def test_idle_human_auto_rolls(self):
        host, game = rigged([2, 2, 3], humans=1, turn_timer=15)
        game.start()
        self.assertEqual(host.views[0]["timer"], 15 + Logic.START_DELAY)
        advance(host, 15 + Logic.START_DELAY - 0.05)
        self.assertEqual(host.events_of("roll"), [])
        advance(host, 0.1)
        self.assertEqual(host.events_of("roll")[0]["seat"], 0)
        self.assertEqual(game.pos[0], 2)

    def test_human_action_cancels_idle_timer(self):
        host, game = rigged([4, 2, 3], n=2, humans=2, turn_timer=15)
        game.start()
        advance(host, 5)
        host.act(0, a="roll")
        deadline = 5 + 15 + anim_time(4, True)    # 4 is a ladder foot
        self.assertAlmostEqual(host.views[1]["timer"], deadline - 5, places=1)
        advance(host, deadline - 5 - 0.05)
        self.assertEqual(len(host.events_of("roll")), 1)
        advance(host, 0.1)
        self.assertEqual(len(host.events_of("roll")), 2)

    def test_disconnect_hands_seat_to_bot(self):
        host, game = rigged([2, 3], humans=2)
        game.start()
        self.assertFalse(host.step(), "humans with no idle timer are waited for")
        game.seats[0].is_bot = True
        game.on_seat_changed(0)
        advance(host, Logic.BOT_DELAY + 0.01)
        self.assertEqual(game.pos[0], 2)


class FullGameTests(unittest.TestCase):
    def test_all_bot_games_every_option(self):
        combos = itertools.product((2, 3, 4), ("classic", "random"), (True, False), (True, False))
        for n, layout, six, exact in combos:
            for seed in range(2):
                with self.subTest(n=n, layout=layout, six=six, exact=exact, seed=seed):
                    opts = {"layout": layout, "six_again": six, "exact_finish": exact}
                    host, game = make_game(Logic, n, humans=0, options=opts, seed=seed)
                    checker = Checker(self, host, game)
                    game.start()
                    host.run_until_over()
                    checker.check_game()

    def test_humans_driven_by_act(self):
        host, game = make_game(Logic, 3, humans=2, options={"turn_timer": 0}, seed=5)
        checker = Checker(self, host, game)
        game.start()
        steps = 0
        while host.result is None and steps < 5000:
            steps += 1
            if game.turn is not None and not game.is_bot(game.turn):
                host.act(game.turn, a="roll")
            elif not host.step():
                break
        checker.check_game()

    def test_idle_humans_finish(self):
        host, game = make_game(Logic, 4, humans=4, options={"turn_timer": 15}, seed=8)
        checker = Checker(self, host, game)
        game.start()
        host.run_until_over()
        result = checker.check_game()
        rolls = len(host.events_of("roll"))
        self.assertGreater(host.t, 15 * rolls)
        self.assertIn(result["winners"][0], range(4))

    def test_deterministic_per_seed(self):
        runs = []
        for _ in range(2):
            host, game = make_game(Logic, 4, humans=0, options={"layout": "random"}, seed=21)
            game.start()
            host.run_until_over()
            runs.append((host.result, host.events))
        self.assertEqual(runs[0], runs[1])


if __name__ == "__main__":
    unittest.main()
