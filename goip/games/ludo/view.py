"""Ludo view: classic 15x15 cross board, glossy pawns, dice between the player corners."""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ... import widgets
from ...gfx import Image, darken, lighten, with_alpha
from ..viewbase import GameView
from ..viewkit import Items, hop_path
from .logic import HOME, START_OFFSET, STAR_SQUARES, TRACK_LEN

Point = Tuple[float, float]

# Track squares as (row, col), absolute square 0 = red's start, clockwise.
LOOP: List[Tuple[int, int]] = (
    [(r, 6) for r in range(13, 8, -1)] + [(8, c) for c in range(5, -1, -1)] + [(7, 0), (6, 0)]
    + [(6, c) for c in range(1, 6)] + [(r, 6) for r in range(5, -1, -1)] + [(0, 7), (0, 8)]
    + [(r, 8) for r in range(1, 6)] + [(6, c) for c in range(9, 15)] + [(7, 14), (8, 14)]
    + [(8, c) for c in range(13, 8, -1)] + [(r, 8) for r in range(9, 15)] + [(14, 7), (14, 6)]
)
assert len(LOOP) == TRACK_LEN
HOME_COLUMN: Dict[str, List[Tuple[int, int]]] = {
    "red": [(r, 7) for r in range(13, 8, -1)],
    "green": [(7, c) for c in range(1, 6)],
    "yellow": [(r, 7) for r in range(1, 6)],
    "blue": [(7, c) for c in range(13, 8, -1)],
}
BASE_ORIGIN = {"red": (9, 0), "green": (0, 0), "yellow": (0, 9), "blue": (9, 9)}   # (row, col)
HOME_CENTROID = {"red": (7.5, 8.55), "green": (6.45, 7.5), "yellow": (7.5, 6.45), "blue": (8.55, 7.5)}  # (x, y)
TRIANGLES = {"red": [(6, 9), (9, 9)], "green": [(6, 6), (6, 9)], "yellow": [(6, 6), (9, 6)], "blue": [(9, 6), (9, 9)]}
CORNER = {"green": (0, 0), "yellow": (0, 1), "red": (1, 0), "blue": (1, 1)}         # chip slot (row, col)
STEP_TIME = 0.15


def spot(color: str, progress: int, token: int) -> Point:
    """Centre of a token in board-cell units (x, y), 0..15."""
    if progress < 0:
        r0, c0 = BASE_ORIGIN[color]
        dx, dy = ((2.0, 2.0), (4.0, 2.0), (2.0, 4.0), (4.0, 4.0))[token % 4]
        return c0 + dx, r0 + dy
    if progress >= HOME:
        cx, cy = HOME_CENTROID[color]
        off = ((-0.28, -0.18), (0.28, -0.18), (-0.28, 0.22), (0.28, 0.22))[token % 4]
        return cx + off[0], cy + off[1]
    if progress > 50:
        r, c = HOME_COLUMN[color][progress - 51]
    else:
        r, c = LOOP[(progress + START_OFFSET[color]) % TRACK_LEN]
    return c + 0.5, r + 0.5


def star_points(cx: float, cy: float, r: float) -> List[Point]:
    pts = []
    for i in range(10):
        ang = -math.pi / 2 + i * math.pi / 5
        rr = r if i % 2 == 0 else r * 0.45
        pts.append((cx + rr * math.cos(ang), cy + rr * math.sin(ang)))
    return pts


# ----------------------------------------------------------------- painters
def paint_board(img: Image, th: Any, colors: Sequence[str]) -> None:
    s = float(min(img.w, img.h))
    u = s / 15.0
    img.fill_rect(0, 0, s, s, th.board_line)
    img.fill_rect(1, 1, s - 2, s - 2, th.board_a)
    inset = max(0.6, u * 0.05)
    for r, c in LOOP:
        img.fill_rect(c * u, r * u, u, u, th.board_line)
        img.fill_rect(c * u + inset, r * u + inset, u - 2 * inset, u - 2 * inset, th.board_a)
    for color, cells in HOME_COLUMN.items():
        main, light, dark = th.seat(color)
        for r, c in cells:
            img.fill_rect(c * u, r * u, u, u, th.board_line)
            img.fill_rect(c * u + inset, r * u + inset, u - 2 * inset, u - 2 * inset, lighten(main, 0.08))
    for color, off in START_OFFSET.items():
        r, c = LOOP[off]
        main = th.seat(color)[0]
        img.fill_rect(c * u + inset, r * u + inset, u - 2 * inset, u - 2 * inset, main)
        img.fill_polygon(star_points((c + 0.5) * u, (r + 0.5) * u, u * 0.33), with_alpha("#ffffff", 200))
    for sq in STAR_SQUARES:
        r, c = LOOP[sq]
        img.fill_polygon(star_points((c + 0.5) * u, (r + 0.5) * u, u * 0.36), with_alpha(th.board_line, 255))
    for color, (r0, c0) in BASE_ORIGIN.items():
        main, light, dark = th.seat(color)
        x, y = c0 * u, r0 * u
        img.fill_rect(x, y, 6 * u, 6 * u, main)
        img.fill_rrect(x + u * 0.12, y + u * 0.12, 6 * u - u * 0.24, 6 * u - u * 0.24, u * 0.5, lighten(main, 0.06),
                       grad_to=darken(main, 0.1))
        img.fill_rrect(x + u * 0.9, y + u * 0.9, u * 4.2, u * 4.2, u * 0.6, th.board_a)
        if color not in colors:
            img.fill_rrect(x + u * 0.9, y + u * 0.9, u * 4.2, u * 4.2, u * 0.6, with_alpha(main, 40))
        for k in range(4):
            sx, sy = spot(color, -1, k)
            img.fill_circle(sx * u, sy * u + u * 0.06, u * 0.62, with_alpha(dark, 90))
            img.fill_circle(sx * u, sy * u, u * 0.6, lighten(main, 0.35))
            img.stroke_circle(sx * u, sy * u, u * 0.6, dark, max(1.0, u * 0.07))
    for color, (a, b) in TRIANGLES.items():
        main, light, dark = th.seat(color)
        pts = [(a[0] * u, a[1] * u), (b[0] * u, b[1] * u), (7.5 * u, 7.5 * u)]
        img.fill_polygon(pts, main)
    for color, (a, b) in TRIANGLES.items():
        img.line(a[0] * u, a[1] * u, 7.5 * u, 7.5 * u, max(1.0, u * 0.05), with_alpha("#ffffff", 120))


def paint_icon(img: Image, theme: Any) -> None:
    s = float(min(img.w, img.h))
    o, q = s * 0.08, s * 0.84
    img.fill_rrect(o, o, q, q, s * 0.16, theme.board_a)
    h = q * 0.4
    for color, (rx, ry) in (("green", (0, 0)), ("yellow", (1, 0)), ("red", (0, 1)), ("blue", (1, 1))):
        main = theme.seat(color)[0]
        x, y = o + rx * (q - h), o + ry * (q - h)
        img.fill_rrect(x, y, h, h, s * 0.1, lighten(main, 0.06), grad_to=darken(main, 0.1))
        img.fill_circle(x + h / 2, y + h / 2, h * 0.24, theme.board_a)
    c = s / 2
    t = q * 0.1
    for color, pts in (("yellow", [(c - t, c - t), (c + t, c - t)]), ("blue", [(c + t, c - t), (c + t, c + t)]),
                       ("red", [(c + t, c + t), (c - t, c + t)]), ("green", [(c - t, c + t), (c - t, c - t)])):
        img.fill_polygon(pts + [(c, c)], theme.seat(color)[0])
    pawn = Image(int(s * 0.46), int(s * 0.46))
    widgets.paint_token(pawn, *theme.seat("red"), style="pawn")
    img.blit(pawn, int(s * 0.5), int(s * 0.44))


def paint_ring(img: Image, color: str, glow: bool) -> None:
    c = img.w / 2.0
    r = img.w * 0.36
    if glow:
        img.soft_circle(c, c, r, with_alpha(color, 120), img.w * 0.18)
    img.stroke_circle(c, c, r, with_alpha("#ffffff", 230), max(1.5, img.w * 0.07))
    img.stroke_circle(c, c, r - img.w * 0.07, with_alpha(color, 230), max(1.2, img.w * 0.05))


# ---------------------------------------------------------------------- view
class View(GameView):
    def __init__(self, canvas: Any, ctx: Any) -> None:
        super().__init__(canvas, ctx)
        ui = self.ui
        self.it = Items(canvas, "ludo_")
        self.chips: Dict[int, widgets.PlayerChip] = {}
        self.dice = widgets.Dice(ui, canvas, ui.px(52))
        self.dice.bind_click(self.roll)
        self.w = self.h = 0
        self.geo: Optional[Tuple[float, float, float]] = None   # bx, by, unit
        self.tokens: List[List[int]] = []
        self.queue: List[Dict[str, Any]] = []
        self.busy = False
        self.moving: Optional[Tuple[int, int]] = None
        self.message = ""
        self.bob = 0.0
        self._bobbing = False
        self._board_key: Any = None
        canvas.tag_bind("ludoclick", "<ButtonRelease-1>", self._click)

    def destroy(self) -> None:
        for chip in self.chips.values():
            chip.destroy()
        self.dice.destroy()
        try:
            self.canvas.tag_unbind("ludoclick", "<ButtonRelease-1>")
        except Exception:
            pass
        self.it.clear()
        super().destroy()

    def color_of(self, seat: int) -> str:
        cols = (self.state or {}).get("colors") or []
        return cols[seat] if seat < len(cols) else self.ctx.seat_color(seat)

    # ---------------------------------------------------------------- layout
    def on_resize(self, w: int, h: int) -> None:
        self.w, self.h = w, h
        ui = self.ui
        m, gap = ui.px(10), ui.px(6)
        status_h, chip_h, dice_w = ui.px(28), ui.px(44), ui.px(76)
        top = m + status_h + chip_h + gap
        bs = max(ui.px(150), min(w - 2 * m, h - top - gap - chip_h - ui.px(8) - m))
        bx = (w - bs) / 2.0
        self.geo = (bx, top, bs / 15.0)
        cw = (w - 2 * m - dice_w - 2 * gap) / 2
        self._chip_rects = {}
        for color, (row, col) in CORNER.items():
            y = m + status_h if row == 0 else top + bs + gap
            x = m if col == 0 else w - m - cw
            self._chip_rects[color] = (x, y, cw, chip_h)
        self.dice.size = ui.px(52)
        self.dice.place(w / 2, top + bs + gap + chip_h / 2)
        self._board_key = None
        self.render()

    def xy(self, color: str, progress: int, token: int) -> Point:
        bx, by, u = self.geo  # type: ignore[misc]
        x, y = spot(color, progress, token)
        return bx + x * u, by + y * u

    # ---------------------------------------------------------------- render
    def on_state(self, state: Dict[str, Any]) -> None:
        self.state = state
        if not self.busy:
            self.tokens = [list(t) for t in state.get("tokens") or []]
        self.render()

    def on_seats(self, seats: List[Dict[str, Any]]) -> None:
        super().on_seats(seats)
        self.render()

    def render(self) -> None:
        if self.geo is None or self._dead:
            return
        ui, th, st = self.ui, self.ui.theme, self.state or {}
        bx, by, u = self.geo
        colors = tuple(st.get("colors") or [self.ctx.seat_color(i) for i in range(len(self.ctx.seats))])
        key = (th.key, int(u * 15), colors)
        if key != self._board_key:
            self._board_key = key
            size = int(u * 15)
            photo = ui.sprites.get(("ludoboard",) + key, size, size, lambda img: paint_board(img, th, colors))
            self.it.image("board", photo, bx, by, anchor="nw", extra_tags=("ludoclick",))
            self.canvas.tag_lower(self.it.ids["board"])
            self.canvas.tag_lower("bg")
        self._render_tokens()
        self._render_chips()
        self._render_status()

    def _pawn(self, color: str, size: int) -> Any:
        th = self.ui.theme
        return self.ui.sprites.get(("pawn", th.key, color), size, size,
                                   lambda img: widgets.paint_token(img, *th.seat(color), style="pawn"))

    def _movable(self) -> List[int]:
        st = self.state or {}
        if (self.busy or st.get("phase") != "move" or st.get("auto") or st.get("turn") is None
                or not self.ctx.is_me(st.get("turn"))):
            return []
        return [k for k in st.get("movable") or [] if isinstance(k, int)]

    def _render_tokens(self) -> None:
        if self.geo is None:
            return
        bx, by, u = self.geo
        th = self.ui.theme
        groups: Dict[Tuple[float, float], List[Tuple[int, int]]] = {}
        for seat, row in enumerate(self.tokens):
            color = self.color_of(seat)
            for k, p in enumerate(row):
                if self.moving == (seat, k):
                    continue
                groups.setdefault(spot(color, p, k), []).append((seat, k))
        movable = self._movable()
        turn = (self.state or {}).get("turn")
        self.it.delete_prefix("ring")
        drawn = []
        for (sx, sy), members in sorted(groups.items(), key=lambda kv: kv[0][1]):
            shared = len(members) > 1
            for j, (seat, k) in enumerate(members):
                size = int(u * (1.0 if shared else 1.25))
                ox = (j - (len(members) - 1) / 2.0) * u * 0.28 if shared else 0.0
                x, y = bx + sx * u + ox, by + sy * u
                lift = self.bob if (seat == turn and k in movable) else 0.0
                if seat == turn and k in movable:
                    main = th.seat(self.color_of(seat))[0]
                    ring = self.ui.sprites.get(("ludoring", th.key, main), int(u * 1.3), int(u * 1.3),
                                               lambda img, c=main: paint_ring(img, c, th.glow))
                    self.it.image("ring%d_%d" % (seat, k), ring, x, y, extra_tags=("ludoclick",))
                self.it.image("tok%d_%d" % (seat, k), self._pawn(self.color_of(seat), size), x, y - u * 0.3 - lift,
                              extra_tags=("ludoclick",))
                drawn.append("tok%d_%d" % (seat, k))
        for name in drawn:
            self.it.raise_(name)
        if self.moving is not None:
            self.it.raise_("tok%d_%d" % self.moving)
        if movable and not self._bobbing:
            self._start_bob()
        try:
            self.canvas.configure(cursor="hand2" if movable else "")
        except Exception:
            pass

    def _start_bob(self) -> None:
        self._bobbing = True

        def step(t: float) -> None:
            if self.geo is None:
                return
            self.bob = abs(math.sin(t * math.pi)) * self.geo[2] * 0.18
            self._reposition_movable()

        def again() -> None:
            if self._movable() and not self._dead:
                self.ui.anim.tween(0.7, step, "linear", done=again, owner=self)
            else:
                self._bobbing = False
                self.bob = 0.0

        self.ui.anim.tween(0.7, step, "linear", done=again, owner=self)

    def _reposition_movable(self) -> None:
        st = self.state or {}
        turn = st.get("turn")
        if turn is None or self.geo is None or turn >= len(self.tokens):
            return
        bx, by, u = self.geo
        color = self.color_of(turn)
        positions = [spot(color, p, k) for k, p in enumerate(self.tokens[turn])]
        for k in self._movable():
            sx, sy = positions[k]
            same = [j for j, q in enumerate(positions) if q == (sx, sy)]
            if len(same) > 1:
                continue
            self.it.move("tok%d_%d" % (turn, k), bx + sx * u, by + sy * u - u * 0.3 - self.bob)

    def _render_chips(self) -> None:
        st, ctx = self.state or {}, self.ctx
        turn = st.get("turn")
        homes = st.get("home_count") or []
        goal = st.get("goal", 4)
        present = set()
        for seat, info in enumerate(ctx.seats):
            color = self.color_of(seat)
            present.add(color)
            chip = self.chips.get(seat)
            if chip is None:
                chip = self.chips[seat] = widgets.PlayerChip(self.ui, self.canvas)
            x, y, cw, ch = self._chip_rects.get(color, (0, 0, 10, 10))
            chip.place(x, y, cw, ch)
            active = turn == seat and not st.get("finished")
            home = homes[seat] if seat < len(homes) else 0
            chip.update(info.get("name", "?"), color, bot=bool(info.get("bot")), active=active,
                        subtitle="%d/%d home" % (home, goal), timer=st.get("timer") if active else None,
                        me=ctx.is_me(seat))

    def _render_status(self) -> None:
        ui, th, st, ctx = self.ui, self.ui.theme, self.state or {}, self.ctx
        turn = st.get("turn")
        if st.get("finished") or st.get("winner") is not None:
            win = st.get("winner")
            main = "You win" if ctx.is_me(win) else "%s wins" % ctx.seat_name(win) if win is not None else "Game over"
        elif turn is None:
            main = "Get ready…"
        elif ctx.is_me(turn):
            if st.get("phase") == "move" and not st.get("auto"):
                main = "Pick a token to move"
            elif st.get("phase") == "move":
                main = "Moving…"
            else:
                main = "Your turn: roll" + (" again" if st.get("streak") else "")
        elif ctx.seats and turn < len(ctx.seats) and ctx.seats[turn].get("bot"):
            main = "%s is thinking…" % ctx.seat_name(turn)
        else:
            main = "%s's turn" % ctx.seat_name(turn)
        line = main + ("   ·   " + self.message if self.message else "")
        text, font = ui.fit(line, 14, self.w - ui.px(20), bold=True)
        self.it.text("status", self.w / 2, ui.px(10) + ui.px(14), text, font, th.text)
        mine = (turn is not None and ctx.is_me(turn) and st.get("phase") == "roll" and not self.busy
                and not st.get("finished"))
        self.dice.set_active(mine)
        if turn is not None and not self.busy:
            self.dice.set_color(self.color_of(turn))

    # ------------------------------------------------------------ animation
    def on_event(self, event: Dict[str, Any]) -> None:
        if event.get("kind") in ("roll", "move", "skip", "home"):
            self.queue.append(event)
            if len(self.queue) > 6:
                self.queue = []
                self.busy = False
                self.moving = None
                self.tokens = [list(t) for t in (self.state or {}).get("tokens") or self.tokens]
                self.render()
                return
            if not self.busy:
                self._next()

    def _next(self) -> None:
        if self._dead:
            return
        if not self.queue:
            self.busy = False
            self.moving = None
            self.tokens = [list(t) for t in (self.state or {}).get("tokens") or self.tokens]
            self.render()
            return
        self.busy = True
        ev = self.queue.pop(0)
        kind = ev.get("kind")
        seat = ev.get("seat", 0)
        who = "You" if self.ctx.is_me(seat) else self.ctx.seat_name(seat)
        if kind == "roll":
            value = int(ev.get("value", 1))
            self.message = "%s rolled %d" % (who, value) + (", roll again" if value == 6 else "")
            self.dice.set_color(self.color_of(seat))
            self.dice.set_active(False)
            self._render_status()
            self.dice.roll(value, done=self._next)
        elif kind == "skip":
            self.message = "Three 6s, turn lost" if ev.get("reason") == "three_sixes" else "No move for %s" % who
            self._render_status()
            self.after(250, self._next)
        elif kind == "home":
            self.message = "%s got a token home" % who
            self._render_status()
            if self.geo is not None:
                bx, by, u = self.geo
                widgets.confetti(self.ui, self.canvas, bx + 7.5 * u, by + 7.5 * u, count=30)
            self._next()
        else:
            self._animate_move(ev)

    def _animate_move(self, ev: Dict[str, Any]) -> None:
        seat, k = int(ev.get("seat", 0)), int(ev.get("token", 0))
        path = [int(p) for p in ev.get("path") or []]
        start = int(ev.get("from", -1))
        color = self.color_of(seat)
        while len(self.tokens) <= seat:
            self.tokens.append([-1, -1, -1, -1])
        if not path or self.geo is None:
            self._finish_move(ev)
            return
        u = self.geo[2]
        self.moving = (seat, k)
        self._render_tokens()
        name = "tok%d_%d" % (seat, k)
        pts = [self.xy(color, start, k)] + [self.xy(color, p, k) for p in path]
        self.it.image(name, self._pawn(color, int(u * 1.25)), pts[0][0], pts[0][1] - u * 0.3, extra_tags=("ludoclick",))
        self.it.raise_(name)

        def place(x: float, y: float, lift: float) -> None:
            self.it.move(name, x, y - u * 0.3 - lift)

        step = STEP_TIME if start >= 0 else 0.3
        hop_path(self.ui.anim, self, pts, step, u * 0.45, place, lambda: self._captures(ev))

    def _captures(self, ev: Dict[str, Any]) -> None:
        captured = [c for c in ev.get("captured") or [] if isinstance(c, dict)]
        seat, k = int(ev.get("seat", 0)), int(ev.get("token", 0))
        self.tokens[seat][k] = int(ev.get("to", (ev.get("path") or [0])[-1]))
        self.moving = None
        if not captured or self.geo is None:
            self._finish_move(ev)
            return
        who = "You" if self.ctx.is_me(seat) else self.ctx.seat_name(seat)
        self.message = "%s captured a token" % who
        self._render_status()
        u = self.geo[2]
        flights = []
        for c in captured:
            cs, ck = int(c.get("seat", 0)), int(c.get("token", 0))
            if cs >= len(self.tokens):
                continue
            ccolor = self.color_of(cs)
            frm = self.xy(ccolor, int(c.get("from", self.tokens[cs][ck])), ck)
            to = self.xy(ccolor, -1, ck)
            self.tokens[cs][ck] = -1
            flights.append(("tok%d_%d" % (cs, ck), frm, to))
        self._render_tokens()
        for name, frm, to in flights:
            self.it.move(name, frm[0], frm[1] - u * 0.3)
            self.it.raise_(name)

        def fly(t: float) -> None:
            for name, frm, to in flights:
                x = frm[0] + (to[0] - frm[0]) * t
                y = frm[1] + (to[1] - frm[1]) * t - math.sin(math.pi * t) * u * 2.0
                self.it.move(name, x, y - u * 0.3)

        self.ui.anim.tween(0.4, fly, "in_out_cubic", done=lambda: self._finish_move(ev), owner=self)

    def _finish_move(self, ev: Dict[str, Any]) -> None:
        seat, k = int(ev.get("seat", 0)), int(ev.get("token", 0))
        if seat < len(self.tokens) and k < len(self.tokens[seat]):
            self.tokens[seat][k] = int(ev.get("to", self.tokens[seat][k]))
        self.moving = None
        self._render_tokens()
        self._next()

    # ----------------------------------------------------------------- input
    def roll(self) -> None:
        st, ctx = self.state or {}, self.ctx
        if ctx.my_seat is None or st.get("finished"):
            return
        if not ctx.is_me(st.get("turn")):
            ctx.toast("Wait for your turn")
            return
        if st.get("phase") != "roll" or self.busy:
            if st.get("phase") == "move" and not st.get("auto"):
                ctx.toast("Pick a token to move")
            return
        self.dice.set_active(False)
        ctx.act(a="roll")

    def _click(self, e: Any) -> None:
        movable = self._movable()
        st = self.state or {}
        if not movable or self.geo is None:
            if st.get("phase") == "roll" and self.ctx.is_me(st.get("turn")):
                self.roll()
            return
        bx, by, u = self.geo
        seat = st["turn"]
        color = self.color_of(seat)
        best, best_d = None, u * 1.0
        for k in movable:
            x, y = self.xy(color, self.tokens[seat][k], k)
            for yy in (y, y - u * 0.3):
                d = math.hypot(e.x - x, e.y - yy)
                if d < best_d:
                    best, best_d = k, d
        if best is not None:
            self.ctx.act(a="move", token=best)

    def on_key(self, keysym: str) -> None:
        if keysym in ("space", "Return", "KP_Enter"):
            self.roll()
        elif keysym in ("1", "2", "3", "4"):
            k = int(keysym) - 1
            if k in self._movable():
                self.ctx.act(a="move", token=k)
