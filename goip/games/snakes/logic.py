"""Snakes and Ladders for 2-4 seats on a 10x10 zig-zag board (1 bottom-left, 100 top-left)."""
from __future__ import annotations

import random
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..base import TURN_TIMER, ActionError, GameLogic, Option, Seat

GOAL = 100

CLASSIC_LADDERS: Tuple[Tuple[int, int], ...] = (
    (1, 38), (4, 14), (9, 31), (21, 42), (28, 84), (36, 44), (51, 67), (71, 91), (80, 100),
)
CLASSIC_SNAKES: Tuple[Tuple[int, int], ...] = (   # (head, tail)
    (16, 6), (47, 26), (49, 11), (56, 53), (62, 19), (64, 60), (87, 24), (93, 73), (95, 75), (98, 78),
)

LAYOUT = Option("layout", "Board", (("classic", "Classic"), ("random", "Random")), "classic")
SIX_AGAIN = Option("six_again", "Six rolls again", ((True, "On"), (False, "Off")), True)
EXACT_FINISH = Option("exact_finish", "Exact roll to finish", ((True, "On"), (False, "Off")), True)

# Random layout parameters.
RANDOM_LADDERS = 8
RANDOM_SNAKES = 8
MIN_LENGTH = 10
MAX_LADDER = 50
MAX_SNAKE = 60
MAX_SIXES = 3            # at most this many rolls in a row

# Pacing (seconds) so the next actor waits for the view's animation.
DICE_TIME = 0.6
STEP_TIME = 0.16
JUMP_TIME = 0.7

Pairs = List[Tuple[int, int]]


def square_to_rowcol(n: int) -> Tuple[int, int]:
    """Grid cell of square n as (row_from_top, col), 10x10 zig-zag from the bottom-left.

    1 -> (9, 0), 10 -> (9, 9), 11 -> (8, 9), 100 -> (0, 0). Square 0 (start, off the
    board beside square 1) maps to (9, -1).
    """
    if n == 0:
        return 9, -1
    if not 1 <= n <= GOAL:
        raise ValueError("square out of range: %r" % (n,))
    idx = n - 1
    row_from_bottom, k = divmod(idx, 10)
    col = k if row_from_bottom % 2 == 0 else 9 - k
    return 9 - row_from_bottom, col


def validate_layout(ladders: Sequence[Sequence[int]], snakes: Sequence[Sequence[int]],
                    n_ladders: int = RANDOM_LADDERS, n_snakes: int = RANDOM_SNAKES,
                    min_length: int = MIN_LENGTH) -> bool:
    """True if a layout meets the random-board rules (counts, lengths, distinct endpoints...)."""
    if len(ladders) != n_ladders or len(snakes) != n_snakes:
        return False
    ends: List[int] = []
    for a, b in ladders:
        if not (1 <= a < b <= GOAL) or b - a < min_length or a == 1 or b == GOAL:
            return False
        ends += [a, b]
    for head, tail in snakes:
        if not (1 <= tail < head <= GOAL) or head - tail < min_length or head == GOAL:
            return False
        ends += [head, tail]
    # Distinct endpoints also rules out chains (an end that is another start).
    return len(set(ends)) == len(ends)


def random_layout(rng: random.Random) -> Tuple[Pairs, Pairs]:
    """8 ladders and 8 snakes drawn from rng (deterministic for a given rng state)."""
    while True:
        used = set()
        ladders: Pairs = []
        snakes: Pairs = []
        ok = True
        for _ in range(RANDOM_LADDERS):
            for _try in range(100):
                a = rng.randint(2, GOAL - 1 - MIN_LENGTH)
                b = rng.randint(a + MIN_LENGTH, min(GOAL - 1, a + MAX_LADDER))
                if a not in used and b not in used:
                    break
            else:
                ok = False
                break
            used.update((a, b))
            ladders.append((a, b))
        if ok:
            for _ in range(RANDOM_SNAKES):
                for _try in range(100):
                    head = rng.randint(1 + MIN_LENGTH, GOAL - 1)
                    tail = rng.randint(max(1, head - MAX_SNAKE), head - MIN_LENGTH)
                    if head not in used and tail not in used:
                        break
                else:
                    ok = False
                    break
                used.update((head, tail))
                snakes.append((head, tail))
        if ok and validate_layout(ladders, snakes):
            return sorted(ladders), sorted(snakes)


def anim_time(path_len: int, jumped: bool) -> float:
    """How long the view needs to show a roll + move."""
    return DICE_TIME + STEP_TIME * path_len + (JUMP_TIME if jumped else 0.0)


class Logic(GameLogic):
    ID = "snakes"
    NAME = "Snakes & Ladders"
    TAGLINE = "Climb up, slide down"
    SEAT_COUNTS = (2, 3, 4)
    OPTIONS = (LAYOUT, SIX_AGAIN, EXACT_FINISH, TURN_TIMER)
    BOT_DELAY = 0.5          # think time on top of the animation time
    START_DELAY = 0.8        # before the very first bot roll

    def __init__(self, host: Any, seats: Sequence[Seat], options: Optional[Dict[str, Any]] = None,
                 rng: Optional[random.Random] = None) -> None:
        super().__init__(host, seats, options, rng)
        n = len(self.seats)
        self.layout: str = str(self.options["layout"])
        self.six_again: bool = bool(self.options["six_again"])
        self.exact_finish: bool = bool(self.options["exact_finish"])
        self.ladders: Pairs = list(CLASSIC_LADDERS)
        self.snakes: Pairs = list(CLASSIC_SNAKES)
        self._jumps: Dict[int, Tuple[str, int]] = {}
        self.pos: List[int] = [0] * n
        self.turn: Optional[int] = None
        self.last_roll: Optional[int] = None
        self.streak = 0
        self.winner: Optional[int] = None
        self.last_move: Optional[Dict[str, Any]] = None
        self.ranking: Optional[List[int]] = None

    # ---------------------------------------------------------------- contract
    def start(self) -> None:
        if self.layout == "random":
            self.ladders, self.snakes = random_layout(self.rng)
        self._jumps = {a: ("ladder", b) for a, b in self.ladders}
        self._jumps.update({h: ("snake", t) for h, t in self.snakes})
        self.turn = 0
        self._schedule(0, self.START_DELAY)
        self.push()

    def view_for(self, seat: Optional[int]) -> Dict[str, Any]:
        return {
            "board": {"ladders": [[a, b] for a, b in self.ladders],
                      "snakes": [[h, t] for h, t in self.snakes]},
            "pos": list(self.pos),
            "turn": self.turn,
            "last_roll": self.last_roll,
            "streak": self.streak,
            "last_move": dict(self.last_move) if self.last_move else None,
            "six_again": self.six_again,
            "exact_finish": self.exact_finish,
            "layout": self.layout,
            "phase": "roll",
            "timer": self.time_left(),
            "finished": self.finished,
            "winner": self.winner,
            "ranking": list(self.ranking) if self.ranking else None,
        }

    def on_action(self, seat: int, action: Dict[str, Any]) -> None:
        if self.finished or self.winner is not None:
            raise ActionError("The game is over")
        kind = action.get("a") if isinstance(action, dict) else None
        if kind != "roll":
            raise ActionError("Unknown action")
        if seat != self.turn:
            raise ActionError("Not your turn")
        self.clear_actor()
        self._roll(seat)

    def bot_act(self, seat: int) -> None:
        if self.finished or self.winner is not None or seat != self.turn:
            return
        self._roll(seat)

    def on_seat_changed(self, seat: int) -> None:
        if self.finished:
            return
        super().on_seat_changed(seat)
        self.push()

    # ------------------------------------------------------------------ flow
    def _schedule(self, seat: int, anim: float) -> None:
        self.schedule_actor(seat, anim + (self.BOT_DELAY if self.is_bot(seat) else 0.0))

    def _roll(self, seat: int) -> None:
        value = self.rng.randint(1, 6)
        self.last_roll = value
        self.emit("roll", seat=seat, value=value)

        frm = self.pos[seat]
        target = frm + value
        overshoot = False
        if target > GOAL:
            if self.exact_finish:
                target = frm
                overshoot = True
            else:
                target = GOAL
        path = list(range(frm + 1, target + 1))
        to = target
        via: Optional[Dict[str, Any]] = None
        if path and target in self._jumps:
            kind, dest = self._jumps[target]
            via = {"type": kind, "from": target, "to": dest}
            to = dest
        self.pos[seat] = to
        move = {"seat": seat, "from": frm, "path": path, "to": to, "via": via,
                "overshoot": overshoot, "value": value}
        self.last_move = move
        self.emit("move", **move)
        anim = anim_time(len(path), via is not None)

        if to == GOAL:
            self.clear_actor()
            self.winner = seat
            self.turn = None
            self.streak = 0
            others = sorted((s for s in range(len(self.seats)) if s != seat),
                            key=lambda s: (-self.pos[s], s))
            self.ranking = [seat] + others
            self.push()
            self.later(anim, self._finish_game)
            return

        if value == 6 and self.six_again and self.streak + 1 < MAX_SIXES:
            self.streak += 1
            nxt = seat
        else:
            self.streak = 0
            nxt = (seat + 1) % len(self.seats)
        self.turn = nxt
        self._schedule(nxt, anim)
        self.push()

    def _finish_game(self) -> None:
        assert self.winner is not None and self.ranking is not None
        self.finish([self.winner], "%s wins!" % self.name(self.winner),
                    ranking=list(self.ranking), pos=list(self.pos))
