"""Ludo with Ludo King rules: server-side logic and bots (no tkinter).

Every token has a *relative progress*:
    -1          in base
    0 .. 50     on the shared main track (absolute square = (progress + offset) % 52)
    51 .. 55    in its own home column
    56          home (the centre)

The view imports the constants and the pure helpers below; it owns all board
geometry (where each absolute square / home-column cell is drawn).
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from ..base import BOT_LEVEL, TURN_TIMER, ActionError, GameLogic, Option

# ------------------------------------------------------------------ board model
TRACK_LEN = 52                      # shared squares on the main loop
TOKENS = 4                          # tokens per player
BASE = -1                           # progress of a token in its base
LAST_TRACK = 50                     # last main-track progress before the home column
HOME_COLUMN = range(51, 56)         # progress values inside a colour's own home column
HOME = 56                           # progress of a token that reached the centre

COLOR_ORDER = ("red", "green", "yellow", "blue")     # clockwise from bottom-left
START_OFFSET = {"red": 0, "green": 13, "yellow": 26, "blue": 39}
START_SQUARES = (0, 13, 26, 39)     # absolute squares where tokens enter the track
STAR_SQUARES = (8, 21, 34, 47)      # absolute squares marked with a star
SAFE_SQUARES = frozenset(START_SQUARES + STAR_SQUARES)

# ------------------------------------------------------------------ pacing (seconds)
ROLL_TIME = 0.7       # dice tumble before the move / next decision
MOVE_PAUSE = 0.7      # base pause after a move before the next actor
STEP_TIME = 0.15      # per square stepped by a moving token
CAPTURE_TIME = 0.4    # extra time when tokens were sent back to base
PASS_TIME = 0.5       # extra beat after a roll that ends the turn with no move
START_DELAY = 1.0     # before the very first roll

GOAL = Option("goal", "Tokens home", ((4, "Classic"), (2, "Quick")), 4)


# ------------------------------------------------------------------ pure helpers
def absolute_square(color: str, progress: int) -> Optional[int]:
    """Absolute main-track square (0..51) of a token, or None in base / home column / home."""
    if not 0 <= progress <= LAST_TRACK:
        return None
    return (progress + START_OFFSET[color]) % TRACK_LEN


def next_progress(progress: int, dice: int) -> Optional[int]:
    """Progress after moving `dice`, or None if that token cannot move."""
    if progress == BASE:
        return 0 if dice == 6 else None
    if progress < 0 or progress >= HOME:
        return None
    new = progress + dice
    return new if new <= HOME else None


def legal_moves(progress_list: Sequence[int], dice: int) -> List[int]:
    """Indexes of the tokens that can move with this roll."""
    return [k for k, p in enumerate(progress_list) if next_progress(p, dice) is not None]


def distinct_moves(progress_list: Sequence[int], dice: int) -> List[int]:
    """One token index per genuinely different move (tokens at the same progress are equivalent)."""
    seen = set()
    out = []
    for k in legal_moves(progress_list, dice):
        p = progress_list[k]
        if p not in seen:
            seen.add(p)
            out.append(k)
    return out


def move_path(progress: int, dice: int) -> List[int]:
    """Progress values a token steps through: leaving base -> [0]; from 3 with 4 -> [4, 5, 6, 7]."""
    if progress == BASE:
        return [0]
    return list(range(progress + 1, progress + dice + 1))


def _abs(offset: int, progress: int) -> Optional[int]:
    if not 0 <= progress <= LAST_TRACK:
        return None
    return (progress + offset) % TRACK_LEN


# ------------------------------------------------------------------ game logic
class Logic(GameLogic):
    ID = "ludo"
    NAME = "Ludo"
    TAGLINE = "Race four tokens home"
    SEAT_COUNTS = (2, 3, 4)
    OPTIONS = (BOT_LEVEL, TURN_TIMER, GOAL)
    BOT_DELAY = 0.8

    @classmethod
    def seat_colors(cls, n: int) -> List[str]:
        if n == 2:
            return ["red", "yellow"]          # opposite corners
        return list(COLOR_ORDER[:n])

    def __init__(self, host, seats, options=None, rng=None) -> None:
        super().__init__(host, seats, options, rng)
        self.n = len(self.seats)
        if self.n not in self.SEAT_COUNTS:
            raise ValueError("Ludo needs 2 to 4 seats, got %d" % self.n)
        colors = [s.color for s in self.seats]
        if any(c not in START_OFFSET for c in colors) or len(set(colors)) != self.n:
            colors = self.seat_colors(self.n)
        self.colors: List[str] = list(colors)
        self.offsets: List[int] = [START_OFFSET[c] for c in self.colors]
        self.goal: int = int(self.options.get("goal", GOAL.default))
        self.tokens: List[List[int]] = [[BASE] * TOKENS for _ in range(self.n)]
        self.turn = 0
        self.phase = "roll"                    # "roll" | "move"
        self.dice: Optional[int] = None        # this turn's pending roll (None in phase roll)
        self.movable: List[int] = []
        self.auto = False                      # server is about to play the only move
        self.sixes = 0                         # consecutive 6s rolled this turn
        self.streak = 0                        # bonus rolls granted so far this turn
        self.winner: Optional[int] = None
        self.last_move: Optional[Dict[str, int]] = None
        self.last_roll: Optional[Dict[str, int]] = None
        # Per-seat bot level override (None = room option). Used by tests and dev tools.
        self.bot_levels: List[Optional[str]] = [None] * self.n
        self._auto_handle: Any = None

    # ------------------------------------------------------------- lifecycle
    def start(self) -> None:
        self.schedule_actor(self.turn, START_DELAY)
        self.push()

    def stop(self) -> None:
        self._auto_handle = None
        super().stop()

    def on_seat_changed(self, seat: int) -> None:
        if self.finished:
            return
        if seat == self.turn and self._auto_handle is None:
            self.schedule_actor(seat)
        self.push()

    # ---------------------------------------------------------------- state
    def home_count(self, seat: int) -> int:
        return sum(1 for p in self.tokens[seat] if p == HOME)

    def home_counts(self) -> List[int]:
        return [self.home_count(s) for s in range(self.n)]

    def score(self, seat: int) -> int:
        """Total progress (base counts 0, home counts 57 per token)."""
        return sum(p + 1 for p in self.tokens[seat])

    def ranking(self) -> List[int]:
        def key(s: int):
            return (0 if s == self.winner else 1, -self.home_count(s), -self.score(s), s)
        return sorted(range(self.n), key=key)

    def view_for(self, seat: Optional[int]) -> Dict[str, Any]:
        return {
            "tokens": [list(row) for row in self.tokens],
            "colors": list(self.colors),
            "turn": None if self.finished else self.turn,
            "dice": self.dice,
            "phase": self.phase,
            "movable": list(self.movable),
            "auto": self.auto,
            "sixes": self.sixes,
            "streak": self.streak,
            "home_count": self.home_counts(),
            "goal": self.goal,
            "timer": self.time_left(),
            "finished": self.finished,
            "winner": self.winner,
            "ranking": self.ranking(),
            "last_move": dict(self.last_move) if self.last_move else None,
            "last_roll": dict(self.last_roll) if self.last_roll else None,
        }

    # -------------------------------------------------------------- actions
    def on_action(self, seat: int, action: Dict[str, Any]) -> None:
        if self.finished:
            raise ActionError("The game is over")
        if not isinstance(seat, int) or not 0 <= seat < self.n:
            raise ActionError("You're watching this game")
        kind = action.get("a") if isinstance(action, dict) else None
        if kind not in ("roll", "move"):
            raise ActionError("Unknown move")
        if seat != self.turn:
            raise ActionError("It's not your turn")
        if kind == "roll":
            if self.phase != "roll":
                raise ActionError("Moving your token" if self.auto else "Pick a token to move")
            self.clear_actor()
            self._roll(seat)
            return
        if self.phase != "move":
            raise ActionError("Roll the dice first")
        token = action.get("token")
        if not isinstance(token, int) or isinstance(token, bool) or token not in self.movable:
            raise ActionError("That token can't move")
        self.clear_actor()
        self._move(seat, token)

    def bot_act(self, seat: int) -> None:
        if self.finished or seat != self.turn:
            return
        if self.phase == "roll":
            self._roll(seat)
        elif self.phase == "move" and self.movable:
            self._move(seat, self.choose_move(seat))

    # ---------------------------------------------------------------- rules
    def _roll_die(self) -> int:
        return self.rng.randint(1, 6)

    def _roll(self, seat: int) -> None:
        value = self._roll_die()
        self.dice = value
        self.last_roll = {"seat": seat, "value": value}
        self.sixes = self.sixes + 1 if value == 6 else 0
        self.emit("roll", seat=seat, value=value)
        if self.sixes >= 3:
            self._skip(seat, "three_sixes", "Three 6s in a row, %s loses the turn" % self.name(seat))
            return
        moves = legal_moves(self.tokens[seat], value)
        if not moves:
            self._skip(seat, "no_moves", "No moves for %s" % self.name(seat))
            return
        self.phase = "move"
        self.movable = moves
        distinct = distinct_moves(self.tokens[seat], value)
        if len(distinct) == 1:
            self.auto = True
            self.clear_actor()
            token = distinct[0]
            self._auto_handle = self.later(ROLL_TIME, lambda: self._auto_fire(seat, token))
        else:
            self.auto = False
            self.schedule_actor(seat, ROLL_TIME)
        self.push()

    def _auto_fire(self, seat: int, token: int) -> None:
        self._auto_handle = None
        if self.turn == seat and self.phase == "move" and token in self.movable:
            self._move(seat, token)

    def _skip(self, seat: int, reason: str, text: str) -> None:
        self.emit("skip", seat=seat, reason=reason, text=text)
        self._next_turn(ROLL_TIME + PASS_TIME)

    def _next_turn(self, delay: float) -> None:
        self.turn = (self.turn + 1) % self.n
        self.phase = "roll"
        self.dice = None
        self.movable = []
        self.auto = False
        self.sixes = 0
        self.streak = 0
        self.schedule_actor(self.turn, delay)
        self.push()

    def _move(self, seat: int, k: int) -> None:
        if self._auto_handle is not None:
            self.cancel(self._auto_handle)
            self._auto_handle = None
        p = self.tokens[seat][k]
        dice = self.dice or 0
        new = next_progress(p, dice)
        if new is None:                       # defensive: never move illegally
            raise ActionError("That token can't move")
        path = move_path(p, dice)
        self.tokens[seat][k] = new
        captured: List[Dict[str, int]] = []
        sq = _abs(self.offsets[seat], new)
        if sq is not None and sq not in SAFE_SQUARES:
            for o in range(self.n):
                if o == seat:
                    continue
                row = self.tokens[o]
                off = self.offsets[o]
                for j in range(TOKENS):
                    q = row[j]
                    if 0 <= q <= LAST_TRACK and (q + off) % TRACK_LEN == sq:
                        row[j] = BASE
                        captured.append({"seat": o, "token": j, "from": q})
        self.last_move = {"seat": seat, "token": k, "from": p, "to": new}
        self.movable = []
        self.auto = False
        move = {"seat": seat, "token": k, "path": path, "captured": captured, "from": p, "to": new}
        self.emit("move", **move)
        reached = new == HOME
        if reached:
            self.emit("home", seat=seat, token=k)
        delay = MOVE_PAUSE + STEP_TIME * len(path) + (CAPTURE_TIME if captured else 0.0)
        if reached and self.home_count(seat) >= self.goal:
            self.winner = seat
            self.phase = "roll"
            self.dice = None
            self.finish([seat], "%s wins!" % self.name(seat), ranking=self.ranking(), goal=self.goal)
            return
        if dice == 6 or captured or reached:
            self.streak += 1
            self.phase = "roll"
            self.dice = None
            self.schedule_actor(seat, delay)
            self.push()
        else:
            self._next_turn(delay)

    # ------------------------------------------------------------------ bots
    def level_for(self, seat: int) -> str:
        override = self.bot_levels[seat] if seat < len(self.bot_levels) else None
        if override:
            return override
        if self.seats[seat].is_bot:
            return str(self.options.get("bot_level", "normal"))
        return "normal"                      # idle human: play sensibly for them

    def choose_move(self, seat: int) -> int:
        """Pick a token from self.movable for `seat` (always legal)."""
        dice = self.dice or 0
        options = distinct_moves(self.tokens[seat], dice)
        options = [k for k in options if k in self.movable] or list(self.movable)
        if len(options) == 1:
            return options[0]
        level = self.level_for(seat)
        if level == "easy" and self.rng.random() < 0.5:
            return self.rng.choice(options)
        if level == "hard":
            return max(options, key=lambda k: self._hard_score(seat, k, dice))
        return max(options, key=lambda k: self._normal_key(seat, k, dice))

    def _captures_at(self, seat: int, sq: Optional[int]) -> List[int]:
        """Progress values of opponent tokens a token of `seat` would capture on square sq."""
        if sq is None or sq in SAFE_SQUARES:
            return []
        out = []
        for o in range(self.n):
            if o == seat:
                continue
            off = self.offsets[o]
            for q in self.tokens[o]:
                if 0 <= q <= LAST_TRACK and (q + off) % TRACK_LEN == sq:
                    out.append(q)
        return out

    def _threat(self, seat: int, sq: Optional[int], near_only: bool = False) -> float:
        """Rough chance that an opponent lands on absolute square sq before `seat` moves again."""
        if sq is None or sq in SAFE_SQUARES:
            return 0.0
        risk = 0.0
        for o in range(self.n):
            if o == seat:
                continue
            off = self.offsets[o]
            in_base = False
            for q in self.tokens[o]:
                if q == BASE:
                    in_base = True
                    continue
                if q > LAST_TRACK:
                    continue
                d = (sq - q - off) % TRACK_LEN
                if d == 0 or q + d > LAST_TRACK:
                    continue
                if d <= 6:
                    risk += 1.0 / 6.0
                elif d <= 12 and not near_only:
                    risk += 1.0 / 36.0
            if in_base and not near_only and 1 <= (sq - off) % TRACK_LEN <= 6:
                risk += 1.0 / 36.0
        return min(risk, 1.0)

    def _normal_key(self, seat: int, k: int, dice: int):
        """Spec priority: capture > leave base > reach home > reach safety > escape danger > furthest."""
        p = self.tokens[seat][k]
        new = next_progress(p, dice)
        if new is None:
            return (False, False, False, False, False, -99)
        off = self.offsets[seat]
        old_sq, new_sq = _abs(off, p), _abs(off, new)
        old_safe = old_sq is None or old_sq in SAFE_SQUARES
        new_safe = new_sq is None or new_sq in SAFE_SQUARES
        captures = bool(self._captures_at(seat, new_sq))
        before = 0.0 if old_safe else self._threat(seat, old_sq, near_only=True)
        after = 0.0 if new_safe else self._threat(seat, new_sq, near_only=True)
        return (captures, p == BASE, new == HOME, new_safe and not old_safe,
                before > 0 and after == 0, p)

    def _hard_score(self, seat: int, k: int, dice: int) -> float:
        """Expected-value style score in 'squares of progress'; also dodges danger."""
        p = self.tokens[seat][k]
        new = next_progress(p, dice)
        if new is None:
            return -1e9
        off = self.offsets[seat]
        old_sq, new_sq = _abs(off, p), _abs(off, new)
        score = 0.0
        caps = self._captures_at(seat, new_sq)
        if caps:
            score += 6.0 + sum(q + 12.0 for q in caps)        # their loss + our bonus roll
        if p == BASE:
            score += 14.0
        if new == HOME:
            score += 12.0
        elif new > LAST_TRACK >= p >= 0:
            score += 6.0                                      # safe forever in the home column
        if old_sq is not None:
            score += self._threat(seat, old_sq) * (p + 10.0)  # escaping a threat
        if new_sq is not None:
            score -= self._threat(seat, new_sq) * (new + 10.0)  # landing in front of opponents
            # Hunting: opponents 1..6 squares ahead become targets next turn.
            for o in range(self.n):
                if o == seat:
                    continue
                o_off = self.offsets[o]
                for q in self.tokens[o]:
                    if 0 <= q <= LAST_TRACK:
                        a = (q + o_off) % TRACK_LEN
                        if a in SAFE_SQUARES:
                            continue
                        d = (a - new_sq) % TRACK_LEN
                        if 1 <= d <= 6 and new + d <= LAST_TRACK:
                            score += (q + 12.0) / 18.0
        score += p * 0.01                                     # tie-break: furthest along
        return score
