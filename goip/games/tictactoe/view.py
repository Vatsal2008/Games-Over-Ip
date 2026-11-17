"""Tic-Tac-Toe view: two player chips, a glossy 3x3 board, glowing X and O."""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple

from ... import widgets
from ...gfx import Image, darken, lighten, with_alpha
from ..viewbase import GameView
from ..viewkit import Items

BOARD_TAG = "tttboard"


# ------------------------------------------------------------------ painters
def paint_mark(img: Image, kind: str, color: str, glow: bool, ghost: bool = False) -> None:
    s = float(min(img.w, img.h))
    c = s / 2.0
    alpha = 90 if ghost else 255
    col = with_alpha(color, alpha)
    if kind == "x":
        m, w = s * 0.24, s * 0.13
        if glow and not ghost:
            for a, b, cc, d in ((m, m, s - m, s - m), (s - m, m, m, s - m)):
                img.line(a, b, cc, d, w * 2.4, with_alpha(color, 45))
        if not ghost:
            for a, b, cc, d in ((m, m, s - m, s - m), (s - m, m, m, s - m)):
                img.line(a, b + w * 0.25, cc, d + w * 0.25, w, with_alpha(darken(color, 0.45), 160))
        img.line(m, m, s - m, s - m, w, col)
        img.line(s - m, m, m, s - m, w, col)
        if not ghost:
            img.line(m + w * 0.1, m - w * 0.12, s - m - w * 0.6, s - m - w * 0.8, w * 0.28,
                     with_alpha(lighten(color, 0.55), 120))
    else:
        r, w = s * 0.25, s * 0.12
        if glow and not ghost:
            img.stroke_circle(c, c, r + w * 1.7, with_alpha(color, 45), w * 2.4)
        if not ghost:
            img.stroke_circle(c, c + w * 0.25, r + w / 2, with_alpha(darken(color, 0.45), 160), w)
        img.stroke_circle(c, c, r + w / 2, col, w)
        if not ghost:
            img.stroke_circle(c, c, r + w * 0.15, with_alpha(lighten(color, 0.55), 90), w * 0.25)


def paint_board(img: Image, th: Any) -> None:
    s = float(min(img.w, img.h))
    pad = s * 0.035
    r = s * 0.07
    img.soft_rrect(pad, pad * 1.6, s - 2 * pad, s - 2 * pad, r, with_alpha(th.shadow, 120 if th.dark else 40), pad * 1.4)
    img.fill_rrect(pad, pad, s - 2 * pad, s - 2 * pad, r, th.board_a, grad_to=th.board_b)
    img.stroke_rrect(pad, pad, s - 2 * pad, s - 2 * pad, r, with_alpha(th.board_line, 200), max(1.0, s * 0.004))
    inner = s - 2 * pad
    cell = inner / 3.0
    lw = max(2.0, s * 0.018)
    m = cell * 0.14
    for i in (1, 2):
        p = pad + cell * i
        img.line(p, pad + m, p, s - pad - m, lw, th.board_line)
        img.line(pad + m, p, s - pad - m, p, lw, th.board_line)


def paint_icon(img: Image, theme: Any) -> None:
    s = float(min(img.w, img.h))
    img.fill_rrect(s * 0.08, s * 0.08, s * 0.84, s * 0.84, s * 0.2, theme.board_a, grad_to=theme.board_b)
    cell = s * 0.84 / 3
    lw = max(1.2, s * 0.035)
    for i in (1, 2):
        p = s * 0.08 + cell * i
        img.line(p, s * 0.16, p, s * 0.84, lw, theme.board_line)
        img.line(s * 0.16, p, s * 0.84, p, lw, theme.board_line)
    red, blue = theme.seat("red")[0], theme.seat("blue")[0]
    sub = Image(int(cell), int(cell))
    paint_mark(sub, "x", red, False)
    img.blit(sub, int(s * 0.08), int(s * 0.08))
    img.blit(sub, int(s * 0.08 + 2 * cell), int(s * 0.08 + 2 * cell))
    sub = Image(int(cell), int(cell))
    paint_mark(sub, "o", blue, False)
    img.blit(sub, int(s * 0.08 + cell), int(s * 0.08 + cell))


# ---------------------------------------------------------------------- view
class View(GameView):
    def __init__(self, canvas: Any, ctx: Any) -> None:
        super().__init__(canvas, ctx)
        self.it = Items(canvas, "ttt_")
        self.chips = [widgets.PlayerChip(self.ui, canvas), widgets.PlayerChip(self.ui, canvas)]
        self.drawn: Dict[int, int] = {}
        self.line_key: Optional[Tuple[int, ...]] = None
        self.hover: Optional[int] = None
        self.geo: Optional[Tuple[float, float, float]] = None
        self.w = self.h = 0
        canvas.tag_bind(BOARD_TAG, "<Motion>", self._motion)
        canvas.tag_bind(BOARD_TAG, "<Leave>", lambda e: self._set_hover(None))
        canvas.tag_bind(BOARD_TAG, "<ButtonRelease-1>", self._click)

    def destroy(self) -> None:
        for chip in self.chips:
            chip.destroy()
        try:
            for seq in ("<Motion>", "<Leave>", "<ButtonRelease-1>"):
                self.canvas.tag_unbind(BOARD_TAG, seq)
        except Exception:
            pass
        self.it.clear()
        super().destroy()

    # ---------------------------------------------------------------- layout
    def on_resize(self, w: int, h: int) -> None:
        self.w, self.h = w, h
        ui = self.ui
        m, gap = ui.px(14), ui.px(10)
        chip_h = ui.px(52)
        cw = (w - 2 * m - gap) / 2
        for i, chip in enumerate(self.chips):
            chip.place(m + i * (cw + gap), m, cw, chip_h)
        top = m + chip_h + ui.px(62)
        bs = max(ui.px(120), min(w - 2 * m, h - top - ui.px(44)))
        self.geo = ((w - bs) / 2.0, top, float(bs))
        self.drawn = {}
        self.it.delete_prefix("mark")
        self.line_key = None
        self.it.delete("winline")
        self.render(animate=False)

    def _cell_box(self, cell: int) -> Tuple[float, float, float]:
        bx, by, bs = self.geo  # type: ignore[misc]
        pad = bs * 0.035
        size = (bs - 2 * pad) / 3.0
        r, c = divmod(cell, 3)
        return bx + pad + c * size + size / 2, by + pad + r * size + size / 2, size

    def _cell_at(self, x: float, y: float) -> Optional[int]:
        if self.geo is None:
            return None
        bx, by, bs = self.geo
        pad = bs * 0.035
        size = (bs - 2 * pad) / 3.0
        c, r = int((x - bx - pad) // size), int((y - by - pad) // size)
        if 0 <= c < 3 and 0 <= r < 3:
            return r * 3 + c
        return None

    # ---------------------------------------------------------------- render
    def on_state(self, state: Dict[str, Any]) -> None:
        self.state = state
        self.render(animate=True)

    def on_seats(self, seats: List[Dict[str, Any]]) -> None:
        super().on_seats(seats)
        self.render(animate=False)

    def _mark_photo(self, owner: int, size: int, ghost: bool = False) -> Any:
        th = self.ui.theme
        color = th.seat(self.ctx.seat_color(owner))[0]
        kind = "x" if owner == 0 else "o"
        return self.ui.sprites.get(("tttmark", th.key, kind, color, ghost), size, size,
                                   lambda img: paint_mark(img, kind, color, th.glow, ghost))

    def render(self, animate: bool = True) -> None:
        if self.geo is None or self._dead:
            return
        ui, th, st, ctx = self.ui, self.ui.theme, self.state or {}, self.ctx
        bx, by, bs = self.geo
        board_photo = ui.sprites.get(("tttboard", th.key), int(bs), int(bs), lambda img: paint_board(img, th))
        self.it.image("board", board_photo, bx, by, anchor="nw", extra_tags=(BOARD_TAG,))
        self.canvas.tag_lower(self.it.ids["board"])
        self.canvas.tag_lower("bg")

        scores = st.get("scores") or [0, 0]
        phase = st.get("phase", "play")
        turn = st.get("turn")
        for i, chip in enumerate(self.chips):
            seat = ctx.seats[i] if i < len(ctx.seats) else {}
            wins = scores[i] if i < len(scores) else 0
            active = turn == i and phase == "play" and not st.get("match_over")
            chip.update(seat.get("name", "?"), seat.get("color", "red"), bot=bool(seat.get("bot")), active=active,
                        subtitle="%s  ·  %d win%s" % ("X" if i == 0 else "O", wins, "" if wins == 1 else "s"),
                        timer=st.get("timer") if active else None, me=ctx.is_me(i))

        board = st.get("board") or [None] * 9
        size = int(self._cell_box(0)[2] * 0.86)
        for cell in range(9):
            owner = board[cell]
            name = "mark%d" % cell
            if owner is None:
                if cell in self.drawn:
                    self.it.delete(name)
                    del self.drawn[cell]
                continue
            if self.drawn.get(cell) == owner and self.it.has(name):
                continue
            self.drawn[cell] = owner
            cx, cy, _s = self._cell_box(cell)
            if animate:
                self._pop(name, owner, size, cx, cy)
            else:
                self.it.image(name, self._mark_photo(owner, size), cx, cy, extra_tags=(BOARD_TAG,))

        line = st.get("line")
        key = tuple(line) if line else None
        if key != self.line_key:
            self.line_key = key
            self.it.delete("winline")
            if key:
                self._draw_line(key, st.get("round_winner"), animate)
        if key:
            self.it.raise_("winline")

        self._render_texts()
        self._set_hover(self.hover)

    def _pop(self, name: str, owner: int, size: int, cx: float, cy: float) -> None:
        ui = self.ui
        self.it.image(name, self._mark_photo(owner, max(4, int(size * 0.4))), cx, cy, extra_tags=(BOARD_TAG,))

        def step(t: float) -> None:
            if self._dead or not self.it.has(name):
                return
            k = round((0.4 + 0.6 * t) * 20) / 20.0
            self.it.image(name, self._mark_photo(owner, max(4, int(size * k))), cx, cy)

        ui.anim.tween(0.28, step, "out_back", owner=self)

    def _draw_line(self, cells: Tuple[int, ...], winner: Optional[int], animate: bool) -> None:
        a = self._cell_box(cells[0])
        b = self._cell_box(cells[-1])
        dx, dy = b[0] - a[0], b[1] - a[1]
        ext = 0.32
        x1, y1 = a[0] - dx * ext / 2, a[1] - dy * ext / 2
        x2, y2 = b[0] + dx * ext / 2, b[1] + dy * ext / 2
        th = self.ui.theme
        color = th.seat(self.ctx.seat_color(winner))[0] if isinstance(winner, int) else th.text_dim
        item = self.canvas.create_line(x1, y1, x1, y1, width=self.ui.px(8), capstyle="round", fill=lighten(color, 0.1),
                                       tags=("ttt_", "ttt_winline", BOARD_TAG))
        self.it.ids["winline"] = item

        def step(t: float) -> None:
            if self._dead or self.it.ids.get("winline") != item:
                return
            self.canvas.coords(item, x1, y1, x1 + (x2 - x1) * t, y1 + (y2 - y1) * t)

        if animate:
            self.ui.anim.tween(0.35, step, "out_cubic", owner=self)
        else:
            step(1.0)

    def _render_texts(self) -> None:
        ui, th, st, ctx = self.ui, self.ui.theme, self.state or {}, self.ctx
        if self.geo is None:
            return
        bx, by, bs = self.geo
        turn, phase = st.get("turn"), st.get("phase", "play")
        if st.get("match_over") or st.get("finished"):
            status = "Match over"
        elif phase == "round_over":
            rw = st.get("round_winner")
            if rw is None:
                status = "Draw. Nobody takes this round"
            elif ctx.is_me(rw):
                status = "You win the round"
            else:
                status = "%s wins the round" % ctx.seat_name(rw)
        elif turn is None:
            status = "Get ready…"
        elif ctx.is_me(turn):
            status = "Your turn: place your %s" % ("X" if turn == 0 else "O")
        elif ctx.seats and turn < len(ctx.seats) and ctx.seats[turn].get("bot"):
            status = "%s is thinking…" % ctx.seat_name(turn)
        else:
            status = "Waiting for %s…" % ctx.seat_name(turn)
        text, font = ui.fit(status, 17, self.w - ui.px(28), bold=True)
        self.it.text("status", self.w / 2, by - ui.px(36), text, font, th.text)
        best = st.get("best_of", 3)
        sub = "Round %d  ·  best of %d" % (st.get("round", 1), best) if best > 1 else "One round, winner takes all"
        if st.get("draws"):
            sub += "  ·  %d draw%s" % (st["draws"], "" if st["draws"] == 1 else "s")
        self.it.text("sub", self.w / 2, by - ui.px(14), sub, ui.font(12), th.text_dim)

    # ----------------------------------------------------------------- input
    def _my_turn(self) -> bool:
        st = self.state or {}
        return (st.get("phase") == "play" and not st.get("match_over") and st.get("turn") is not None
                and self.ctx.is_me(st.get("turn")))

    def _motion(self, e: Any) -> None:
        self._set_hover(self._cell_at(e.x, e.y))

    def _set_hover(self, cell: Optional[int]) -> None:
        self.hover = cell
        st = self.state or {}
        board = st.get("board") or [None] * 9
        if cell is None or not self._my_turn() or board[cell] is not None or self.geo is None:
            self.it.hide("ghost")
            try:
                self.canvas.configure(cursor="")
            except Exception:
                pass
            return
        cx, cy, size = self._cell_box(cell)
        self.it.image("ghost", self._mark_photo(self.ctx.my_seat, int(size * 0.86), ghost=True), cx, cy,
                      extra_tags=(BOARD_TAG,))
        try:
            self.canvas.configure(cursor="hand2")
        except Exception:
            pass

    def _click(self, e: Any) -> None:
        self._place(self._cell_at(e.x, e.y))

    def _place(self, cell: Optional[int]) -> None:
        st = self.state or {}
        if cell is None or self.ctx.my_seat is None or not st:
            return
        if st.get("phase") != "play" or st.get("match_over"):
            return
        if not self._my_turn():
            self.ctx.toast("Wait for your turn")
            return
        if (st.get("board") or [None] * 9)[cell] is not None:
            return
        self.it.hide("ghost")
        self.ctx.act(a="place", cell=cell)

    def on_key(self, keysym: str) -> None:
        if keysym in tuple("123456789"):
            self._place(int(keysym) - 1)
        elif keysym.startswith("KP_") and keysym[3:].isdigit() and keysym[3:] != "0":
            n = int(keysym[3:])
            # numeric keypad mirrors the board: 7 8 9 on top
            self._place({7: 0, 8: 1, 9: 2, 4: 3, 5: 4, 6: 5, 1: 6, 2: 7, 3: 8}[n])
