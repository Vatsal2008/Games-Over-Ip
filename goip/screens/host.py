"""Hosting: step 1 (name, IPv4, port) and step 2 (game, players, options)."""
from __future__ import annotations

import logging
import queue
import threading
from typing import Any, Dict, List, Optional, Tuple

from .. import widgets
from ..client import Client
from ..games import GAME_IDS, logic_class
from ..netutil import best_ipv4, friendly_error, is_valid_ipv4, local_ipv4s
from ..protocol import clean_name
from ..server import RoomServer
from .base import Screen, game_icon, tile_photo

log = logging.getLogger(__name__)


def parse_port(text: str) -> Optional[int]:
    text = (text or "").strip()
    if not text.isdigit():
        return None
    port = int(text)
    return port if 1024 <= port <= 65535 else None


# ======================================================================= step 1
class HostNetScreen(Screen):
    def build(self) -> None:
        ui, c, app, s = self.ui, self.canvas, self.app, self.app.settings
        self.header("Host a game", "Step 1 of 2: your name and address", back=app.go_menu)
        self.f_name = self.add(widgets.TextField(ui, c, s["name"], "Your name", on_enter=lambda v: self.f_ip.focus(),
                                                 max_len=16))
        ip = s["host_ip"] if is_valid_ipv4(s["host_ip"]) else best_ipv4()
        self.f_ip = self.add(widgets.TextField(ui, c, ip, "192.168.1.5", on_enter=lambda v: self.next(), max_len=15))
        self.f_port = self.add(widgets.TextField(ui, c, str(s["port"]), "50505", max_len=5))
        self.dd = self.add(widgets.Dropdown(ui, c, [("", "Finding your addresses…")], "", self._picked))
        self.b_next = self.add(widgets.Button(ui, c, "Next", self.next, kind="primary"))
        self._found: "queue.Queue[List[Tuple[str, str]]]" = queue.Queue()
        threading.Thread(target=lambda: self._found.put(self._detect()), daemon=True).start()
        self.after(60, self._poll_found)

    @staticmethod
    def _detect() -> List[Tuple[str, str]]:
        try:
            return local_ipv4s()
        except Exception:
            log.exception("local_ipv4s failed")
            return []

    def _poll_found(self) -> None:
        try:
            found = self._found.get_nowait()
        except queue.Empty:
            self.after(80, self._poll_found)
            return
        if not found:
            self.dd.set_choices([("", "No network found - type your IPv4")], "")
            return
        choices = [(ip, "%s  (%s)" % (ip, label)) for ip, label in found]
        current = self.f_ip.value.strip()
        value = current if any(ip == current for ip, _l in found) else ""
        if not value:
            choices = [("", "Detected: %d address%s" % (len(found), "" if len(found) == 1 else "es"))] + choices
        self.dd.set_choices(choices, value)
        if not is_valid_ipv4(current):
            self.f_ip.value = found[0][0]

    def _picked(self, value: Any) -> None:
        if value:
            self.f_ip.value = value
            self.f_ip.set_error(None)

    def layout(self, w: int, h: int) -> None:
        ui, th = self.ui, self.th
        y = self.layout_header(w)
        x0, cw = self.column(w)
        fh, lab, gap = ui.px(44), ui.px(20), ui.px(26)
        self.label("l_name", x0, y, "Your name", cw)
        self.f_name.place(x0, y + lab, cw, fh)
        y += lab + fh + gap
        self.label("l_ip", x0, y, "Your IPv4 address (what ipconfig shows)", cw)
        pw = ui.px(92)
        self.f_ip.place(x0, y + lab, cw - pw - ui.px(10), fh)
        self.f_port.place(x0 + cw - pw, y + lab, pw, fh)
        y += lab + fh + gap
        self.label("l_dd", x0, y, "Addresses found on this PC", cw)
        self.dd.place(x0, y + lab, cw, ui.px(40))
        y += lab + ui.px(40) + ui.px(14)
        self.text("hint", x0, y, "Friends type this address to join. Everyone must be on the same Wi-Fi "
                  "or phone hotspot.", 12, fill=th.text_faint, anchor="nw", width=cw, justify="left")
        self.b_next.place(x0, max(y + ui.px(56), h - ui.px(16) - ui.px(50)), cw, ui.px(50))

    def next(self) -> None:
        name = clean_name(self.f_name.value, "")
        ip = self.f_ip.value.strip()
        port = parse_port(self.f_port.value)
        ok = True
        if not name:
            self.f_name.set_error("Type a name so friends know it's you")
            ok = False
        if not is_valid_ipv4(ip):
            self.f_ip.set_error("That doesn't look like an IPv4 address")
            ok = False
        if port is None:
            self.f_port.set_error("1024-65535")
            ok = False
        if not ok:
            return
        s = self.app.settings
        s["name"], s["host_ip"], s["port"] = name, ip, port
        s.save()
        self.app.host_net = (name, ip, port)
        self.app.show(HostGameScreen, mode="create")


# ======================================================================= step 2
class HostGameScreen(Screen):
    """mode='create': make a new room. mode='change': reconfigure the room from the lobby."""

    def build(self) -> None:
        ui, c, app = self.ui, self.canvas, self.app
        self.mode = self.kw.get("mode", "create")
        change = self.mode == "change"
        room = app.room or {}
        if change and room:
            self.game = room.get("game", "ludo")
            self.people = int(room.get("humans", 1))
            self.bots = int(room.get("bots", 1))
            self.options: Dict[str, Dict[str, Any]] = {self.game: dict(room.get("options") or {})}
        else:
            self.game = app.settings["game"] if app.settings["game"] in GAME_IDS else "ludo"
            self.people, self.bots = 2, 0
            self.options = {}
        self.header("Change game" if change else "Pick a game", "Pick a game, players and rules" if change
                    else "Step 2 of 2: the game", back=app.go_lobby if change else app.go_host)
        self.seg_people = self.add(widgets.Segmented(ui, c, [(1, "1")], 1, self._people_changed))
        self.seg_bots = self.add(widgets.Segmented(ui, c, [(0, "0")], 0, self._bots_changed))
        self.opt_segs: List[Tuple[Any, Any]] = []
        self.b_go = self.add(widgets.Button(ui, c, "Save changes" if change else "Create room", self.go,
                                            kind="primary"))
        self._hover: Optional[str] = None
        for gid in GAME_IDS:
            tag = "tile_" + gid
            self.canvas.tag_bind(tag, "<ButtonRelease-1>", lambda e, g=gid: self.pick(g))
            self.canvas.tag_bind(tag, "<Enter>", lambda e, g=gid: self._tile_hover(g, True))
            self.canvas.tag_bind(tag, "<Leave>", lambda e, g=gid: self._tile_hover(g, False))
        self._normalise_seats()
        self._build_options()

    # ------------------------------------------------------------- choices
    def logic(self) -> Any:
        return logic_class(self.game)

    def _normalise_seats(self) -> None:
        counts = self.logic().SEAT_COUNTS
        top = max(counts)
        people_ok = [p for p in range(1, top + 1) if any(p + b in counts for b in range(0, top))]
        if self.people not in people_ok:
            self.people = min(people_ok, key=lambda p: abs(p - self.people))
        bots_ok = [b for b in range(0, top) if self.people + b in counts]
        if self.bots not in bots_ok:
            self.bots = bots_ok[0] if self.people > 1 else (bots_ok[-1] if self.game == "ludo" else bots_ok[0])
        self.seg_people.set_choices([(p, "1 + bots" if p == 1 else str(p)) for p in people_ok], self.people)
        self.seg_bots.set_choices([(b, str(b)) for b in bots_ok], self.bots)

    def _people_changed(self, value: Any) -> None:
        self.people = int(value)
        self._normalise_seats()
        self.relayout()

    def _bots_changed(self, value: Any) -> None:
        self.bots = int(value)

    def _build_options(self) -> None:
        for _opt, seg in self.opt_segs:
            self.remove(seg)
        self.opt_segs = []
        self.drop("optlab_")
        current = self.logic().clean_options(self.options.get(self.game))
        self.options[self.game] = current
        for opt in self.logic().OPTIONS:
            seg = self.add(widgets.Segmented(self.ui, self.canvas, list(opt.choices), current[opt.key],
                                             lambda v, k=opt.key: self._opt_changed(k, v)))
            self.opt_segs.append((opt, seg))

    def _opt_changed(self, key: str, value: Any) -> None:
        self.options.setdefault(self.game, {})[key] = value

    def pick(self, gid: str) -> None:
        if gid == self.game:
            return
        self.game = gid
        self._normalise_seats()
        self._build_options()
        self.relayout()

    def _tile_hover(self, gid: str, on: bool) -> None:
        self._hover = gid if on else (None if self._hover == gid else self._hover)
        try:
            self.canvas.configure(cursor="hand2" if on else "")
        except Exception:
            pass
        self._draw_tiles()

    # -------------------------------------------------------------- layout
    def layout(self, w: int, h: int) -> None:
        ui, th = self.ui, self.th
        y = self.layout_header(w)
        x0, cw = self.column(w, 420)
        self._tiles_geo = (x0, y, cw)
        self._draw_tiles()
        tile_h = self._tile_h
        y += tile_h + ui.px(10)
        L = self.logic()
        lo, hi = min(L.SEAT_COUNTS), max(L.SEAT_COUNTS)
        players = "%d players" % lo if lo == hi else "%d-%d players" % (lo, hi)
        self.text("gname", x0, y, "%s  ·  %s" % (L.NAME, players), 15, bold=True, anchor="nw", fit=cw)
        self.text("gtag", x0, y + ui.px(22), L.TAGLINE, 12, fill=th.text_dim, anchor="nw", fit=cw)
        y += ui.px(50)
        seg_h, lab = ui.px(34), ui.px(18)
        half = (cw - ui.px(12)) // 2
        self.label("l_people", x0, y, "People", half)
        self.label("l_bots", x0 + half + ui.px(12), y, "Bots", half)
        self.seg_people.place(x0, y + lab, half, seg_h)
        self.seg_bots.place(x0 + half + ui.px(12), y + lab, half, seg_h)
        y += lab + seg_h + ui.px(14)
        lab_w = int(cw * 0.42)
        for opt, seg in self.opt_segs:
            self.text("optlab_" + opt.key, x0, y + seg_h / 2, opt.label, 12, bold=True, fill=th.text_dim,
                      anchor="w", fit=lab_w - ui.px(8))
            seg.place(x0 + lab_w, y, cw - lab_w, seg_h)
            y += seg_h + ui.px(10)
        self.b_go.place(x0, max(y + ui.px(8), h - ui.px(16) - ui.px(50)), cw, ui.px(50))

    def _draw_tiles(self) -> None:
        if not hasattr(self, "_tiles_geo"):
            return
        ui, th = self.ui, self.th
        x0, y, cw = self._tiles_geo
        gap = ui.px(8)
        n = len(GAME_IDS)
        tw = (cw - gap * (n - 1)) // n
        icon = max(ui.px(26), min(ui.px(48), tw - ui.px(20)))
        th_h = icon + ui.px(34)
        self._tile_h = th_h
        for i, gid in enumerate(GAME_IDS):
            tx = x0 + i * (tw + gap)
            photo, pad = tile_photo(ui, tw, th_h, gid == self.game, gid == self._hover)
            tag = "tile_" + gid
            self.image("tilebg_" + gid, photo, tx - pad, y - pad, tags=(tag,))
            self.image("tileic_" + gid, game_icon(ui, gid, icon), tx + tw / 2, y + ui.px(8), anchor="n",
                       tags=(tag,))
            name = logic_class(gid).NAME
            short = {"Snakes & Ladders": "Snakes", "Tic-Tac-Toe": "Tic-Tac-Toe"}.get(name, name)
            self.text("tilename_" + gid, tx + tw / 2, y + th_h - ui.px(8), short, 11, bold=gid == self.game,
                      fill=th.text if gid == self.game else th.text_dim, anchor="s", fit=tw - ui.px(6))

    # ------------------------------------------------------------------ go
    def go(self) -> None:
        app = self.app
        opts = self.logic().clean_options(self.options.get(self.game))
        app.settings["game"] = self.game
        app.settings.save()
        if self.mode == "change":
            if app.client is not None:
                app.client.send({"t": "configure", "game": self.game, "humans": self.people, "bots": self.bots,
                                 "options": opts})
            app.go_lobby()
            return
        name, ip, port = app.host_net or (app.settings["name"], best_ipv4(), app.settings["port"])
        self.b_go.set_enabled(False)
        self.b_go.set_text("Creating room…")
        self.canvas.update_idletasks()
        server: Optional[RoomServer] = None
        try:
            server = RoomServer(ip, port, name, self.game, self.people, self.bots, opts)
            server.start()
        except (OSError, ValueError, RuntimeError) as exc:
            self._fail(friendly_error(exc, "host") if isinstance(exc, OSError) else str(exc))
            return
        bound_ip, bound_port = server.address
        client = Client(bound_ip if bound_ip not in ("", "0.0.0.0") else "127.0.0.1", bound_port, name,
                        token=server.host_token)
        try:
            welcome = client.connect(timeout=4)
        except Exception as exc:  # noqa: BLE001 - show any failure to the user
            log.exception("host could not join its own room")
            server.stop()
            self._fail(friendly_error(exc, "connect"))
            return
        app.attach(client, server)
        app.on_welcome(welcome)

    def _fail(self, message: str) -> None:
        self.b_go.set_enabled(True)
        self.b_go.set_text("Create room")
        self.toast(message, kind="bad", seconds=5)
