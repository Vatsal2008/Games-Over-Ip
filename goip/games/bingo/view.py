"""Bingo view: player strip, B-I-N-G-O letters, your private 5x5 board."""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional, Tuple

from ... import widgets
from ...gfx import Image, darken, lighten, with_alpha
from ..viewbase import GameView
from ..viewkit import Items
from .logic import LINES

LETTERS = "BINGO"
TILE_TAG = "bingotile"


# ------------------------------------------------------------------ painters
def paint_tile(img: Image, th: Any, state: str, color: str) -> None:
    """state: plain | hover | marked | selected"""
    s = float(min(img.w, img.h))
    p = max(1.0, s * 0.05)
    r = s * 0.2
    x, y, w = p, p, s - 2 * p
    img.soft_rrect(x, y + p * 0.7, w, w, r, with_alpha(th.shadow, 70 if th.dark else 25), p * 1.4)
    if state == "marked":
        main, light, dark = th.seat(color)
        img.fill_rrect(x, y, w, w, r, lighten(main, 0.12), grad_to=darken(main, 0.12))
        img.stroke_circle(s / 2, s / 2, s * 0.33, with_alpha("#ffffff", 150), max(1.2, s * 0.05))
        img.stroke_rrect(x, y, w, w, r, with_alpha(dark, 160), max(1.0, s * 0.02))
    else:
        top = lighten(th.board_a, 0.04) if state == "hover" else th.board_a
        img.fill_rrect(x, y, w, w, r, top, grad_to=th.board_b)
        edge = th.accent if state == "selected" else with_alpha(th.board_line, 230)
        img.stroke_rrect(x, y, w, w, r, edge, max(1.5, s * 0.06) if state == "selected" else max(1.0, s * 0.02))


def paint_letter_tile(img: Image, th: Any, lit: bool) -> None:
    s = float(min(img.w, img.h))
    p = max(1.0, s * 0.06)
    if lit:
        if th.glow:
            img.soft_rrect(p, p, img.w - 2 * p, img.h - 2 * p, s * 0.25, with_alpha(th.warn, 120), p * 2)
        img.fill_rrect(p, p, img.w - 2 * p, img.h - 2 * p, s * 0.25, lighten(th.warn, 0.15), grad_to=darken(th.warn, 0.1))
    else:
        img.fill_rrect(p, p, img.w - 2 * p, img.h - 2 * p, s * 0.25, th.panel_hi if th.dark else th.panel,
                       grad_to=th.panel)
        img.stroke_rrect(p, p, img.w - 2 * p, img.h - 2 * p, s * 0.25, with_alpha(th.edge, 200), 1)


def paint_icon(img: Image, theme: Any) -> None:
    s = float(min(img.w, img.h))
    img.fill_rrect(s * 0.08, s * 0.08, s * 0.84, s * 0.84, s * 0.18, theme.board_a, grad_to=theme.board_b)
    n, g = 4, s * 0.035
    cell = (s * 0.72 - g * (n - 1)) / n
    marked = {(0, 0), (1, 1), (2, 2), (3, 3), (0, 2), (3, 1)}
    main = theme.seat("blue")[0]
    for r in range(n):
        for c in range(n):
            x, y = s * 0.14 + c * (cell + g), s * 0.14 + r * (cell + g)
            if (r, c) in marked:
                img.fill_rrect(x, y, cell, cell, cell * 0.25, lighten(main, 0.1), grad_to=darken(main, 0.15))
            else:
                img.fill_rrect(x, y, cell, cell, cell * 0.25, theme.board_line)
    img.line(s * 0.16, s * 0.16, s * 0.84, s * 0.84, max(1.5, s * 0.05), theme.warn)


# ---------------------------------------------------------------------- view
class View(GameView):
    def __init__(self, canvas: Any, ctx: Any) -> None:
        super().__init__(canvas, ctx)
        ui = self.ui
        self.it = Items(canvas, "bgo_")
        self.w = self.h = 0
        self.geo: Optional[Tuple[float, float, float, float]] = None   # bx, by, cell, gap
        self.selected: Optional[int] = None
        self.hover: Optional[int] = None
        self.deadline: Optional[float] = None
        self.lit_shown = 0
        self.b_shuffle = widgets.Button(ui, canvas, "Shuffle", lambda: self.ctx.act(a="shuffle"), kind="secondary",
                                        icon="↻", font_px=14)
        self.b_ready = widgets.Button(ui, canvas, "I'm ready", lambda: self.ctx.act(a="ready"), kind="primary",
                                      font_px=14)
        canvas.tag_bind(TILE_TAG, "<ButtonRelease-1>", self._click)
        canvas.tag_bind(TILE_TAG, "<Motion>", self._motion)
        canvas.tag_bind(TILE_TAG, "<Leave>", lambda e: self._set_hover(None))
        self._tick()

    def destroy(self) -> None:
        self.b_shuffle.destroy()
        self.b_ready.destroy()
        for seq in ("<ButtonRelease-1>", "<Motion>", "<Leave>"):
            try:
                self.canvas.tag_unbind(TILE_TAG, seq)
            except Exception:
                pass
        self.it.clear()
        super().destroy()

    # ---------------------------------------------------------------- layout
    def on_resize(self, w: int, h: int) -> None:
        self.w, self.h = w, h
        ui = self.ui
        m = ui.px(12)
        strip_h = ui.px(56)
        letters_h = ui.px(38)
        bottom = ui.px(116)
        top = m + strip_h + ui.px(12) + letters_h + ui.px(8)
        bs = max(ui.px(150), min(w - 2 * m, h - top - bottom))
        gap = max(2.0, bs * 0.022)
        cell = (bs - 4 * gap) / 5.0
        self.geo = ((w - bs) / 2.0, top, cell, gap)
        self.it.clear()
        self.render(animate=False)

    def _cell_xy(self, i: int) -> Tuple[float, float]:
        bx, by, cell, gap = self.geo  # type: ignore[misc]
        r, c = divmod(i, 5)
        return bx + c * (cell + gap), by + r * (cell + gap)

    def _cell_at(self, x: float, y: float) -> Optional[int]:
        if self.geo is None:
            return None
        bx, by, cell, gap = self.geo
        c, r = int((x - bx) // (cell + gap)), int((y - by) // (cell + gap))
        if 0 <= c < 5 and 0 <= r < 5:
            return r * 5 + c
        return None

    # ---------------------------------------------------------------- render
    def on_state(self, state: Dict[str, Any]) -> None:
        old = self.state or {}
        self.state = state
        timer = state.get("timer")
        self.deadline = time.monotonic() + float(timer) if isinstance(timer, (int, float)) else None
        if state.get("phase") != "arrange" or (state.get("ready") or [False] * 8)[self.ctx.my_seat or 0]:
            self.selected = None
        self.render(animate=bool(old))

    def on_seats(self, seats: List[Dict[str, Any]]) -> None:
        super().on_seats(seats)
        self.render(animate=False)

    def on_event(self, event: Dict[str, Any]) -> None:
        kind = event.get("kind")
        if kind == "called" and self.geo is not None:
            self._flash_number(event.get("n"))

    def _my_board(self) -> List[int]:
        return list((self.state or {}).get("board") or [])

    def render(self, animate: bool = True) -> None:
        if self.geo is None or self._dead:
            return
        ui, th, st, ctx = self.ui, self.ui.theme, self.state or {}, self.ctx
        self._render_strip()
        bx, by, cell, gap = self.geo
        board = self._my_board()
        marks = st.get("marks") or [False] * 25
        my_color = ctx.seat_color(ctx.my_seat) if ctx.my_seat is not None else "blue"
        csz = int(cell + 0.5)
        num_font = ui.font(max(10, cell * 0.36 / ui.scale), bold=True, display=True)

        # letters
        my_lines = st.get("my_lines") or []
        lit = min(5, len(my_lines))
        lh = ui.px(38)
        for i, ch in enumerate(LETTERS):
            x, _y = self._cell_xy(i)
            on = i < lit
            ph = ui.sprites.get(("bgolet", th.key, on), csz, lh, lambda img, o=on: paint_letter_tile(img, th, o))
            self.it.image("let%d" % i, ph, x, by - lh - ui.px(8), anchor="nw")
            self.it.text("lett%d" % i, x + cell / 2, by - lh / 2 - ui.px(8), ch, ui.font(18, bold=True, display=True),
                         "#ffffff" if on else th.text_faint)
            if animate and on and i >= self.lit_shown:
                self._pop_letter(i, x + cell / 2, by - lh / 2 - ui.px(8))
        self.lit_shown = lit

        # tiles + numbers
        for i in range(25):
            x, y = self._cell_xy(i)
            if i < len(board):
                marked = bool(marks[i]) if i < len(marks) else False
                state = "marked" if marked else ("selected" if i == self.selected else
                                                 ("hover" if i == self.hover else "plain"))
                ph = ui.sprites.get(("bgotile", th.key, state, my_color), csz, csz,
                                    lambda img, s=state: paint_tile(img, th, s, my_color))
                self.it.image("tile%d" % i, ph, x, y, anchor="nw", extra_tags=(TILE_TAG,))
                self.it.text("num%d" % i, x + cell / 2, y + cell / 2, str(board[i]), num_font,
                             "#ffffff" if marked else th.text if not th.dark else "#232336")
            else:
                self.it.delete("tile%d" % i)
                self.it.delete("num%d" % i)
        if not board:
            self.it.text("spect", self.w / 2, by + cell * 2.5, "You're watching this game", ui.font(14, bold=True),
                         th.text_dim)

        # completed lines (between tiles and numbers)
        self.it.delete_prefix("line")
        for k in my_lines:
            if not isinstance(k, int) or not 0 <= k < len(LINES):
                continue
            cells = LINES[k]
            ax, ay = self._cell_xy(cells[0])
            zx, zy = self._cell_xy(cells[-1])
            item = self.canvas.create_line(ax + cell / 2, ay + cell / 2, zx + cell / 2, zy + cell / 2,
                                           width=max(3, ui.px(5)), capstyle="round", fill=th.warn,
                                           tags=("bgo_", "bgo_line%d" % k), state="disabled")
            self.it.ids["line%d" % k] = item
        for i in range(25):
            self.it.raise_("num%d" % i)
        self._render_controls()
        self._render_status()

    def _render_strip(self) -> None:
        ui, th, st, ctx = self.ui, self.ui.theme, self.state or {}, self.ctx
        n = max(1, len(ctx.seats))
        m, gap = ui.px(12), ui.px(8)
        tw = (self.w - 2 * m - gap * (n - 1)) / n
        th_h = ui.px(56)
        lines = st.get("lines") or [0] * n
        ready = st.get("ready") or [False] * n
        turn = st.get("turn")
        arrange = st.get("phase") == "arrange"
        av = ui.px(26)
        names = [s.get("name", "?") + (" (you)" if ctx.is_me(i) else "") for i, s in enumerate(ctx.seats)]
        size = 12.0
        while size > 9 and any(ui.measure(nm, ui.font(size, bold=True)) > tw - av - ui.px(20) for nm in names):
            size -= 0.5
        for i, seat in enumerate(ctx.seats):
            x = m + i * (tw + gap)
            active = (turn == i and not arrange and not st.get("winners"))
            main, light, dark = th.seat(seat.get("color", "red"))

            def paint(img: Image, a: bool = active, mc: str = main) -> None:
                p = max(2.0, img.h * 0.06)
                if a and th.glow:
                    img.soft_rrect(p, p, img.w - 2 * p, img.h - 2 * p, img.h * 0.25, with_alpha(mc, 120), p * 2)
                img.fill_rrect(p, p, img.w - 2 * p, img.h - 2 * p, img.h * 0.25, th.panel_hi if a else th.panel)
                img.stroke_rrect(p, p, img.w - 2 * p, img.h - 2 * p, img.h * 0.25,
                                 mc if a else with_alpha(th.edge, 220), max(1.5, ui.px(2)) if a else 1)

            ph = ui.sprites.get(("bgostrip", th.key, active, main), int(tw), th_h, paint)
            self.it.image("st_bg%d" % i, ph, x, m, anchor="nw")
            avp = ui.sprites.get(("avatar", main), av, av, lambda img, c=(main, light, dark): widgets.paint_avatar(img, *c))
            self.it.image("st_av%d" % i, avp, x + ui.px(8), m + ui.px(17), anchor="w")
            text, font = ui.fit(names[i], size, tw - av - ui.px(20), bold=True)
            self.it.text("st_n%d" % i, x + ui.px(12) + av, m + ui.px(17), text, font, th.text, anchor="w")
            self.it.text("st_i%d" % i, x + ui.px(8) + av / 2, m + ui.px(17), (seat.get("name") or "?")[:1].upper(),
                         ui.font(12, bold=True), "#ffffff")
            if arrange:
                label = "Ready" if (ready[i] if i < len(ready) else False) else "Arranging…"
                color = th.good if label == "Ready" else th.text_faint
                self.it.text("st_l%d" % i, x + tw / 2, m + ui.px(42), label, ui.font(11, bold=True), color)
            else:
                count = lines[i] if i < len(lines) else 0
                word = "".join(ch if k < count else "·" for k, ch in enumerate(LETTERS))
                self.it.text("st_l%d" % i, x + tw / 2, m + ui.px(42), " ".join(word),
                             ui.font(12, bold=True, display=True), th.warn if count else th.text_faint)
        for i in range(len(ctx.seats), 4):
            for part in ("st_bg", "st_av", "st_n", "st_i", "st_l"):
                self.it.delete("%s%d" % (part, i))

    def _render_controls(self) -> None:
        ui, st, ctx = self.ui, self.state or {}, self.ctx
        bx, by, cell, gap = self.geo  # type: ignore[misc]
        bs = 5 * cell + 4 * gap
        y = by + bs + ui.px(40)
        me = ctx.my_seat
        ready = st.get("ready") or []
        show = (st.get("phase") == "arrange" and me is not None and me < len(ready) and not ready[me])
        bw = (min(bs, self.w - ui.px(24)) - ui.px(10)) / 2
        self.b_shuffle.place(bx, y, bw, ui.px(42))
        self.b_ready.place(bx + bw + ui.px(10), y, bw, ui.px(42))
        self.b_shuffle.set_visible(show)
        self.b_ready.set_visible(show)

    def _status_text(self) -> Tuple[str, str]:
        st, ctx = self.state or {}, self.ctx
        left = ""
        if self.deadline is not None:
            left = "  ·  %ds" % max(0, int(self.deadline - time.monotonic() + 0.99))
        if st.get("winners"):
            names = [ctx.seat_name(s) for s in st["winners"]]
            return "BINGO! " + " and ".join(names), ""
        if st.get("phase") == "arrange":
            me = ctx.my_seat
            ready = st.get("ready") or []
            if me is not None and me < len(ready) and ready[me]:
                return "Waiting for the others" + left, ""
            return "Arrange your board" + left, "Tap two numbers to swap them"
        turn = st.get("turn")
        last = st.get("last_call") or {}
        hint = ""
        if last:
            who = "You" if ctx.is_me(last.get("seat")) else ctx.seat_name(last.get("seat", -1))
            hint = "%s called %s" % (who, last.get("n"))
        if turn is None:
            return "Get ready…", hint
        if ctx.is_me(turn):
            return "Your turn: pick a number" + left, hint
        return "%s is picking%s" % (ctx.seat_name(turn), left), hint

    def _render_status(self) -> None:
        if self.geo is None:
            return
        ui, th = self.ui, self.ui.theme
        bx, by, cell, gap = self.geo
        bs = 5 * cell + 4 * gap
        main, hint = self._status_text()
        text, font = ui.fit(main, 15, self.w - ui.px(24), bold=True)
        self.it.text("status", self.w / 2, by + bs + ui.px(14), text, font, th.text)
        st = self.state or {}
        if st.get("phase") != "arrange" or not self.b_ready.visible:
            called = st.get("called") or []
            tail = "  ".join(str(n) for n in called[-9:])
            line = hint if not called else "%s   ·   called: %s" % (hint, tail) if hint else "called: " + tail
            text, font = ui.fit(line, 12, self.w - ui.px(24))
            self.it.text("hint", self.w / 2, by + bs + ui.px(36), text, font, th.text_dim)
        else:
            text, font = ui.fit(hint, 12, self.w - ui.px(24))
            self.it.text("hint", self.w / 2, by + bs + ui.px(32), text, font, th.text_faint)
            self.it.move("hint", self.w / 2, by + bs + ui.px(90))

    def _tick(self) -> None:
        if self._dead:
            return
        if self.deadline is not None and self.geo is not None:
            self._render_status()
        self.after(250, self._tick)

    # ------------------------------------------------------------ animations
    def _pop_letter(self, i: int, cx: float, cy: float) -> None:
        name = "lett%d" % i
        ui = self.ui

        def step(t: float) -> None:
            if not self.it.has(name):
                return
            size = 18 * (1.0 + 0.6 * (1 - t))
            self.canvas.itemconfigure(self.it.ids[name], font=ui.font(round(size), bold=True, display=True))

        ui.anim.tween(0.45, step, "out_back", owner=self)

    def _flash_number(self, n: Any) -> None:
        board = self._my_board()
        if n not in board or self.geo is None:
            return
        i = board.index(n)
        x, y = self._cell_xy(i)
        bx, by, cell, gap = self.geo
        th, ui = self.ui.theme, self.ui
        cx, cy = x + cell / 2, y + cell / 2
        item = self.canvas.create_oval(cx, cy, cx, cy, outline=th.warn, width=max(2, ui.px(3)),
                                       tags=("bgo_", "bgo_flash"), state="disabled")

        def step(t: float) -> None:
            r = cell * (0.3 + 0.55 * t)
            try:
                self.canvas.coords(item, cx - r, cy - r, cx + r, cy + r)
                if t >= 1.0:
                    self.canvas.delete(item)
            except Exception:
                pass

        ui.anim.tween(0.45, step, "out_cubic", owner=self)

    # ----------------------------------------------------------------- input
    def _motion(self, e: Any) -> None:
        self._set_hover(self._cell_at(e.x, e.y))

    def _set_hover(self, i: Optional[int]) -> None:
        if i == self.hover:
            return
        self.hover = i
        st = self.state or {}
        clickable = i is not None and (st.get("phase") == "arrange" or
                                       (st.get("turn") is not None and self.ctx.is_me(st.get("turn"))))
        try:
            self.canvas.configure(cursor="hand2" if clickable else "")
        except Exception:
            pass
        self.render(animate=False)

    def _click(self, e: Any) -> None:
        i = self._cell_at(e.x, e.y)
        st, ctx = self.state or {}, self.ctx
        board = self._my_board()
        if i is None or i >= len(board) or ctx.my_seat is None or st.get("winners"):
            return
        if st.get("phase") == "arrange":
            ready = st.get("ready") or []
            if ctx.my_seat < len(ready) and ready[ctx.my_seat]:
                ctx.toast("You're ready. Waiting for the others")
                return
            if self.selected is None:
                self.selected = i
            elif self.selected == i:
                self.selected = None
            else:
                ctx.act(a="swap", i=self.selected, j=i)
                self.selected = None
            self.render(animate=False)
            return
        marks = st.get("marks") or [False] * 25
        if marks[i]:
            return
        if not ctx.is_me(st.get("turn")):
            ctx.toast("Wait for your turn")
            return
        ctx.act(a="call", n=board[i])

    def on_key(self, keysym: str) -> None:
        st = self.state or {}
        if st.get("phase") == "arrange" and self.b_ready.visible:
            if keysym in ("s", "S"):
                self.ctx.act(a="shuffle")
            elif keysym in ("r", "R", "Return"):
                self.ctx.act(a="ready")
