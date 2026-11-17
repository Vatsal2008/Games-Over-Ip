"""Ludo logic tests: rules on hand-built positions, pacing, bots, full all-bot games."""
from __future__ import annotations

import heapq
import random
import unittest

from goip.games import logic_class
from goip.games.base import ActionError, Seat
from goip.games.testing import FakeHost, make_game, wire
from goip.games.ludo import logic as L
from goip.games.ludo.logic import (
    HOME, HOME_COLUMN, SAFE_SQUARES, START_OFFSET, STAR_SQUARES, TRACK_LEN, Logic,
    absolute_square, distinct_moves, legal_moves, move_path,
)

VIEW_KEYS = {"tokens", "colors", "turn", "dice", "phase", "movable", "sixes", "home_count",
             "goal", "timer", "finished", "ranking", "last_move", "streak"}


def advance(host, seconds):
    """Like FakeHost.advance, but never runs a timer due after the target time.

    (FakeHost.step skips cancelled timers and then runs the next live one even when
    it lies beyond the target, so a cancelled timer at the top of the heap overshoots.)
    """
    target = host.t + seconds
    while True:
        while host._heap and host._heap[0][1] in host._cancelled:
            host._cancelled.discard(heapq.heappop(host._heap)[1])
        if not host._heap or host._heap[0][0] > target:
            break
        host.step()
    host.t = max(host.t, target)


def rig(logic, values, strict=True):
    """Make the next rolls return `values` (then fail, or fall back to the rng)."""
    seq = list(values)

    def roll():
        if seq:
            return seq.pop(0)
        if strict:
            raise AssertionError("ran out of rigged dice")
        return logic.rng.randint(1, 6)

    logic._roll_die = roll


def position(n=2, tokens=None, turn=0, humans=None, options=None, seed=1, dice=None, start=True):
    """A game with a hand-built position. Default: everyone human, idle timer off."""
    opts = {"turn_timer": 0}
    opts.update(options or {})
    host, lg = make_game(Logic, n, humans=n if humans is None else humans, options=opts, seed=seed)
    for s, row in enumerate(tokens or []):
        if row is not None:
            lg.tokens[s] = list(row)
    lg.turn = turn
    if dice is not None:
        rig(lg, dice)
    if start:
        lg.start()
    return host, lg


def kinds(host):
    return [e["kind"] for _to, e in host.events]


def guard_moves(tc, lg):
    """Fail the test if any code path (bot, auto, human) attempts an illegal move."""
    original = lg._move

    def checked(seat, k):
        tc.assertFalse(lg.finished)
        tc.assertEqual(seat, lg.turn)
        tc.assertEqual(lg.phase, "move")
        tc.assertIn(k, legal_moves(lg.tokens[seat], lg.dice))
        tc.assertIn(k, lg.movable)
        original(seat, k)

    lg._move = checked


def make_checker(tc, n, goal, turn_timer=0):
    stats = {"pushes": 0, "human_timers": 0}

    def check(host):
        stats["pushes"] += 1
        lg = host.logic
        v = host.views[None]
        for s in range(n):
            tc.assertEqual(host.views[s], v)           # Ludo has no private info
        tc.assertTrue(VIEW_KEYS <= set(v))
        tokens, colors = v["tokens"], v["colors"]
        tc.assertEqual(len(tokens), n)
        occupied = {}
        for s, row in enumerate(tokens):
            tc.assertEqual(len(row), 4)
            for p in row:
                tc.assertTrue(-1 <= p <= HOME, p)
                sq = absolute_square(colors[s], p)
                if sq is not None and sq not in SAFE_SQUARES:
                    owner = occupied.setdefault(sq, s)
                    tc.assertEqual(owner, s, "two colours share non-safe square %d: %r" % (sq, tokens))
        homes = [row.count(HOME) for row in tokens]
        tc.assertEqual(v["home_count"], homes)
        tc.assertEqual(sorted(v["ranking"]), list(range(n)))
        tc.assertEqual(v["goal"], goal)
        if v["finished"]:
            tc.assertIsNone(v["turn"])
            tc.assertIsNone(v["timer"])
            w = v["winner"]
            tc.assertEqual(homes[w], goal)
            tc.assertEqual(v["ranking"][0], w)
            tc.assertEqual([s for s in range(n) if homes[s] >= goal], [w])
            return
        tc.assertLess(max(homes), goal)
        turn = v["turn"]
        tc.assertIn(turn, range(n))
        tc.assertIn(v["phase"], ("roll", "move"))
        tc.assertIn(v["sixes"], (0, 1, 2))
        tc.assertGreaterEqual(v["streak"], 0)
        if v["phase"] == "roll":
            tc.assertIsNone(v["dice"])
            tc.assertEqual(v["movable"], [])
            tc.assertFalse(v["auto"])
        else:
            tc.assertIn(v["dice"], range(1, 7))
            tc.assertTrue(v["movable"], "phase move with nothing movable")
            tc.assertEqual(v["movable"], legal_moves(tokens[turn], v["dice"]))
            distinct = distinct_moves(tokens[turn], v["dice"])
            tc.assertEqual(v["auto"], len(distinct) == 1)
        if v["auto"] or lg.seats[turn].is_bot or not turn_timer:
            tc.assertIsNone(v["timer"])
        else:
            stats["human_timers"] += 1
            tc.assertIsNotNone(v["timer"])
            tc.assertTrue(0 <= v["timer"] <= turn_timer + 3)

    return check, stats


def check_event_stream(tc, host, n):
    last_roll = None
    rolls_by_seat = []
    prev = None
    for _to, e in host.events:
        k = e["kind"]
        if k == "roll":
            tc.assertIn(e["value"], range(1, 7))
            tc.assertIn(e["seat"], range(n))
            last_roll = e
            rolls_by_seat.append(e)
        elif k == "move":
            tc.assertEqual(prev["kind"], "roll")
            tc.assertEqual(last_roll["seat"], e["seat"])
            dice = last_roll["value"]
            if e["from"] == -1:
                tc.assertEqual(dice, 6)
                tc.assertEqual(e["path"], [0])
            else:
                tc.assertEqual(e["path"], list(range(e["from"] + 1, e["from"] + dice + 1)))
            tc.assertEqual(e["to"], e["path"][-1])
            for c in e["captured"]:
                tc.assertNotEqual(c["seat"], e["seat"])
        elif k == "home":
            tc.assertEqual(prev["kind"], "move")
            tc.assertEqual((prev["seat"], prev["token"], prev["to"]), (e["seat"], e["token"], HOME))
        elif k == "skip":
            tc.assertEqual(prev["kind"], "roll")
            tc.assertEqual(prev["seat"], e["seat"])
            tc.assertIn(e["reason"], ("no_moves", "three_sixes"))
            if e["reason"] == "three_sixes":
                same = [r for r in rolls_by_seat[-3:] if r["seat"] == e["seat"] and r["value"] == 6]
                tc.assertEqual(len(same), 3)
        else:
            tc.fail("unexpected event %r" % (e,))
        prev = e


# ---------------------------------------------------------------------------------------
class HelperTests(unittest.TestCase):
    def test_constants(self):
        self.assertEqual(TRACK_LEN, 52)
        self.assertEqual(HOME, 56)
        self.assertEqual(list(HOME_COLUMN), [51, 52, 53, 54, 55])
        self.assertEqual(START_OFFSET, {"red": 0, "green": 13, "yellow": 26, "blue": 39})
        self.assertEqual(set(SAFE_SQUARES), {0, 8, 13, 21, 26, 34, 39, 47})
        self.assertEqual(set(STAR_SQUARES), {8, 21, 34, 47})
        self.assertTrue(set(STAR_SQUARES) <= SAFE_SQUARES)

    def test_absolute_square(self):
        self.assertEqual(absolute_square("red", 0), 0)
        self.assertEqual(absolute_square("green", 0), 13)
        self.assertEqual(absolute_square("yellow", 0), 26)
        self.assertEqual(absolute_square("blue", 0), 39)
        self.assertEqual(absolute_square("blue", 13), 0)
        self.assertEqual(absolute_square("red", 50), 50)
        self.assertEqual(absolute_square("green", 50), 11)     # last square before green's column
        for p in (-1, 51, 55, 56):
            self.assertIsNone(absolute_square("red", p))
        # Each colour's main track visits 51 distinct squares and never its own start-1 square.
        for color, off in START_OFFSET.items():
            squares = {absolute_square(color, p) for p in range(51)}
            self.assertEqual(len(squares), 51)
            self.assertNotIn((off - 1) % 52, squares)

    def test_legal_moves(self):
        self.assertEqual(legal_moves([-1, -1, -1, -1], 5), [])
        self.assertEqual(legal_moves([-1, -1, -1, -1], 6), [0, 1, 2, 3])
        self.assertEqual(legal_moves([-1, 50, 55, 56], 1), [1, 2])
        self.assertEqual(legal_moves([-1, 50, 55, 56], 6), [0, 1])
        self.assertEqual(legal_moves([53, 54, 56, 56], 3), [0])
        self.assertEqual(legal_moves([53, 54, 56, 56], 4), [])
        self.assertEqual(legal_moves([0, 10, 49, 52], 2), [0, 1, 2, 3])

    def test_distinct_moves(self):
        self.assertEqual(distinct_moves([-1, -1, -1, -1], 6), [0])
        self.assertEqual(distinct_moves([10, 10, -1, -1], 3), [0])
        self.assertEqual(distinct_moves([10, 10, -1, -1], 6), [0, 2])
        self.assertEqual(distinct_moves([56, 20, 20, 30], 2), [1, 3])

    def test_move_path(self):
        self.assertEqual(move_path(-1, 6), [0])
        self.assertEqual(move_path(3, 4), [4, 5, 6, 7])
        self.assertEqual(move_path(48, 5), [49, 50, 51, 52, 53])

    def test_class_api(self):
        self.assertIs(logic_class("ludo"), Logic)
        self.assertEqual(Logic.ID, "ludo")
        self.assertEqual(Logic.NAME, "Ludo")
        self.assertTrue(0 < len(Logic.TAGLINE) <= 28)
        self.assertEqual(Logic.SEAT_COUNTS, (2, 3, 4))
        self.assertEqual(Logic.seat_colors(2), ["red", "yellow"])
        self.assertEqual(Logic.seat_colors(3), ["red", "green", "yellow"])
        self.assertEqual(Logic.seat_colors(4), ["red", "green", "yellow", "blue"])
        opts = Logic.clean_options({})
        self.assertEqual(opts, {"bot_level": "normal", "turn_timer": 30, "goal": 4})
        self.assertEqual(Logic.clean_options({"goal": 2})["goal"], 2)
        self.assertEqual(Logic.clean_options({"goal": 3})["goal"], 4)
        self.assertEqual(Logic.clean_options({"goal": "2"})["goal"], 4)

    def test_bad_colours_fall_back(self):
        host = FakeHost()
        seats = [Seat(0, "A", False, "red"), Seat(1, "B", False, "blue")]
        lg = host.attach(Logic(host, seats, {}, random.Random(1)))
        self.assertEqual(lg.colors, ["red", "blue"])               # valid distinct colours kept
        seats = [Seat(0, "A", False, "red"), Seat(1, "B", False, "red")]
        lg = host.attach(Logic(host, seats, {}, random.Random(1)))
        self.assertEqual(lg.colors, ["red", "yellow"])


# ---------------------------------------------------------------------------------------
class RuleTests(unittest.TestCase):
    def test_start_state(self):
        host, lg = position(n=4, options={"turn_timer": 30})
        v = host.views[0]
        self.assertTrue(VIEW_KEYS <= set(v))
        self.assertEqual(v["tokens"], [[-1] * 4] * 4)
        self.assertEqual(v["colors"], ["red", "green", "yellow", "blue"])
        self.assertEqual((v["turn"], v["phase"], v["dice"], v["movable"]), (0, "roll", None, []))
        self.assertEqual(v["home_count"], [0, 0, 0, 0])
        self.assertEqual(v["goal"], 4)
        self.assertEqual(v["timer"], 31.0)                  # turn_timer + start delay
        self.assertFalse(v["finished"])
        self.assertIsNone(v["last_move"])
        self.assertEqual(wire(lg.view_for(None)), lg.view_for(None))

    def test_no_legal_move_passes(self):
        host, lg = position(dice=[3])
        host.act(0, a="roll")
        self.assertEqual(kinds(host), ["roll", "skip"])
        self.assertEqual(host.events_of("skip")[0]["reason"], "no_moves")
        self.assertEqual(host.events_of("skip")[0]["seat"], 0)
        v = host.views[0]
        self.assertEqual((v["turn"], v["phase"], v["dice"]), (1, "roll", None))
        self.assertEqual(v["last_roll"], {"seat": 0, "value": 3})
        self.assertEqual(v["tokens"][0], [-1] * 4)

    def test_six_leaves_base_by_auto_move(self):
        host, lg = position(dice=[6])
        host.act(0, a="roll")
        v = host.views[0]
        self.assertEqual((v["phase"], v["dice"], v["auto"], v["movable"]), ("move", 6, True, [0, 1, 2, 3]))
        self.assertIsNone(v["timer"])
        advance(host, L.ROLL_TIME - 0.05)
        self.assertEqual(host.views[0]["phase"], "move")
        advance(host, 0.1)
        move = host.events_of("move")[0]
        self.assertEqual(move, {"kind": "move", "seat": 0, "token": 0, "path": [0], "captured": [],
                                "from": -1, "to": 0})
        v = host.views[0]
        self.assertEqual(v["tokens"][0], [0, -1, -1, -1])
        self.assertEqual((v["turn"], v["phase"], v["sixes"], v["streak"]), (0, "roll", 1, 1))
        self.assertEqual(v["last_move"], {"seat": 0, "token": 0, "from": -1, "to": 0})

    def test_human_may_pick_during_auto_pause(self):
        host, lg = position(dice=[6])
        host.act(0, a="roll")
        host.act(0, a="move", token=2)
        self.assertEqual(lg.tokens[0], [-1, -1, 0, -1])
        advance(host, 5)                                       # cancelled auto-move does nothing
        self.assertEqual(len(host.events_of("move")), 1)
        with self.assertRaises(ActionError):
            host.act(0, a="move", token=0)

    def test_choice_and_path(self):
        host, lg = position(tokens=[[3, 20, -1, -1]], dice=[4])
        host.act(0, a="roll")
        v = host.views[0]
        self.assertEqual((v["phase"], v["movable"], v["auto"]), ("move", [0, 1], False))
        advance(host, 30)                                      # idle timer off: nothing happens
        self.assertEqual(host.views[0]["phase"], "move")
        host.act(0, a="move", token=0)
        self.assertEqual(host.events_of("move")[0]["path"], [4, 5, 6, 7])
        v = host.views[0]
        self.assertEqual(v["tokens"][0], [7, 20, -1, -1])
        self.assertEqual((v["turn"], v["phase"], v["streak"]), (1, "roll", 0))

    def test_equal_tokens_count_as_one_move(self):
        host, lg = position(tokens=[[10, 10, -1, -1]], dice=[3])
        host.act(0, a="roll")
        v = host.views[0]
        self.assertEqual((v["movable"], v["auto"]), ([0, 1], True))
        advance(host, 1)
        self.assertEqual(lg.tokens[0], [13, 10, -1, -1])

    def test_movable_lists_only_legal_tokens(self):
        host, lg = position(tokens=[[-1, 50, 55, 56]], dice=[6])
        host.act(0, a="roll")
        self.assertEqual(host.views[0]["movable"], [0, 1])
        host, lg = position(tokens=[[-1, 50, 55, 56]], dice=[1])
        host.act(0, a="roll")
        self.assertEqual(host.views[0]["movable"], [1, 2])
        with self.assertRaises(ActionError):
            host.act(0, a="move", token=0)
        with self.assertRaises(ActionError):
            host.act(0, a="move", token=3)

    def test_capture_sends_all_opponents_home_and_rolls_again(self):
        tokens = [[1, -1, -1, -1], [43, 43, 10, -1], [30, -1, -1, -1], [-1] * 4]
        host, lg = position(n=4, tokens=tokens, dice=[3])
        self.assertEqual(absolute_square("green", 43), 4)
        self.assertEqual(absolute_square("yellow", 30), 4)
        host.act(0, a="roll")
        advance(host, L.ROLL_TIME)
        move = host.events_of("move")[0]
        self.assertEqual(move["path"], [2, 3, 4])
        self.assertEqual(sorted((c["seat"], c["token"], c["from"]) for c in move["captured"]),
                         [(1, 0, 43), (1, 1, 43), (2, 0, 30)])
        v = host.views[0]
        self.assertEqual(v["tokens"], [[4, -1, -1, -1], [-1, -1, 10, -1], [-1] * 4, [-1] * 4])
        self.assertEqual((v["turn"], v["phase"], v["sixes"], v["streak"]), (0, "roll", 0, 1))

    def test_no_capture_on_star(self):
        host, lg = position(tokens=[[5, -1, -1, -1], [47, -1, -1, -1]], n=3, dice=[3])
        self.assertEqual(absolute_square("green", 47), 8)
        host.act(0, a="roll")
        advance(host, L.ROLL_TIME)
        self.assertEqual(host.events_of("move")[0]["captured"], [])
        self.assertEqual(lg.tokens[0][0], 8)
        self.assertEqual(lg.tokens[1][0], 47)
        self.assertEqual(host.views[0]["turn"], 1)            # no capture, no 6: turn passes

    def test_no_capture_on_start_squares(self):
        # Green leaves base onto square 13 where a red token stands.
        host, lg = position(n=4, tokens=[[13, -1, -1, -1]], turn=1, dice=[6])
        host.act(1, a="roll")
        advance(host, L.ROLL_TIME)
        self.assertEqual(host.events_of("move")[0]["captured"], [])
        self.assertEqual((lg.tokens[0][0], lg.tokens[1][0]), (13, 0))
        # Red lands on green's start square holding a green token.
        host, lg = position(n=4, tokens=[[10, -1, -1, -1], [0, -1, -1, -1]], dice=[3])
        host.act(0, a="roll")
        advance(host, L.ROLL_TIME)
        self.assertEqual(host.events_of("move")[0]["captured"], [])
        self.assertEqual((lg.tokens[0][0], lg.tokens[1][0]), (13, 0))

    def test_same_colour_stacks(self):
        host, lg = position(tokens=[[4, 1, -1, -1]], dice=[3])
        host.act(0, a="roll")
        host.act(0, a="move", token=1)
        self.assertEqual(lg.tokens[0][:2], [4, 4])
        self.assertEqual(host.events_of("move")[0]["captured"], [])

    def test_home_column_is_out_of_reach(self):
        host, lg = position(tokens=[[48, -1, -1, -1]], dice=[5])
        host.act(0, a="roll")
        advance(host, L.ROLL_TIME)
        self.assertEqual(host.events_of("move")[0]["path"], [49, 50, 51, 52, 53])
        self.assertIsNone(absolute_square("red", 53))
        lg.tokens[0] = [51, 52, 53, 55]
        for sq in range(TRACK_LEN):
            self.assertEqual(lg._captures_at(1, sq), [])

    def test_exact_roll_needed_for_home(self):
        host, lg = position(tokens=[[53, -1, -1, -1]], dice=[4])
        host.act(0, a="roll")
        self.assertEqual(host.events_of("skip")[0]["reason"], "no_moves")
        self.assertEqual(lg.tokens[0][0], 53)
        host, lg = position(tokens=[[53, -1, -1, -1]], dice=[3])
        host.act(0, a="roll")
        advance(host, L.ROLL_TIME)
        self.assertEqual(kinds(host), ["roll", "move", "home"])
        self.assertEqual(host.events_of("home")[0], {"kind": "home", "seat": 0, "token": 0})
        v = host.views[0]
        self.assertEqual(v["home_count"], [1, 0])
        self.assertEqual((v["turn"], v["phase"], v["streak"]), (0, "roll", 1))   # extra roll
        self.assertFalse(v["finished"])

    def test_six_gives_extra_roll_then_turn_passes(self):
        host, lg = position(tokens=[[10, -1, -1, -1]], dice=[6, 2])
        host.act(0, a="roll")
        self.assertEqual(host.views[0]["movable"], [0, 1, 2, 3])
        host.act(0, a="move", token=0)
        v = host.views[0]
        self.assertEqual((v["turn"], v["phase"], v["sixes"], v["streak"]), (0, "roll", 1, 1))
        host.act(0, a="roll")
        advance(host, L.ROLL_TIME)
        self.assertEqual(lg.tokens[0][0], 18)
        v = host.views[0]
        self.assertEqual((v["turn"], v["sixes"], v["streak"]), (1, 0, 0))

    def test_third_six_forfeits(self):
        host, lg = position(tokens=[[10, 20, -1, -1]], dice=[6, 6, 6])
        host.act(0, a="roll")
        host.act(0, a="move", token=0)
        self.assertEqual(host.views[0]["sixes"], 1)
        host.act(0, a="roll")
        host.act(0, a="move", token=1)
        self.assertEqual((host.views[0]["sixes"], host.views[0]["streak"]), (2, 2))
        host.act(0, a="roll")
        self.assertEqual(kinds(host)[-2:], ["roll", "skip"])
        self.assertEqual(host.events_of("skip")[0]["reason"], "three_sixes")
        v = host.views[0]
        self.assertEqual(v["tokens"][0], [16, 26, -1, -1])
        self.assertEqual((v["turn"], v["phase"], v["sixes"], v["streak"]), (1, "roll", 0, 0))

    def test_capture_bonus_resets_six_chain(self):
        # 6 (move), then a 3 that captures: bonus roll, but the six chain is broken.
        tokens = [[10, 1, -1, -1], [43, -1, -1, -1]]
        host, lg = position(n=3, tokens=tokens, dice=[6, 3])
        host.act(0, a="roll")
        host.act(0, a="move", token=0)
        host.act(0, a="roll")
        host.act(0, a="move", token=1)                         # 1 -> 4 captures green
        v = host.views[0]
        self.assertEqual(v["tokens"][1][0], -1)
        self.assertEqual((v["turn"], v["sixes"], v["streak"]), (0, 0, 2))

    def test_reaching_goal_ends_the_game(self):
        host, lg = position(tokens=[[56, 54, -1, -1]], options={"goal": 2}, dice=[2])
        host.act(0, a="roll")
        advance(host, L.ROLL_TIME)
        self.assertEqual(kinds(host), ["roll", "move", "home"])
        self.assertIsNotNone(host.result)
        self.assertEqual(host.result["winners"], [0])
        self.assertEqual(host.result["text"], "P0 wins!")
        self.assertEqual(host.result["ranking"], [0, 1])
        v = host.views[1]
        self.assertTrue(v["finished"])
        self.assertEqual((v["turn"], v["winner"], v["timer"], v["home_count"]), (None, 0, None, [2, 0]))
        self.assertFalse(host.step())                          # nothing left scheduled
        with self.assertRaises(ActionError):
            host.act(0, a="roll")

    def test_classic_goal_needs_four(self):
        host, lg = position(tokens=[[56, 56, 54, 10]], dice=[2])
        host.act(0, a="roll")
        host.act(0, a="move", token=2)
        self.assertIsNone(host.result)
        self.assertEqual(host.views[0]["home_count"], [3, 0])
        self.assertEqual((host.views[0]["turn"], host.views[0]["phase"]), (0, "roll"))

    def test_ranking_by_progress(self):
        host, lg = position(n=4, tokens=[[10, -1, -1, -1], [56, 3, -1, -1], [30, 30, -1, -1], [5, -1, -1, -1]])
        self.assertEqual(host.views[0]["ranking"], [1, 2, 0, 3])

    def test_invalid_actions(self):
        host, lg = position(tokens=[[10, 20, -1, -1]], dice=[3])
        bad = [(1, {"a": "roll"}), (0, {"a": "move", "token": 0}), (0, {"a": "jump"}), (0, {}),
               (5, {"a": "roll"})]
        for seat, action in bad:
            with self.assertRaises(ActionError):
                host.act(seat, **action)
        host.act(0, a="roll")
        for action in ({"a": "roll"}, {"a": "move"}, {"a": "move", "token": "1"},
                       {"a": "move", "token": True}, {"a": "move", "token": 7},
                       {"a": "move", "token": 2}, {"a": "move", "token": None}):
            with self.assertRaises(ActionError):
                host.act(0, **action)
        with self.assertRaises(ActionError):
            host.act(1, a="move", token=0)
        self.assertEqual(host.views[0]["phase"], "move")       # state untouched by bad actions
        host.act(0, a="move", token=1)
        self.assertEqual(lg.tokens[0][1], 23)

    def test_bot_act_ignores_other_seats(self):
        host, lg = position(dice=[3])
        lg.bot_act(1)
        self.assertEqual(host.events, [])


# ---------------------------------------------------------------------------------------
class PacingTests(unittest.TestCase):
    def test_bot_timeline(self):
        host, lg = position(tokens=[[10, 20, -1, -1]], humans=0, dice=[4, 3, 2, 1], start=False)
        lg.start()
        self.assertTrue(host.step())
        self.assertAlmostEqual(host.t, L.START_DELAY)
        self.assertEqual((lg.phase, lg.auto), ("move", False))        # choice: bot thinks
        self.assertTrue(host.step())
        self.assertAlmostEqual(host.t, L.START_DELAY + L.ROLL_TIME)
        self.assertEqual(lg.turn, 1)                                  # moved 4, no capture
        t_move = host.t
        self.assertTrue(host.step())                                  # yellow: 3, no moves
        self.assertAlmostEqual(host.t, t_move + L.MOVE_PAUSE + 4 * L.STEP_TIME)
        self.assertEqual(host.events_of("skip")[0]["seat"], 1)
        t_skip = host.t
        self.assertTrue(host.step())                                  # red rolls 2 -> choice
        self.assertAlmostEqual(host.t, t_skip + L.ROLL_TIME + L.PASS_TIME)
        self.assertEqual(host.events_of("roll")[-1], {"kind": "roll", "seat": 0, "value": 2})

    def test_auto_move_and_capture_timing(self):
        host, lg = position(tokens=[[1, -1, -1, -1], [30, -1, -1, -1]], humans=0, dice=[3, 5], start=False)
        lg.start()
        host.step()                                                   # roll 3 at START_DELAY
        self.assertTrue(lg.auto)
        host.step()                                                   # auto-move with capture
        self.assertAlmostEqual(host.t, L.START_DELAY + L.ROLL_TIME)
        self.assertEqual(len(host.events_of("move")[0]["captured"]), 1)
        t_move = host.t
        host.step()                                                   # bonus roll
        self.assertAlmostEqual(host.t, t_move + L.MOVE_PAUSE + 3 * L.STEP_TIME + L.CAPTURE_TIME)
        self.assertEqual(host.events_of("roll")[-1]["seat"], 0)

    def test_idle_human_is_played_by_timer(self):
        host, lg = position(humans=1, options={"turn_timer": 15}, tokens=[[10, 20, -1, -1]], dice=[4])
        self.assertEqual(host.views[0]["timer"], 16.0)
        advance(host, 15.9)
        self.assertEqual(host.events_of("roll"), [])
        advance(host, 0.2)
        self.assertEqual(len(host.events_of("roll")), 1)              # bot_act rolled for them
        v = host.views[0]
        self.assertEqual((v["phase"], v["auto"]), ("move", False))
        self.assertAlmostEqual(v["timer"], 15 + L.ROLL_TIME)
        roll_t = 16.0                                                 # the idle roll happened here
        advance(host, roll_t + 15 + L.ROLL_TIME - 0.05 - host.t)
        self.assertEqual(host.events_of("move"), [])
        advance(host, 0.1)
        self.assertEqual(len(host.events_of("move")), 1)              # ...and moved for them
        self.assertEqual(host.views[0]["turn"], 1)

    def test_human_timer_after_move_includes_animation(self):
        host, lg = position(humans=2, options={"turn_timer": 30}, tokens=[[10, 20, -1, -1]], dice=[4])
        host.act(0, a="roll")
        host.act(0, a="move", token=0)
        self.assertAlmostEqual(host.views[1]["timer"], 30 + L.MOVE_PAUSE + 4 * L.STEP_TIME)


# ---------------------------------------------------------------------------------------
class SeatChangeTests(unittest.TestCase):
    def test_human_leaves_in_move_phase(self):
        host, lg = position(tokens=[[10, 20, -1, -1]])
        rig(lg, [3], strict=False)
        guard_moves(self, lg)
        host.act(0, a="roll")
        self.assertFalse(host.step())                                 # waits for the human forever
        lg.seats[0].is_bot = True
        lg.on_seat_changed(0)
        self.assertTrue(host.step())
        self.assertEqual(host.events_of("move")[0]["seat"], 0)
        self.assertEqual(lg.turn, 1)
        self.assertFalse(host.step())                                 # seat 1 is a timer-less human
        lg.seats[1].is_bot = True
        lg.on_seat_changed(1)
        self.assertIsNotNone(host.run_until_over())

    def test_bot_becomes_human_gets_idle_timer(self):
        host, lg = position(humans=0, options={"turn_timer": 30})
        lg.seats[0].is_bot = False
        lg.on_seat_changed(0)
        self.assertEqual(host.views[0]["timer"], 30.0)
        advance(host, 29.9)
        self.assertEqual(host.events_of("roll"), [])
        advance(host, 0.2)
        self.assertEqual(len(host.events_of("roll")), 1)

    def test_change_during_auto_pause_moves_once(self):
        host, lg = position(tokens=[[10, -1, -1, -1]], dice=None)
        rig(lg, [3], strict=False)
        host.act(0, a="roll")
        self.assertTrue(lg.auto)
        lg.seats[0].is_bot = True
        lg.on_seat_changed(0)
        advance(host, L.ROLL_TIME + 0.01)
        self.assertEqual(len(host.events_of("move")), 1)
        self.assertEqual(lg.tokens[0][0], 13)

    def test_change_of_other_seat_keeps_schedule(self):
        host, lg = position(humans=0)
        lg.seats[1].is_bot = False
        lg.on_seat_changed(1)
        host.step()
        self.assertAlmostEqual(host.t, L.START_DELAY)
        self.assertEqual(host.events_of("roll")[0]["seat"], 0)

    def test_stop_cancels_everything(self):
        host, lg = make_game(Logic, 4, humans=0, seed=3)
        lg.start()
        for _ in range(10):
            host.step()
        n_events = len(host.events)
        lg.stop()
        host.run_until_over()
        self.assertIsNone(host.result)
        self.assertEqual(len(host.events), n_events)


# ---------------------------------------------------------------------------------------
class BotTests(unittest.TestCase):
    def setup_choice(self, tokens, dice, level, n=2):
        host, lg = position(n=n, tokens=tokens, dice=[dice], humans=0, start=False)
        lg.bot_levels = [level] * n
        lg._roll(0)
        self.assertEqual(lg.phase, "move")
        return lg

    def test_normal_priorities(self):
        # capture > advancing the furthest token
        lg = self.setup_choice([[1, 30, -1, -1], [30, -1, -1, -1]], 3, "normal")
        self.assertEqual(lg.choose_move(0), 0)
        # leave base > advancing
        lg = self.setup_choice([[10, -1, -1, -1]], 6, "normal")
        self.assertEqual(lg.choose_move(0), 1)
        # reach home > reach safety
        lg = self.setup_choice([[53, 5, -1, -1]], 3, "normal")
        self.assertEqual(lg.choose_move(0), 0)
        # reach a safe square > furthest
        lg = self.setup_choice([[5, 30, -1, -1]], 3, "normal")
        self.assertEqual(lg.choose_move(0), 0)

    def test_hard_avoids_landing_in_front_of_opponent(self):
        # Yellow sits on star 21; red 20 -> 22 would land right in front of it.
        tokens = [[20, 5, -1, -1], [47, 56, 56, 56]]
        self.assertEqual(absolute_square("yellow", 47), 21)
        lg = self.setup_choice(tokens, 2, "normal")
        self.assertEqual(lg.choose_move(0), 0)                        # normal: furthest token
        lg = self.setup_choice(tokens, 2, "hard")
        self.assertEqual(lg.choose_move(0), 1)                        # hard: stays out of reach

    def test_hard_still_captures(self):
        lg = self.setup_choice([[1, 30, -1, -1], [30, -1, -1, -1]], 3, "hard")
        self.assertEqual(lg.choose_move(0), 0)

    def test_easy_is_sometimes_random(self):
        lg = self.setup_choice([[1, 30, -1, -1], [30, -1, -1, -1]], 3, "easy")
        picks = [lg.choose_move(0) for _ in range(400)]
        others = picks.count(1)
        self.assertTrue(40 < others < 160, others)                    # about 25 %
        self.assertTrue(all(p in (0, 1) for p in picks))

    def test_idle_human_uses_normal_level(self):
        host, lg = position(tokens=[[1, 30, -1, -1], [30, -1, -1, -1]], dice=[3],
                            options={"bot_level": "easy"})
        self.assertEqual(lg.level_for(0), "normal")
        lg.seats[1].is_bot = True
        self.assertEqual(lg.level_for(1), "easy")


# ---------------------------------------------------------------------------------------
class FullGameTests(unittest.TestCase):
    def play(self, n, goal, seed, humans=0, turn_timer=0, level=None):
        options = {"goal": goal, "turn_timer": turn_timer,
                   "bot_level": level or ("easy", "normal", "hard")[seed % 3]}
        host, lg = make_game(Logic, n, humans=humans, options=options, seed=seed)
        check, stats = make_checker(self, n, goal, turn_timer)
        host.on_push = check
        guard_moves(self, lg)
        lg.start()
        result = host.run_until_over()
        self.assertIsNotNone(result, "game did not finish (n=%d goal=%d seed=%d)" % (n, goal, seed))
        self.assertEqual(len(result["winners"]), 1)
        w = result["winners"][0]
        self.assertEqual(lg.home_count(w), goal)
        self.assertEqual(result["ranking"][0], w)
        self.assertIn("wins", result["text"])
        self.assertTrue(host.views[None]["finished"])
        check_event_stream(self, host, n)
        self.assertGreater(stats["pushes"], 20)
        return host, lg, stats

    def test_all_bot_games(self):
        for n in (2, 3, 4):
            for goal in (2, 4):
                for seed in (1, 2, 3):
                    with self.subTest(n=n, goal=goal, seed=seed):
                        host, lg, _ = self.play(n, goal, seed)
                        kinds_seen = set(kinds(host))
                        self.assertTrue({"roll", "move", "skip"} <= kinds_seen)

    def test_rare_rules_happen_in_long_games(self):
        captures = homes = three = 0
        for seed in range(4, 8):
            host, _lg, _ = self.play(4, 4, seed)
            captures += sum(len(e["captured"]) for e in host.events_of("move"))
            homes += len(host.events_of("home"))
            three += sum(1 for e in host.events_of("skip") if e["reason"] == "three_sixes")
        self.assertGreater(captures, 0)
        self.assertGreater(homes, 4)
        self.assertGreater(three, 0)

    def test_humans_driven_by_idle_timer(self):
        for n, humans in ((2, 1), (3, 2), (4, 4)):
            with self.subTest(n=n, humans=humans):
                _host, _lg, stats = self.play(n, 2, 11 + n, humans=humans, turn_timer=15)
                self.assertGreater(stats["human_timers"], 10)


class _FastHost(FakeHost):
    """FakeHost without JSON or views, for quick statistics."""

    def push_state(self):
        self.pushes += 1

    def send_event(self, event, to_seat=None):
        pass


class BotStrengthTests(unittest.TestCase):
    def win_rate(self, levels, games, goal=4):
        n = len(levels)
        colors = Logic.seat_colors(n)
        wins = 0
        for g in range(games):
            target = g % n                                   # rotate the strong seat
            lv = [levels[(s - target) % n] for s in range(n)]
            host = _FastHost()
            seats = [Seat(i, "B%d" % i, True, colors[i]) for i in range(n)]
            lg = host.attach(Logic(host, seats, {"goal": goal}, random.Random(1000 + g)))
            lg.bot_levels = lv
            lg.start()
            result = host.run_until_over()
            self.assertIsNotNone(result)
            wins += result["winners"] == [target]
        return wins / float(games)

    def test_hard_beats_three_easy_bots(self):
        rate = self.win_rate(["hard", "easy", "easy", "easy"], 160)
        self.assertGreater(rate, 0.40, rate)

    def test_hard_beats_easy_head_to_head(self):
        rate = self.win_rate(["hard", "easy"], 120)
        self.assertGreater(rate, 0.65, rate)


if __name__ == "__main__":
    unittest.main()
