"""Client-side game view contract.

The play screen creates one tk.Canvas below its header and hands it to the game's
``View(canvas, ctx)``. The view owns everything drawn on that canvas.

Data flow:   server --event--> on_event()   (animation hint, arrives first)
             server --state--> on_state()   (authoritative truth for your seat)
             server --seats--> on_seats()   (a player became a bot / came back)
             user input        ctx.act(a="move", token=2)   -> server validates

Rules for implementers:
    * State is truth, events are only hints. If a new state arrives while an
      animation runs, finish/skip the animation quickly and show the new state.
      The view must never get stuck or desync because an event was missed.
    * Layout is computed from the canvas size in on_resize(w, h); it is called once
      right after construction and on every resize. Never hardcode window sizes.
    * Use only ctx.ui.theme colours and ctx.ui.font() fonts; respect theme.glow.
    * Use self.after(ms, fn) instead of canvas.after so destroy() can cancel it,
      and pass owner=self to ui.anim.tween(...).
"""
from __future__ import annotations

import tkinter as tk
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional


@dataclass
class ViewContext:
    ui: Any                                   # widgets.UI (theme, sprites, anim, font, px, scale)
    my_seat: Optional[int]                    # your seat index, None = spectator
    seats: List[Dict[str, Any]]               # public seats: {"seat", "name", "bot", "color"}
    options: Dict[str, Any]
    is_host: bool
    send_action: Callable[[Dict[str, Any]], None]
    toast: Callable[[str], None]

    def act(self, **action: Any) -> None:
        self.send_action(action)

    def seat_name(self, seat: int) -> str:
        if 0 <= seat < len(self.seats):
            return str(self.seats[seat].get("name", "?"))
        return "?"

    def seat_color(self, seat: int) -> str:
        if 0 <= seat < len(self.seats):
            return str(self.seats[seat].get("color", "red"))
        return "red"

    def is_me(self, seat: Optional[int]) -> bool:
        return seat is not None and seat == self.my_seat


class GameView:
    def __init__(self, canvas: tk.Canvas, ctx: ViewContext) -> None:
        self.canvas = canvas
        self.ctx = ctx
        self.ui = ctx.ui
        self.state: Optional[Dict[str, Any]] = None
        self._afters: set = set()
        self._dead = False

    # ------------------------------------------------------------ to override
    def on_state(self, state: Dict[str, Any]) -> None:
        self.state = state

    def on_event(self, event: Dict[str, Any]) -> None:
        pass

    def on_seats(self, seats: List[Dict[str, Any]]) -> None:
        self.ctx.seats = seats

    def on_resize(self, w: int, h: int) -> None:
        pass

    def on_key(self, keysym: str) -> None:
        """Optional keyboard support (e.g. space = roll). keysym as tkinter reports it."""

    # ---------------------------------------------------------------- helpers
    def after(self, ms: int, fn: Callable[[], None]) -> Optional[str]:
        if self._dead:
            return None
        holder: Dict[str, str] = {}

        def run() -> None:
            self._afters.discard(holder.get("id"))
            if not self._dead:
                fn()

        ident = self.canvas.after(max(0, int(ms)), run)
        holder["id"] = ident
        self._afters.add(ident)
        return ident

    def cancel_after(self, ident: Optional[str]) -> None:
        if ident:
            try:
                self.canvas.after_cancel(ident)
            except tk.TclError:
                pass
            self._afters.discard(ident)

    def destroy(self) -> None:
        self._dead = True
        for ident in list(self._afters):
            try:
                self.canvas.after_cancel(ident)
            except tk.TclError:
                pass
        self._afters.clear()
        try:
            self.ui.anim.cancel_owner(self)
        except Exception:
            pass
        try:
            self.canvas.delete("all")
        except tk.TclError:
            pass
