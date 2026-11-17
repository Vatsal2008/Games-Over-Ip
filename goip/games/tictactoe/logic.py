"""Tic-Tac-Toe: best-of-N match between two seats (seat 0 = X, seat 1 = O)."""
from __future__ import annotations

import random
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ..base import BOT_LEVEL, TURN_TIMER, ActionError, GameLogic, Option, Seat

WIN_LINES: Tuple[Tuple[int, int, int], ...] = (
    (0, 1, 2), (3, 4, 5), (6, 7, 8),      # rows
    (0, 3, 6), (1, 4, 7), (2, 5, 8),      # columns
    (0, 4, 8), (2, 4, 6),                 # diagonals
)
_CELL_LINES: Tuple[Tuple[Tuple[int, int, int], ...], ...] = tuple(
    tuple(line for line in WIN_LINES if cell in line) for cell in range(9)
)

MATCH = Option("match", "Match", ((1, "Best of 1"), (3, "Best of 3"), (5, "Best of 5")), 3)
TURN_TIMER_15 = Option(TURN_TIMER.key, TURN_TIMER.label, TURN_TIMER.choices, 15)

Board = Tuple[Optional[int], ...]


# --------------------------------------------------------------------- rules
def find_win(board: Sequence[Optional[int]]) -> Optional[Tuple[int, Tuple[int, int, int]]]:
    """(seat, line) if someone has three in a row, else None."""
    for line in WIN_LINES:
        a, b, c = line
        v = board[a]
        if v is not None and v == board[b] and v == board[c]:
            return v, line
    return None


def _wins_with(board: Sequence[Optional[int]], cell: int, player: int) -> bool:
    """Would `player` complete a line by playing `cell` (which is empty)?"""
    for line in _CELL_LINES[cell]:
        if all(board[c] == player for c in line if c != cell):
            return True
    return False


def empty_cells(board: Sequence[Optional[int]]) -> List[int]:
    return [i for i, v in enumerate(board) if v is None]


def winning_moves(board: Sequence[Optional[int]], player: int) -> List[int]:
    return [i for i in empty_cells(board) if _wins_with(board, i, player)]


# ------------------------------------------------------------------- minimax
_MEMO: Dict[Tuple[Board, int], int] = {}


def _negamax(board: Board, player: int) -> int:
    """Best achievable score for `player` to move on a non-terminal board.

    A win scores 1 + (empty squares left after the winning move), so faster
    wins (and slower losses) are preferred; a draw scores 0.
    """
    key = (board, player)
    hit = _MEMO.get(key)
    if hit is not None:
        return hit
    empties = [i for i, v in enumerate(board) if v is None]
    best = -100
    for i in empties:
        if _wins_with(board, i, player):
            score = len(empties)
        elif len(empties) == 1:
            score = 0
        else:
            nxt = board[:i] + (player,) + board[i + 1:]
            score = -_negamax(nxt, 1 - player)
        if score > best:
            best = score
    if not empties:
        best = 0
    _MEMO[key] = best
    return best


def move_scores(board: Sequence[Optional[int]], player: int) -> Dict[int, int]:
    """Minimax score of every empty cell for `player` (higher is better)."""
    b: Board = tuple(board)
    empties = empty_cells(b)
    out: Dict[int, int] = {}
    for i in empties:
        if _wins_with(b, i, player):
            out[i] = len(empties)
        elif len(empties) == 1:
            out[i] = 0
        else:
            out[i] = -_negamax(b[:i] + (player,) + b[i + 1:], 1 - player)
    return out


def best_moves(board: Sequence[Optional[int]], player: int) -> List[int]:
    scores = move_scores(board, player)
    if not scores:
        return []
    top = max(scores.values())
    return sorted(c for c, s in scores.items() if s == top)


def choose_move(board: Sequence[Optional[int]], player: int, level: str, rng: random.Random) -> int:
    """Pick a cell for `player` on a non-terminal board at the given bot level."""
    empties = empty_cells(board)
    if not empties:
        raise ValueError("board is full")
    if level == "easy":
        wins = winning_moves(board, player)
        if wins and rng.random() < 0.5:
            return rng.choice(wins)
        return rng.choice(empties)
    if level == "hard":
        return rng.choice(best_moves(board, player))
    # normal
    wins = winning_moves(board, player)
    if wins:
        return rng.choice(wins)
    blocks = winning_moves(board, 1 - player)
    if blocks:
        return rng.choice(blocks)
    if rng.random() < 0.6:
        return rng.choice(best_moves(board, player))
    return rng.choice(empties)


def _is_int(v: Any) -> bool:
    return isinstance(v, int) and not isinstance(v, bool)


# --------------------------------------------------------------------- logic
class Logic(GameLogic):
    ID = "tictactoe"
    NAME = "Tic-Tac-Toe"
    TAGLINE = "Classic X and O duel"
    SEAT_COUNTS = (2,)
    OPTIONS = (BOT_LEVEL, MATCH, TURN_TIMER_15)
    BOT_DELAY = 0.8
    ROUND_PAUSE = 1.8     # seconds between a finished round and the next one
    FINAL_PAUSE = 1.2     # seconds the deciding board stays up before finish()

    def __init__(self, host: Any, seats: Sequence[Seat], options: Optional[Dict[str, Any]] = None,
                 rng: Optional[random.Random] = None) -> None:
        super().__init__(host, seats, options, rng)
        self.best_of: int = int(self.options["match"])
        self.board: List[Optional[int]] = [None] * 9
        self.turn: Optional[int] = None
        self.scores: List[int] = [0, 0]
        self.draws = 0
        self.round = 0
        self.starter = 0
        self.line: Optional[List[int]] = None
        self.last: Optional[int] = None
        self.phase = "play"
        self.round_winner: Optional[int] = None
        self.match_over = False

    # ---------------------------------------------------------------- helpers
    @property
    def need(self) -> int:
        """Round wins needed to take the match."""
        return self.best_of // 2 + 1

    @property
    def max_rounds(self) -> int:
        return 2 * self.best_of

    def _level(self, seat: int) -> str:
        # Idle humans get a sensible (not perfect) auto-move.
        return str(self.options.get("bot_level", "normal")) if self.is_bot(seat) else "normal"

    # ---------------------------------------------------------------- contract
    def start(self) -> None:
        self._begin_round()

    def view_for(self, seat: Optional[int]) -> Dict[str, Any]:
        return {
            "board": list(self.board),
            "turn": self.turn,
            "scores": list(self.scores),
            "draws": self.draws,
            "round": self.round,
            "best_of": self.best_of,
            "max_rounds": self.max_rounds,
            "starter": self.starter,
            "line": list(self.line) if self.line else None,
            "last": self.last,
            "round_winner": self.round_winner,
            "match_over": self.match_over,
            "phase": self.phase,
            "timer": self.time_left(),
            "finished": self.finished,
        }

    def on_action(self, seat: int, action: Dict[str, Any]) -> None:
        if self.finished or self.match_over:
            raise ActionError("The match is over")
        kind = action.get("a") if isinstance(action, dict) else None
        if kind != "place":
            raise ActionError("Unknown action")
        if self.phase != "play":
            raise ActionError("Wait for the next round")
        if seat != self.turn:
            raise ActionError("Not your turn")
        cell = action.get("cell")
        if not _is_int(cell) or not 0 <= cell <= 8:
            raise ActionError("Pick a square on the board")
        if self.board[cell] is not None:
            raise ActionError("That square is taken")
        self.clear_actor()
        self._place(seat, cell)

    def bot_act(self, seat: int) -> None:
        if self.finished or self.phase != "play" or seat != self.turn:
            return
        cell = choose_move(self.board, seat, self._level(seat), self.rng)
        self._place(seat, cell)

    def on_seat_changed(self, seat: int) -> None:
        if self.finished:
            return
        super().on_seat_changed(seat)
        self.push()

    # ------------------------------------------------------------------ flow
    def _begin_round(self) -> None:
        self.round += 1
        self.board = [None] * 9
        self.starter = (self.round - 1) % 2
        self.turn = self.starter
        self.line = None
        self.last = None
        self.round_winner = None
        self.phase = "play"
        self.emit("new_round", round=self.round, starter=self.starter)
        self.schedule_actor(self.turn)
        self.push()

    def _place(self, seat: int, cell: int) -> None:
        self.board[cell] = seat
        self.last = cell
        self.emit("place", seat=seat, cell=cell)
        win = find_win(self.board)
        if win is not None:
            self.scores[seat] += 1
            self._end_round(seat, list(win[1]))
        elif all(v is not None for v in self.board):
            self.draws += 1
            self._end_round(None, None)
        else:
            self.turn = 1 - seat
            self.schedule_actor(self.turn)
            self.push()

    def _end_round(self, winner: Optional[int], line: Optional[List[int]]) -> None:
        self.clear_actor()
        self.phase = "round_over"
        self.turn = None
        self.line = line
        self.round_winner = winner
        self.match_over = max(self.scores) >= self.need or self.round >= self.max_rounds
        self.emit("round_over", winner=winner, line=line)
        self.push()
        if self.match_over:
            self.later(self.FINAL_PAUSE, self._finish_match)
        else:
            self.later(self.ROUND_PAUSE, self._begin_round)

    def _finish_match(self) -> None:
        s0, s1 = self.scores
        score = "" if self.best_of == 1 else " %d-%d" % (max(s0, s1), min(s0, s1))
        if s0 != s1:
            w = 0 if s0 > s1 else 1
            self.finish([w], "%s wins%s!" % (self.name(w), score),
                        scores=list(self.scores), draws=self.draws)
        else:
            text = "It's a draw" if self.best_of == 1 or s0 == 0 else "It's a draw,%s" % score
            self.finish([], text, scores=list(self.scores), draws=self.draws)
