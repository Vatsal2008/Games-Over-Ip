"""Join: type the host's IPv4 or pick a room from 'Nearby games'."""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from .. import widgets
from ..client import Client
from ..discovery import Scanner
from ..netutil import is_valid_ipv4
from ..protocol import clean_name
from .base import Screen, tile_photo
from .host import parse_port

HELP = ("Can't connect?\n"
        "• Be on the same Wi-Fi or phone hotspot as the host.\n"
        "• The host must click Allow in the Windows Firewall prompt.\n"
        "• College Wi-Fi often blocks laptops from talking to each other. "
        "Use a phone hotspot instead.")


class JoinScreen(Screen):
    def build(self) -> None:
        ui, c, app, s = self.ui, self.canvas, self.app, self.app.settings
        self.header("Join a game", "Type the IPv4 address shown on the host's screen", back=app.go_menu)
        self.f_name = self.add(widgets.TextField(ui, c, s["name"], "Your name", max_len=16,
                                                 on_enter=lambda v: self.f_ip.focus()))
        self.f_ip = self.add(widgets.TextField(ui, c, s["join_ip"], "192.168.1.5", max_len=15,
                                               on_enter=lambda v: self.connect()))
        self.f_port = self.add(widgets.TextField(ui, c, str(s["port"]), "50505", max_len=5))
        self.b_go = self.add(widgets.Button(ui, c, "Connect", self.connect, kind="primary"))
        self.rooms: List[Dict[str, Any]] = []
        self._rooms_key: Tuple = ()
        self.failed = False
        self.error_text = ""
        self.connecting = False
        self.scanner: Optional[Scanner] = None
        try:
            self.scanner = Scanner().start()
        except Exception:
            self.scanner = None
        self.after(300, self._scan)

    def destroy(self) -> None:
        if self.scanner is not None:
            try:
                self.scanner.stop()
            except Exception:
                pass
        super().destroy()

    # ------------------------------------------------------------ discovery
    def _scan(self) -> None:
        if self.scanner is not None:
            rooms = [r for r in self.scanner.hosts() if r.get("compatible", True)][:3]
            key = tuple((r.get("ip"), r.get("port"), r.get("players"), r.get("state"), r.get("game")) for r in rooms)
            if key != self._rooms_key:
                self._rooms_key = key
                self.rooms = rooms
                self.relayout()
        self.after(1000, self._scan)

    def _pick_room(self, i: int) -> None:
        if 0 <= i < len(self.rooms):
            room = self.rooms[i]
            self.f_ip.value = str(room.get("ip", ""))
            self.f_port.value = str(room.get("port", ""))
            self.connect()

    # --------------------------------------------------------------- layout
    def layout(self, w: int, h: int) -> None:
        ui, th = self.ui, self.th
        y = self.layout_header(w)
        x0, cw = self.column(w)
        fh, lab, gap = ui.px(44), ui.px(20), ui.px(26)
        self.label("l_name", x0, y, "Your name", cw)
        self.f_name.place(x0, y + lab, cw, fh)
        y += lab + fh + gap
        self.label("l_ip", x0, y, "Host's IPv4 address", cw)
        pw = ui.px(92)
        self.f_ip.place(x0, y + lab, cw - pw - ui.px(10), fh)
        self.f_port.place(x0 + cw - pw, y + lab, pw, fh)
        y += lab + fh + gap
        self.b_go.place(x0, y, cw, ui.px(50))
        y += ui.px(50) + ui.px(18)
        if self.failed:
            self.text("errmsg", x0, y, self.error_text, 13, bold=True, fill=th.bad, anchor="nw", width=cw,
                      justify="left")
            bbox = self.canvas.bbox(self.items["errmsg"])
            y = (bbox[3] if bbox else y + ui.px(20)) + ui.px(8)
            self.text("help", x0, y, HELP, 12, fill=th.text_dim, anchor="nw", width=cw, justify="left")
            bbox = self.canvas.bbox(self.items["help"])
            y = (bbox[3] if bbox else y + ui.px(80)) + ui.px(14)
        else:
            self.hide("help", "errmsg")
        self.label("l_near", x0, y, "Nearby games", cw)
        y += lab + ui.px(2)
        self.drop("room_")
        row_h = ui.px(52)
        if not self.rooms:
            msg = ("Looking for games on this network…" if self.scanner is not None and self.scanner.available
                   else "Nearby games aren't available here. Type the IP instead.")
            self.text("room_empty", x0, y, msg, 12, fill=th.text_faint, anchor="nw", fit=cw)
            return
        for i, room in enumerate(self.rooms):
            if y + row_h > h - ui.px(8):
                break
            tag = "roomrow_%d" % i
            photo, pad = tile_photo(ui, cw, row_h, False)
            self.image("room_bg%d" % i, photo, x0 - pad, y - pad, tags=(tag,))
            self.canvas.tag_bind(tag, "<ButtonRelease-1>", lambda e, k=i: self._pick_room(k))
            self.canvas.tag_bind(tag, "<Enter>", lambda e: self.canvas.configure(cursor="hand2"))
            self.canvas.tag_bind(tag, "<Leave>", lambda e: self.canvas.configure(cursor=""))
            title = "%s  ·  %s" % (room.get("host") or "Host", room.get("game_name") or room.get("game") or "")
            state = "in game, tap to join" if room.get("state") == "playing" else "%s joined" % room.get("players", "")
            self.text("room_t%d" % i, x0 + ui.px(14), y + row_h * 0.34, title, 14, bold=True, anchor="w",
                      fit=cw - ui.px(110))
            self.text("room_s%d" % i, x0 + ui.px(14), y + row_h * 0.72, "%s:%s" % (room.get("ip"), room.get("port")),
                      11, fill=th.text_dim, anchor="w", fit=cw - ui.px(110))
            self.text("room_p%d" % i, x0 + cw - ui.px(14), y + row_h / 2, state, 12,
                      fill=th.accent2, anchor="e")
            y += row_h + ui.px(8)

    # --------------------------------------------------------------- connect
    def connect(self) -> None:
        if self.connecting:
            return
        name = clean_name(self.f_name.value, "")
        ip = self.f_ip.value.strip()
        port = parse_port(self.f_port.value)
        ok = True
        if not name:
            self.f_name.set_error("Type a name so friends know it's you")
            ok = False
        if not is_valid_ipv4(ip):
            self.f_ip.set_error("Type the host's IPv4, like 192.168.1.5")
            ok = False
        if port is None:
            self.f_port.set_error("1024-65535")
            ok = False
        if not ok:
            return
        s = self.app.settings
        s["name"], s["join_ip"], s["port"] = name, ip, port
        s.save()
        self.connecting = True
        self.b_go.set_enabled(False)
        self.b_go.set_text("Connecting…")
        client = Client(ip, port, name, rejoin=self.app.rejoin_key(ip, port))
        self.app.attach(client, None)
        client.connect_async(timeout=5)

    def on_connect_failed(self, error: str) -> None:
        self.connecting = False
        self.failed = True
        self.b_go.set_enabled(True)
        self.b_go.set_text("Try again")
        self.error_text = error or "Couldn't connect"
        self.f_ip.set_error("Couldn't connect")
        self.relayout()
