"""Snakes & Ladders view: anti-aliased board with curvy snakes and wooden ladders."""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from ... import widgets
from ...gfx import Image, darken, lighten, luminance, mix, with_alpha
from ..viewbase import GameView
from ..viewkit import Items, along, hop_path
from .logic import square_to_rowcol

Point = Tuple[float, float]
SNAKE_COLORS = ("#22c55e", "#a855f7", "#f97316", "#14b8a6", "#ec4899", "#eab308", "#0ea5e9", "#ef4444")
WOOD, WOOD_DARK = "#c98a35", "#7a4a14"
STEP_TIME = 0.16


# ---------------------------------------------------------------- geometry
def cell_center(n: int, cell: float, ox: float = 0.0, oy: float = 0.0) -> Point:
    n = max(1, min(100, int(n)))
    row, col = square_to_rowcol(n)
    return ox + (col + 0.5) * cell, oy + (row + 0.5) * cell


def snake_curve(head: Point, tail: Point, cell: float, index: int, n: int = 40) -> List[Point]:
    """S-shaped body from head to tail (same maths for painting and for the slide animation)."""
    hx, hy = head
    tx, ty = tail
    dx, dy = tx - hx, ty - hy
    length = math.hypot(dx, dy) or 1.0
    px, py = -dy / length, dx / length
    amp = min(cell * 1.1, length * 0.22) * (1 if index % 2 == 0 else -1)
    p1 = (hx + dx * 0.33 + px * amp, hy + dy * 0.33 + py * amp)
    p2 = (hx + dx * 0.66 - px * amp, hy + dy * 0.66 - py * amp)
    out = []
    for i in range(n + 1):
        t = i / n
        u = 1 - t
        out.append((u ** 3 * hx + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t ** 3 * tx,
                    u ** 3 * hy + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t ** 3 * ty))
    return out


def ladder_rails(bottom: Point, top: Point, cell: float) -> Tuple[Point, Point, Point, Point]:
    bx, by = bottom
    tx, ty = top
    dx, dy = tx - bx, ty - by
    length = math.hypot(dx, dy) or 1.0
    px, py = -dy / length * cell * 0.17, dx / length * cell * 0.17
    return (bx + px, by + py), (tx + px, ty + py), (bx - px, by - py), (tx - px, ty - py)


# ---------------------------------------------------------------- painters
def paint_ladder(img: Image, bottom: Point, top: Point, cell: float) -> None:
    a1, a2, b1, b2 = ladder_rails(bottom, top, cell)
    rail, rung = cell * 0.075, cell * 0.06
    sh = cell * 0.05
    length = math.hypot(top[0] - bottom[0], top[1] - bottom[1])
    steps = max(2, int(length / (cell * 0.42)))
    rungs = [((a1[0] + (a2[0] - a1[0]) * k, a1[1] + (a2[1] - a1[1]) * k),
              (b1[0] + (b2[0] - b1[0]) * k, b1[1] + (b2[1] - b1[1]) * k))
             for k in [(i + 0.5) / steps for i in range(steps)]]
    shadow = (0, 0, 0, 55)
    for p, q in [(a1, a2), (b1, b2)] + rungs:
        img.line(p[0] + sh, p[1] + sh * 1.4, q[0] + sh, q[1] + sh * 1.4, rail, shadow)
    for p, q in rungs:
        img.line(p[0], p[1], q[0], q[1], rung + 1.6, WOOD_DARK)
        img.line(p[0], p[1], q[0], q[1], rung, lighten(WOOD, 0.1))
    for p, q in ((a1, a2), (b1, b2)):
        img.line(p[0], p[1], q[0], q[1], rail + 1.8, WOOD_DARK)
        img.line(p[0], p[1], q[0], q[1], rail, WOOD)
        img.line(p[0] - rail * 0.18, p[1] - rail * 0.18, q[0] - rail * 0.18, q[1] - rail * 0.18, rail * 0.3,
                 with_alpha("#ffe0a8", 150))


def paint_snake(img: Image, head: Point, tail: Point, cell: float, index: int) -> None:
    pts = snake_curve(head, tail, cell, index)
    color = SNAKE_COLORS[index % len(SNAKE_COLORS)]
    dark, light = darken(color, 0.38), lighten(color, 0.35)
    w0, w1 = cell * 0.27, cell * 0.09
    chunks = 5
    seg = len(pts) // chunks
    sh = cell * 0.05

    def chunked(width_scale: float, extra: float, col: Any, dx: float = 0.0, dy: float = 0.0) -> None:
        for c in range(chunks):
            part = pts[c * seg: (c + 1) * seg + 1] if c < chunks - 1 else pts[c * seg:]
            t = (c + 0.5) / chunks
            w = (w0 + (w1 - w0) * t) * width_scale + extra
            img.polyline([(x + dx, y + dy) for x, y in part], w, col)

    chunked(1.0, 0.0, (0, 0, 0, 50), sh, sh * 1.5)
    chunked(1.0, 2.2, dark)
    chunked(1.0, 0.0, color)
    chunked(0.32, 0.0, with_alpha(light, 170), -cell * 0.03, -cell * 0.03)
    for i in range(3, len(pts) - 3, 4):
        t = i / len(pts)
        r = (w0 + (w1 - w0) * t) * 0.22
        img.fill_circle(pts[i][0], pts[i][1], r, with_alpha(dark, 170))
    # head
    hx, hy = pts[0]
    fx, fy = hx - pts[2][0], hy - pts[2][1]
    fl = math.hypot(fx, fy) or 1.0
    fx, fy = fx / fl, fy / fl
    sx, sy = -fy, fx
    hr = w0 * 0.78
    img.line(hx + fx * hr * 0.9, hy + fy * hr * 0.9, hx + fx * hr * 1.7, hy + fy * hr * 1.7, max(1.0, cell * 0.035),
             "#e11d48")
    for sgn in (1, -1):
        img.line(hx + fx * hr * 1.7, hy + fy * hr * 1.7, hx + fx * hr * 2.05 + sx * sgn * hr * 0.3,
                 hy + fy * hr * 2.05 + sy * sgn * hr * 0.3, max(1.0, cell * 0.03), "#e11d48")
    img.fill_ellipse(hx, hy, hr + 1.2, hr + 1.2, dark)
    img.radial_circle(hx, hy, hr, light, color, focus=(hx - hr * 0.3, hy - hr * 0.4))
    for sgn in (1, -1):
        ex, ey = hx + fx * hr * 0.35 + sx * sgn * hr * 0.45, hy + fy * hr * 0.35 + sy * sgn * hr * 0.45
        img.fill_circle(ex, ey, hr * 0.3, "#ffffff")
        img.fill_circle(ex + fx * hr * 0.1, ey + fy * hr * 0.1, hr * 0.15, "#111827")


def paint_board(img: Image, th: Any, ladders: Sequence[Sequence[int]], snakes: Sequence[Sequence[int]]) -> None:
    s = float(min(img.w, img.h))
    cell = s / 10.0
    pal = [th.board_a, th.board_b]
    tints = [th.seat(c)[0] for c in ("red", "blue", "green", "yellow")]
    for n in range(1, 101):
        row, col = square_to_rowcol(n)
        base = pal[(row + col) % 2]
        if n == 100:
            base = mix(base, th.warn, 0.45)
        elif n == 1:
            base = mix(base, th.good, 0.35)
        elif (row + col) % 2 == 0:
            base = mix(base, tints[(row // 2 + col // 3) % 4], 0.10 if not th.dark else 0.06)
        img.fill_rect(col * cell, row * cell, cell + 0.5, cell + 0.5, base)
    for i in range(1, 10):
        img.line(i * cell, 0, i * cell, s, 1.0, with_alpha(th.board_line, 120))
        img.line(0, i * cell, s, i * cell, 1.0, with_alpha(th.board_line, 120))
    for a, b in ladders:
        paint_ladder(img, cell_center(a, cell), cell_center(b, cell), cell)
    for i, (h, t) in enumerate(snakes):
        paint_snake(img, cell_center(h, cell), cell_center(t, cell), cell, i)


def paint_icon(img: Image, theme: Any) -> None:
    s = float(min(img.w, img.h))
    img.fill_rrect(s * 0.08, s * 0.08, s * 0.84, s * 0.84, s * 0.18, theme.board_a, grad_to=theme.board_b)
    cell = s * 0.84 / 4
    for r in range(4):
        for c in range(4):
            if (r + c) % 2:
                img.fill_rect(s * 0.08 + c * cell, s * 0.08 + r * cell, cell, cell, with_alpha(theme.board_line, 120))
    paint_ladder(img, (s * 0.3, s * 0.8), (s * 0.42, s * 0.22), s * 0.2)
    paint_snake(img, (s * 0.72, s * 0.24), (s * 0.66, s * 0.82), s * 0.17, 0)


# --------------------------------------------------------------------- view
class View(GameView):
    def __init__(self, canvas: Any, ctx: Any) -> None:
        super().__init__(canvas, ctx)
        ui = self.ui
        self.it = Items(canvas, "snl_")
        self.chips: List[widgets.PlayerChip] = []
        self.dice = widgets.Dice(ui, canvas, ui.px(48))
        self.dice.bind_click(self.roll)
        self.w = self.h = 0
        self.geo: Optional[Tuple[float, float, float]] = None   # bx, by, cell
        self.pos: List[int] = []
        self.queue: List[Dict[str, Any]] = []
        self.busy = False
        self.moving: Optional[int] = None
        self.message = ""
        self._board_key: Any = None

    def destroy(self) -> None:
        for chip in self.chips:
            chip.destroy()
        self.dice.destroy()
        self.it.clear()
        super().destroy()

    # ---------------------------------------------------------------- layout
    def on_resize(self, w: int, h: int) -> None:
        self.w, self.h = w, h
        ui = self.ui
        n = max(1, len(self.ctx.seats))
        while len(self.chips) < n:
            self.chips.append(widgets.PlayerChip(ui, self.canvas))
        m, gap = ui.px(12), ui.px(8)
        rows = 1 if n <= 2 else 2
        chip_h = ui.px(44)
        cw = (w - 2 * m - gap) / 2
        for i, chip in enumerate(self.chips[:n]):
            chip.place(m + (i % 2) * (cw + gap), m + (i // 2) * (chip_h + gap), cw, chip_h)
        top = m + rows * chip_h + (rows - 1) * gap + ui.px(10)
        bottom = ui.px(70)
        bs = max(ui.px(150), min(w - 2 * m, h - top - bottom))
        self.geo = ((w - bs) / 2.0, top, bs / 10.0)
        self.dice.size = ui.px(48)
        self.dice.place((w - bs) / 2.0 + ui.px(30), top + bs + bottom / 2)
        self._board_key = None
        self.render()

    def center(self, n: int) -> Point:
        bx, by, cell = self.geo  # type: ignore[misc]
        return cell_center(n, cell, bx, by)

    # ---------------------------------------------------------------- render
    def on_state(self, state: Dict[str, Any]) -> None:
        self.state = state
        if not self.busy:
            self.pos = list(state.get("pos") or [])
        self.render()

    def on_seats(self, seats: List[Dict[str, Any]]) -> None:
        super().on_seats(seats)
        self.render()

    def render(self) -> None:
        if self.geo is None or self._dead:
            return
        ui, th, st, ctx = self.ui, self.ui.theme, self.state or {}, self.ctx
        bx, by, cell = self.geo
        board = st.get("board") or {}
        ladders = [tuple(x) for x in board.get("ladders") or []]
        snakes = [tuple(x) for x in board.get("snakes") or []]
        key = (th.key, int(cell * 10), tuple(ladders), tuple(snakes))
        if key != self._board_key and board:
            self._board_key = key
            size = int(cell * 10)
            photo = ui.sprites.get(("snlboard",) + key, size, size, lambda img: paint_board(img, th, ladders, snakes))
            self.it.image("board", photo, bx, by, anchor="nw")
            self.canvas.tag_lower(self.it.ids["board"])
            self.canvas.tag_lower("bg")
            numc = th.text_faint if luminance(th.board_a) < 0.5 else darken(th.board_line, 0.35)
            font = ui.font(max(7.0, cell * 0.24 / ui.scale), bold=True)
            for n in range(1, 101):
                row, col = square_to_rowcol(n)
                self.it.text("n%d" % n, bx + col * cell + cell * 0.08, by + row * cell + cell * 0.06, str(n), font,
                             numc, anchor="nw")
        self._render_tokens()
        self._render_chips()
        self._render_status()

    def _token_photo(self, seat: int, size: int) -> Any:
        th = self.ui.theme
        color = self.ctx.seat_color(seat)
        return self.ui.sprites.get(("disc", th.key, color), size, size,
                                   lambda img: widgets.paint_token(img, *th.seat(color), style="disc"))

    def _render_tokens(self) -> None:
        if self.geo is None:
            return
        bx, by, cell = self.geo
        groups: Dict[int, List[int]] = {}
        for seat, p in enumerate(self.pos):
            if seat == self.moving:
                continue
            groups.setdefault(max(1, int(p)), []).append(seat)
        offs = [(-0.2, -0.2), (0.2, -0.2), (-0.2, 0.2), (0.2, 0.2)]
        for square, seats in groups.items():
            cx, cy = self.center(square)
            for k, seat in enumerate(seats):
                shared = len(seats) > 1
                size = int(cell * (0.62 if shared else 0.78))
                ox, oy = offs[k % 4] if shared else (0, 0)
                self.it.image("tok%d" % seat, self._token_photo(seat, size), cx + ox * cell, cy + oy * cell - cell * 0.06)
        for seat in range(len(self.pos)):
            if seat != self.moving:
                self.it.raise_("tok%d" % seat)

    def _render_chips(self) -> None:
        st, ctx = self.state or {}, self.ctx
        turn = st.get("turn")
        for i, chip in enumerate(self.chips):
            if i >= len(ctx.seats):
                continue
            seat = ctx.seats[i]
            p = self.pos[i] if i < len(self.pos) else 0
            active = turn == i and not st.get("finished")
            chip.update(seat.get("name", "?"), seat.get("color", "red"), bot=bool(seat.get("bot")), active=active,
                        subtitle="At the start" if p <= 0 else "On square %d" % p,
                        timer=st.get("timer") if active else None, me=ctx.is_me(i))

    def _render_status(self) -> None:
        ui, th, st, ctx = self.ui, self.ui.theme, self.state or {}, self.ctx
        if self.geo is None:
            return
        bx, by, cell = self.geo
        bs = cell * 10
        x = bx + ui.px(64)
        turn = st.get("turn")
        winner = st.get("winner")
        if winner is not None:
            main = "You reached 100" if ctx.is_me(winner) else "%s reached 100" % ctx.seat_name(winner)
        elif turn is None:
            main = "Get ready…"
        elif ctx.is_me(turn):
            main = "Your turn: roll" + (" again" if st.get("streak") else "") if not self.busy else "Moving…"
        else:
            main = "%s's turn" % ctx.seat_name(turn)
        mine = turn is not None and ctx.is_me(turn) and not self.busy and winner is None
        self.dice.set_active(mine)
        if turn is not None and not self.busy:
            self.dice.set_color(ctx.seat_color(turn))
        top_y = by + bs + ui.px(24)
        text, font = ui.fit(main, 15, self.w - x - ui.px(12), bold=True)
        self.it.text("status", x, top_y, text, font, th.text, anchor="w")
        text, font = ui.fit(self.message or "Space or click the dice to roll", 12, self.w - x - ui.px(12))
        self.it.text("msg", x, top_y + ui.px(22), text, font, th.text_dim, anchor="w")

    # ------------------------------------------------------------ animation
    def on_event(self, event: Dict[str, Any]) -> None:
        if event.get("kind") in ("roll", "move"):
            self.queue.append(event)
            if len(self.queue) > 4:          # far behind: drop animations, show the truth
                self.queue = [e for e in self.queue if e.get("kind") == "move"][-1:]
            if not self.busy:
                self._next()

    def _next(self) -> None:
        if self._dead:
            return
        if not self.queue:
            self.busy = False
            self.moving = None
            self.pos = list((self.state or {}).get("pos") or self.pos)
            self.render()
            return
        self.busy = True
        ev = self.queue.pop(0)
        if ev.get("kind") == "roll":
            seat, value = ev.get("seat", 0), int(ev.get("value", 1))
            who = "You" if self.ctx.is_me(seat) else self.ctx.seat_name(seat)
            self.message = "%s rolled %d" % (who, value)
            self.dice.set_color(self.ctx.seat_color(seat))
            self.dice.set_active(False)
            self._render_status()
            self.dice.roll(value, done=self._next)
        else:
            self._animate_move(ev)

    def _animate_move(self, ev: Dict[str, Any]) -> None:
        seat = int(ev.get("seat", 0))
        start = int(ev.get("from", 0))
        path = [int(p) for p in ev.get("path") or []]
        via = ev.get("via")
        if ev.get("overshoot"):
            self.message += "  ·  needs exactly %d" % (100 - start)
        while len(self.pos) <= seat:
            self.pos.append(0)
        if not path or self.geo is None:
            self.pos[seat] = int(ev.get("to", start))
            self._next()
            return
        self.moving = seat
        self._render_tokens()
        cell = self.geo[2]
        size = int(cell * 0.78)
        photo = self._token_photo(seat, size)
        name = "tok%d" % seat
        self.it.image(name, photo, *self.center(max(1, start)))
        self.it.raise_(name)
        pts = [self.center(max(1, start))] + [self.center(p) for p in path]

        def place(x: float, y: float, lift: float) -> None:
            self.it.move(name, x, y - cell * 0.06 - lift)

        def after_steps() -> None:
            if via and self.geo is not None:
                a, b = int(via.get("from", 0)), int(via.get("to", 0))
                if via.get("type") == "ladder":
                    self.message += "  ·  ladder up to %d" % b
                    curve = [self.center(a), self.center(b)]
                else:
                    self.message += "  ·  snake down to %d" % b
                    idx = self._snake_index(a, b)
                    bx, by, c = self.geo
                    curve = snake_curve(self.center(a), self.center(b), c, idx)
                self._render_status()

                def glide(t: float) -> None:
                    x, y = along(curve, t)
                    place(x, y, 0.0)

                self.ui.anim.tween(0.7, glide, "in_out_cubic", done=finish, owner=self)
            else:
                finish()

        def finish() -> None:
            self.pos[seat] = int(ev.get("to", path[-1]))
            self.moving = None
            self._render_tokens()
            self._next()

        hop_path(self.ui.anim, self, pts, STEP_TIME, cell * 0.35, place, after_steps)

    def _snake_index(self, head: int, tail: int) -> int:
        snakes = ((self.state or {}).get("board") or {}).get("snakes") or []
        for i, (h, t) in enumerate(snakes):
            if h == head and t == tail:
                return i
        return 0

    # ----------------------------------------------------------------- input
    def roll(self) -> None:
        st, ctx = self.state or {}, self.ctx
        if ctx.my_seat is None or st.get("winner") is not None:
            return
        if not ctx.is_me(st.get("turn")):
            ctx.toast("Wait for your turn")
            return
        if self.busy:
            return
        self.dice.set_active(False)
        ctx.act(a="roll")

    def on_key(self, keysym: str) -> None:
        if keysym in ("space", "Return", "KP_Enter"):
            self.roll()
