"""Deterministic test harness for game logic (no sockets, no Tk, virtual clock).

    host, logic = make_game(Logic, n_seats=4, humans=0, seed=7)
    logic.start()
    result = host.run_until_over()     # bots play the whole game instantly
    assert result is not None

Everything that would cross the network is JSON round-tripped, exactly like the
real wire, so int dict keys become strings here too.
"""
from __future__ import annotations

import heapq
import json
import random
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple, Type

from .base import GameLogic, Seat


def wire(obj: Any) -> Any:
    """Round-trip through JSON (raises if something is not JSON-serialisable)."""
    return json.loads(json.dumps(obj, allow_nan=False))


class FakeHost:
    def __init__(self) -> None:
        self.t = 0.0
        self._heap: List[Tuple[float, int, Callable[[], None]]] = []
        self._seq = 0
        self._cancelled: set = set()
        self.logic: Optional[GameLogic] = None
        self.events: List[Tuple[Optional[int], Dict[str, Any]]] = []
        self.views: Dict[Optional[int], Dict[str, Any]] = {}
        self.result: Optional[Dict[str, Any]] = None
        self.notices: List[str] = []
        self.pushes = 0
        self.on_push: Optional[Callable[["FakeHost"], None]] = None  # hook for invariant checks

    def attach(self, logic: GameLogic) -> GameLogic:
        self.logic = logic
        return logic

    # ---------------------------------------------------------------- GameHost
    def now(self) -> float:
        return self.t

    def push_state(self) -> None:
        assert self.logic is not None
        self.pushes += 1
        for seat in self.logic.seats:
            self.views[seat.index] = wire(self.logic.view_for(seat.index))
        self.views[None] = wire(self.logic.view_for(None))
        if self.on_push:
            self.on_push(self)

    def send_event(self, event: Dict[str, Any], to_seat: Optional[int] = None) -> None:
        assert "kind" in event, "events need a 'kind'"
        self.events.append((to_seat, wire(event)))

    def call_later(self, delay: float, fn: Callable[[], None]) -> int:
        self._seq += 1
        heapq.heappush(self._heap, (self.t + max(0.0, float(delay)), self._seq, fn))
        return self._seq

    def cancel(self, handle: Any) -> None:
        self._cancelled.add(handle)

    def game_over(self, result: Dict[str, Any]) -> None:
        assert self.result is None, "game_over called twice"
        self.result = wire(result)

    def notice(self, text: str) -> None:
        self.notices.append(text)

    # ----------------------------------------------------------------- driving
    def step(self) -> bool:
        """Run the next pending timer. Returns False when nothing is scheduled."""
        while self._heap:
            when, seq, fn = heapq.heappop(self._heap)
            if seq in self._cancelled:
                self._cancelled.discard(seq)
                continue
            self.t = max(self.t, when)
            fn()
            return True
        return False

    def _drop_cancelled(self) -> None:
        while self._heap and self._heap[0][1] in self._cancelled:
            _when, seq, _fn = heapq.heappop(self._heap)
            self._cancelled.discard(seq)

    def advance(self, seconds: float) -> None:
        """Run every live timer due within `seconds`, then move the clock to the target."""
        target = self.t + seconds
        while True:
            self._drop_cancelled()
            if not self._heap or self._heap[0][0] > target:
                break
            self.step()
        self.t = max(self.t, target)

    def run_until_over(self, max_seconds: float = 200000.0, max_steps: int = 500000) -> Optional[Dict[str, Any]]:
        steps = 0
        while self.result is None and steps < max_steps and self.t <= max_seconds:
            if not self.step():
                break
            steps += 1
        return self.result

    def act(self, seat: int, **action: Any) -> None:
        assert self.logic is not None
        self.logic.on_action(seat, wire(action))

    def events_of(self, kind: str) -> List[Dict[str, Any]]:
        return [e for _to, e in self.events if e.get("kind") == kind]


def make_game(logic_cls: Type[GameLogic], n_seats: int, humans: int = 0,
              options: Optional[Dict[str, Any]] = None, seed: int = 1,
              names: Optional[Sequence[str]] = None) -> Tuple[FakeHost, GameLogic]:
    """Seats 0..humans-1 are humans, the rest bots."""
    host = FakeHost()
    colors = logic_cls.seat_colors(n_seats)
    names = list(names) if names else ["P%d" % i for i in range(n_seats)]
    seats = [Seat(i, names[i], is_bot=(i >= humans), color=colors[i]) for i in range(n_seats)]
    logic = logic_cls(host, seats, options or {}, random.Random(seed))
    host.attach(logic)
    return host, logic
