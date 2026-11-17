"""Server-side game contract.

Game rules run ONLY on the host, on RoomServer's thread (or inside FakeHost in
tests). Nothing in a game's logic.py may import tkinter or touch sockets.

Lifecycle (driven by the room):
    logic = Logic(host, seats, options, rng)
    logic.start()                    # set up, emit events, push(), schedule first actor
    logic.on_action(seat, action)    # a player's intent; raise ActionError if invalid
    logic.on_seat_changed(seat)      # seat.is_bot flipped (player left / came back)
    logic.stop()                     # room closing; cancel timers

Rules of thumb for implementers:
    * State lives in plain Python objects; view_for(seat) turns it into JSON-safe
      dicts/lists. Use lists indexed by seat, never dicts with int keys (JSON turns
      int keys into strings).
    * Order of output for one change: emit(...) animation events first, then push().
    * Every time some seat must decide something next, call schedule_actor(seat, delay).
      Bots then act after `delay`; humans get the idle timer (option "turn_timer").
      Either way bot_act(seat) is called to make exactly ONE step for that seat.
    * End the game with finish(winners, text). It pushes the final state for you.
"""
from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

SEAT_COLORS: Tuple[str, ...] = ("red", "blue", "green", "yellow")


@dataclass
class Seat:
    index: int
    name: str
    is_bot: bool
    color: str  # one of SEAT_COLORS

    def public(self) -> Dict[str, Any]:
        return {"seat": self.index, "name": self.name, "bot": self.is_bot, "color": self.color}


@dataclass(frozen=True)
class Option:
    """A host-configurable setting shown as a segmented control on the host screen."""

    key: str
    label: str
    choices: Tuple[Tuple[Any, str], ...]  # (value, label)
    default: Any

    def coerce(self, value: Any) -> Any:
        for v, _label in self.choices:
            if v == value and type(v) is type(value):
                return v
        return self.default


# Options most games share.
TURN_TIMER = Option(
    "turn_timer", "Auto-move when idle",
    ((0, "Off"), (15, "15s"), (30, "30s"), (60, "60s")), 30,
)
BOT_LEVEL = Option(
    "bot_level", "Bot level",
    (("easy", "Easy"), ("normal", "Normal"), ("hard", "Hard")), "normal",
)


class ActionError(Exception):
    """Raise from on_action for an invalid move. The message is shown to that player."""


class GameHost:
    """What the room provides to a game (RoomServer and testing.FakeHost implement this)."""

    def now(self) -> float: ...                                   # monotonic seconds
    def push_state(self) -> None: ...                             # send view_for(seat) to every client
    def send_event(self, event: Dict[str, Any], to_seat: Optional[int] = None) -> None: ...
    def call_later(self, delay: float, fn: Callable[[], None]) -> Any: ...   # returns a handle
    def cancel(self, handle: Any) -> None: ...
    def game_over(self, result: Dict[str, Any]) -> None: ...
    def notice(self, text: str) -> None: ...


class GameLogic:
    """Base class for every game's ``logic.Logic``."""

    ID = ""                                  # registry key, e.g. "ludo"
    NAME = ""                                # display name, e.g. "Ludo"
    TAGLINE = ""                             # <= 28 chars, shown on the game card
    SEAT_COUNTS: Tuple[int, ...] = (2,)      # allowed total seats (people + bots)
    OPTIONS: Tuple[Option, ...] = ()
    BOT_DELAY = 0.9                          # seconds a bot "thinks" before acting

    # ----------------------------------------------------------------- class API
    @classmethod
    def seat_colors(cls, n: int) -> List[str]:
        return list(SEAT_COLORS[:n])

    @classmethod
    def clean_options(cls, options: Optional[Dict[str, Any]]) -> Dict[str, Any]:
        options = options or {}
        return {o.key: o.coerce(options.get(o.key, o.default)) for o in cls.OPTIONS}

    # -------------------------------------------------------------- construction
    def __init__(self, host: GameHost, seats: Sequence[Seat], options: Optional[Dict[str, Any]] = None,
                 rng: Optional[random.Random] = None) -> None:
        self.host = host
        self.seats: List[Seat] = list(seats)
        self.options: Dict[str, Any] = self.clean_options(options)
        self.rng = rng or random.Random()
        self.finished = False
        self._handles: set = set()
        self._actor: Optional[Tuple[int, int]] = None   # (seat, token)
        self._actor_handle: Any = None
        self._actor_token = 0
        self.actor_deadline: Optional[float] = None     # host.now() when the idle timer fires

    # ------------------------------------------------------- must be implemented
    def start(self) -> None:
        raise NotImplementedError

    def view_for(self, seat: Optional[int]) -> Dict[str, Any]:
        """JSON-safe state as seen by `seat` (None = spectator). Hide private info."""
        raise NotImplementedError

    def on_action(self, seat: int, action: Dict[str, Any]) -> None:
        raise NotImplementedError

    def bot_act(self, seat: int) -> None:
        """Make exactly one valid step for `seat` (a bot, or an idle human)."""
        raise NotImplementedError

    # ----------------------------------------------------------------- optional
    def on_seat_changed(self, seat: int) -> None:
        """seats[seat].is_bot changed. Default: re-arm the actor timer if it's their move."""
        if not self.finished and self._actor is not None and self._actor[0] == seat:
            self.schedule_actor(seat)

    def stop(self) -> None:
        self.finished = True
        self.clear_actor()
        for handle in list(self._handles):
            self.host.cancel(handle)
        self._handles.clear()

    # ------------------------------------------------------------------ helpers
    def emit(self, kind: str, to_seat: Optional[int] = None, **data: Any) -> None:
        event = {"kind": kind}
        event.update(data)
        self.host.send_event(event, to_seat)

    def push(self) -> None:
        self.host.push_state()

    def notice(self, text: str) -> None:
        self.host.notice(text)

    def name(self, seat: int) -> str:
        return self.seats[seat].name

    def is_bot(self, seat: int) -> bool:
        return self.seats[seat].is_bot

    def later(self, delay: float, fn: Callable[[], None]) -> Any:
        """Run fn after `delay` seconds on the game thread (skipped once finished)."""
        box: Dict[str, Any] = {}

        def run() -> None:
            self._handles.discard(box.get("h"))
            if not self.finished:
                fn()

        handle = self.host.call_later(max(0.0, delay), run)
        box["h"] = handle
        self._handles.add(handle)
        return handle

    def cancel(self, handle: Any) -> None:
        if handle is not None:
            self.host.cancel(handle)
            self._handles.discard(handle)

    def schedule_actor(self, seat: int, delay: Optional[float] = None) -> None:
        """`seat` must make the next decision.

        Bots: bot_act(seat) runs after `delay` (default BOT_DELAY).
        Humans: if option turn_timer > 0, bot_act(seat) runs after turn_timer (+delay)
        unless the human acts first. Call clear_actor() (or schedule the next actor)
        as soon as the human's valid action arrives.
        """
        self.clear_actor()
        self._actor_token += 1
        token = self._actor_token
        self._actor = (seat, token)

        def fire() -> None:
            if self.finished or self._actor != (seat, token):
                return
            self._actor = None
            self._actor_handle = None
            self.actor_deadline = None
            self.bot_act(seat)

        if self.seats[seat].is_bot:
            self.actor_deadline = None
            self._actor_handle = self.later(self.BOT_DELAY if delay is None else delay, fire)
        else:
            idle = self.options.get("turn_timer", 0) or 0
            if idle:
                wait = idle + (delay or 0.0)
                self.actor_deadline = self.host.now() + wait
                self._actor_handle = self.later(wait, fire)
            else:
                self.actor_deadline = None

    def clear_actor(self) -> None:
        if self._actor_handle is not None:
            self.cancel(self._actor_handle)
        self._actor_handle = None
        self._actor = None
        self.actor_deadline = None

    @property
    def actor_seat(self) -> Optional[int]:
        return None if self._actor is None else self._actor[0]

    def time_left(self) -> Optional[float]:
        """Seconds until the idle timer fires (for the view's countdown), or None."""
        if self.actor_deadline is None:
            return None
        return max(0.0, round(self.actor_deadline - self.host.now(), 1))

    def finish(self, winners: Iterable[int], text: str, **extra: Any) -> None:
        """End the game: final state is pushed, then the room is told (once)."""
        if self.finished:
            return
        self.stop()
        self.push()
        result: Dict[str, Any] = {"winners": list(winners), "text": text}
        result.update(extra)
        self.host.game_over(result)
