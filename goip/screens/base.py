"""Screen base class and small drawing helpers shared by all screens."""
from __future__ import annotations

import logging
import tkinter as tk
from typing import Any, Callable, Dict, List, Optional, Tuple

from .. import gfx, widgets
from ..gfx import Image, with_alpha

log = logging.getLogger(__name__)


class Screen:
    """A full-window canvas. Subclasses implement build(), layout(w, h) and on_room(room)."""

    def __init__(self, app: Any, **kw: Any) -> None:
        self.app = app
        self.ui = app.ui
        self.th = app.ui.theme
        self.kw = kw
        self.w = self.h = 0
        self._dead = False
        self._afters: set = set()
        self._job: Optional[str] = None
        self.widgets: List[Any] = []
        self.items: Dict[str, int] = {}
        self.canvas = tk.Canvas(app.stage, highlightthickness=0, bd=0, bg=self.th.bg_top, takefocus=0)
        self.canvas.place(x=0, y=0, relwidth=1, relheight=1)
        self.build()
        self.toaster = widgets.Toaster(self.ui, self.canvas, position="top")
        self.canvas.bind("<Configure>", self._configure)
        self.after(0, self.relayout)

    # ------------------------------------------------------------ to override
    def build(self) -> None:
        pass

    def layout(self, w: int, h: int) -> None:
        pass

    def on_room(self, room: Dict[str, Any]) -> None:
        pass

    def on_key(self, keysym: str) -> None:
        pass

    # ---------------------------------------------------------------- plumbing
    def add(self, widget: Any) -> Any:
        self.widgets.append(widget)
        return widget

    def remove(self, widget: Any) -> None:
        if widget is None:
            return
        try:
            self.widgets.remove(widget)
        except ValueError:
            pass
        widget.destroy()

    def after(self, ms: int, fn: Callable[[], None]) -> Optional[str]:
        if self._dead:
            return None
        box: Dict[str, str] = {}

        def run() -> None:
            self._afters.discard(box.get("id"))
            if not self._dead:
                fn()

        ident = self.canvas.after(max(0, int(ms)), run)
        box["id"] = ident
        self._afters.add(ident)
        return ident

    def _configure(self, _event: Any = None) -> None:
        if self._job is not None:
            try:
                self.canvas.after_cancel(self._job)
            except tk.TclError:
                pass
            self._afters.discard(self._job)
        self._job = self.after(20, self.relayout)

    def relayout(self) -> None:
        self._job = None
        if self._dead:
            return
        w, h = self.canvas.winfo_width(), self.canvas.winfo_height()
        if w < 4 or h < 4:
            return
        self.w, self.h = w, h
        self.app.update_scale(w, h)
        widgets.draw_background(self.canvas, self.ui, w, h)
        try:
            self.layout(w, h)
        except Exception:
            log.exception("layout failed in %s", type(self).__name__)
        self.toaster.raise_()

    def toast(self, text: str, kind: str = "info", seconds: float = 2.6) -> None:
        if not self._dead and text:
            self.toaster.show(text, kind=kind, seconds=seconds)

    def destroy(self) -> None:
        self._dead = True
        for ident in list(self._afters):
            try:
                self.canvas.after_cancel(ident)
            except tk.TclError:
                pass
        self._afters.clear()
        for widget in self.widgets:
            try:
                widget.destroy()
            except Exception:
                log.exception("widget destroy failed")
        self.widgets = []
        try:
            self.toaster.destroy()
        except Exception:
            pass
        self.ui.anim.cancel_owner(self)
        try:
            self.canvas.destroy()
        except tk.TclError:
            pass

    # --------------------------------------------------------- drawing helpers
    def column(self, w: int, max_w: float = 400) -> Tuple[int, int]:
        """(x0, width) of the centred content column."""
        m = self.ui.px(16)
        cw = int(min(w - 2 * m, self.ui.px(max_w)))
        return (w - cw) // 2, cw

    def text(self, name: str, x: float, y: float, text: str, px: float, bold: bool = False,
             display: bool = False, fill: Optional[str] = None, anchor: str = "center",
             width: float = 0, justify: str = "center", fit: float = 0) -> int:
        """Create or update a named text item. fit>0 shrinks/ellipsises to that width."""
        font = self.ui.font(px, bold, display)
        if fit:
            text, font = self.ui.fit(text, px, fit, bold, display)
        fill = fill or self.th.text
        item = self.items.get(name)
        opts = dict(text=text, font=font, fill=fill, anchor=anchor, justify=justify)
        if width:
            opts["width"] = int(width)
        if item is None:
            item = self.canvas.create_text(x, y, state="disabled", tags=("txt", name), **opts)
            self.items[name] = item
        else:
            self.canvas.coords(item, x, y)
            self.canvas.itemconfigure(item, state="disabled", **opts)
        return item

    def hide(self, *names: str) -> None:
        for name in names:
            item = self.items.get(name)
            if item is not None:
                self.canvas.itemconfigure(item, state="hidden")

    def drop(self, prefix: str) -> None:
        for name in [n for n in self.items if n.startswith(prefix)]:
            self.canvas.delete(self.items.pop(name))

    def image(self, name: str, photo: Any, x: float, y: float, anchor: str = "nw",
              tags: Tuple[str, ...] = ()) -> int:
        item = self.items.get(name)
        if item is None:
            item = self.canvas.create_image(x, y, image=photo, anchor=anchor, tags=(name,) + tuple(tags))
            self.items[name] = item
        else:
            self.canvas.coords(item, x, y)
            self.canvas.itemconfigure(item, image=photo, anchor=anchor, state="normal")
        refs = getattr(self.canvas, "_goip_refs", None)
        if refs is None:
            refs = {}
            self.canvas._goip_refs = refs  # type: ignore[attr-defined]
        refs[name] = photo
        return item

    def header(self, title: str, subtitle: str = "", back: Optional[Callable[[], None]] = None,
               back_text: str = "Back") -> None:
        """Create the header widgets once (call from build)."""
        self._hdr = (title, subtitle)
        self._back = self.add(widgets.Button(self.ui, self.canvas, back_text, back, kind="ghost",
                                             icon="‹", font_px=13)) if back else None

    def layout_header(self, w: int) -> int:
        """Place the header; returns the y where content starts."""
        ui = self.ui
        title, subtitle = self._hdr
        top = ui.px(12)
        if self._back is not None:
            self._back.place(ui.px(8), top, ui.px(86), ui.px(34))
        y = top + ui.px(34) + ui.px(10)
        x0, cw = self.column(w)
        self.text("hdr_title", x0, y, title, 24, bold=True, display=True, anchor="nw", fit=cw)
        y += ui.px(32)
        if subtitle:
            self.text("hdr_sub", x0, y, subtitle, 13, fill=self.th.text_dim, anchor="nw", fit=cw)
            y += ui.px(22)
        return y + ui.px(8)

    def label(self, name: str, x: float, y: float, text: str, max_w: float = 0) -> None:
        self.text(name, x, y, text, 12, bold=True, fill=self.th.text_dim, anchor="nw", fit=max_w)


# ------------------------------------------------------------------ overlays
class Overlay:
    """Dimmed sheet + centred card with a headline, body text and buttons.

    buttons: [(label, kind, callback)]. Drawn on any canvas (the play screen uses
    the game canvas so the card sits above the board).
    """

    GUARD_MS = 60

    def __init__(self, ui: Any, canvas: tk.Canvas, title: str, body: str,
                 buttons: List[Tuple[str, str, Callable[[], None]]], celebrate: bool = False,
                 allow_above: Tuple[str, ...] = ()) -> None:
        self.ui, self.canvas = ui, canvas
        self.title, self.body, self.celebrate = title, body, celebrate
        self.tag = "overlay%d" % id(self)
        self.allow_above = tuple(allow_above)   # tags that may sit above the card (toasts)
        self.panel = widgets.Panel(ui, canvas, radius=20, kind="card")
        self.buttons = [widgets.Button(ui, canvas, text, fn, kind=kind) for text, kind, fn in buttons]
        self._ref: Any = None
        self._celebrated = False
        self._dead = False
        self._guard_job: Optional[str] = None
        self.place()
        self._guard()

    def _top_tag(self) -> str:
        return self.buttons[-1].tag if self.buttons else self.tag + "txt"

    def _allowed(self, item: int) -> bool:
        tags = self.canvas.gettags(item)
        return any(t.startswith(("goipconf", self.tag)) or t in self.allow_above for t in tags)

    def _guard(self) -> None:
        """Games redraw on their own (hover, timers, animation ends) and may land on top; push them back."""
        if self._dead:
            return
        try:
            items = self.canvas.find_all()
            top = self.canvas.find_withtag(self._top_tag())
            if top:
                above = items[items.index(top[-1]) + 1:]
                if any(not self._allowed(i) for i in above):
                    self.raise_()
                    for tag in self.allow_above:
                        self.canvas.tag_raise(tag)
            self._guard_job = self.canvas.after(self.GUARD_MS, self._guard)
        except (tk.TclError, ValueError):
            self._guard_job = None

    def place(self) -> None:
        ui, th, c = self.ui, self.ui.theme, self.canvas
        w, h = max(1, c.winfo_width()), max(1, c.winfo_height())
        c.delete(self.tag)
        self._ref = dim_photo(ui, w, h)
        dim, txt = (self.tag, self.tag + "dim"), (self.tag, self.tag + "txt")
        c.create_image(0, 0, image=self._ref, anchor="nw", tags=dim)
        cw = int(min(w - ui.px(32), ui.px(320)))
        n = len(self.buttons)
        btn_h = ui.px(46)
        body_font = ui.font(13)
        body_lines = max(1, len(self.body) * ui.measure("m", body_font) // max(1, int(cw * 1.6)) + 1) if self.body else 0
        ch = ui.px(28) + ui.px(36) + body_lines * ui.linespace(body_font) + ui.px(18) + n * (btn_h + ui.px(10)) + ui.px(10)
        x0, y0 = (w - cw) // 2, max(ui.px(8), (h - ch) // 2)
        self.panel.place(x0, y0, cw, ch)
        y = y0 + ui.px(24)
        title, font = ui.fit(self.title, 24, cw - ui.px(28), bold=True, display=True)
        c.create_text(w / 2, y, text=title, font=font, fill=th.text, anchor="n", tags=txt, state="disabled")
        y += ui.px(40)
        if self.body:
            item = c.create_text(w / 2, y, text=self.body, font=body_font, fill=th.text_dim, anchor="n",
                                 justify="center", width=cw - ui.px(32), tags=txt, state="disabled")
            bbox = c.bbox(item)
            y = (bbox[3] if bbox else y + ui.px(20)) + ui.px(18)
        for b in self.buttons:
            b.place(x0 + ui.px(18), y, cw - ui.px(36), btn_h)
            y += btn_h + ui.px(10)
        self.raise_()
        if self.celebrate and not self._celebrated:
            self._celebrated = True
            widgets.confetti(ui, c, w / 2, y0 + ui.px(30), count=80)

    def raise_(self) -> None:
        try:
            self.canvas.tag_raise(self.tag + "dim")
            self.panel.raise_()
            self.canvas.tag_raise(self.tag + "txt")
        except tk.TclError:
            return
        for b in self.buttons:
            b.raise_()

    def destroy(self) -> None:
        self._dead = True
        if self._guard_job is not None:
            try:
                self.canvas.after_cancel(self._guard_job)
            except tk.TclError:
                pass
            self._guard_job = None
        try:
            self.canvas.delete(self.tag)
        except tk.TclError:
            pass
        self.panel.destroy()
        for b in self.buttons:
            b.destroy()
        self.buttons = []


# ------------------------------------------------------------------ game icons
def game_icon(ui: Any, game_id: str, size: int) -> Any:
    """PhotoImage of a game's card icon (falls back to a plain tile if the view fails)."""
    th = ui.theme
    size = max(8, int(size))

    def paint(img: Image) -> None:
        try:
            from ..games import view_module
            view_module(game_id).paint_icon(img, th)
        except Exception:
            log.exception("paint_icon failed for %s", game_id)
            img.fill_rrect(2, 2, img.w - 4, img.h - 4, img.w * 0.22, th.accent, grad_to=th.accent_hi)

    return ui.sprites.get(("gameicon", game_id, th.key), size, size, paint)


def dim_photo(ui: Any, w: int, h: int, alpha: int = 150) -> Any:
    """Full-size translucent dark sheet for overlays."""
    th = ui.theme
    color = with_alpha("#000000" if th.dark else "#ffffff", alpha if th.dark else min(255, alpha + 40))

    def paint(img: Image) -> None:
        img.fill_rect(0, 0, img.w, img.h, color)

    return ui.sprites.get(("dim", th.key, alpha), max(1, w), max(1, h), paint)


def tile_photo(ui: Any, w: int, h: int, selected: bool, hover: bool = False) -> Any:
    """Rounded selectable tile (game cards, nearby-room rows)."""
    th = ui.theme
    w, h = max(8, int(w)), max(8, int(h))
    pad = ui.px(6)
    r = ui.px(14)

    def paint(img: Image) -> None:
        x, y, ww, hh = pad, pad, img.w - 2 * pad, img.h - 2 * pad
        if selected and th.glow:
            img.soft_rrect(x, y, ww, hh, r, with_alpha(th.accent, 120), pad * 1.6)
        img.soft_rrect(x, y + pad * 0.4, ww, hh, r, with_alpha(th.shadow, 90 if th.dark else 30), pad)
        fill = th.panel_hi if (selected or hover) else th.panel
        img.fill_rrect(x, y, ww, hh, r, gfx.lighten(fill, 0.04) if th.dark else fill, grad_to=fill)
        if selected:
            img.stroke_rrect(x, y, ww, hh, r, th.accent, max(1.5, ui.px(2)))
        else:
            img.stroke_rrect(x, y, ww, hh, r, with_alpha(th.edge, 200), 1)

    photo = ui.sprites.get(("tile", th.key, selected, hover, pad, r), w + 2 * pad, h + 2 * pad, paint)
    return photo, pad
