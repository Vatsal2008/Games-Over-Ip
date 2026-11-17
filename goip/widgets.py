"""UI kit on a plain tk.Canvas: UI context, Animator, widgets and shared painters.

Every widget draws sprites from ``ui.sprites`` (anti-aliased PNGs painted by gfx) plus
canvas text, tags all its items with a unique tag, and removes its items, bindings,
tweens and after() callbacks in ``destroy()``. Geometry passed to ``place`` is in canvas
pixels; font sizes (``font_px``) are logical and scaled through ``ui.font``.
"""
from __future__ import annotations

import itertools
import logging
import math
import random
import time
import tkinter as tk
import tkinter.font as tkfont
from collections import OrderedDict
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import gfx
from .gfx import Image, darken, lighten, mix, parse, with_alpha
from .theme import NEON, Theme, display_family, text_family

log = logging.getLogger(__name__)

_WHITE = "#ffffff"


# ================================================================== easing
def _out_bounce(t: float) -> float:
    n, d = 7.5625, 2.75
    if t < 1 / d:
        return n * t * t
    if t < 2 / d:
        t -= 1.5 / d
        return n * t * t + 0.75
    if t < 2.5 / d:
        t -= 2.25 / d
        return n * t * t + 0.9375
    t -= 2.625 / d
    return n * t * t + 0.984375


def _out_elastic(t: float) -> float:
    if t <= 0 or t >= 1:
        return float(t >= 1)
    return 2 ** (-10 * t) * math.sin((t * 10 - 0.75) * (2 * math.pi / 3)) + 1


EASES: Dict[str, Callable[[float], float]] = {
    "linear": lambda t: t,
    "in_quad": lambda t: t * t,
    "out_quad": lambda t: 1 - (1 - t) * (1 - t),
    "out_cubic": lambda t: 1 - (1 - t) ** 3,
    "in_out_cubic": lambda t: 4 * t ** 3 if t < 0.5 else 1 - (-2 * t + 2) ** 3 / 2,
    "out_back": lambda t: 1 + 2.70158 * (t - 1) ** 3 + 1.70158 * (t - 1) ** 2,
    "out_bounce": _out_bounce,
    "out_elastic": _out_elastic,
}


# ================================================================== animator
class _Tween:
    __slots__ = ("start", "dur", "step", "ease", "done", "owner")

    def __init__(self, start, dur, step, ease, done, owner) -> None:
        self.start, self.dur, self.step, self.ease, self.done, self.owner = start, dur, step, ease, done, owner


class Animator:
    """One after() loop at `fps` that only runs while tweens exist."""

    def __init__(self, root: Any, fps: int = 60) -> None:
        self.root = root
        self.interval = max(1, int(round(1000.0 / max(1, fps))))
        self._tweens: "OrderedDict[int, _Tween]" = OrderedDict()
        self._ids = itertools.count(1)
        self._job: Optional[str] = None
        self._paused = False
        self._paused_at = 0.0
        self._next = 0.0

    def tween(self, duration: float, step: Callable[[float], None], ease: Any = "out_cubic",
              done: Optional[Callable[[], None]] = None, owner: Any = None, delay: float = 0.0) -> int:
        """step(eased t) every frame; step(1.0) is always the last call, then done()."""
        fn = ease if callable(ease) else EASES.get(ease, EASES["out_cubic"])
        h = next(self._ids)
        start = time.perf_counter() + max(0.0, float(delay))
        if self._paused:
            start = self._paused_at + max(0.0, float(delay))
        self._tweens[h] = _Tween(start, max(0.0, float(duration)), step, fn, done, owner)
        self._ensure()
        return h

    def cancel(self, handle: Any) -> None:
        self._tweens.pop(handle, None)
        if not self._tweens:
            self._unschedule()

    def cancel_owner(self, owner: Any) -> None:
        for h in [h for h, tw in self._tweens.items() if tw.owner is owner]:
            del self._tweens[h]
        if not self._tweens:
            self._unschedule()

    def busy(self, owner: Any = None) -> bool:
        if owner is None:
            return bool(self._tweens)
        return any(tw.owner is owner for tw in self._tweens.values())

    def pause(self) -> None:
        if not self._paused:
            self._paused = True
            self._paused_at = time.perf_counter()
            self._unschedule()

    def resume(self) -> None:
        if self._paused:
            shift = time.perf_counter() - self._paused_at
            for tw in self._tweens.values():
                tw.start += shift
            self._paused = False
            self._ensure()

    @property
    def paused(self) -> bool:
        return self._paused

    def stop(self) -> None:
        """Drop every tween and stop the loop (e.g. when the window closes)."""
        self._tweens.clear()
        self._unschedule()

    def _unschedule(self) -> None:
        if self._job is not None:
            try:
                self.root.after_cancel(self._job)
            except Exception:
                pass
            self._job = None

    def _ensure(self, delay: Optional[int] = None) -> None:
        if self._job is None and not self._paused and self._tweens:
            if delay is None:
                self._next = time.perf_counter() + self.interval / 1000.0
            try:
                self._job = self.root.after(self.interval if delay is None else delay, self._tick)
            except Exception:  # root destroyed
                self._job = None

    def _tick(self) -> None:
        self._job = None
        if self._paused:
            return
        now = time.perf_counter()
        # Fixed cadence: time spent repainting between ticks does not stretch the frame.
        self._next += self.interval / 1000.0
        if self._next < now:
            self._next = now + 0.002
        for h, tw in list(self._tweens.items()):
            if h not in self._tweens or now < tw.start:
                continue
            t = (now - tw.start) / tw.dur if tw.dur > 0 else 1.0
            fin = t >= 1.0
            try:
                tw.step(1.0 if fin else float(tw.ease(t)))
            except Exception:
                log.exception("animation step failed; tween dropped")
                self._tweens.pop(h, None)
                continue
            if fin and self._tweens.pop(h, None) is not None and tw.done is not None:
                try:
                    tw.done()
                except Exception:
                    log.exception("animation done-callback failed")
        self._ensure(max(1, int((self._next - time.perf_counter()) * 1000)))


# ================================================================== UI context
class UI:
    """Shared UI context: theme, scale, fonts, sprite cache and animator."""

    def __init__(self, root: Any, theme: Optional[Theme] = None, scale: float = 1.0,
                 max_sprites: int = 600) -> None:
        self.root = root
        self.theme = theme or NEON
        self.scale = float(scale) if scale and scale > 0 else 1.0
        self.sprites = gfx.SpriteCache(root, max_sprites)
        self.anim = Animator(root)
        self._fonts: Dict[Tuple, Tuple] = {}
        self._tkfonts: Dict[Tuple, Any] = {}
        self._bg: "OrderedDict[Tuple, Any]" = OrderedDict()

    def font(self, px: float, bold: bool = False, display: bool = False) -> Tuple:
        """Tk font tuple; px is the size at scale 1.0 (negative Tk size = pixels)."""
        key = (px, bold, display, self.scale)
        f = self._fonts.get(key)
        if f is None:
            fam = display_family(self.root) if display else text_family(self.root)
            size = -max(6, int(round(px * self.scale)))
            f = (fam, size, "bold") if bold else (fam, size)
            self._fonts[key] = f
        return f

    def px(self, n: float) -> int:
        return max(1, int(round(n * self.scale)))

    def set_theme(self, theme: Theme) -> None:
        self.theme = theme
        self.sprites.clear()
        self._bg.clear()

    def set_scale(self, scale: float) -> None:
        self.scale = float(scale) if scale and scale > 0 else 1.0

    def tkfont(self, font: Tuple) -> Any:
        f = self._tkfonts.get(font)
        if f is None:
            f = tkfont.Font(root=self.root, font=font)
            self._tkfonts[font] = f
        return f

    def measure(self, text: str, font: Tuple) -> int:
        return int(self.tkfont(font).measure(text))

    def linespace(self, font: Tuple) -> int:
        return int(self.tkfont(font).metrics("linespace"))

    def fit(self, text: str, px: float, max_w: float, bold: bool = False, display: bool = False,
            min_px: float = 9) -> Tuple[str, Tuple]:
        """Shrink the font (down to min_px) and then ellipsize so text fits max_w pixels."""
        size = float(px)
        while True:
            f = self.font(size, bold, display)
            if self.measure(text, f) <= max_w or size <= min_px:
                break
            size = max(min_px, size - 1)
        if self.measure(text, f) <= max_w:
            return text, f
        while text and self.measure(text + "…", f) > max_w:
            text = text[:-1]
        return (text.rstrip() + "…") if text else "", f


def _style(th: Theme) -> Dict[str, Any]:
    """Per-theme finish: shadow strength, gloss, button lip darkness."""
    if not th.dark:
        return {"shadow": 34, "gloss": 0, "lip": 0.16, "edge": 255, "on_accent": _WHITE}
    if th.glow:
        return {"shadow": 150, "gloss": 46, "lip": 0.4, "edge": 150, "on_accent": _WHITE}
    return {"shadow": 120, "gloss": 10, "lip": 0.35, "edge": 200, "on_accent": lighten(th.text, 0.35)}


def field_fill(th: Theme) -> str:
    """Solid fill used behind text fields (tk.Entry background must match it)."""
    return th.panel_hi if not th.dark else darken(th.panel, 0.22)


# ================================================================== helpers
def blank_photo(ui: UI, w: int, h: int):
    """Fully transparent photo, used as an exact-size hit area for events."""
    return ui.sprites.get(("blank",), w, h, lambda img: None)


def sliced(ui: UI, key: Any, w: int, h: int, border: Tuple[int, int, int, int],
           paint: Callable[[Image], None]):
    """Photo of w x h assembled 9-slice style from a small painted prototype.

    border = (left, top, right, bottom) fixed edges; the 1-px middle row/column of the
    prototype is tiled. paint(img) must draw relative to img.w / img.h.
    """
    w, h = max(1, int(w)), max(1, int(h))
    l, t, r, b = border
    if w < l + r + 4 or h < t + b + 4:
        return ui.sprites.get(key, w, h, paint)
    k2 = ("9s", key)
    out = ui.sprites.peek(k2, w, h)
    if out is not None:
        return out
    pw, ph = l + r + 1, t + b + 1
    proto = ui.sprites.get(key, pw, ph, paint)
    out = tk.PhotoImage(master=ui.root, width=w, height=h)
    call, name, src = out.tk.call, out.name, proto.name

    def cp(fx0, fy0, fx1, fy1, tx0, ty0, tx1, ty1):
        call(name, "copy", src, "-from", fx0, fy0, fx1, fy1, "-to", tx0, ty0, tx1, ty1,
             "-compositingrule", "set")

    cp(0, 0, l, t, 0, 0, l, t)
    cp(l + 1, 0, pw, t, w - r, 0, w, t)
    cp(0, t + 1, l, ph, 0, h - b, l, h)
    cp(l + 1, t + 1, pw, ph, w - r, h - b, w, h)
    cp(l, 0, l + 1, t, l, 0, w - r, t)
    cp(l, t + 1, l + 1, ph, l, h - b, w - r, h)
    cp(0, t, l, t + 1, 0, t, l, h - b)
    cp(l + 1, t, pw, t + 1, w - r, t, w, h - b)
    cp(l, t, l + 1, t + 1, l, t, w - r, h - b)
    ui.sprites.put(k2, w, h, out)
    return out


def _safe(fn: Callable, *a: Any, **kw: Any) -> Any:
    """Call a Tk method, ignoring TclError (widget or item already gone)."""
    try:
        return fn(*a, **kw)
    except tk.TclError:
        return None


# ================================================================== background
_BLOBS = ((0.12, 0.10, 0.62, "accent", 64), (1.0, 0.52, 0.5, "accent2", 34), (0.05, 0.98, 0.55, "bad", 30))


def draw_background(canvas: tk.Canvas, ui: UI, w: int, h: int) -> None:
    """Gradient (+ soft blobs when theme.glow) as items tagged 'bg', kept lowest."""
    w, h = max(1, int(w)), max(1, int(h))
    th = ui.theme
    canvas.delete("bg")
    key = (th.key, w, h)
    photo = ui._bg.get(key)
    if photo is None:
        photo = _background_photo(ui, w, h)
        ui._bg[key] = photo
        while len(ui._bg) > 6:
            ui._bg.popitem(last=False)
    canvas.create_image(0, 0, image=photo, anchor="nw", tags=("bg",), state="disabled")
    canvas.tag_lower("bg")
    canvas._goip_bg_refs = [photo]  # type: ignore[attr-defined]


def _background_photo(ui: UI, w: int, h: int):
    """Opaque w x h photo: a 1-px gradient column zoomed to width, with glow blobs
    composited in by Tk (C code), so canvas repaints never alpha-blend the background."""
    th = ui.theme
    c0, c1 = parse(th.bg_top), parse(th.bg_bottom)
    col = tk.PhotoImage(master=ui.root, width=1, height=h)
    rows = []
    for y in range(h):
        t = y / max(1, h - 1)
        rows.append("{#%02x%02x%02x}" % tuple(int(c0[i] + (c1[i] - c0[i]) * t + 0.5) for i in range(3)))
    col.put(" ".join(rows))
    photo = col.zoom(w, 1)
    if th.glow:
        for fx, fy, fr, attr, alpha in _BLOBS:
            ph = _blob_photo(ui, getattr(th, attr), alpha, 2 * fr * max(w, h))
            S = ph.width()
            bx, by = int(fx * w - S / 2), int(fy * h - S / 2)
            sx0, sy0 = max(0, -bx), max(0, -by)
            sx1, sy1 = min(S, w - bx), min(S, h - by)
            if sx0 < sx1 and sy0 < sy1:
                photo.tk.call(photo.name, "copy", ph.name, "-from", sx0, sy0, sx1, sy1,
                              "-to", bx + sx0, by + sy0, "-compositingrule", "overlay")
    return photo


def _blob_photo(ui: UI, color: str, alpha: int, diameter: float):
    """Gaussian glow disc. Separable, so every row is one 1-D profile scaled by a
    per-row factor (bytes.translate, C speed); built at half size and zoomed 2x.
    The diameter snaps to 20 % steps so live resizing reuses cached blobs."""
    step = round(math.log(max(32.0, diameter) / 32.0) / math.log(1.2))
    n = max(16, int(32 * 1.2 ** step / 16.0) * 8)
    key = ("blob", color, alpha)
    ph = ui.sprites.peek(key, 2 * n, 2 * n)
    if ph is not None:
        return ph
    c, inv = (n - 1) / 2.0, 1.0 / (2.0 * (n / 6.0) ** 2)
    prof = [math.exp(-((i - c) ** 2) * inv) for i in range(n)]
    gx = bytes(int(255 * p + 0.5) for p in prof)
    tables: Dict[int, bytes] = {}
    rows = []
    for y in range(n):
        q = int(alpha * prof[y] + 0.5)
        t = tables.get(q)
        if t is None:
            t = tables[q] = bytes((i * q + 127) // 255 for i in range(256))
        rows.append(gx.translate(t))
    r, g, b, _a = parse(color)
    rgba = bytearray(bytes((r, g, b, 0)) * (n * n))
    rgba[3::4] = b"".join(rows)
    small = tk.PhotoImage(master=ui.root, format="png",
                          data=__import__("base64").b64encode(gfx.encode_png(n, n, rgba, 1)).decode("ascii"))
    ph = small.zoom(2)
    ui.sprites.put(key, 2 * n, 2 * n, ph)
    return ph


# ================================================================== painters
def paint_panel(img: Image, th: Theme, kind: str, r: float, pad: float, off: float) -> None:
    st = _style(th)
    x, y, w, h = pad, pad, img.w - 2 * pad, img.h - 2 * pad
    if kind == "card":
        img.soft_rrect(x, y + off, w, h, r, with_alpha(th.shadow, st["shadow"] * 0.8), pad * 1.3)
        img.fill_rrect(x, y, w, h, r, th.panel)
        img.stroke_rrect(x, y, w, h, r, with_alpha(th.edge, st["edge"]), 1)
        if th.dark:
            img.stroke_rrect(x + 1, y + 1, w - 2, h - 2, r - 1, (255, 255, 255, 8), 1)
    elif kind == "glass":
        img.soft_rrect(x, y + off, w, h, r, with_alpha(th.shadow, st["shadow"] * 0.45), pad * 1.3)
        if th.dark:
            img.fill_rrect(x, y, w, h, r, (255, 255, 255, 18))
            img.stroke_rrect(x, y, w, h, r, (255, 255, 255, 40), 1)
        else:
            img.fill_rrect(x, y, w, h, r, (255, 255, 255, 200))
            img.stroke_rrect(x, y, w, h, r, th.edge, 1)
    else:  # inset
        img.fill_rrect(x, y, w, h, r, field_fill(th))
        img.stroke_rrect(x, y, w, h, r, with_alpha(th.edge, st["edge"] * 0.9), 1)
        if th.dark:
            img.stroke_rrect(x, y, w, h, r, (0, 0, 0, 60), 2)


def paint_button(img: Image, th: Theme, kind: str, state: str, r: float, pad: float, lip: float) -> None:
    """Chunky rounded button with lip, shadow and gloss. state: normal|hover|pressed|disabled."""
    st = _style(th)
    x, w = pad, img.w - 2 * pad
    fh = img.h - 2 * pad - lip
    if kind == "ghost":
        a = {"hover": 22, "pressed": 36}.get(state, 0)
        if a:
            img.fill_rrect(x, pad, w, fh + lip, r, with_alpha(th.text, a))
        return
    if kind == "secondary":
        top, bot, edge = lighten(th.panel_hi, 0.05), th.panel_hi, with_alpha(th.edge, st["edge"])
        lipc = darken(th.panel, 0.45) if th.dark else darken(th.edge, 0.12)
    else:
        base = th.bad if kind == "danger" else th.accent
        hi = lighten(th.bad, 0.18) if kind == "danger" else th.accent_hi
        top, bot, edge = hi, base, (255, 255, 255, 34 if th.dark else 0)
        lipc = darken(base, st["lip"])
    if state == "hover":
        top, bot = lighten(top, 0.08), lighten(bot, 0.08)
    elif state == "pressed":
        top, bot = darken(top, 0.06), darken(bot, 0.06)
    elif state == "disabled":
        top, bot, lipc = mix(top, th.panel, 0.6), mix(bot, th.panel, 0.6), mix(lipc, th.panel, 0.6)
    dy = lip if state == "pressed" else 0
    if th.glow and kind == "primary" and state != "disabled":
        img.soft_rrect(x, pad + lip * 0.5, w, fh, r, with_alpha(th.accent, 70), pad * 1.6)
    if state != "pressed":
        img.soft_rrect(x, pad + lip + pad * 0.25, w, fh, r, with_alpha(th.shadow, st["shadow"] * 0.7), pad * 0.9)
    img.fill_rrect(x, pad + lip, w, fh, r, lipc)
    img.fill_rrect(x, pad + dy, w, fh, r, top, grad_to=bot)
    if st["gloss"] and state != "disabled":
        img.fill_rrect(x + 2, pad + dy + 1.5, w - 4, fh * 0.5, max(0, r - 2),
                       (255, 255, 255, st["gloss"]), grad_to=(255, 255, 255, 0))
    img.stroke_rrect(x, pad + dy, w, fh, r, edge, 1)


def paint_pill(img: Image, top: Any, bottom: Any, pad: float, shadow: float = 0.0, th: Optional[Theme] = None) -> None:
    w, h = img.w - 2 * pad, img.h - 2 * pad
    if shadow and th is not None:
        img.soft_rrect(pad, pad + pad * 0.35, w, h, h / 2, with_alpha(th.shadow, shadow), pad * 1.2)
    img.fill_rrect(pad, pad, w, h, h / 2, top, grad_to=bottom)


def paint_die(img: Image, value: int, face: Any, pip: Any, angle: float = 0.0, shadow: bool = True) -> None:
    """Rounded 3D die showing `value` (1-6); angle (degrees) tilts it for tumble frames."""
    S = float(min(img.w, img.h))
    size = S * 0.84
    lip = size * 0.07
    x, y = (img.w - size) / 2, (img.h - size) / 2 - lip * 0.5
    fh = size - lip
    r = size * 0.22
    cx, cy = x + size / 2, y + fh / 2
    side = darken(face, 0.32)
    if shadow:
        img.soft_rrect(x + size * 0.04, y + lip + size * 0.06, size * 0.92, fh, r, (0, 0, 0, 80), size * 0.14)
    if angle:
        img.fill_polygon(gfx.rrect_points(x, y + lip, size, fh, r, angle, cx, cy + lip), side)
        img.fill_polygon(gfx.rrect_points(x, y, size, fh, r, angle, cx, cy), lighten(face, 0.04))
    else:
        img.fill_rrect(x, y + lip, size, fh, r, side)
        img.fill_rrect(x, y, size, fh, r, lighten(face, 0.10), grad_to=darken(face, 0.05))
        img.fill_rrect(x + size * 0.08, y + size * 0.04, size * 0.84, fh * 0.42, r * 0.8,
                       (255, 255, 255, 70), grad_to=(255, 255, 255, 0))
        img.stroke_rrect(x, y, size, fh, r, with_alpha(darken(face, 0.3), 70), max(1.0, size * 0.02))
    spots = {1: [(0, 0)], 2: [(-1, -1), (1, 1)], 3: [(-1, -1), (0, 0), (1, 1)],
             4: [(-1, -1), (1, -1), (-1, 1), (1, 1)], 5: [(-1, -1), (1, -1), (0, 0), (-1, 1), (1, 1)],
             6: [(-1, -1), (-1, 0), (-1, 1), (1, -1), (1, 0), (1, 1)]}.get(int(value), [(0, 0)])
    ca, sa = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    step, pr = size * 0.245, size * (0.1 if value == 1 else 0.085)
    hi = with_alpha(_WHITE, 90) if gfx.luminance(face) > 0.5 else with_alpha(_WHITE, 25)
    for gx, gy in spots:
        px, py = gx * step, gy * step
        px, py = cx + px * ca - py * sa, cy + px * sa + py * ca
        img.fill_circle(px, py + pr * 0.18, pr, hi)
        img.radial_circle(px, py, pr, lighten(pip, 0.22), pip, focus=(px - pr * 0.35, py - pr * 0.4))


def paint_token(img: Image, main: Any, light: Any, dark: Any, style: str = "pawn") -> None:
    """Glossy game piece with a soft shadow: 'pawn' (Ludo pin) or 'disc' (checker)."""
    u = float(min(img.w, img.h))
    cx, y0 = img.w / 2.0, (img.h - u) / 2.0
    o = max(0.8, u * 0.022)
    if style == "disc":
        cy, R = y0 + u * 0.46, u * 0.4
        img.soft_circle(cx, cy + u * 0.07, R, (0, 0, 0, 90), u * 0.12)
        img.fill_circle(cx, cy + u * 0.05, R, dark)
        img.radial_circle(cx, cy, R, light, main, focus=(cx - R * 0.35, cy - R * 0.4))
        img.stroke_circle(cx, cy, R, with_alpha(dark, 170), o * 1.2)
        img.stroke_circle(cx, cy, R * 0.7, with_alpha(dark, 110), o * 1.6)
        img.stroke_circle(cx, cy + o * 0.6, R * 0.7 - o * 1.6, with_alpha(light, 150), o)
        img.soft_ellipse(cx - R * 0.3, cy - R * 0.45, R * 0.32, R * 0.17, (255, 255, 255, 150), R * 0.22)
        return
    hy, hr = y0 + u * 0.3, u * 0.18
    by, brx, bry = y0 + u * 0.8, u * 0.3, u * 0.085
    ny, nw = y0 + u * 0.5, u * 0.095
    img.soft_ellipse(cx, by + bry * 0.9, brx * 1.05, bry * 1.1, (0, 0, 0, 95), u * 0.08)
    left = gfx.bezier((cx - brx, by), (cx - brx * 0.9, by - u * 0.14), (cx - nw * 1.2, ny + u * 0.12), (cx - nw, ny), 10)
    right = [(2 * cx - px, py) for px, py in reversed(left)]
    skirt = left + right
    img.fill_ellipse(cx, by, brx + o, bry + o, dark)
    img.polyline(skirt, o * 2, dark)
    img.fill_circle(cx, hy, hr + o, dark)
    img.fill_ellipse(cx, by, brx, bry, darken(main, 0.18))
    img.fill_polygon(skirt, main)
    shade = [(cx + (px - cx) * 0.45, py) for px, py in reversed(right)]
    img.fill_polygon(right + shade, with_alpha(dark, 95))
    lit = [(cx + (px - cx) * 0.62, py) for px, py in reversed(left)]
    img.fill_polygon([(cx + (px - cx) * 0.88, py) for px, py in left] + lit, with_alpha(light, 120))
    img.fill_ellipse(cx, ny, nw * 1.55, u * 0.045, lighten(main, 0.12))
    img.radial_circle(cx, hy, hr, light, darken(main, 0.12), focus=(cx - hr * 0.35, hy - hr * 0.4))
    img.stroke_circle(cx, hy, hr, with_alpha(dark, 90), o * 0.8)
    img.soft_circle(cx - hr * 0.38, hy - hr * 0.42, hr * 0.24, (255, 255, 255, 215), hr * 0.3)


def paint_avatar(img: Image, main: Any, light: Any, dark: Any) -> None:
    S = float(min(img.w, img.h))
    c, R = img.w / 2.0, S / 2.0 - 1
    img.radial_circle(c, img.h / 2.0, R, light, main, focus=(c - R * 0.3, img.h / 2.0 - R * 0.45))
    img.stroke_circle(c, img.h / 2.0, R, with_alpha(dark, 150), max(1.0, S * 0.04))
    img.soft_ellipse(c - R * 0.2, img.h / 2.0 - R * 0.5, R * 0.45, R * 0.22, (255, 255, 255, 90), R * 0.3)


def paint_chevron(img: Image, color: Any) -> None:
    w, h = img.w, img.h
    lw = max(1.4, h * 0.16)
    img.polyline([(w * 0.22, h * 0.36), (w * 0.5, h * 0.64), (w * 0.78, h * 0.36)], lw, color)


# ================================================================== widget base
_uid = itertools.count(1)


class _Widget:
    def __init__(self, ui: UI, canvas: tk.Canvas) -> None:
        self.ui = ui
        self.canvas = canvas
        self.tag = "goipw%d" % next(_uid)
        self.htag = self.tag + "h"
        self.x = self.y = self.w = self.h = 0
        self.visible = True
        self._dead = False
        self._afters: set = set()
        self._binds: List[Tuple[str, str, str]] = []
        self._refs: Dict[str, Any] = {}
        self._hits: set = set()

    @property
    def bounds(self) -> Tuple[int, int, int, int]:
        return (self.x, self.y, self.w, self.h)

    def _after(self, ms: int, fn: Callable[[], None]) -> Optional[str]:
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

    def _bind(self, seq: str, fn: Callable, tag: Optional[str] = None) -> None:
        tag = tag or self.htag
        fid = self.canvas.tag_bind(tag, seq, fn, add="+")
        self._binds.append((tag, seq, fid))

    def _hand(self, on: bool) -> None:
        _safe(self.canvas.configure, cursor="hand2" if on else "")

    def _img(self, name: str, photo: Any, x: float, y: float, anchor: str = "nw", hit: bool = False) -> int:
        """Create or move+update a named image item; keeps a reference to the photo."""
        self._refs[name] = photo
        item = self._refs.get("#" + name)
        tags = (self.tag, self.htag) if hit else (self.tag,)
        if item is None:
            item = self.canvas.create_image(x, y, image=photo, anchor=anchor, tags=tags,
                                            state="normal" if hit else "disabled")
            self._refs["#" + name] = item
            if hit:
                self._hits.add("#" + name)
        else:
            self.canvas.coords(item, x, y)
            self.canvas.itemconfigure(item, image=photo, anchor=anchor)
        return item

    def _text(self, name: str, x: float, y: float, text: str, font: Tuple, fill: str, anchor: str = "center",
              **kw: Any) -> int:
        item = self._refs.get("#" + name)
        if item is None:
            item = self.canvas.create_text(x, y, text=text, font=font, fill=fill, anchor=anchor,
                                           tags=(self.tag,), state="disabled", **kw)
            self._refs["#" + name] = item
        else:
            self.canvas.coords(item, x, y)
            self.canvas.itemconfigure(item, text=text, font=font, fill=fill, anchor=anchor, **kw)
        return item

    def _z(self, names: Sequence[str]) -> None:
        """Restack named items bottom-to-top in this order (items made later land on top)."""
        for n in names:
            item = self._refs.get("#" + n)
            if item is not None:
                self.canvas.tag_raise(item)

    def _drop(self, name: str) -> None:
        item = self._refs.pop("#" + name, None)
        self._refs.pop(name, None)
        if item is not None:
            _safe(self.canvas.delete, item)

    def _hit(self, x: float, y: float, w: float, h: float) -> None:
        """Transparent exact-size image on top that receives this widget's events."""
        ph = blank_photo(self.ui, max(1, int(w)), max(1, int(h)))
        item = self._img("hit", ph, x, y, hit=True)
        self.canvas.tag_raise(item)
        if not self.visible:
            self.canvas.itemconfigure(item, state="hidden")

    def set_visible(self, b: bool) -> None:
        self.visible = bool(b)
        if b:
            for k, v in self._refs.items():
                if k.startswith("#"):
                    _safe(self.canvas.itemconfigure, v, state="normal" if k in self._hits else "disabled")
        else:
            _safe(self.canvas.itemconfigure, self.tag, state="hidden")

    def raise_(self) -> None:
        _safe(self.canvas.tag_raise, self.tag)

    def destroy(self) -> None:
        if self._dead:
            return
        self._dead = True
        for ident in list(self._afters):
            _safe(self.canvas.after_cancel, ident)
        self._afters.clear()
        self.ui.anim.cancel_owner(self)
        for tag, seq, fid in self._binds:
            _safe(self.canvas.tag_unbind, tag, seq, fid)
        self._binds = []
        _safe(self.canvas.delete, self.tag)
        self._refs.clear()


# ================================================================== Button
class Button(_Widget):
    """Sprite button: kind primary|secondary|ghost|danger; icon = glyph str or painter fn(img, color)."""

    def __init__(self, ui: UI, canvas: tk.Canvas, text: str, command: Optional[Callable[[], None]] = None,
                 kind: str = "primary", icon: Any = None, font_px: float = 15) -> None:
        super().__init__(ui, canvas)
        self.text, self.command, self.kind, self.icon, self.font_px = text, command, kind, icon, font_px
        self.enabled = True
        self._state = "normal"
        self._pressed = False
        self._bind("<Motion>", self._on_motion)
        self._bind("<Leave>", lambda e: self._set_hover(False))
        self._bind("<ButtonPress-1>", self._on_press)
        self._bind("<ButtonRelease-1>", self._on_release)

    def place(self, x: float, y: float, w: float, h: float) -> "Button":
        self.x, self.y, self.w, self.h = int(round(x)), int(round(y)), max(1, int(round(w))), max(1, int(round(h)))
        self._draw()
        return self

    def set_text(self, s: str) -> None:
        self.text = s
        self._draw()

    def set_enabled(self, b: bool) -> None:
        self.enabled = bool(b)
        self._state = "normal" if b else "disabled"
        self._draw()

    def refresh(self) -> None:
        self._draw()

    def _metrics(self) -> Tuple[int, int, int]:
        ui = self.ui
        lip = 0 if self.kind == "ghost" or self.h < 20 else min(ui.px(4), max(1, self.h // 12))
        pad = min(ui.px(10), max(2, self.h // 3))
        r = min(self.h / 2.0, ui.px(14))
        return lip, pad, r

    def _draw(self) -> None:
        if self._dead or not self.w:
            return
        ui, th = self.ui, self.ui.theme
        st = self._state if self.enabled else "disabled"
        lip, pad, r = self._metrics()
        key = ("btn", th.key, self.kind, st, lip, pad, r)
        paint = lambda img: paint_button(img, th, self.kind, st, r, pad, lip)  # noqa: E731
        w2, h2 = self.w + 2 * pad, self.h + 2 * pad
        if self.w > 2 * r + 2 * pad + 8:
            l = pad + int(math.ceil(r)) + 2
            ph = sliced(ui, key, w2, h2, (l, h2 // 2, l, h2 - h2 // 2 - 1), paint)
        else:
            ph = ui.sprites.get(key, w2, h2, paint)
        self._img("bg", ph, self.x - pad, self.y - pad)
        dy = lip if st == "pressed" else 0
        cy = self.y + (self.h - lip) / 2.0 + dy
        if self.kind in ("primary", "danger"):
            fg = _style(th)["on_accent"] if st != "disabled" else mix(_style(th)["on_accent"], th.panel, 0.45)
        elif self.kind == "ghost":
            fg = th.text if st in ("hover", "pressed") else th.text_dim
        else:
            fg = th.text
        if st == "disabled" and self.kind != "primary" and self.kind != "danger":
            fg = th.text_faint
        avail = self.w - 2 * max(ui.px(10), int(r * 0.6))
        icon_w = 0
        label = self.text
        if isinstance(self.icon, str) and self.icon:
            label = self.icon + "  " + label if label else self.icon
        elif callable(self.icon):
            icon_w = ui.px(self.font_px * 1.15)
            avail -= icon_w + ui.px(6)
        text, font = ui.fit(label, self.font_px, max(4, avail), bold=True)
        tw = ui.measure(text, font) if text else 0
        if icon_w:
            total = icon_w + (ui.px(6) + tw if text else 0)
            ix = self.x + (self.w - total) / 2.0
            iph = ui.sprites.get(("icon", id(self.icon), fg), icon_w, icon_w, lambda img: self.icon(img, fg))
            self._img("icon", iph, ix, cy - icon_w / 2.0)
            tx = ix + icon_w + ui.px(6) + tw / 2.0
        else:
            tx = self.x + self.w / 2.0
        self._text("label", tx, cy, text, font, fg)
        self._hit(self.x, self.y, self.w, self.h)
        if not self.visible:
            self.set_visible(False)

    def _inside(self, e: Any) -> bool:
        x, y = self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)
        return self.x <= x < self.x + self.w and self.y <= y < self.y + self.h

    def _on_motion(self, e: Any) -> None:
        self._set_hover(self._inside(e))

    def _set_hover(self, on: bool) -> None:
        if self._dead or not self.enabled:
            self._hand(False)
            return
        self._hand(on)
        new = ("pressed" if self._pressed else "hover") if on else "normal"
        if new != self._state:
            self._state = new
            self._draw()

    def _on_press(self, e: Any) -> None:
        if self.enabled and self.visible:
            self._pressed = True
            self._state = "pressed"
            self._draw()

    def _on_release(self, e: Any) -> None:
        was = self._pressed
        self._pressed = False
        if self._dead or not self.enabled:
            return
        inside = self._inside(e)
        self._state = "hover" if inside else "normal"
        self._draw()
        if was and inside and self.command is not None:
            self.command()


# ================================================================== Panel
class Panel(_Widget):
    def __init__(self, ui: UI, canvas: tk.Canvas, radius: float = 16, kind: str = "card") -> None:
        super().__init__(ui, canvas)
        self.radius, self.kind = radius, kind

    def place(self, x: float, y: float, w: float, h: float) -> "Panel":
        self.x, self.y, self.w, self.h = int(round(x)), int(round(y)), max(1, int(round(w))), max(1, int(round(h)))
        ui, th = self.ui, self.ui.theme
        r = min(ui.px(self.radius), self.w / 2.0, self.h / 2.0)
        pad = 2 if self.kind == "inset" else min(ui.px(14), max(2, min(self.w, self.h) // 4))
        off = 0 if self.kind == "inset" else max(1, pad // 3)
        key = ("panel", th.key, self.kind, r, pad, off)
        b = pad + int(math.ceil(r)) + 2
        ph = sliced(ui, key, self.w + 2 * pad, self.h + 2 * pad, (b, b, b, b),
                    lambda img: paint_panel(img, th, self.kind, r, pad, off))
        self._img("bg", ph, self.x - pad, self.y - pad)
        if not self.visible:
            self.set_visible(False)
        return self

    def refresh(self) -> None:
        if self.w:
            self.place(self.x, self.y, self.w, self.h)


# ================================================================== TextField
class TextField(_Widget):
    """Themed tk.Entry on a rounded field with placeholder and inline error text."""

    def __init__(self, ui: UI, canvas: tk.Canvas, value: str = "", placeholder: str = "",
                 on_enter: Optional[Callable[[str], None]] = None, max_len: int = 40) -> None:
        super().__init__(ui, canvas)
        self.placeholder, self.on_enter, self.max_len = placeholder, on_enter, max(1, int(max_len))
        self.error: Optional[str] = None
        self._focused = False
        self._ph = False
        self._quiet = False
        th = ui.theme
        self.var = tk.StringVar(master=canvas, value=str(value)[: self.max_len])
        self.entry = tk.Entry(canvas, textvariable=self.var, relief="flat", bd=0, highlightthickness=0,
                              font=ui.font(15), bg=field_fill(th), fg=th.text, insertbackground=th.text,
                              insertwidth=max(1, ui.px(2)), selectbackground=th.accent,
                              selectforeground=_WHITE, disabledbackground=field_fill(th))
        self._win = canvas.create_window(0, 0, window=self.entry, anchor="w", tags=(self.tag,), state="hidden")
        self._trace = self.var.trace_add("write", self._on_write)
        self.entry.bind("<FocusIn>", self._focus_in, add="+")
        self.entry.bind("<FocusOut>", self._focus_out, add="+")
        self.entry.bind("<Return>", self._enter, add="+")
        self.entry.bind("<KP_Enter>", self._enter, add="+")
        self._bind("<ButtonPress-1>", lambda e: self.focus())
        self._show_placeholder()

    # value -----------------------------------------------------------------
    @property
    def value(self) -> str:
        return "" if self._ph else self.var.get()

    @value.setter
    def value(self, s: str) -> None:
        self._ph = False
        self._quiet = True
        self.var.set(str(s)[: self.max_len])
        self._quiet = False
        self.entry.configure(fg=self.ui.theme.text)
        if not s and not self._focused:
            self._show_placeholder()

    def focus(self) -> None:
        _safe(self.entry.focus_set)

    def set_error(self, msg: Optional[str]) -> None:
        self.error = msg or None
        self._draw()

    def _show_placeholder(self) -> None:
        if self.placeholder and not self.var.get() and not self._focused:
            self._ph = True
            self._quiet = True
            self.var.set(self.placeholder)
            self._quiet = False
            self.entry.configure(fg=self.ui.theme.text_faint)

    def _on_write(self, *_: Any) -> None:
        if self._quiet or self._ph:
            return
        s = self.var.get()
        if len(s) > self.max_len:
            self._quiet = True
            self.var.set(s[: self.max_len])
            self._quiet = False
        if self.error:
            self.set_error(None)

    def _focus_in(self, _e: Any = None) -> None:
        self._focused = True
        if self._ph:
            self._ph = False
            self._quiet = True
            self.var.set("")
            self._quiet = False
            self.entry.configure(fg=self.ui.theme.text)
        self._draw()

    def _focus_out(self, _e: Any = None) -> None:
        self._focused = False
        self._show_placeholder()
        self._draw()

    def _enter(self, _e: Any = None) -> str:
        if self.on_enter is not None:
            self.on_enter(self.value)
        return "break"

    # layout ----------------------------------------------------------------
    def place(self, x: float, y: float, w: float, h: float) -> "TextField":
        self.x, self.y, self.w, self.h = int(round(x)), int(round(y)), max(1, int(round(w))), max(1, int(round(h)))
        self._draw()
        return self

    def _draw(self) -> None:
        if self._dead or not self.w:
            return
        ui, th = self.ui, self.ui.theme
        r = min(ui.px(12), self.h / 2.0)
        state = "error" if self.error else ("focus" if self._focused else "normal")
        sw = 1 if state == "normal" else max(1.5, ui.px(2))
        ring = {"error": th.bad, "focus": th.accent}.get(state, th.edge)

        def paint(img: Image) -> None:
            img.fill_rrect(1, 1, img.w - 2, img.h - 2, r, field_fill(th))
            img.stroke_rrect(1, 1, img.w - 2, img.h - 2, r, ring, sw)

        b = int(math.ceil(r)) + 4
        ph = sliced(ui, ("field", th.key, state, r, sw), self.w + 2, self.h + 2, (b, b, b, b), paint)
        self._img("bg", ph, self.x - 1, self.y - 1, hit=True)
        padx = min(ui.px(12), max(1, self.w // 6))
        font = ui.font(15)
        eh = max(1, min(self.h - 4, ui.linespace(font) + 2))
        self.entry.configure(font=font, bg=field_fill(th), disabledbackground=field_fill(th),
                             fg=th.text_faint if self._ph else th.text,
                             insertbackground=th.text, selectbackground=th.accent)
        self.canvas.coords(self._win, self.x + padx, self.y + self.h / 2.0)
        self.canvas.itemconfigure(self._win, width=max(1, self.w - 2 * padx), height=eh,
                                  state="normal" if self.visible else "hidden")
        if self.error:
            text, f = ui.fit(self.error, 12, max(10, self.w - ui.px(4)))
            self._text("err", self.x + ui.px(4), self.y + self.h + ui.px(4), text, f, th.bad, anchor="nw")
        else:
            self._drop("err")
        if not self.visible:
            self.set_visible(False)

    def set_visible(self, b: bool) -> None:
        super().set_visible(b)
        _safe(self.canvas.itemconfigure, self._win, state="normal" if b else "hidden")

    def destroy(self) -> None:
        if self._dead:
            return
        try:
            self.var.trace_remove("write", self._trace)
        except Exception:
            pass
        super().destroy()
        _safe(self.entry.destroy)


# ================================================================== Segmented
class Segmented(_Widget):
    """Pill of choices with an animated sliding highlight."""

    def __init__(self, ui: UI, canvas: tk.Canvas, choices: Sequence[Tuple[Any, str]], value: Any,
                 on_change: Optional[Callable[[Any], None]] = None) -> None:
        super().__init__(ui, canvas)
        self.choices = list(choices)
        self._value = value
        self.on_change = on_change
        self._hx = None
        self._bind("<ButtonRelease-1>", self._click)
        self._bind("<Enter>", lambda e: self._hand(True))
        self._bind("<Leave>", lambda e: self._hand(False))

    @property
    def value(self) -> Any:
        return self._value

    @value.setter
    def value(self, v: Any) -> None:
        self._select(v, notify=False)

    def set_choices(self, choices: Sequence[Tuple[Any, str]], value: Any) -> None:
        self.choices = list(choices)
        self._value = value
        for k in [k for k in self._refs if k.startswith("#lab")]:
            self._drop(k[1:])
        self._draw(animate=False)

    def place(self, x: float, y: float, w: float, h: float) -> "Segmented":
        self.x, self.y, self.w, self.h = int(round(x)), int(round(y)), max(1, int(round(w))), max(1, int(round(h)))
        self._draw(animate=False)
        return self

    def _index(self) -> int:
        for i, (v, _l) in enumerate(self.choices):
            if v == self._value and type(v) is type(self._value):
                return i
        return -1

    def _geom(self) -> Tuple[int, float]:
        ip = max(1, min(self.ui.px(3), self.h // 8))
        n = max(1, len(self.choices))
        return ip, (self.w - 2 * ip) / float(n)

    def _draw(self, animate: bool = False) -> None:
        if self._dead or not self.w:
            return
        ui, th = self.ui, self.ui.theme
        r = self.h / 2.0
        b = int(math.ceil(r)) + 4
        track = sliced(ui, ("segtrack", th.key, r), self.w + 4, self.h + 4, (b, b, b, b),
                       lambda img: paint_panel(img, th, "inset", r, 2, 0))
        self._img("track", track, self.x - 2, self.y - 2)
        ip, sw = self._geom()
        idx = self._index()
        hh = max(1, self.h - 2 * ip)
        if idx >= 0 and sw >= 2:
            pad = max(1, min(ui.px(4), hh // 4))
            hl = ui.sprites.get(("seghl", th.key, pad, hh), int(sw) + 2 * pad, hh + 2 * pad,
                                lambda img: paint_pill(img, th.accent_hi, th.accent, pad,
                                                       _style(th)["shadow"] * 0.6, th))
            tx = self.x + ip + idx * sw - pad
            item = self._img("hl", hl, tx if self._hx is None or not animate else self._hx, self.y + ip - pad)
            if animate and self._hx is not None and abs(self._hx - tx) > 0.5:
                x0 = self._hx
                self.ui.anim.cancel_owner(self)

                def step(t: float, x0: float = x0, tx: float = tx, item: int = item) -> None:
                    self._hx = x0 + (tx - x0) * t
                    self.canvas.coords(item, self._hx, self.y + ip - pad)

                self.ui.anim.tween(0.22, step, "out_cubic", owner=self)
            else:
                self._hx = tx
        else:
            self._drop("hl")
            self._hx = None
        on = _style(th)["on_accent"]
        for i, (_v, label) in enumerate(self.choices):
            text, f = ui.fit(str(label), 13, max(4, sw - ui.px(6)), bold=True)
            self._text("lab%d" % i, self.x + ip + (i + 0.5) * sw, self.y + self.h / 2.0, text, f,
                       on if i == idx else th.text_dim)
        self._hit(self.x, self.y, self.w, self.h)
        if not self.visible:
            self.set_visible(False)

    def _select(self, v: Any, notify: bool) -> None:
        changed = not (v == self._value and type(v) is type(self._value))
        self._value = v
        self._draw(animate=True)
        if changed and notify and self.on_change is not None:
            self.on_change(v)

    def _click(self, e: Any) -> None:
        if not self.choices or self._dead:
            return
        ip, sw = self._geom()
        x, y = self.canvas.canvasx(e.x), self.canvas.canvasy(e.y)
        if not (self.x <= x < self.x + self.w and self.y <= y < self.y + self.h):
            return
        i = int((x - self.x - ip) // max(1.0, sw))
        i = max(0, min(len(self.choices) - 1, i))
        self._select(self.choices[i][0], notify=True)


# ================================================================== Dropdown
class Dropdown(_Widget):
    """Field-like button that opens a themed tk.Menu."""

    def __init__(self, ui: UI, canvas: tk.Canvas, choices: Sequence[Tuple[Any, str]], value: Any,
                 on_change: Optional[Callable[[Any], None]] = None) -> None:
        super().__init__(ui, canvas)
        self.choices = list(choices)
        self._value = value
        self.on_change = on_change
        self._hover = False
        self.menu: Optional[tk.Menu] = None
        self._bind("<Enter>", lambda e: self._set_hover(True))
        self._bind("<Leave>", lambda e: self._set_hover(False))
        self._bind("<ButtonRelease-1>", lambda e: self.open())

    @property
    def value(self) -> Any:
        return self._value

    @value.setter
    def value(self, v: Any) -> None:
        self._value = v
        self._draw()

    def set_choices(self, choices: Sequence[Tuple[Any, str]], value: Any) -> None:
        self.choices = list(choices)
        self._value = value
        self._draw()

    def label(self) -> str:
        for v, l in self.choices:
            if v == self._value:
                return str(l)
        return "" if self._value is None else str(self._value)

    def place(self, x: float, y: float, w: float, h: float) -> "Dropdown":
        self.x, self.y, self.w, self.h = int(round(x)), int(round(y)), max(1, int(round(w))), max(1, int(round(h)))
        self._draw()
        return self

    def _set_hover(self, on: bool) -> None:
        self._hover = on
        self._hand(on)
        self._draw()

    def _draw(self) -> None:
        if self._dead or not self.w:
            return
        ui, th = self.ui, self.ui.theme
        r = min(ui.px(12), self.h / 2.0)
        fill = th.panel_hi if self._hover else field_fill(th)

        def paint(img: Image) -> None:
            img.fill_rrect(1, 1, img.w - 2, img.h - 2, r, fill)
            img.stroke_rrect(1, 1, img.w - 2, img.h - 2, r, th.edge, 1)

        b = int(math.ceil(r)) + 4
        self._img("bg", sliced(ui, ("dd", th.key, fill, r), self.w + 2, self.h + 2, (b, b, b, b), paint),
                  self.x - 1, self.y - 1)
        cs = max(4, min(ui.px(14), self.h // 2))
        padx = min(ui.px(12), max(1, self.w // 8))
        chev = ui.sprites.get(("chev", th.text_dim), cs, cs, lambda img: paint_chevron(img, th.text_dim))
        self._img("chev", chev, self.x + self.w - padx - cs, self.y + (self.h - cs) / 2.0)
        text, f = ui.fit(self.label(), 14, max(4, self.w - 2 * padx - cs - ui.px(4)))
        self._text("label", self.x + padx, self.y + self.h / 2.0, text, f, th.text, anchor="w")
        self._hit(self.x, self.y, self.w, self.h)
        if not self.visible:
            self.set_visible(False)

    def open(self) -> None:
        if self._dead or not self.choices or not self.visible:
            return
        th = self.ui.theme
        if self.menu is not None:
            _safe(self.menu.destroy)
        m = tk.Menu(self.canvas, tearoff=0, bg=th.panel_hi, fg=th.text, activebackground=th.accent,
                    activeforeground=_WHITE, bd=0, relief="flat", activeborderwidth=0,
                    font=self.ui.font(14), selectcolor=th.text)
        for v, l in self.choices:
            mark = "✓  " if v == self._value else "     "
            m.add_command(label=mark + str(l), command=lambda v=v: self._pick(v))
        self.menu = m
        try:
            m.tk_popup(self.canvas.winfo_rootx() + self.x, self.canvas.winfo_rooty() + self.y + self.h + 2)
        finally:
            _safe(m.grab_release)

    def _pick(self, v: Any) -> None:
        changed = v != self._value
        self._value = v
        self._draw()
        if changed and self.on_change is not None:
            self.on_change(v)

    def destroy(self) -> None:
        if self.menu is not None:
            _safe(self.menu.destroy)
            self.menu = None
        super().destroy()


# ================================================================== Toaster
class Toaster(_Widget):
    """Stacked toasts that slide and fade in, hold, then fade out."""

    MAX = 4

    def __init__(self, ui: UI, canvas: tk.Canvas, position: str = "top") -> None:
        super().__init__(ui, canvas)
        self.position = position
        self._toasts: List[Dict[str, Any]] = []
        self._ontop: Optional[str] = None

    def show(self, text: str, kind: str = "info", seconds: float = 2.5) -> None:
        if self._dead:
            return
        ui, th, c = self.ui, self.ui.theme, self.canvas
        cw = max(c.winfo_width(), int(c.cget("width") or 0), 60)
        font = ui.font(13, bold=True)
        maxw = max(40, min(cw - 2 * ui.px(16), ui.px(380)))
        dot = ui.px(8)
        padl, padr = ui.px(14) + dot + ui.px(10), ui.px(16)
        tw = min(ui.measure(text, font), maxw - padl - padr)
        lines = max(1, int(math.ceil(ui.measure(text, font) / float(max(1, tw)))))
        h = max(ui.px(38), lines * ui.linespace(font) + ui.px(18))
        w = int(min(maxw, tw + padl + padr))
        tag = "%st%d" % (self.tag, next(_uid))
        pad = ui.px(10)
        color = {"good": th.good, "bad": th.bad}.get(kind, th.accent2)
        frames = []
        for lv in (0.25, 0.5, 0.75, 1.0):
            frames.append(ui.sprites.get(("toast", th.key, color, lv, pad), w + 2 * pad, h + 2 * pad,
                                         lambda img, lv=lv: self._paint(img, th, color, pad, dot, lv)))
        x0 = (cw - w) / 2.0 - pad
        bg = c.create_image(x0, 0, image=frames[0], anchor="nw", tags=(self.tag, tag), state="disabled")
        tx = c.create_text(x0 + pad + padl, 0, text=text, font=font, fill=th.panel_hi, anchor="w",
                           width=max(10, tw + 2), tags=(self.tag, tag), state="disabled")
        t = {"tag": tag, "bg": bg, "tx": tx, "h": h, "pad": pad, "x0": x0, "frames": frames,
             "y": None, "lv": 0.0, "dy": -ui.px(10), "color": color, "tdx": pad + padl}
        self._toasts.append(t)
        while len(self._toasts) > self.MAX:
            self._kill(self._toasts[0], fast=True)
        self._restack()

        def fin(t: Dict[str, Any] = t) -> None:
            self._after(int(seconds * 1000), lambda: self._fade_out(t))

        self.ui.anim.tween(0.24, lambda k, t=t: self._fade(t, k), "out_cubic", done=fin, owner=self)
        if self._ontop is None:
            self._keep_on_top()

    @staticmethod
    def _paint(img: Image, th: Theme, color: str, pad: int, dot: int, lv: float) -> None:
        w, h = img.w - 2 * pad, img.h - 2 * pad
        img.soft_rrect(pad, pad + pad * 0.4, w, h, h / 2.0, with_alpha(th.shadow, _style(th)["shadow"]), pad * 1.4)
        img.fill_rrect(pad, pad, w, h, h / 2.0, th.panel_hi if th.dark else _WHITE)
        img.stroke_rrect(pad, pad, w, h, h / 2.0, with_alpha(th.edge, 255), 1)
        dx, dy = pad + h * 0.25 + dot * 0.6, pad + h / 2.0
        if th.glow:
            img.soft_circle(dx, dy, dot * 0.6, with_alpha(color, 120), dot * 1.6)
        img.fill_circle(dx, dy, dot / 2.0, color)
        if lv < 1:
            img.scale_alpha(lv)

    def _fade(self, t: Dict[str, Any], k: float) -> None:
        c, th = self.canvas, self.ui.theme
        t["lv"] = k
        idx = min(3, int(k * 4))
        c.itemconfigure(t["bg"], image=t["frames"][idx])
        bgc = th.panel_hi if th.dark else _WHITE
        c.itemconfigure(t["tx"], fill=mix(bgc, th.text, k))
        self._move(t)

    def _move(self, t: Dict[str, Any]) -> None:
        y = (t["y"] or 0) + t["dy"] * (1 - t["lv"])
        self.canvas.coords(t["bg"], t["x0"], y - t["pad"])
        self.canvas.coords(t["tx"], t["x0"] + t["tdx"], y + t["h"] / 2.0)

    def _restack(self) -> None:
        ui, c = self.ui, self.canvas
        gap = ui.px(8)
        ch = max(c.winfo_height(), int(c.cget("height") or 0), 60)
        y = ui.px(14)
        order = self._toasts if self.position == "top" else list(reversed(self._toasts))
        for t in order:
            ty = y if self.position == "top" else ch - y - t["h"]
            if t["y"] is None:
                t["y"] = ty
                self._move(t)
            elif abs(t["y"] - ty) > 0.5:
                y0 = t["y"]

                def step(k: float, t: Dict[str, Any] = t, y0: float = y0, ty: float = ty) -> None:
                    t["y"] = y0 + (ty - y0) * k
                    self._move(t)

                self.ui.anim.tween(0.2, step, "out_cubic", owner=self)
            y += t["h"] + gap

    def _fade_out(self, t: Dict[str, Any]) -> None:
        if t not in self._toasts:
            return
        t["dy"] = -self.ui.px(8)
        self.ui.anim.tween(0.22, lambda k, t=t: self._fade(t, 1 - k), "out_quad",
                           done=lambda t=t: self._kill(t), owner=self)

    def _kill(self, t: Dict[str, Any], fast: bool = False) -> None:
        if t in self._toasts:
            self._toasts.remove(t)
            _safe(self.canvas.delete, t["tag"])
            if not fast:
                self._restack()

    def _keep_on_top(self) -> None:
        self._ontop = None
        if self._toasts and not self._dead:
            _safe(self.canvas.tag_raise, self.tag)
            self._ontop = self._after(150, self._keep_on_top)

    def clear(self) -> None:
        for t in list(self._toasts):
            self._kill(t, fast=True)


# ================================================================== confetti
def confetti(ui: UI, canvas: tk.Canvas, x: float, y: float, count: int = 60,
             colors: Optional[Sequence[str]] = None) -> Optional[int]:
    """Particle burst (~1.2 s) that cleans up after itself. No-op unless theme.glow."""
    th = ui.theme
    if not th.glow:
        return None
    pal = list(colors or [p[0] for p in th.seats.values()] + [th.accent, th.accent2, th.warn])
    tag = "goipconf%d" % next(_uid)
    parts = []
    sp0 = ui.px(560)
    for _ in range(max(0, int(count))):
        ang = random.uniform(-math.pi * 0.92, -math.pi * 0.08)
        sp = random.uniform(0.35, 1.0) * sp0
        s = ui.px(random.uniform(5.5, 10.0))
        oval = random.random() < 0.3
        mk = canvas.create_oval if oval else canvas.create_polygon
        coords = (0, 0, 1, 1) if oval else (0, 0, 1, 0, 1, 1, 0, 1)
        item = mk(*coords, fill=random.choice(pal), outline="", tags=(tag,), state="disabled")
        parts.append((item, oval, math.cos(ang) * sp, math.sin(ang) * sp, s, random.uniform(0, 6.3),
                      random.uniform(-9, 9)))
    g = ui.px(1500)
    owner = object()

    def step(t: float) -> None:
        T = t * 1.2
        shrink = min(1.0, (1.0 - t) / 0.3)
        try:
            for item, oval, vx, vy, s, ph, spin in parts:
                px = x + vx * T * (1 - 0.3 * T)
                py = y + vy * T + 0.5 * g * T * T
                a = ph + spin * T
                hw = s * 0.5 * shrink
                hh = hw * (0.25 + 0.75 * abs(math.cos(a * 1.3)))
                if oval:
                    canvas.coords(item, px - hw * 0.7, py - hh * 0.7, px + hw * 0.7, py + hh * 0.7)
                else:
                    ca, sa = math.cos(a), math.sin(a)
                    pts = []
                    for qx, qy in ((-hw, -hh * 0.55), (hw, -hh * 0.55), (hw, hh * 0.55), (-hw, hh * 0.55)):
                        pts += [px + qx * ca - qy * sa, py + qx * sa + qy * ca]
                    canvas.coords(item, *pts)
            if t <= 0.05:
                canvas.tag_raise(tag)
        except tk.TclError:
            ui.anim.cancel_owner(owner)
        if t >= 1.0:
            _safe(canvas.delete, tag)

    return ui.anim.tween(1.2, step, "linear", done=lambda: _safe(canvas.delete, tag), owner=owner)


# ================================================================== Dice
class Dice(_Widget):
    """Clickable die with a tumble animation and an optional 'your turn' pulse ring."""

    _TILTS = (-24, -14, 14, 24)

    def __init__(self, ui: UI, canvas: tk.Canvas, size: int) -> None:
        super().__init__(ui, canvas)
        self.size = max(8, int(size))
        self.value = 1
        self.color: Optional[str] = None
        self.active = False
        self.cx = self.cy = 0.0
        self._click: Optional[Callable[[], None]] = None
        self._pulse = object()
        self._lift = 0.0
        self._warming = False
        self._bind("<ButtonRelease-1>", self._on_click)
        self._bind("<Enter>", lambda e: self._hand(self._click is not None))
        self._bind("<Leave>", lambda e: self._hand(False))

    def _colors(self) -> Tuple[str, str]:
        th = self.ui.theme
        face = th.board_a if th.key != "neon" else _WHITE
        light = gfx.luminance(face) > 0.5
        pip = (th.seat(self.color)[2] if self.color else "#262636") if light else th.text
        return face, pip

    def _face(self, value: int, angle: float = 0.0):
        face, pip = self._colors()
        S = self.size
        return self.ui.sprites.get(("die", face, pip, value, angle), S, S,
                                   lambda img: paint_die(img, value, face, pip, angle, shadow=False))

    def place(self, cx: float, cy: float) -> "Dice":
        self.cx, self.cy = float(cx), float(cy)
        self._draw()
        if not self._warming:
            self._warming = True
            self._after(300, self._warm)
        return self

    def _warm(self) -> None:
        """Paint the tumble frames in idle moments, one per callback, so the first roll is smooth."""
        face, pip = self._colors()
        for a in self._TILTS:
            for v in range(1, 7):
                if self.ui.sprites.peek(("die", face, pip, v, a), self.size, self.size) is None:
                    self._face(v, a)
                    self._after(30, self._warm)
                    return
        self._warming = False

    def _draw(self, value: Optional[int] = None, angle: float = 0.0) -> None:
        if self._dead:
            return
        ui, th, S = self.ui, self.ui.theme, self.size
        sh = ui.sprites.get(("dieshadow", th.key), S, S // 2,
                            lambda img: img.soft_ellipse(img.w / 2.0, img.h / 2.0, img.w * 0.36, img.h * 0.2,
                                                         (0, 0, 0, 110 if th.dark else 60), img.h * 0.35))
        self._img("shadow", sh, self.cx, self.cy + S * 0.38, anchor="center")
        self._img("die", self._face(value or self.value, angle), self.cx, self.cy - self._lift, anchor="center")
        self._hit(self.cx - S / 2.0, self.cy - S / 2.0, S, S)
        if "#ring" in self._refs:
            self.canvas.coords(self._refs["#ring"], self.cx, self.cy)
        if not self.visible:
            self.set_visible(False)

    def show(self, value: int) -> None:
        self.ui.anim.cancel_owner(self)
        self.value = max(1, min(6, int(value)))
        self._lift = 0.0
        self._draw()

    def roll(self, value: int, done: Optional[Callable[[], None]] = None) -> None:
        """~0.6 s tumble with a small bounce, landing on `value`; then done()."""
        self.ui.anim.cancel_owner(self)
        final = max(1, min(6, int(value)))
        frames, last = [], final
        for _ in range(9):
            v = random.choice([n for n in range(1, 7) if n != last])
            frames.append((v, random.choice(self._TILTS)))
            last = v
        state = {"i": -1}
        hop = self.size * 0.32

        def step(t: float) -> None:
            if self._dead:
                return
            if t >= 1.0:
                self.value = final
                self._lift = 0.0
                self._draw()
                return
            i = min(len(frames) - 1, int((1 - (1 - t) ** 2) * len(frames)))
            self._lift = hop * abs(math.sin(t * 2 * math.pi)) * (1 - t)
            if i != state["i"]:
                state["i"] = i
                self._draw(*frames[i])
            else:
                self.canvas.coords(self._refs["#die"], self.cx, self.cy - self._lift)

        self.ui.anim.tween(0.6, step, "linear", done=done, owner=self)

    def set_color(self, seat_color: Optional[str]) -> None:
        self.color = seat_color
        self._draw()
        if self.active:
            self._ring_frame(0.0)
        if not self._warming:
            self._warming = True
            self._after(300, self._warm)

    def set_active(self, b: bool) -> None:
        b = bool(b)
        if b == self.active:
            return
        self.active = b
        self.ui.anim.cancel_owner(self._pulse)
        if not b:
            self._drop("ring")
            return

        def loop() -> None:
            if self._dead or not self.active:
                return
            self.ui.anim.tween(1.6, lambda t: self._ring_frame(0.5 - 0.5 * math.cos(2 * math.pi * t)),
                               "linear", done=loop, owner=self._pulse)

        self._ring_frame(0.0)
        loop()

    def _ring_frame(self, k: float) -> None:
        if self._dead or not self.active:
            return
        th, S = self.ui.theme, self.size
        lv = int(round(k * 5))
        color = th.seat(self.color)[0] if self.color else th.accent
        R = int(S * 0.92)

        def paint(img: Image) -> None:
            c = img.w / 2.0
            if th.glow:
                img.soft_circle(c, c, S * 0.56 + lv * S * 0.015, with_alpha(color, 70 + lv * 22), S * 0.32)
            else:
                img.stroke_circle(c, c, S * 0.62 + lv * S * 0.012, with_alpha(color, 110 + lv * 22),
                                  max(1.5, S * 0.05))

        ph = self.ui.sprites.get(("diering", th.key, color, lv, S), R * 2, R * 2, paint)
        new = "#ring" not in self._refs
        item = self._img("ring", ph, self.cx, self.cy, anchor="center")
        if new:
            self.canvas.tag_lower(item, self._refs.get("#shadow", self.tag))
        if not self.visible:
            self.canvas.itemconfigure(item, state="hidden")

    def bind_click(self, fn: Optional[Callable[[], None]]) -> None:
        self._click = fn

    def _on_click(self, e: Any) -> None:
        if self._click is not None and self.visible and not self._dead:
            self._click()

    def destroy(self) -> None:
        self.ui.anim.cancel_owner(self._pulse)
        super().destroy()


# ================================================================== PlayerChip
class PlayerChip(_Widget):
    """Card with glossy avatar, name, subtitle, BOT/YOU tags, turn glow and countdown arc."""

    def __init__(self, ui: UI, canvas: tk.Canvas) -> None:
        super().__init__(ui, canvas)
        self.info: Dict[str, Any] = {"name": "", "color": "red", "bot": False, "active": False,
                                     "subtitle": "", "timer": None, "me": False}
        self._deadline: Optional[float] = None
        self._total = 0.0
        self._tick_id: Optional[str] = None

    def place(self, x: float, y: float, w: float, h: float) -> "PlayerChip":
        self.x, self.y, self.w, self.h = int(round(x)), int(round(y)), max(1, int(round(w))), max(1, int(round(h)))
        self._draw()
        return self

    def update(self, name: str, color: str, bot: bool = False, active: bool = False, subtitle: str = "",
               timer: Optional[float] = None, me: bool = False, timer_total: Optional[float] = None) -> None:
        self.info = {"name": name or "", "color": color or "red", "bot": bool(bot), "active": bool(active),
                     "subtitle": subtitle or "", "timer": timer, "me": bool(me)}
        now = time.monotonic()
        if timer is None or not active:
            self._deadline = None
        else:
            left = max(0.0, float(timer))
            remaining = (self._deadline - now) if self._deadline is not None else -1.0
            if timer_total:
                self._total = float(timer_total)
            elif self._deadline is None or left > remaining + 0.75:
                self._total = max(left, 1.0)
            self._total = max(self._total, left, 1.0)
            self._deadline = now + left
        self._draw()

    def _draw(self) -> None:
        if self._dead or not self.w:
            return
        ui, th, inf = self.ui, self.ui.theme, self.info
        main, light, dark = th.seat(inf["color"])
        act = inf["active"]
        r = min(ui.px(14), self.h / 2.0, self.w / 2.0)
        pad = min(ui.px(12), max(2, min(self.w, self.h) // 4))
        off = max(1, pad // 3)

        def paint(img: Image) -> None:
            paint_panel(img, th, "card", r, pad, off)
            if act:
                x, y, w, h = pad, pad, img.w - 2 * pad, img.h - 2 * pad
                if th.glow:
                    img.soft_rrect(x, y, w, h, r, with_alpha(main, 70), pad * 1.2)
                    img.fill_rrect(x, y, w, h, r, th.panel_hi)
                img.stroke_rrect(x, y, w, h, r, main, max(1.5, ui.px(2)))

        b = pad + int(math.ceil(r)) + 2
        self._img("card", sliced(ui, ("chip", th.key, act, main, r, pad), self.w + 2 * pad, self.h + 2 * pad,
                                 (b, b, b, b), paint), self.x - pad, self.y - pad)
        ip = max(2, min(ui.px(8), self.h // 6))
        d = max(4, self.h - 2 * ip - ui.px(4))
        acx, acy = self.x + ip + ui.px(2) + d / 2.0, self.y + self.h / 2.0
        timed = act and self._deadline is not None
        if act and (th.glow or not timed):
            gs = int(d * 1.6)

            def ring(img: Image) -> None:
                cc = img.w / 2.0
                if th.glow:
                    img.soft_circle(cc, cc, d * 0.56, with_alpha(main, 150), d * 0.35)
                else:
                    img.stroke_circle(cc, cc, d / 2.0 + max(2.0, d * 0.08), main, max(1.5, d * 0.06))

            self._img("glow", ui.sprites.get(("chipglow", th.key, main, d), gs, gs, ring), acx, acy, anchor="center")
        else:
            self._drop("glow")
        av = ui.sprites.get(("avatar", main, light, dark), d, d, lambda img: paint_avatar(img, main, light, dark))
        self._img("avatar", av, acx, acy, anchor="center")
        initial = (inf["name"].strip()[:1] or "?").upper()
        self._text("initial", acx, acy, initial, ui.font(max(6, d * 0.46 / ui.scale), bold=True), _WHITE)
        tx = acx + d / 2.0 + max(2, ui.px(10))
        right = self.x + self.w - max(2, ui.px(10))
        tags = (["YOU"] if inf["me"] else []) + (["BOT"] if inf["bot"] else [])
        tf = ui.font(10, bold=True)
        tag_w = [ui.measure(s, tf) + ui.px(12) for s in tags]
        reserve = sum(tag_w) + ui.px(6) * len(tags)
        name, nf = ui.fit(inf["name"], 14, max(4, right - tx - reserve), bold=True)
        sub = inf["subtitle"]
        two = bool(sub) and self.h >= ui.px(40)
        ny = acy - (ui.linespace(ui.font(12)) / 2.0 if two else 0)
        self._text("name", tx, ny, name, nf, th.text, anchor="w")
        if two:
            s, sf = ui.fit(sub, 12, max(4, right - tx))
            self._text("sub", tx, ny + ui.linespace(nf) * 0.5 + ui.linespace(sf) * 0.55, s, sf, th.text_dim,
                       anchor="w")
        else:
            self._drop("sub")
        for k in [k[1:] for k in self._refs if k.startswith("#tag") or k.startswith("#tt")]:
            self._drop(k)
        x = tx + ui.measure(name, nf) + ui.px(6)
        th_ = max(6, ui.px(17))
        for i, (s, tw) in enumerate(zip(tags, tag_w)):
            if x + tw > right + 1:
                break
            col = th.accent if s == "YOU" else th.text_faint
            ph = ui.sprites.get(("tagpill", th.key, s, col), tw, th_,
                                lambda img, col=col: img.fill_rrect(0, 0, img.w, img.h, img.h / 2.0,
                                                                    with_alpha(col, 70 if th.dark else 45)))
            self._img("tag%d" % i, ph, x, ny, anchor="w")
            self._text("tt%d" % i, x + tw / 2.0, ny, s, tf, th.text if th.dark else th.text_dim)
            x += tw + ui.px(6)
        self._arc_geom = (acx, acy, d / 2.0 + max(2, ui.px(4)))
        if self._tick_id is not None:
            _safe(self.canvas.after_cancel, self._tick_id)
            self._afters.discard(self._tick_id)
        self._update_arc()
        self._z(["card", "glow", "avatar", "initial", "name", "sub"]
                + ["tag%d" % i for i in range(2)] + ["tt%d" % i for i in range(2)]
                + ["track", "arc", "cap0", "cap1"])
        if not self.visible:
            self.set_visible(False)

    def _update_arc(self) -> None:
        """Countdown ring: faint AA track sprite, a Tk arc for the time left, round AA end caps."""
        self._tick_id = None
        if self._dead:
            return
        if self._deadline is None or not self.info["active"]:
            for n in ("track", "arc", "cap0", "cap1"):
                self._drop(n)
            return
        ui, th, c = self.ui, self.ui.theme, self.canvas
        left = max(0.0, self._deadline - time.monotonic())
        frac = left / max(0.001, self._total)
        acx, acy, R = self._arc_geom
        col = th.seat(self.info["color"])[0] if left > 10 else (th.warn if left > 5 else th.bad)
        wdt = max(2, ui.px(3))
        S = int(2 * R + wdt + 4)
        tcol = with_alpha(th.text_faint if th.dark else th.edge, 110 if th.dark else 255)
        track = ui.sprites.get(("chiptrack", th.key, R, wdt), S, S,
                               lambda img: img.stroke_circle(img.w / 2.0, img.h / 2.0, R + wdt / 2.0, tcol, wdt))
        new = "#track" not in self._refs
        self._img("track", track, acx, acy, anchor="center")
        ext = -359.9 * frac
        box = (acx - R, acy - R, acx + R, acy + R)
        state = "disabled" if self.visible else "hidden"
        item = self._refs.get("#arc")
        if frac <= 0.004:
            self._drop("arc")
        elif item is None:
            self._refs["#arc"] = c.create_arc(*box, start=90, extent=ext, style="arc", width=wdt, outline=col,
                                              tags=(self.tag,), state=state)
        else:
            c.coords(item, *box)
            c.itemconfigure(item, extent=ext, outline=col, width=wdt, state=state)
        cs = max(2, int(wdt) + 1)
        cap = ui.sprites.get(("cap", col), cs, cs, lambda img: img.fill_circle(cs / 2.0, cs / 2.0, wdt / 2.0 + 0.3, col))
        a = math.radians(90.0 + ext)
        for name, x, y in (("cap0", acx, acy - R), ("cap1", acx + R * math.cos(a), acy - R * math.sin(a))):
            if frac <= 0.004:
                self._drop(name)
            else:
                self._img(name, cap, x, y, anchor="center")
        if new:
            self._z(["track", "arc", "cap0", "cap1"])
        if not self.visible:
            _safe(c.itemconfigure, self.tag, state="hidden")
        if left > 0:
            self._tick_id = self._after(100, self._update_arc)

    def destroy(self) -> None:
        self._deadline = None
        super().destroy()
