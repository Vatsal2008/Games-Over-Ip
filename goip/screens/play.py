"""Play screen: slim header + the game's View on its own canvas + result overlay."""
from __future__ import annotations

import logging
import tkinter as tk
from typing import Any, Dict, Optional, Tuple

from .. import widgets
from ..games import logic_class, view_class
from ..games.viewbase import ViewContext
from .base import Overlay, Screen

log = logging.getLogger(__name__)

HEADER = 46


class PlayScreen(Screen):
    def build(self) -> None:
        ui, c, app = self.ui, self.canvas, self.app
        begin = app.begin or {}
        self.game = begin.get("game", "")
        self.my_seat: Optional[int] = begin.get("seat")
        try:
            self.game_name = logic_class(self.game).NAME
        except KeyError:
            self.game_name = self.game
        self.b_leave = self.add(widgets.Button(ui, c, "Leave", self.leave, kind="ghost", icon="‹", font_px=13))
        self.vcanvas = tk.Canvas(c, highlightthickness=0, bd=0, bg=self.th.bg_bottom, takefocus=0)
        self.vtoaster = widgets.Toaster(ui, self.vcanvas, position="top")
        self.overlay: Optional[Overlay] = None
        self.result: Optional[Dict[str, Any]] = None
        self._vsize: Tuple[int, int] = (0, 0)
        self._view_failed = False
        self.view: Any = None
        ctx = ViewContext(ui=ui, my_seat=self.my_seat, seats=list(begin.get("seats") or []),
                          options=dict(begin.get("options") or {}), is_host=app.is_host,
                          send_action=self._send_action, toast=lambda text: self.toast(text))
        try:
            self.view = view_class(self.game)(self.vcanvas, ctx)
        except Exception:
            log.exception("could not create the %s view", self.game)
            self._view_failed = True

    def _send_action(self, action: Dict[str, Any]) -> None:
        if self.app.client is not None:
            self.app.client.act(action)

    def destroy(self) -> None:
        if self.overlay is not None:
            self.overlay.destroy()
            self.overlay = None
        if self.view is not None:
            try:
                self.view.destroy()
            except Exception:
                log.exception("view destroy failed")
        try:
            self.vtoaster.destroy()
        except Exception:
            pass
        super().destroy()

    def toast(self, text: str, kind: str = "info", seconds: float = 2.6) -> None:
        if not self._dead and text:
            self.vtoaster.show(text, kind=kind, seconds=seconds)

    # ---------------------------------------------------------------- layout
    def layout(self, w: int, h: int) -> None:
        ui, th, room = self.ui, self.th, self.app.room or {}
        hh = ui.px(HEADER)
        c = self.canvas
        c.delete("hdrbar")
        c.create_rectangle(0, 0, w, hh, fill=th.panel, outline="", tags=("hdrbar",), state="disabled")
        c.create_line(0, hh - 1, w, hh - 1, fill=th.edge, tags=("hdrbar",), state="disabled")
        c.tag_raise("hdrbar", "bg")
        self.b_leave.place(ui.px(4), (hh - ui.px(34)) // 2, ui.px(82), ui.px(34))
        right = w - ui.px(12)
        self.text("g_name", right, hh * 0.36, self.game_name, 14, bold=True, anchor="e", fit=w - ui.px(110))
        self.text("g_addr", right, hh * 0.74, room.get("address", ""), 10, fill=th.text_faint, anchor="e",
                  fit=w - ui.px(110))
        self.b_leave.raise_()
        vw, vh = w, max(1, h - hh)
        self.vcanvas.place(x=0, y=hh, width=vw, height=vh)
        if (vw, vh) != self._vsize:
            self._vsize = (vw, vh)
            self.vcanvas.configure(width=vw, height=vh)
            widgets.draw_background(self.vcanvas, ui, vw, vh)
            if self._view_failed:
                self.vcanvas.delete("failmsg")
                self.vcanvas.create_text(vw / 2, vh / 2, text="This game couldn't load.\nLeave and try another one.",
                                         font=ui.font(14, bold=True), fill=th.text_dim, justify="center",
                                         tags=("failmsg",))
            elif self.view is not None:
                self._safe_view("on_resize", vw, vh)
            if self.overlay is not None:
                self.overlay.place()
        self.vtoaster.raise_()

    # -------------------------------------------------------------- messages
    def _safe_view(self, method: str, *args: Any) -> None:
        if self.view is None:
            return
        try:
            getattr(self.view, method)(*args)
        except Exception:
            log.exception("view.%s failed", method)
        if self.overlay is not None:
            self.overlay.raise_()

    def on_message(self, msg: Dict[str, Any]) -> None:
        t = msg.get("t")
        if t == "state":
            self._safe_view("on_state", msg.get("s") or {})
        elif t == "event":
            self._safe_view("on_event", msg.get("e") or {})
        elif t == "seats":
            self._safe_view("on_seats", msg.get("seats") or [])
        elif t == "over":
            self.show_result(msg.get("result") or {})

    def on_key(self, keysym: str) -> None:
        if self.overlay is None:
            self._safe_view("on_key", keysym)

    # ---------------------------------------------------------------- result
    def show_result(self, result: Dict[str, Any]) -> None:
        self.result = result
        winners = [w for w in result.get("winners") or [] if isinstance(w, int)]
        text = str(result.get("text") or "Game over")
        won = self.my_seat is not None and self.my_seat in winners
        if won and len(winners) == 1:
            title, body = "You win!", text
        elif won:
            title, body = "Shared win!", text
        elif winners:
            title, body = text, "Good game. Rematch?"
        else:
            title, body = text, "Nobody wins this one."
        if self.app.is_host:
            buttons = [("Rematch", "primary", self.rematch), ("Back to lobby", "secondary", self.app.go_lobby)]
        else:
            body += "\nThe host can start a rematch."
            buttons = [("Back to lobby", "secondary", self.app.go_lobby)]
        self._show_overlay(title, body, buttons, celebrate=won)

    def rematch(self) -> None:
        if self.app.client is not None:
            self.app.client.send({"t": "start"})

    def leave(self) -> None:
        if self.result is not None:
            self.app.go_lobby()
            return
        if self.app.is_host:
            self._show_overlay("Close the room?", "The game ends for everyone.",
                               [("Close room", "danger", self.app.leave), ("Keep playing", "secondary",
                                                                           self._close_overlay)])
        else:
            self._show_overlay("Leave this game?", "A bot takes your seat. Rejoin with the same name to get it back.",
                               [("Leave", "danger", self.app.leave), ("Keep playing", "secondary",
                                                                      self._close_overlay)])

    def _show_overlay(self, title: str, body: str, buttons: Any, celebrate: bool = False) -> None:
        self._close_overlay()
        self.overlay = Overlay(self.ui, self.vcanvas, title, body, buttons, celebrate=celebrate,
                               allow_above=(self.vtoaster.tag,))
        self.vtoaster.raise_()

    def _close_overlay(self) -> None:
        if self.overlay is not None:
            self.overlay.destroy()
            self.overlay = None
