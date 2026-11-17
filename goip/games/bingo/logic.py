"""Bingo for 2-4 seats: private 5x5 boards of 1..25, take turns calling numbers, 5 lines wins."""
from __future__ import annotations

import random
from typing import Any, Dict, List, Optional, Sequence, Set, Tuple

from ..base import BOT_LEVEL, TURN_TIMER, ActionError, GameLogic, Seat

SIZE = 5
CELLS = SIZE * SIZE
NUMBERS = tuple(range(1, CELLS + 1))
WIN_LINES = 5            # lines needed: B-I-N-G-O
LETTERS = "BINGO"

# The 12 lines as tuples of cell indexes: rows 0-4, columns 5-9, diagonal 10, anti-diagonal 11.
LINES: Tuple[Tuple[int, ...], ...] = (
    tuple(tuple(r * SIZE + c for c in range(SIZE)) for r in range(SIZE))
    + tuple(tuple(r * SIZE + c for r in range(SIZE)) for c in range(SIZE))
    + (tuple(i * SIZE + i for i in range(SIZE)),
       tuple(i * SIZE + (SIZE - 1 - i) for i in range(SIZE)))
)
CELL_LINES: Tuple[Tuple[int, ...], ...] = tuple(
    tuple(li for li, line in enumerate(LINES) if cell in line) for cell in range(CELLS)
)

ARRANGE_SECONDS = 40.0   # arrange phase time limit
CALL_ANIM = 0.5          # time the view needs to show a call before the next actor


def new_board(rng: random.Random) -> List[int]:
    return rng.sample(NUMBERS, CELLS)


def completed_lines(board: Sequence[int], called: Set[int]) -> List[int]:
    """Indexes (into LINES) of the lines fully marked on `board`."""
    return [li for li, line in enumerate(LINES) if all(board[c] in called for c in line)]


def call_scores(board: Sequence[int], called: Set[int]) -> Dict[int, int]:
    """Score of every uncalled number for this board: sum of 4**marks over its lines."""
    marked = [board[c] in called for c in range(CELLS)]
    out: Dict[int, int] = {}
    for cell in range(CELLS):
        if marked[cell]:
            continue
        score = 0
        for li in CELL_LINES[cell]:
            score += 4 ** sum(1 for c in LINES[li] if marked[c])
        out[board[cell]] = score
    return out


def choose_call(board: Sequence[int], called: Set[int], level: str, rng: random.Random) -> int:
    """The number a bot calls next (must have at least one uncalled number)."""
    scores = call_scores(board, called)
    if not scores:
        raise ValueError("every number has been called")
    if level == "easy" and rng.random() < 0.5:
        return rng.choice(sorted(scores))
    if level == "normal" and rng.random() < 0.15:
        ranked = sorted(scores, key=lambda n: (-scores[n], n))
        return rng.choice(ranked[:3])
    top = max(scores.values())
    return rng.choice(sorted(n for n, s in scores.items() if s == top))


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


def _join_names(names: List[str]) -> str:
    if len(names) <= 1:
        return "".join(names)
    return ", ".join(names[:-1]) + " and " + names[-1]


class Logic(GameLogic):
    ID = "bingo"
    NAME = "Bingo"
    TAGLINE = "Five lines spell BINGO"
    SEAT_COUNTS = (2, 3, 4)
    OPTIONS = (BOT_LEVEL, TURN_TIMER)
    BOT_DELAY = 1.0
    ARRANGE_SECONDS = ARRANGE_SECONDS
    FINAL_PAUSE = 1.0     # the winning call stays up this long before finish()

    def __init__(self, host: Any, seats: Sequence[Seat], options: Optional[Dict[str, Any]] = None,
                 rng: Optional[random.Random] = None) -> None:
        super().__init__(host, seats, options, rng)
        n = len(self.seats)
        self.boards: List[List[int]] = [list(NUMBERS) for _ in range(n)]
        self.ready: List[bool] = [False] * n
        self.my_lines: List[List[int]] = [[] for _ in range(n)]
        self.called: List[int] = []
        self._called_set: Set[int] = set()
        self.phase = "arrange"
        self.turn: Optional[int] = None
        self.first: Optional[int] = None
        self.winners: List[int] = []
        self.last_call: Optional[Dict[str, int]] = None
        self.arrange_deadline: Optional[float] = None
        self._arrange_handle: Any = None

    # ---------------------------------------------------------------- helpers
    def _own_board(self, seat: Optional[int]) -> Optional[List[int]]:
        if _is_int(seat) and 0 <= seat < len(self.seats):  # type: ignore[operator]
            return self.boards[seat]  # type: ignore[index]
        return None

    def _level(self, seat: int) -> str:
        return str(self.options.get("bot_level", "normal")) if self.is_bot(seat) else "normal"

    def _schedule(self, seat: int) -> None:
        self.schedule_actor(seat, CALL_ANIM + (self.BOT_DELAY if self.is_bot(seat) else 0.0))

    # ---------------------------------------------------------------- contract
    def start(self) -> None:
        self.boards = [new_board(self.rng) for _ in self.seats]
        self.phase = "arrange"
        for s in range(len(self.seats)):
            if self.is_bot(s):
                self.ready[s] = True
                self.emit("ready", seat=s)
        if all(self.ready):
            self._begin_play()
            return
        self.arrange_deadline = self.host.now() + self.ARRANGE_SECONDS
        self._arrange_handle = self.later(self.ARRANGE_SECONDS, self._arrange_timeout)
        self.push()

    def view_for(self, seat: Optional[int]) -> Dict[str, Any]:
        own = self._own_board(seat)
        board = list(own) if own is not None else None
        if self.phase == "arrange" and self.arrange_deadline is not None:
            timer: Optional[float] = max(0.0, round(self.arrange_deadline - self.host.now(), 1))
        else:
            timer = self.time_left()
        view: Dict[str, Any] = {
            "board": board,
            "marks": [n in self._called_set for n in board] if board is not None else None,
            "called": list(self.called),
            "last_call": dict(self.last_call) if self.last_call else None,
            "lines": [len(x) for x in self.my_lines],
            "my_lines": list(self.my_lines[seat]) if board is not None else [],  # type: ignore[index]
            "ready": list(self.ready),
            "turn": self.turn,
            "first": self.first,
            "phase": self.phase,
            "timer": timer,
            "finished": self.finished,
            "winners": list(self.winners),
        }
        if self.finished:
            view["boards"] = [list(b) for b in self.boards]
        return view

    def on_action(self, seat: int, action: Dict[str, Any]) -> None:
        if self.finished or self.winners:
            raise ActionError("The game is over")
        kind = action.get("a") if isinstance(action, dict) else None
        if kind in ("shuffle", "swap", "ready"):
            if self.phase != "arrange":
                raise ActionError("The game has already started")
            if kind == "ready":
                self._set_ready(seat)
                return
            if self.ready[seat]:
                raise ActionError("You're ready, wait for the others")
            if kind == "shuffle":
                self.boards[seat] = new_board(self.rng)
            else:
                i, j = action.get("i"), action.get("j")
                if not (_is_int(i) and _is_int(j) and 0 <= i < CELLS and 0 <= j < CELLS):
                    raise ActionError("Pick two squares on your board")
                b = self.boards[seat]
                b[i], b[j] = b[j], b[i]
            self.push()
            return
        if kind == "call":
            if self.phase != "play":
                raise ActionError("Wait until everyone is ready")
            if seat != self.turn:
                raise ActionError("Not your turn")
            n = action.get("n")
            if not _is_int(n) or not 1 <= n <= CELLS:
                raise ActionError("Pick a number from 1 to 25")
            if n in self._called_set:
                raise ActionError("That number was already called")
            self.clear_actor()
            self._call(seat, n)
            return
        raise ActionError("Unknown action")

    def bot_act(self, seat: int) -> None:
        if self.finished or self.winners:
            return
        if self.phase == "arrange":
            self._set_ready(seat)
        elif seat == self.turn:
            self._call(seat, choose_call(self.boards[seat], self._called_set, self._level(seat), self.rng))

    def on_seat_changed(self, seat: int) -> None:
        if self.finished:
            return
        if self.phase == "arrange":
            if self.is_bot(seat) and not self.ready[seat]:
                self._set_ready(seat)
            return
        super().on_seat_changed(seat)
        self.push()

    def stop(self) -> None:
        self._arrange_handle = None
        super().stop()

    # ------------------------------------------------------------------ flow
    def _set_ready(self, seat: int) -> None:
        if self.ready[seat]:
            return
        self.ready[seat] = True
        self.emit("ready", seat=seat)
        if all(self.ready):
            self._begin_play()
        else:
            self.push()

    def _arrange_timeout(self) -> None:
        self._arrange_handle = None
        if self.phase != "arrange":
            return
        for s in range(len(self.seats)):
            if not self.ready[s]:
                self.ready[s] = True
                self.emit("ready", seat=s)
        self._begin_play()

    def _begin_play(self) -> None:
        if self._arrange_handle is not None:
            self.cancel(self._arrange_handle)
            self._arrange_handle = None
        self.arrange_deadline = None
        self.phase = "play"
        self.first = self.rng.randrange(len(self.seats))
        self.turn = self.first
        self.emit("phase", phase="play", turn=self.turn)
        self._schedule(self.turn)
        self.push()

    def _call(self, seat: int, n: int) -> None:
        self.called.append(n)
        self._called_set.add(n)
        self.last_call = {"seat": seat, "n": n}
        self.emit("called", seat=seat, n=n)
        for s, board in enumerate(self.boards):
            lines = completed_lines(board, self._called_set)
            new = [li for li in lines if li not in self.my_lines[s]]
            if new:
                self.my_lines[s] = lines
                self.emit("line", to_seat=s, seat=s, count=len(lines), new=new)
        winners = [s for s in range(len(self.seats)) if len(self.my_lines[s]) >= WIN_LINES]
        if winners:
            self.clear_actor()
            self.winners = winners
            self.turn = None
            self.push()
            self.later(self.FINAL_PAUSE, self._finish_game)
            return
        self.turn = (seat + 1) % len(self.seats)
        self._schedule(self.turn)
        self.push()

    def _finish_game(self) -> None:
        names = [self.name(s) for s in self.winners]
        if len(names) == 1:
            text = "%s wins!" % names[0]
        else:
            text = "%s tie!" % _join_names(names)
        self.finish(list(self.winners), text, lines=[len(x) for x in self.my_lines])
