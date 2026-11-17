"""Tic-Tac-Toe logic: rules, series, timers, bot strength and full all-bot matches."""
from __future__ import annotations

import heapq
import itertools
import random
import unittest
from typing import Any, Dict, List, Optional

from goip.games.base import ActionError
from goip.games.testing import FakeHost, make_game
from goip.games.tictactoe import logic as ttt
from goip.games.tictactoe.logic import Logic

REQUIRED_KEYS = ("turn", "timer", "phase", "finished", "board", "scores", "round", "best_of",
                 "line", "last")


def new_game(humans: int = 2, seed: int = 1, **options: Any):
    host, game = make_game(Logic, 2, humans=humans, options=options, seed=seed)
    return host, game


def advance(host: FakeHost, seconds: float) -> None:
    """Like FakeHost.advance, but never runs a timer past the target.

    FakeHost.step() skips a cancelled heap entry and then runs the next live timer
    even when that one is later than advance()'s target, so drop cancelled heads first.
    """
    target = host.t + seconds
    while True:
        while host._heap and host._heap[0][1] in host._cancelled:
            host._cancelled.discard(heapq.heappop(host._heap)[1])
        if not host._heap or host._heap[0][0] > target:
            break
        host.step()
    host.t = max(host.t, target)


def play_round(tc: unittest.TestCase, host: FakeHost, game: Logic, moves: List[int]) -> None:
    """Humans play `moves` (cells) in turn order, starting with whoever's turn it is."""
    for cell in moves:
        tc.assertEqual(game.phase, "play")
        host.act(game.turn, a="place", cell=cell)


class Checker:
    """Asserts rule invariants on every push."""

    def __init__(self, tc: unittest.TestCase, host: FakeHost, game: Logic) -> None:
        self.tc = tc
        self.host = host
        self.game = game
        self.pushes = 0
        host.on_push = self

    def __call__(self, host: FakeHost) -> None:
        tc, g = self.tc, self.game
        self.pushes += 1
        v = host.views[None]
        for key in REQUIRED_KEYS:
            tc.assertIn(key, v)
        for seat in (0, 1):
            tc.assertEqual(host.views[seat], v, "tic-tac-toe has no private state")
        board = v["board"]
        tc.assertEqual(len(board), 9)
        for cell in board:
            tc.assertIn(cell, (None, 0, 1))
        rnd, starter = v["round"], v["starter"]
        tc.assertGreaterEqual(rnd, 1)
        tc.assertLessEqual(rnd, v["max_rounds"])
        tc.assertEqual(v["max_rounds"], 2 * v["best_of"])
        tc.assertEqual(starter, (rnd - 1) % 2, "starter alternates every round")
        a, b = board.count(starter), board.count(1 - starter)
        tc.assertIn(a - b, (0, 1))
        win = ttt.find_win(board)
        need = v["best_of"] // 2 + 1
        tc.assertLessEqual(max(v["scores"]), need)
        if v["phase"] == "play":
            tc.assertFalse(v["finished"])
            tc.assertIsNone(win)
            tc.assertIn(None, board)
            tc.assertEqual(v["turn"], starter if a == b else 1 - starter)
            tc.assertEqual(sum(v["scores"]) + v["draws"], rnd - 1)
            tc.assertIsNone(v["line"])
            seat = v["turn"]
            tt = g.options["turn_timer"]
            if g.is_bot(seat) or not tt:
                tc.assertIsNone(v["timer"])
            else:
                tc.assertIsNotNone(v["timer"])
                tc.assertGreaterEqual(v["timer"], 0)
                tc.assertLessEqual(v["timer"], tt)
        else:
            tc.assertEqual(v["phase"], "round_over")
            tc.assertIsNone(v["turn"])
            tc.assertIsNone(v["timer"])
            tc.assertEqual(sum(v["scores"]) + v["draws"], rnd)
            if v["round_winner"] is None:
                tc.assertIsNone(win)
                tc.assertNotIn(None, board)
                tc.assertIsNone(v["line"])
            else:
                tc.assertIsNotNone(win)
                tc.assertEqual(win[0], v["round_winner"])
                tc.assertEqual(sorted(v["line"]), sorted(win[1]))
                tc.assertEqual(v["last"] in v["line"], True)
        if v["finished"]:
            tc.assertTrue(v["match_over"])
        if v["match_over"]:
            tc.assertTrue(max(v["scores"]) >= need or rnd == v["max_rounds"])

    def check_result(self) -> Dict[str, Any]:
        tc, g = self.tc, self.game
        result = self.host.result
        tc.assertIsNotNone(result)
        assert result is not None
        s0, s1 = g.scores
        if s0 == s1:
            tc.assertEqual(result["winners"], [])
            tc.assertIn("draw", result["text"])
        else:
            w = 0 if s0 > s1 else 1
            tc.assertEqual(result["winners"], [w])
            tc.assertIn(g.name(w), result["text"])
        tc.assertEqual(result["scores"], g.scores)
        tc.assertTrue(self.host.views[None]["finished"])
        rounds = self.host.events_of("round_over")
        tc.assertEqual(len(rounds), g.round)
        return result


class MetadataTests(unittest.TestCase):
    def test_class_attributes(self):
        self.assertEqual(Logic.ID, "tictactoe")
        self.assertEqual(Logic.NAME, "Tic-Tac-Toe")
        self.assertLessEqual(len(Logic.TAGLINE), 28)
        self.assertEqual(Logic.SEAT_COUNTS, (2,))
        keys = [o.key for o in Logic.OPTIONS]
        self.assertEqual(sorted(keys), ["bot_level", "match", "turn_timer"])

    def test_defaults_and_cleaning(self):
        opts = Logic.clean_options({})
        self.assertEqual(opts, {"bot_level": "normal", "match": 3, "turn_timer": 15})
        opts = Logic.clean_options({"match": 5, "turn_timer": 0, "bot_level": "hard"})
        self.assertEqual(opts, {"bot_level": "hard", "match": 5, "turn_timer": 0})
        opts = Logic.clean_options({"match": 4, "turn_timer": "15", "bot_level": "x"})
        self.assertEqual(opts, {"bot_level": "normal", "match": 3, "turn_timer": 15})

    def test_registry(self):
        from goip.games import logic_class
        self.assertIs(logic_class("tictactoe"), Logic)


class RuleTests(unittest.TestCase):
    def test_find_win(self):
        self.assertIsNone(ttt.find_win([None] * 9))
        self.assertEqual(ttt.find_win([0, 0, 0, None, 1, 1, None, None, None]), (0, (0, 1, 2)))
        self.assertEqual(ttt.find_win([1, 0, None, 0, 1, None, None, 0, 1]), (1, (0, 4, 8)))
        self.assertEqual(ttt.find_win([None, None, 1, 0, 1, 0, 1, None, None]), (1, (2, 4, 6)))

    def test_winning_moves(self):
        board = [0, 0, None, 1, 1, None, None, None, None]
        self.assertEqual(ttt.winning_moves(board, 0), [2])
        self.assertEqual(ttt.winning_moves(board, 1), [5])

    def test_minimax_empty_board_is_draw(self):
        scores = ttt.move_scores([None] * 9, 0)
        self.assertEqual(len(scores), 9)
        self.assertTrue(all(s == 0 for s in scores.values()))

    def test_minimax_prefers_win_and_blocks(self):
        rng = random.Random(0)
        board = [0, 0, None, 1, 1, None, None, None, None]   # X to move: win at 2
        for _ in range(20):
            self.assertEqual(ttt.choose_move(board, 0, "hard", rng), 2)
            self.assertEqual(ttt.choose_move(board, 0, "normal", rng), 2)
        board = [0, None, None, 1, 1, None, 0, None, None]   # X to move must block 5
        for _ in range(20):
            self.assertEqual(ttt.choose_move(board, 0, "hard", rng), 5)
            self.assertEqual(ttt.choose_move(board, 0, "normal", rng), 5)

    def test_hard_prefers_fastest_win(self):
        # X can win now at 2, or set up a fork; the immediate win must score highest.
        board = [0, 0, None, None, 1, None, None, 1, None]
        scores = ttt.move_scores(board, 0)
        self.assertEqual(max(scores, key=lambda c: scores[c]), 2)

    def test_easy_takes_win_about_half_the_time(self):
        rng = random.Random(5)
        board = [0, 0, None, 1, 1, None, None, None, 1]   # 4 empty cells, one wins
        hits = sum(ttt.choose_move(board, 0, "easy", rng) == 2 for _ in range(2000))
        # 50% deliberate + 1/4 of the random half = 62.5% expected
        self.assertGreater(hits, 1100)
        self.assertLess(hits, 1400)

    def test_choose_move_full_board_raises(self):
        with self.assertRaises(ValueError):
            ttt.choose_move([0, 1, 0, 1, 0, 1, 1, 0, 1], 0, "hard", random.Random(1))


def _play_round_bots(rng: random.Random, levels: List[str], starter: int) -> Optional[int]:
    """One round between two bot levels; returns the winning seat or None."""
    board: List[Optional[int]] = [None] * 9
    turn = starter
    while True:
        cell = ttt.choose_move(board, turn, levels[turn], rng)
        assert board[cell] is None
        board[cell] = turn
        win = ttt.find_win(board)
        if win is not None:
            return win[0]
        if None not in board:
            return None
        turn = 1 - turn


class BotStrengthTests(unittest.TestCase):
    def test_hard_never_loses_to_easy(self):
        rng = random.Random(42)
        hard_wins = 0
        for i in range(200):
            hard = i % 2                       # alternate sides
            levels = ["easy", "easy"]
            levels[hard] = "hard"
            winner = _play_round_bots(rng, levels, starter=(i // 2) % 2)
            self.assertNotEqual(winner, 1 - hard, "hard lost to easy in game %d" % i)
            hard_wins += winner == hard
        self.assertGreater(hard_wins, 100)

    def test_hard_never_loses_to_normal(self):
        rng = random.Random(7)
        for i in range(200):
            hard = i % 2
            levels = ["normal", "normal"]
            levels[hard] = "hard"
            winner = _play_round_bots(rng, levels, starter=(i // 2) % 2)
            self.assertNotEqual(winner, 1 - hard)

    def test_hard_vs_hard_always_draws(self):
        rng = random.Random(3)
        for i in range(100):
            self.assertIsNone(_play_round_bots(rng, ["hard", "hard"], starter=i % 2))

    def test_normal_beats_easy_more_often(self):
        rng = random.Random(11)
        tally = {"normal": 0, "easy": 0}
        for i in range(200):
            normal = i % 2
            levels = ["easy", "easy"]
            levels[normal] = "normal"
            winner = _play_round_bots(rng, levels, starter=(i // 2) % 2)
            if winner is not None:
                tally["normal" if winner == normal else "easy"] += 1
        self.assertGreater(tally["normal"], 3 * tally["easy"])

    def test_full_match_hard_bots_is_a_draw(self):
        host, game = make_game(Logic, 2, humans=0, options={"bot_level": "hard", "match": 3}, seed=9)
        checker = Checker(self, host, game)
        game.start()
        result = host.run_until_over()
        checker.check_result()
        self.assertEqual(result["winners"], [])
        self.assertEqual(game.round, 6)
        self.assertEqual(game.draws, 6)


class HumanPlayTests(unittest.TestCase):
    def test_round_win_then_pause_then_next_round(self):
        host, game = new_game(match=3, turn_timer=0)
        Checker(self, host, game)
        game.start()
        v = host.views[0]
        self.assertEqual(v["turn"], 0)
        self.assertEqual(v["phase"], "play")
        self.assertIsNone(v["timer"])
        play_round(self, host, game, [0, 3, 1, 4, 2])
        v = host.views[1]
        self.assertEqual(v["phase"], "round_over")
        self.assertEqual(v["scores"], [1, 0])
        self.assertEqual(v["line"], [0, 1, 2])
        self.assertEqual(v["last"], 2)
        self.assertEqual(v["round_winner"], 0)
        self.assertIsNone(v["turn"])
        ev = host.events_of("round_over")[-1]
        self.assertEqual(ev, {"kind": "round_over", "winner": 0, "line": [0, 1, 2]})
        with self.assertRaisesRegex(ActionError, "next round"):
            host.act(1, a="place", cell=8)
        advance(host, Logic.ROUND_PAUSE - 0.05)
        self.assertEqual(host.views[0]["phase"], "round_over")
        advance(host, 0.1)
        v = host.views[0]
        self.assertEqual(v["phase"], "play")
        self.assertEqual(v["round"], 2)
        self.assertEqual(v["turn"], 1, "starter alternates")
        self.assertEqual(v["board"], [None] * 9)
        self.assertIsNone(v["line"])
        self.assertIsNone(v["last"])

    def test_place_events(self):
        host, game = new_game(turn_timer=0)
        game.start()
        host.act(0, a="place", cell=4)
        self.assertEqual(host.events_of("place")[-1], {"kind": "place", "seat": 0, "cell": 4})
        self.assertEqual(host.views[0]["last"], 4)
        self.assertEqual(host.views[0]["board"][4], 0)

    def test_errors(self):
        host, game = new_game(turn_timer=0)
        game.start()
        with self.assertRaisesRegex(ActionError, "^Not your turn$"):
            host.act(1, a="place", cell=0)
        for bad in (9, -1, "3", True, None, 2.0, [1]):
            with self.assertRaises(ActionError):
                host.act(0, a="place", cell=bad)
        with self.assertRaises(ActionError):
            host.act(0, a="roll")
        with self.assertRaises(ActionError):
            host.act(0)
        with self.assertRaises(ActionError):
            game.on_action(0, "place")   # type: ignore[arg-type]
        host.act(0, a="place", cell=0)
        with self.assertRaisesRegex(ActionError, "taken"):
            host.act(1, a="place", cell=0)
        self.assertEqual(host.views[0]["board"].count(None), 8)

    def test_series_best_of_three_ends_at_two_wins(self):
        host, game = new_game(match=3, turn_timer=0)
        checker = Checker(self, host, game)
        game.start()
        play_round(self, host, game, [0, 3, 1, 4, 2])          # seat 0 wins round 1
        advance(host, Logic.ROUND_PAUSE)
        self.assertEqual(game.turn, 1)
        play_round(self, host, game, [3, 0, 4, 1, 8, 2])       # seat 0 wins round 2
        self.assertTrue(host.views[0]["match_over"])
        self.assertIsNone(host.result, "the deciding board stays visible for a moment")
        with self.assertRaises(ActionError):
            host.act(1, a="place", cell=5)
        advance(host, Logic.FINAL_PAUSE)
        result = checker.check_result()
        self.assertEqual(result["winners"], [0])
        self.assertEqual(result["text"], "P0 wins 2-0!")
        self.assertTrue(host.views[1]["finished"])
        with self.assertRaises(ActionError):
            host.act(1, a="place", cell=5)

    def test_draws_do_not_count_and_tied_match_is_a_draw(self):
        host, game = new_game(match=1, turn_timer=0)
        checker = Checker(self, host, game)
        game.start()
        draw = [0, 1, 2, 4, 3, 5, 7, 6, 8]
        play_round(self, host, game, draw)
        v = host.views[0]
        self.assertEqual(v["phase"], "round_over")
        self.assertIsNone(v["round_winner"])
        self.assertEqual(host.events_of("round_over")[-1]["winner"], None)
        self.assertEqual(v["scores"], [0, 0])
        self.assertFalse(v["match_over"], "a draw does not decide a best-of-1")
        advance(host, Logic.ROUND_PAUSE)
        self.assertEqual(game.turn, 1)
        play_round(self, host, game, draw)
        self.assertTrue(host.views[0]["match_over"])
        advance(host, Logic.FINAL_PAUSE)
        result = checker.check_result()
        self.assertEqual(result["winners"], [])
        self.assertEqual(result["text"], "It's a draw")

    def test_higher_score_wins_after_round_cap(self):
        host, game = new_game(match=3, turn_timer=0)
        checker = Checker(self, host, game)
        game.start()
        draw = [0, 1, 2, 4, 3, 5, 7, 6, 8]
        play_round(self, host, game, [0, 3, 1, 4, 2])   # round 1: seat 0 wins
        for _ in range(5):                             # rounds 2..6 are draws
            advance(host, Logic.ROUND_PAUSE)
            play_round(self, host, game, draw)
        self.assertEqual(game.round, 6)
        advance(host, Logic.FINAL_PAUSE)
        result = checker.check_result()
        self.assertEqual(result["winners"], [0])
        self.assertEqual(result["text"], "P0 wins 1-0!")

    def test_idle_timer_moves_for_human(self):
        host, game = new_game(humans=2, match=1)
        Checker(self, host, game)
        game.start()
        self.assertEqual(host.views[0]["timer"], 15.0)
        advance(host, 14.9)
        self.assertEqual(host.views[0]["board"], [None] * 9)
        advance(host, 0.2)
        self.assertEqual(9 - host.views[0]["board"].count(None), 1)
        self.assertEqual(host.views[0]["turn"], 1)
        self.assertEqual(host.views[0]["timer"], 15.0)

    def test_human_action_cancels_idle_timer(self):
        host, game = new_game(humans=2, match=1)
        game.start()
        advance(host, 10)
        host.act(0, a="place", cell=4)
        advance(host, 14.9)
        self.assertEqual(host.views[0]["board"].count(None), 8, "seat 1 still has time")
        advance(host, 0.2)
        self.assertEqual(host.views[0]["board"].count(None), 7)

    def test_idle_humans_finish_a_match(self):
        host, game = new_game(humans=2, match=3)
        checker = Checker(self, host, game)
        game.start()
        host.run_until_over()
        checker.check_result()
        self.assertGreater(host.t, 15 * 5)

    def test_human_vs_bot(self):
        host, game = new_game(humans=1, match=1, turn_timer=0, bot_level="hard")
        checker = Checker(self, host, game)
        game.start()
        while host.result is None:
            if game.turn == 0:
                cell = ttt.choose_move(game.board, 0, "hard", random.Random(1))
                host.act(0, a="place", cell=cell)
            elif not host.step():
                break
        checker.check_result()
        self.assertEqual(host.result["winners"], [])

    def test_bot_takes_over_disconnected_seat(self):
        host, game = new_game(humans=2, turn_timer=0)
        game.start()
        self.assertFalse(host.step(), "nothing scheduled while a human thinks")
        game.seats[0].is_bot = True
        game.on_seat_changed(0)
        advance(host, Logic.BOT_DELAY + 0.01)
        self.assertEqual(game.board.count(None), 8)
        self.assertEqual(game.turn, 1)

    def test_bot_act_ignores_stale_calls(self):
        host, game = new_game(turn_timer=0)
        game.start()
        game.bot_act(1)                       # not seat 1's turn
        self.assertEqual(game.board, [None] * 9)


class AllBotMatchTests(unittest.TestCase):
    def test_every_level_and_match_length(self):
        for level, match, seed in itertools.product(("easy", "normal", "hard"), (1, 3, 5), range(4)):
            with self.subTest(level=level, match=match, seed=seed):
                host, game = make_game(Logic, 2, humans=0,
                                       options={"bot_level": level, "match": match}, seed=seed)
                checker = Checker(self, host, game)
                game.start()
                host.run_until_over()
                checker.check_result()
                self.assertGreater(checker.pushes, 5)
                self.assertLessEqual(game.round, 2 * match)

    def test_bots_are_deterministic_per_seed(self):
        results = []
        for _ in range(2):
            host, game = make_game(Logic, 2, humans=0, options={"bot_level": "easy"}, seed=77)
            game.start()
            host.run_until_over()
            results.append((host.result, host.events))
        self.assertEqual(results[0], results[1])


if __name__ == "__main__":
    unittest.main()
