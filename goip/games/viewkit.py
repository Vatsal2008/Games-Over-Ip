"""Small helpers shared by the game views (named canvas items, token tweening)."""
from __future__ import annotations

import math
import tkinter as tk
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

Point = Tuple[float, float]


class Items:
    """Create-or-update named canvas items and keep photo references alive."""

    def __init__(self, canvas: tk.Canvas, prefix: str) -> None:
        self.canvas = canvas
        self.prefix = prefix
        self.ids: Dict[str, int] = {}
        self.refs: Dict[str, Any] = {}

    def tag(self, name: str) -> str:
        return self.prefix + name

    def text(self, name: str, x: float, y: float, text: str, font: Any, fill: str, anchor: str = "center",
             state: str = "disabled", **kw: Any) -> int:
        item = self.ids.get(name)
        if item is None:
            item = self.canvas.create_text(x, y, text=text, font=font, fill=fill, anchor=anchor, state=state,
                                           tags=(self.prefix, self.tag(name)), **kw)
            self.ids[name] = item
        else:
            self.canvas.coords(item, x, y)
            self.canvas.itemconfigure(item, text=text, font=font, fill=fill, anchor=anchor, state=state, **kw)
        return item

    def image(self, name: str, photo: Any, x: float, y: float, anchor: str = "center", state: str = "normal",
              extra_tags: Sequence[str] = ()) -> int:
        self.refs[name] = photo
        item = self.ids.get(name)
        if item is None:
            item = self.canvas.create_image(x, y, image=photo, anchor=anchor, state=state,
                                            tags=(self.prefix, self.tag(name)) + tuple(extra_tags))
            self.ids[name] = item
        else:
            self.canvas.coords(item, x, y)
            self.canvas.itemconfigure(item, image=photo, anchor=anchor, state=state)
        return item

    def move(self, name: str, x: float, y: float) -> None:
        item = self.ids.get(name)
        if item is not None:
            self.canvas.coords(item, x, y)

    def hide(self, name: str) -> None:
        item = self.ids.get(name)
        if item is not None:
            self.canvas.itemconfigure(item, state="hidden")

    def delete(self, name: str) -> None:
        item = self.ids.pop(name, None)
        self.refs.pop(name, None)
        if item is not None:
            try:
                self.canvas.delete(item)
            except tk.TclError:
                pass

    def delete_prefix(self, start: str) -> None:
        for name in [n for n in self.ids if n.startswith(start)]:
            self.delete(name)

    def has(self, name: str) -> bool:
        return name in self.ids

    def raise_(self, name: str) -> None:
        item = self.ids.get(name)
        if item is not None:
            self.canvas.tag_raise(item)

    def clear(self) -> None:
        try:
            self.canvas.delete(self.prefix)
        except tk.TclError:
            pass
        self.ids.clear()
        self.refs.clear()


def lerp(a: Point, b: Point, t: float) -> Point:
    return (a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t)


def along(points: Sequence[Point], t: float) -> Point:
    """Point at fraction t of the polyline's length."""
    if len(points) == 1:
        return points[0]
    segs = []
    total = 0.0
    for a, b in zip(points, points[1:]):
        d = ((b[0] - a[0]) ** 2 + (b[1] - a[1]) ** 2) ** 0.5
        segs.append(d)
        total += d
    if total <= 0:
        return points[-1]
    target = max(0.0, min(1.0, t)) * total
    for (a, b), d in zip(zip(points, points[1:]), segs):
        if target <= d:
            return lerp(a, b, target / d if d else 1.0)
        target -= d
    return points[-1]


def hop_path(anim: Any, owner: Any, points: Sequence[Point], step_time: float, hop: float,
             on_pos: Callable[[float, float, float], None], done: Optional[Callable[[], None]] = None) -> None:
    """Move through points one hop per segment. on_pos(x, y, lift) each frame."""
    pts: List[Point] = list(points)
    if len(pts) < 2:
        if pts:
            on_pos(pts[0][0], pts[0][1], 0.0)
        if done:
            done()
        return
    n = len(pts) - 1

    def step(t: float) -> None:
        f = t * n
        i = min(n - 1, int(f))
        k = f - i
        if t >= 1.0:
            i, k = n - 1, 1.0
        x, y = lerp(pts[i], pts[i + 1], k)
        on_pos(x, y, hop * math.sin(math.pi * k))

    anim.tween(step_time * n, step, "linear", done=done, owner=owner)
