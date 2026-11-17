"""Lobby: share the address, see who's in, host starts or changes the game."""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .. import widgets
from ..games import logic_class
from .base import Overlay, Screen


def options_summary(game: str, options: Dict[str, Any]) -> str:
    try:
        logic = logic_class(game)
    except KeyError:
        return ""
    parts = []
    for opt in logic.OPTIONS:
        value = (options or {}).get(opt.key, opt.default)
        label = dict((v, l) for v, l in opt.choices).get(value, str(value))
        parts.append("%s: %s" % (opt.label, label))
    return "  ·  ".join(parts)


class LobbyScreen(Screen):
    def build(self) -> None:
        ui, c, app = self.ui, self.canvas, self.app
        self.header("Lobby", "", back=self.leave, back_text="Leave")
        self.share = self.add(widgets.Panel(ui, c, radius=16, kind="glass"))
        self.chips: List[widgets.PlayerChip] = []
        self.kicks: Dict[int, widgets.Button] = {}
        self.b_start = self.add(widgets.Button(ui, c, "Start", self.start, kind="primary", icon="▶"))
        self.b_change = self.add(widgets.Button(ui, c, "Change game", app.go_change_game, kind="secondary",
                                                font_px=14))
        self.overlay: Optional[Overlay] = None

    def destroy(self) -> None:
        if self.overlay is not None:
            self.overlay.destroy()
        super().destroy()

    # --------------------------------------------------------------- actions
    def start(self) -> None:
        if self.app.client is not None:
            self.app.client.send({"t": "start"})

    def kick(self, client_id: int) -> None:
        if self.app.client is not None:
            self.app.client.send({"t": "kick", "client": client_id})

    def leave(self) -> None:
        if self.overlay is not None:
            return
        if self.app.is_host:
            self.overlay = Overlay(self.ui, self.canvas, "Close the room?",
                                   "Everyone in the lobby goes back to the menu.",
                                   [("Close room", "danger", self.app.leave),
                                    ("Stay", "secondary", self._close_overlay)])
        else:
            self.app.leave()

    def _close_overlay(self) -> None:
        if self.overlay is not None:
            self.overlay.destroy()
            self.overlay = None

    def on_room(self, room: Dict[str, Any]) -> None:
        self.relayout()

    # ---------------------------------------------------------------- layout
    def layout(self, w: int, h: int) -> None:
        ui, th, app = self.ui, self.th, self.app
        room = app.room or {}
        seats = room.get("seats") or []
        host_name = room.get("host_name") or "the host"
        self._hdr = (room.get("game_name") or "Lobby",
                     "You're hosting" if app.is_host else "Room by %s" % host_name)
        y = self.layout_header(w)
        x0, cw = self.column(w)

        share_h = ui.px(86)
        self.share.place(x0, y, cw, share_h)
        self.text("sh_1", x0 + cw / 2, y + ui.px(12), "Friends join with", 12, fill=th.text_dim, anchor="n")
        self.text("sh_2", x0 + cw / 2, y + ui.px(30), room.get("address", ""), 24, bold=True, display=True,
                  fill=th.accent2, anchor="n", fit=cw - ui.px(20))
        self.text("sh_3", x0 + cw / 2, y + share_h - ui.px(10), "Join a game → type this address", 11,
                  fill=th.text_faint, anchor="s", fit=cw - ui.px(20))
        y += share_h + ui.px(16)

        humans = [s for s in seats if s.get("kind") == "human"]
        slots = [s for s in seats if s.get("kind") != "bot"]
        self.label("l_players", x0, y, "Players  %d/%d joined" % (len(humans), len(slots)), cw)
        y += ui.px(22)

        while len(self.chips) < len(seats):
            self.chips.append(self.add(widgets.PlayerChip(ui, self.canvas)))
        while len(self.chips) > len(seats):
            self.remove(self.chips.pop())
        bottom_reserved = ui.px(50) + ui.px(16) + (ui.px(44) + ui.px(10) if app.is_host else 0) + ui.px(28)
        avail = h - y - bottom_reserved
        chip_h = max(ui.px(40), min(ui.px(54), (avail - ui.px(8) * max(0, len(seats) - 1)) // max(1, len(seats))))
        wanted_kicks = set()
        for i, seat in enumerate(seats):
            kind = seat.get("kind")
            me = seat.get("client") is not None and seat.get("client") == app.my_id
            kickable = app.is_host and kind == "human" and not seat.get("host") and seat.get("client") is not None
            kw = ui.px(44) if kickable else 0
            chip = self.chips[i]
            chip.place(x0, y, cw - kw - (ui.px(6) if kw else 0), chip_h)
            if kind == "human":
                chip.update(seat.get("name") or "Player", seat.get("color", "red"), bot=False,
                            subtitle="Host" if seat.get("host") else "Ready", me=me)
            elif kind == "bot":
                chip.update(seat.get("name") or "Bot", seat.get("color", "red"), bot=True, subtitle="Plays by itself")
            else:
                chip.update("Waiting…", seat.get("color", "red"), subtitle="Open seat")
            if kickable:
                wanted_kicks.add(i)
                btn = self.kicks.get(i)
                if btn is None:
                    btn = self.add(widgets.Button(ui, self.canvas, "", None, kind="ghost", icon="✕", font_px=14))
                    self.kicks[i] = btn
                cid = seat.get("client")
                btn.command = (lambda c=cid: self.kick(c))
                btn.place(x0 + cw - kw, y + (chip_h - ui.px(40)) / 2, kw, ui.px(40))
            y += chip_h + ui.px(8)
        for i in [k for k in self.kicks if k not in wanted_kicks]:
            self.remove(self.kicks.pop(i))

        summary = options_summary(room.get("game", ""), room.get("options") or {})
        self.text("opts", x0, y + ui.px(4), summary, 11, fill=th.text_faint, anchor="nw", width=cw, justify="left")

        if app.is_host:
            open_seats = sum(1 for s in seats if s.get("kind") == "open")
            label = "Start" if not open_seats else "Start (%d bot%s fill%s in)" % (
                open_seats, "" if open_seats == 1 else "s", "s" if open_seats == 1 else "")
            self.b_start.set_text(label)
            self.b_start.set_visible(True)
            self.b_change.set_visible(True)
            self.b_start.place(x0, h - ui.px(16) - ui.px(50), cw, ui.px(50))
            self.b_change.place(x0, h - ui.px(16) - ui.px(50) - ui.px(10) - ui.px(44), cw, ui.px(44))
            self.hide("wait")
        else:
            self.b_start.set_visible(False)
            self.b_change.set_visible(False)
            self.text("wait", w / 2, h - ui.px(30), "Waiting for the host to start…", 14, bold=True,
                      fill=th.text_dim, anchor="center", fit=cw)
        if self.overlay is not None:
            self.overlay.place()
