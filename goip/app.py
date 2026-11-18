"""App shell: window, DPI, screen router, network pump, boss key. Entry point: main()."""
from __future__ import annotations

import logging
import os
import sys
import tempfile
import tkinter as tk
from typing import Any, Dict, Optional, Type

from . import BASE_SIZE, MIN_WINDOW, WINDOW_SIZES, widgets
from . import protocol as P
from .client import Client
from .gfx import Image
from .screens import (HostGameScreen, HostNetScreen, JoinScreen, LobbyScreen, MenuScreen, PlayScreen,
                      PrefsScreen)
from .screens.base import Screen
from .screens.notes import NotesOverlay
from .server import RoomServer
from .settings import Settings
from .theme import get_theme

log = logging.getLogger(__name__)


def _dpi_aware() -> None:
    if sys.platform == "win32":
        try:
            import ctypes
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass


def _paint_icon(img: Image, theme: Any) -> None:
    s = float(img.w)
    if theme.key == "paper":   # plain document, matches the "Notes" title
        img.fill_rrect(s * 0.18, s * 0.08, s * 0.64, s * 0.84, s * 0.06, "#ffffff")
        img.stroke_rrect(s * 0.18, s * 0.08, s * 0.64, s * 0.84, s * 0.06, "#8a8a8a", max(1.0, s * 0.04))
        for i in range(4):
            img.line(s * 0.3, s * (0.3 + i * 0.14), s * 0.7, s * (0.3 + i * 0.14), max(1.0, s * 0.05), "#9aa6b8")
        return
    widgets.paint_die(img, 5, "#ffffff", theme.seat("blue")[2], angle=-10, shadow=False)


class App:
    def __init__(self, dpi_aware: bool = True) -> None:
        if dpi_aware:   # process-wide and irreversible, so tests opt out
            _dpi_aware()
        self.root = root = tk.Tk()
        root.withdraw()
        self.settings = Settings()
        self.dpi = max(1.0, root.winfo_fpixels("1i") / 96.0)
        theme = get_theme(self.settings["theme"])
        self.ui = widgets.UI(root, theme)
        root.title(theme.title)
        root.configure(bg=theme.bg_top)
        root.minsize(int(MIN_WINDOW[0] * self.dpi), int(MIN_WINDOW[1] * self.dpi))
        self.apply_size(self.settings["size"], first=True)
        self.apply_opacity(self.settings["opacity"])
        self._icon: Any = None
        self._set_icon()
        self.stage = tk.Frame(root, bg=theme.bg_top, bd=0, highlightthickness=0)
        self.stage.pack(fill="both", expand=True)

        self.client: Optional[Client] = None
        self.server: Optional[RoomServer] = None
        self.welcome: Optional[Dict[str, Any]] = None
        self.room: Optional[Dict[str, Any]] = None
        self.begin: Optional[Dict[str, Any]] = None
        self.my_id: Optional[int] = None
        self.is_host = False
        self.host_net: Optional[tuple] = None
        self.screen: Optional[Screen] = None
        self.notes: Optional[NotesOverlay] = None
        self._closing = False

        root.bind_all("<Escape>", self.boss_key)
        root.bind("<Key>", self._on_key)
        root.bind("<Map>", self._on_map)
        root.protocol("WM_DELETE_WINDOW", self.quit)
        root.report_callback_exception = self._report  # type: ignore[assignment]
        root.deiconify()
        self.show(MenuScreen)
        self._pump_job: Optional[str] = root.after(15, self._pump)

    # ------------------------------------------------------------------ window
    def apply_size(self, key: str, first: bool = False) -> None:
        """Resize to a preset, clamped so the whole window stays on screen (taskbar included)."""
        w, h = WINDOW_SIZES.get(key, WINDOW_SIZES["M"])
        sw, sh = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        max_w, max_h = sw - int(24 * self.dpi), sh - int(96 * self.dpi)   # room for title bar + taskbar
        W = max(int(MIN_WINDOW[0] * self.dpi), min(int(w * self.dpi), max_w))
        H = max(int(MIN_WINDOW[1] * self.dpi), min(int(h * self.dpi), max_h))
        if first:
            x = max(0, sw - W - int(48 * self.dpi))
            y = max(0, (sh - H) // 2 - int(20 * self.dpi))
        else:
            self.root.update_idletasks()
            x = min(max(0, self.root.winfo_x()), max(0, sw - W - int(8 * self.dpi)))
            y = min(max(0, self.root.winfo_y()), max(0, sh - H - int(72 * self.dpi)))
        self.root.geometry("%dx%d+%d+%d" % (W, H, x, y))

    def apply_opacity(self, percent: int) -> None:
        try:
            self.root.attributes("-alpha", max(0.3, min(1.0, percent / 100.0)))
        except tk.TclError:
            pass

    def _set_icon(self) -> None:
        try:
            th = self.ui.theme
            self._icon = self.ui.sprites.get(("appicon", th.key), 64, 64, lambda img: _paint_icon(img, th))
            self.root.iconphoto(True, self._icon)
        except Exception:
            log.debug("icon failed", exc_info=True)

    def update_scale(self, w: int, h: int) -> None:
        bw, bh = BASE_SIZE
        s = min(w / (bw * self.dpi), h / (bh * self.dpi))
        s = max(0.8, min(1.5, s)) * self.dpi
        s = round(s * 20) / 20.0
        if abs(s - self.ui.scale) > 1e-6:
            self.ui.set_scale(s)

    def set_theme(self, key: str) -> None:
        theme = get_theme(key)
        self.settings["theme"] = theme.key
        self.settings.save()
        self.ui.set_theme(theme)
        self.root.configure(bg=theme.bg_top)
        self.stage.configure(bg=theme.bg_top)
        if not (self.notes and self.notes.visible):
            self.root.title(theme.title)
        self._set_icon()
        if self.screen is not None:
            self.show(type(self.screen), **self.screen.kw)

    # ----------------------------------------------------------------- screens
    def show(self, cls: Type[Screen], **kw: Any) -> None:
        if self.screen is not None:
            self.screen.destroy()
            self.screen = None
        self.screen = cls(self, **kw)

    def toast(self, text: str, kind: str = "info", seconds: float = 2.6) -> None:
        if self.screen is not None:
            self.screen.toast(text, kind, seconds)

    def go_menu(self) -> None:
        self._drop_pending()
        self.show(MenuScreen)

    def go_host(self) -> None:
        self.show(HostNetScreen)

    def go_join(self) -> None:
        self.show(JoinScreen)

    def go_settings(self) -> None:
        self.show(PrefsScreen)

    def go_lobby(self) -> None:
        if self.client is not None and self.welcome is not None:
            self.show(LobbyScreen)
        else:
            self.show(MenuScreen)

    def go_change_game(self) -> None:
        if self.is_host and self.room is not None:
            self.show(HostGameScreen, mode="change")

    # ----------------------------------------------------------------- network
    def attach(self, client: Client, server: Optional[RoomServer] = None) -> None:
        self._drop_pending()
        self.client, self.server = client, server
        self.welcome = self.room = self.begin = None
        self.is_host = server is not None

    def _drop_pending(self) -> None:
        """Forget a join attempt that hasn't been welcomed yet (user pressed Back)."""
        if self.client is not None and self.welcome is None and self.server is None:
            self.client.close()
            self.client = None

    def on_welcome(self, welcome: Dict[str, Any]) -> None:
        self.welcome = welcome
        self.my_id = welcome.get("you")
        key, client = welcome.get("key"), self.client
        if isinstance(key, str) and client is not None and self.server is None:
            self.settings["rejoin"] = "%s:%d|%s" % (client.ip, client.port, key)
            self.settings.save()
        self.room = welcome.get("room")
        self.is_host = bool(welcome.get("host"))
        self.show(LobbyScreen)

    def _pump(self) -> None:
        if self._closing:
            return
        client = self.client
        if client is not None:
            try:
                msgs = client.poll()
            except Exception:
                log.exception("client.poll failed")
                msgs = []
            for msg in msgs:
                if self.client is not client:
                    break
                try:
                    self._route(msg)
                except Exception:
                    log.exception("error handling %r", msg.get("t"))
        if not self._closing:
            self._pump_job = self.root.after(15, self._pump)

    def _route(self, msg: Dict[str, Any]) -> None:
        t = msg.get("t")
        screen = self.screen
        if t == P.L_CONNECTED:
            self.on_welcome(msg.get("welcome") or {})
        elif t == P.L_CONNECT_FAILED:
            failed = self.client
            self.client = None
            if failed is not None:
                failed.close()
            if screen is not None and hasattr(screen, "on_connect_failed"):
                screen.on_connect_failed(str(msg.get("error") or "Couldn't connect"))  # type: ignore[attr-defined]
        elif t == P.L_DISCONNECTED:
            self.leave(reason=str(msg.get("reason") or "Disconnected"), kind="bad")
        elif t == P.LOBBY:
            self.room = msg.get("room") or self.room
            if screen is not None:
                screen.on_room(self.room or {})
        elif t == P.BEGIN:
            self.begin = msg
            self.show(PlayScreen)
        elif t in (P.STATE, P.EVENT, P.SEATS, P.OVER):
            if isinstance(screen, PlayScreen):
                screen.on_message(msg)
        elif t == P.ERROR:
            self.toast(str(msg.get("msg") or ""), "bad")
        elif t == P.NOTICE:
            self.toast(str(msg.get("msg") or ""), "info")

    def rejoin_key(self, ip: str, port: int) -> Optional[str]:
        """The seat key saved from the last time we joined ip:port, if any."""
        where, _sep, key = self.settings.get("rejoin", "").partition("|")
        if where != "%s:%d" % (ip, port):
            return None
        return key or None

    def leave(self, reason: Optional[str] = None, kind: str = "info") -> None:
        client, server = self.client, self.server
        self.client = self.server = None
        self.welcome = self.room = self.begin = None
        self.is_host = False
        if client is not None:
            client.close()
        if server is not None:
            server.stop()
        if not self._closing:
            self.show(MenuScreen)
            if reason:
                self.toast(reason, kind, seconds=4)

    # ---------------------------------------------------------------- keyboard
    def boss_key(self, _event: Any = None) -> str:
        if self.notes is not None and self.notes.visible:
            self.notes.hide()
            self.ui.anim.resume()
            return "break"
        if self.settings["boss"] == "notes":
            if self.notes is None:
                self.notes = NotesOverlay(self)
            self.notes.show()
        else:
            self.ui.anim.pause()
            self.root.iconify()
        return "break"

    def _on_map(self, event: Any) -> None:
        if event.widget is self.root and not (self.notes and self.notes.visible):
            self.ui.anim.resume()

    def _on_key(self, event: Any) -> None:
        if self.notes is not None and self.notes.visible:
            return
        if isinstance(event.widget, (tk.Entry, tk.Text)):
            return
        if self.screen is not None:
            self.screen.on_key(event.keysym)

    # ---------------------------------------------------------------- lifecycle
    def _report(self, exc: type, val: BaseException, tb: Any) -> None:
        log.error("unhandled UI error", exc_info=(exc, val, tb))
        try:
            self.toast("Something went wrong. You can keep playing.", "bad")
        except Exception:
            pass

    def quit(self) -> None:
        if self._closing:
            return
        self._closing = True
        try:
            if self.notes is not None:
                self.notes.save()
            self.settings.save()
        except Exception:
            log.exception("saving settings failed")
        try:
            self.root.withdraw()
        except tk.TclError:
            pass
        client, server = self.client, self.server
        self.client = self.server = None
        if client is not None:
            client.close()
        if server is not None:
            server.stop()
        if self._pump_job is not None:
            try:
                self.root.after_cancel(self._pump_job)
            except tk.TclError:
                pass
        if self.screen is not None:
            try:
                self.screen.destroy()
            except Exception:
                log.exception("screen destroy failed")
            self.screen = None
        self.ui.anim.stop()
        try:
            self.root.destroy()
        except tk.TclError:
            pass

    def run(self) -> None:
        self.root.mainloop()


def _setup_logging() -> None:
    if logging.getLogger().handlers:
        return
    if sys.stderr is None:   # pythonw: no console, log to a file instead
        path = os.path.join(tempfile.gettempdir(), "games_over_ip.log")
        logging.basicConfig(filename=path, level=logging.WARNING,
                            format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    else:
        logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")


def main() -> None:
    _setup_logging()
    App().run()
