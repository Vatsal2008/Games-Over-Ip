"""Bingo logic: lines, bots, private boards, arrange phase, calls, ties and full games."""
from __future__ import annotations

import heapq
import itertools
import random
import unittest
from typing import Any, Dict, List, Optional

from goip.games.base import ActionError
from goip.games.bingo import logic as bl
from goip.games.bingo.logic import LINES, Logic, call_scores, choose_call, completed_lines
from goip.games.testing import FakeHost, make_game

REQUIRED_KEYS = ("turn", "timer", "phase", "finished", "board", "called", "lines", "my_lines", "ready")
NAMES = ["Asha", "Ravi", "Mia", "Leo"]


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


def new_game(n: int = 2, humans: int = 2, seed: int = 1, **options: Any):
    options.setdefault("turn_timer", 0)
    return make_game(Logic, n, humans=humans, options=options, seed=seed, names=NAMES[:n])


def contains_list(obj: Any, target: List[int]) -> bool:
    """Is `target` anywhere inside obj (as a list value)?"""
    if isinstance(obj, list):
        return obj == target or any(contains_list(x, target) for x in obj)
    if isinstance(obj, dict):
        return any(contains_list(x, target) for x in obj.values())
    return False


def ready_all(host: FakeHost, game: Logic) -> None:
    for s in range(len(game.seats)):
        if not game.ready[s]:
            host.act(s, a="ready")


class Checker:
    """Invariants on every push, including that no board leaks before the end."""

    def __init__(self, tc: unittest.TestCase, host: FakeHost, game: Logic) -> None:
        self.tc, self.host, self.game = tc, host, game
        self.pushes = 0
        self.leak_checks = 0
        host.on_push = self

    def __call__(self, host: FakeHost) -> None:
        tc, g = self.tc, self.game
        self.pushes += 1
        n = len(g.seats)
        spectator = host.views[None]
        called = spectator["called"]
        called_set = set(called)
        tc.assertEqual(len(called), len(called_set), "numbers are called once")
        for x in called:
            tc.assertIn(x, range(1, 26))
        true_lines = [completed_lines(b, called_set) for b in g.boards]
        for seat in list(range(n)) + [None]:
            v = host.views[seat]
            for key in REQUIRED_KEYS:
                tc.assertIn(key, v)
            tc.assertEqual(v["called"], called)
            tc.assertEqual(v["lines"], [len(x) for x in true_lines])
            tc.assertEqual(v["ready"], spectator["ready"])
            tc.assertEqual(v["turn"], spectator["turn"])
            tc.assertEqual(v["phase"], spectator["phase"])
            if seat is None:
                tc.assertIsNone(v["board"])
                tc.assertIsNone(v["marks"])
                tc.assertEqual(v["my_lines"], [])
            else:
                tc.assertEqual(v["board"], g.boards[seat])
                tc.assertEqual(sorted(v["board"]), list(range(1, 26)))
                tc.assertEqual(v["marks"], [x in called_set for x in v["board"]])
                tc.assertEqual(v["my_lines"], true_lines[seat])
            if v["finished"]:
                tc.assertEqual(v["boards"], g.boards)
            else:
                tc.assertNotIn("boards", v)
                for other in range(n):
                    if other != seat and g.boards[other] != (g.boards[seat] if seat is not None else None):
                        tc.assertFalse(contains_list(v, g.boards[other]),
                                       "seat %s can see seat %d's board" % (seat, other))
                        self.leak_checks += 1
        v = spectator
        if v["phase"] == "arrange":
            tc.assertIsNone(v["turn"])
            tc.assertEqual(called, [])
            tc.assertFalse(all(v["ready"]))
            tc.assertIsNotNone(v["timer"])
            tc.assertTrue(0 <= v["timer"] <= Logic.ARRANGE_SECONDS)
            for s in range(n):
                if g.is_bot(s):
                    tc.assertTrue(v["ready"][s], "bots are ready immediately")
        else:
            tc.assertEqual(v["phase"], "play")
            tc.assertTrue(all(v["ready"]))
            winners = [s for s in range(n) if len(true_lines[s]) >= 5]
            tc.assertEqual(v["winners"], winners)
            if winners:
                tc.assertIsNone(v["turn"])
                tc.assertIsNone(v["timer"])
            else:
                tc.assertIn(v["turn"], range(n))
                tc.assertEqual(v["turn"], (v["first"] + len(called)) % n, "turns rotate")
                if g.is_bot(v["turn"]) or not g.options["turn_timer"]:
                    tc.assertIsNone(v["timer"])
                else:
                    tc.assertGreater(v["timer"], 0)
        if v["finished"]:
            tc.assertTrue(v["winners"])

    def check_game(self) -> Dict[str, Any]:
        tc, g, host = self.tc, self.game, self.host
        result = host.result
        tc.assertIsNotNone(result)
        assert result is not None
        n = len(g.seats)
        called = [e["n"] for e in host.events_of("called")]
        tc.assertEqual(called, g.called)
        first = g.first
        for i, e in enumerate(host.events_of("called")):
            tc.assertEqual(e["seat"], (first + i) % n)
        # Nobody had 5 lines before the final call; the winners are everyone with 5+ after it.
        before = set(called[:-1])
        for b in g.boards:
            tc.assertLess(len(completed_lines(b, before)), 5)
        winners = [s for s in range(n) if len(completed_lines(g.boards[s], set(called))) >= 5]
        tc.assertEqual(result["winners"], winners)
        tc.assertEqual(result["lines"], [len(completed_lines(b, set(called))) for b in g.boards])
        if len(winners) == 1:
            tc.assertEqual(result["text"], "%s wins!" % g.name(winners[0]))
        else:
            tc.assertTrue(result["text"].endswith(" tie!"))
            for s in winners:
                tc.assertIn(g.name(s), result["text"])
        # Line events go only to the seat whose board it is, with a growing count.
        counts = [0] * n
        for to, e in host.events:
            tc.assertNotIn("board", e)
            tc.assertNotIn("boards", e)
            if e["kind"] == "line":
                tc.assertEqual(to, e["seat"])
                tc.assertGreater(e["count"], counts[to])
                tc.assertEqual(e["count"] - counts[to], len(e["new"]))
                counts[to] = e["count"]
            else:
                tc.assertIsNone(to)
        tc.assertEqual(counts, result["lines"])
        tc.assertEqual(sorted(e["seat"] for e in host.events_of("ready")), list(range(n)))
        tc.assertTrue(host.views[None]["finished"])
        return result


class MetadataTests(unittest.TestCase):
    def test_class_attributes(self):
        self.assertEqual(Logic.ID, "bingo")
        self.assertEqual(Logic.NAME, "Bingo")
        self.assertLessEqual(len(Logic.TAGLINE), 28)
        self.assertEqual(Logic.SEAT_COUNTS, (2, 3, 4))
        self.assertEqual(Logic.clean_options({}), {"bot_level": "normal", "turn_timer": 30})
        self.assertEqual(Logic.ARRANGE_SECONDS, 40)

    def test_registry(self):
        from goip.games import logic_class
        self.assertIs(logic_class("bingo"), Logic)


class LineTests(unittest.TestCase):
    def test_lines(self):
        self.assertEqual(len(LINES), 12)
        self.assertEqual(len(set(LINES)), 12)
        for line in LINES:
            self.assertEqual(len(line), 5)
        self.assertEqual(LINES[0], (0, 1, 2, 3, 4))
        self.assertEqual(LINES[4], (20, 21, 22, 23, 24))
        self.assertEqual(LINES[5], (0, 5, 10, 15, 20))
        self.assertEqual(LINES[9], (4, 9, 14, 19, 24))
        self.assertEqual(LINES[10], (0, 6, 12, 18, 24))
        self.assertEqual(LINES[11], (4, 8, 12, 16, 20))
        through = [sum(c in line for line in LINES) for c in range(25)]
        self.assertEqual(through[12], 4)
        self.assertEqual(through[0], 3)
        self.assertEqual(through[1], 2)

    def test_completed_lines(self):
        board = list(range(1, 26))       # cell i holds i + 1
        self.assertEqual(completed_lines(board, set()), [])
        self.assertEqual(completed_lines(board, {1, 2, 3, 4, 5}), [0])
        self.assertEqual(completed_lines(board, {1, 6, 11, 16, 21}), [5])
        self.assertEqual(completed_lines(board, {1, 7, 13, 19, 25}), [10])
        self.assertEqual(completed_lines(board, {5, 9, 13, 17, 21}), [11])
        self.assertEqual(completed_lines(board, set(range(1, 26))), list(range(12)))


class BotTests(unittest.TestCase):
    def test_scores_weight_nearly_complete_lines(self):
        board = list(range(1, 26))
        scores = call_scores(board, {1, 2, 3, 4})
        self.assertNotIn(1, scores)
        self.assertEqual(len(scores), 21)
        self.assertEqual(scores[5], 4 ** 4 + 1 + 1)     # row, column, anti-diagonal
        self.assertEqual(max(scores, key=lambda n: scores[n]), 5)

    def test_hard_and_normal_complete_the_line(self):
        board = list(range(1, 26))
        rng = random.Random(1)
        for _ in range(30):
            self.assertEqual(choose_call(board, {1, 2, 3, 4}, "hard", rng), 5)
        hits = sum(choose_call(board, {1, 2, 3, 4}, "normal", rng) == 5 for _ in range(300))
        self.assertGreater(hits, 240)

    def test_easy_is_random_half_the_time(self):
        board = list(range(1, 26))
        rng = random.Random(2)
        hits = sum(choose_call(board, {1, 2, 3, 4}, "easy", rng) == 5 for _ in range(1000))
        self.assertGreater(hits, 430)
        self.assertLess(hits, 640)

    def test_never_calls_a_called_number(self):
        rng = random.Random(3)
        for level in ("easy", "normal", "hard"):
            board = bl.new_board(rng)
            called: set = set()
            while len(called) < 25:
                x = choose_call(board, called, level, rng)
                self.assertNotIn(x, called)
                self.assertIn(x, range(1, 26))
                called.add(x)
            with self.assertRaises(ValueError):
                choose_call(board, called, level, rng)

    def test_hard_needs_fewer_calls_than_random(self):
        rng = random.Random(4)

        def calls_needed(board: List[int], level: Optional[str]) -> int:
            called: set = set()
            order = rng.sample(range(1, 26), 25)
            while len(completed_lines(board, called)) < 5:
                called.add(choose_call(board, called, level, rng) if level
                           else next(x for x in order if x not in called))
            return len(called)

        boards = [bl.new_board(rng) for _ in range(30)]
        smart = sum(calls_needed(b, "hard") for b in boards)
        dumb = sum(calls_needed(b, None) for b in boards)
        self.assertLess(smart, dumb)


class ArrangeTests(unittest.TestCase):
    def test_start_deals_private_boards_and_bots_are_ready(self):
        host, game = new_game(n=3, humans=2)
        Checker(self, host, game)
        game.start()
        v = host.views[0]
        self.assertEqual(v["phase"], "arrange")
        self.assertEqual(v["ready"], [False, False, True])
        self.assertIsNone(v["turn"])
        self.assertEqual(v["timer"], 40.0)
        self.assertEqual(host.events_of("ready"), [{"kind": "ready", "seat": 2}])
        self.assertNotEqual(game.boards[0], game.boards[1])
        with self.assertRaisesRegex(ActionError, "ready"):
            host.act(0, a="call", n=5)

    def test_shuffle_and_swap(self):
        host, game = new_game()
        checker = Checker(self, host, game)
        game.start()
        before0, before1 = list(game.boards[0]), list(game.boards[1])
        host.act(0, a="shuffle")
        self.assertNotEqual(host.views[0]["board"], before0)
        self.assertEqual(sorted(host.views[0]["board"]), list(range(1, 26)))
        self.assertEqual(game.boards[1], before1, "only your own board changes")
        b = list(host.views[0]["board"])
        host.act(0, a="swap", i=0, j=24)
        b[0], b[24] = b[24], b[0]
        self.assertEqual(host.views[0]["board"], b)
        host.act(0, a="swap", i=3, j=3)
        self.assertEqual(host.views[0]["board"], b)
        for i, j in ((0, 25), (-1, 3), ("1", 2), (True, 2), (None, None), (1.0, 2)):
            with self.assertRaises(ActionError):
                host.act(0, a="swap", i=i, j=j)
        with self.assertRaises(ActionError):
            host.act(0, a="swap")
        self.assertEqual(host.views[0]["board"], b)
        self.assertGreater(checker.leak_checks, 0)

    def test_ready_locks_board_and_everyone_ready_starts_play(self):
        host, game = new_game(n=3, humans=2)
        Checker(self, host, game)
        game.start()
        host.act(0, a="ready")
        self.assertEqual(host.events_of("ready")[-1], {"kind": "ready", "seat": 0})
        self.assertEqual(host.views[1]["ready"], [True, False, True])
        with self.assertRaisesRegex(ActionError, "ready"):
            host.act(0, a="shuffle")
        with self.assertRaises(ActionError):
            host.act(0, a="swap", i=0, j=1)
        events = len(host.events)
        host.act(0, a="ready")                     # harmless repeat
        self.assertEqual(len(host.events), events)
        host.act(1, a="ready")
        v = host.views[0]
        self.assertEqual(v["phase"], "play")
        self.assertIn(v["turn"], range(3))
        self.assertEqual(v["turn"], v["first"])
        self.assertEqual(host.events_of("phase")[-1], {"kind": "phase", "phase": "play", "turn": v["turn"]})
        for a in ("shuffle", "ready"):
            with self.assertRaisesRegex(ActionError, "started"):
                host.act(0, a=a)

    def test_arrange_times_out_after_40_seconds(self):
        host, game = new_game(n=2, humans=2)
        Checker(self, host, game)
        game.start()
        host.act(1, a="ready")
        advance(host, 39.9)
        v = host.views[0]
        self.assertEqual(game.phase, "arrange")
        self.assertAlmostEqual(game.view_for(0)["timer"], 0.1, places=1)
        advance(host, 0.2)
        v = host.views[0]
        self.assertEqual(v["phase"], "play")
        self.assertEqual(v["ready"], [True, True])
        self.assertEqual([e["seat"] for e in host.events_of("ready")], [1, 0])

    def test_early_start_cancels_the_timeout(self):
        host, game = new_game(n=2, humans=2)
        game.start()
        ready_all(host, game)
        first = game.turn
        advance(host, 60)
        self.assertEqual(game.phase, "play")
        self.assertEqual(game.turn, first)
        self.assertEqual(game.called, [], "humans without an idle timer are waited for")

    def test_all_bots_skip_arrange(self):
        host, game = make_game(Logic, 3, humans=0, seed=2)
        game.start()
        self.assertEqual(host.views[0]["phase"], "play")
        self.assertEqual(host.pushes, 1)

    def test_player_leaving_during_arrange_becomes_ready(self):
        host, game = new_game(n=3, humans=3)
        Checker(self, host, game)
        game.start()
        host.act(0, a="ready")
        game.seats[1].is_bot = True
        game.on_seat_changed(1)
        self.assertEqual(game.ready, [True, True, False])
        game.seats[2].is_bot = True
        game.on_seat_changed(2)
        self.assertEqual(game.phase, "play")


class PlayTests(unittest.TestCase):
    def started(self, n: int = 2, humans: int = 2, seed: int = 1, **options: Any):
        host, game = new_game(n=n, humans=humans, seed=seed, **options)
        checker = Checker(self, host, game)
        game.start()
        ready_all(host, game)
        return host, game, checker

    def test_call_validation(self):
        host, game, _c = self.started()
        me = game.turn
        other = 1 - me
        with self.assertRaisesRegex(ActionError, "^Not your turn$"):
            host.act(other, a="call", n=5)
        for bad in (0, 26, "5", True, None, 5.0):
            with self.assertRaises(ActionError):
                host.act(me, a="call", n=bad)
        with self.assertRaises(ActionError):
            host.act(me, a="mark", n=5)
        host.act(me, a="call", n=7)
        self.assertEqual(host.events_of("called")[-1], {"kind": "called", "seat": me, "n": 7})
        v = host.views[other]
        self.assertEqual(v["called"], [7])
        self.assertEqual(v["turn"], other)
        self.assertEqual(v["last_call"], {"seat": me, "n": 7})
        self.assertEqual(sum(v["marks"]), 1)
        self.assertTrue(v["marks"][v["board"].index(7)])
        with self.assertRaisesRegex(ActionError, "^That number was already called$"):
            host.act(other, a="call", n=7)

    def test_starting_seat_is_random(self):
        firsts = set()
        for seed in range(40):
            host, game = make_game(Logic, 4, humans=0, seed=seed)
            game.start()
            firsts.add(game.first)
        self.assertEqual(firsts, {0, 1, 2, 3})

    def _call_in_board_order(self, host: FakeHost, game: Logic) -> None:
        order = list(game.boards[0])
        while not game.winners:
            x = next(v for v in order if v not in game.called)
            host.act(game.turn, a="call", n=x)

    def test_simultaneous_winners_share(self):
        host, game = new_game(n=2, humans=2)
        checker = Checker(self, host, game)
        game.start()
        game.boards[1] = list(game.boards[0])
        ready_all(host, game)
        self._call_in_board_order(host, game)
        # 21 numbers in cell order: rows 0-3, column 0 and the anti-diagonal.
        self.assertEqual(len(game.called), 21)
        self.assertEqual(host.views[0]["lines"], [6, 6])
        self.assertEqual(host.views[0]["winners"], [0, 1])
        self.assertIsNone(host.views[0]["turn"])
        self.assertIsNone(host.result, "the last call stays up for a moment")
        with self.assertRaises(ActionError):
            host.act(0, a="call", n=25)
        advance(host, Logic.FINAL_PAUSE + 0.01)
        result = checker.check_game()
        self.assertEqual(result["winners"], [0, 1])
        self.assertEqual(result["text"], "Asha and Ravi tie!")
        self.assertEqual(host.views[0]["boards"], game.boards)

    def test_three_way_tie_text(self):
        host, game = new_game(n=3, humans=3)
        checker = Checker(self, host, game)
        game.start()
        game.boards[1] = list(game.boards[0])
        game.boards[2] = list(game.boards[0])
        ready_all(host, game)
        self._call_in_board_order(host, game)
        host.run_until_over()
        self.assertEqual(checker.check_game()["text"], "Asha, Ravi and Mia tie!")

    def test_line_events_go_only_to_the_owner(self):
        host, game, checker = self.started(n=3, humans=3)
        order = list(game.boards[0])
        while host.result is None:
            if game.winners:
                host.run_until_over()
                break
            x = next(v for v in order if v not in game.called)
            host.act(game.turn, a="call", n=x)
        checker.check_game()
        lines = [(to, e) for to, e in host.events if e["kind"] == "line"]
        self.assertTrue(lines)
        self.assertTrue(all(to == e["seat"] for to, e in lines))
        first = next(e for to, e in lines if to == 0)
        self.assertEqual(first["count"], 1)
        self.assertEqual(first["new"], [0])

    def test_idle_human_calls_automatically(self):
        host, game, _c = self.started(n=2, humans=2, turn_timer=15)
        first = game.turn
        wait = 15 + bl.CALL_ANIM
        self.assertAlmostEqual(host.views[0]["timer"], wait, places=1)
        advance(host, wait - 0.05)
        self.assertEqual(game.called, [])
        advance(host, 0.1)
        self.assertEqual(len(game.called), 1)
        self.assertEqual(host.events_of("called")[0]["seat"], first)

    def test_idle_humans_finish_the_game(self):
        host, game = new_game(n=3, humans=2, turn_timer=15)
        checker = Checker(self, host, game)
        game.start()
        host.run_until_over()
        checker.check_game()
        self.assertGreaterEqual(host.t, Logic.ARRANGE_SECONDS)

    def test_disconnected_player_is_replaced_by_bot(self):
        host, game, _c = self.started(n=2, humans=2)
        seat = game.turn
        self.assertFalse(host.step())
        game.seats[seat].is_bot = True
        game.on_seat_changed(seat)
        advance(host, Logic.BOT_DELAY + 0.01)
        self.assertEqual(len(game.called), 1)


class PrivacyTests(unittest.TestCase):
    def test_boards_stay_private_until_the_end(self):
        host, game = new_game(n=4, humans=2, seed=6)
        checker = Checker(self, host, game)
        game.start()
        host.act(0, a="shuffle")
        host.act(1, a="swap", i=2, j=20)
        host.act(1, a="shuffle")
        ready_all(host, game)
        rng = random.Random(9)
        while host.result is None:
            if game.turn is not None and not game.is_bot(game.turn):
                s = game.turn
                host.act(s, a="call", n=choose_call(game.boards[s], set(game.called), "normal", rng))
            elif not host.step():
                break
        checker.check_game()
        self.assertGreater(checker.leak_checks, 100)
        for seat in (0, 1, 2, 3, None):
            self.assertEqual(host.views[seat]["boards"], game.boards)


class FullGameTests(unittest.TestCase):
    def test_all_bot_games(self):
        for n, level, seed in itertools.product((2, 3, 4), ("easy", "normal", "hard"), range(5)):
            with self.subTest(n=n, level=level, seed=seed):
                host, game = make_game(Logic, n, humans=0, options={"bot_level": level}, seed=seed)
                checker = Checker(self, host, game)
                game.start()
                host.run_until_over()
                checker.check_game()
                self.assertLessEqual(len(game.called), 25)

    def test_mixed_humans_and_bots_with_idle_timers(self):
        for n in (2, 3, 4):
            with self.subTest(n=n):
                host, game = new_game(n=n, humans=1, seed=n, turn_timer=15)
                checker = Checker(self, host, game)
                game.start()
                host.run_until_over()
                checker.check_game()

    def test_deterministic_per_seed(self):
        runs = []
        for _ in range(2):
            host, game = make_game(Logic, 4, humans=0, seed=33)
            game.start()
            host.run_until_over()
            runs.append((host.result, host.events, game.boards))
        self.assertEqual(runs[0], runs[1])


if __name__ == "__main__":
    unittest.main()
