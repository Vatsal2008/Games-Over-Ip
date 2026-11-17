"""Play seat 0 of each game with synthetic mouse clicks against a real server (bots elsewhere).

    python tools/click_play.py                 # all games
    python tools/click_play.py ludo snakes

The idle timer is off, so if a click misses, the game stalls and this reports FAIL.
"""
from __future__ import annotations

import os
import sys
import time
import tkinter as tk
from typing import Any, Dict, List, Optional

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from goip import protocol as P, widgets  # noqa: E402
from goip.client import Client  # noqa: E402
from goip.games import GAME_IDS, view_class  # noqa: E402
from goip.games.viewbase import ViewContext  # noqa: E402
from goip.server import RoomServer  # noqa: E402
from goip.theme import NEON  # noqa: E402

OPTIONS = {"tictactoe": {"match": 1, "turn_timer": 0}, "snakes": {"turn_timer": 0},
           "ludo": {"goal": 2, "turn_timer": 0}, "bingo": {"turn_timer": 0}}


def click(canvas: tk.Canvas, x: float, y: float) -> None:
    x, y = int(x), int(y)
    canvas.event_generate("<Motion>", x=x, y=y)
    canvas.event_generate("<ButtonPress-1>", x=x, y=y)
    canvas.event_generate("<ButtonRelease-1>", x=x, y=y)


def play(game: str, timeout: float = 240.0) -> bool:
    root = tk.Tk()
    root.geometry("420x560+30+30")
    ui = widgets.UI(root, NEON)
    canvas = tk.Canvas(root, width=420, height=560, highlightthickness=0)
    canvas.pack(fill="both", expand=True)
    server = RoomServer("127.0.0.1", 0, "Me", game, 1, 1, OPTIONS[game], discovery=False)
    server.start()
    client = Client(*server.address, "Me", token=server.host_token)
    client.connect(timeout=5)
    client.send({"t": P.START})
    view: Optional[Any] = None
    over: Optional[Dict[str, Any]] = None
    clicks = 0
    errors: List[str] = []
    root.report_callback_exception = lambda *e: errors.append(repr(e[1]))  # type: ignore[assignment]
    last_click = 0.0
    last_progress = time.monotonic()
    start = time.monotonic()
    try:
        while time.monotonic() - start < timeout and over is None:
            for msg in client.poll():
                t = msg.get("t")
                if t == P.BEGIN:
                    ctx = ViewContext(ui=ui, my_seat=msg["seat"], seats=msg["seats"], options=msg["options"],
                                      is_host=True, send_action=client.act, toast=lambda s: None)
                    view = view_class(game)(canvas, ctx)
                    root.update()
                    view.on_resize(420, 560)
                elif t == P.STATE and view:
                    view.on_state(msg["s"])
                    last_progress = time.monotonic()
                elif t == P.EVENT and view:
                    view.on_event(msg["e"])
                elif t == P.OVER:
                    over = msg["result"]
                elif t == P.ERROR:
                    errors.append("server error: " + msg.get("msg", ""))
            root.update()
            time.sleep(0.01)
            if view is None or view.state is None or time.monotonic() - last_click < 0.35:
                continue
            if time.monotonic() - last_progress > 25:
                st = view.state
                print("FAIL %s: stalled (a click did not register). busy=%s anim=%s target=%s state=%s" % (
                    game, getattr(view, "busy", None), ui.anim.busy(view), pick_target(game, view, st),
                    {k: st.get(k) for k in ("phase", "turn", "movable", "auto", "dice", "tokens")}))
                return False
            st = view.state
            if getattr(view, "busy", False) or (game != "ludo" and ui.anim.busy(view)):
                continue      # ludo's "pick me" bob loops forever while a choice is pending
            target = pick_target(game, view, st)
            if target is not None:
                click(canvas, *target)
                clicks += 1
                last_click = time.monotonic()
        ok = over is not None and not errors
        print("%s %s: %d clicks, result=%r, errors=%s" % ("PASS" if ok else "FAIL", game, clicks,
                                                        over and over.get("text"), errors[:3]))
        return ok
    finally:
        client.close()
        server.stop()
        if view is not None:
            view.destroy()
        root.destroy()


def pick_target(game: str, view: Any, st: Dict[str, Any]) -> Optional[tuple]:
    me = view.ctx.my_seat
    if game == "tictactoe":
        if st.get("phase") == "play" and st.get("turn") == me:
            free = [i for i, v in enumerate(st["board"]) if v is None]
            if free:
                x, y, _s = view._cell_box(free[0])
                return x, y
        return None
    if game == "snakes":
        if st.get("turn") == me and st.get("winner") is None:
            return view.dice.cx, view.dice.cy
        return None
    if game == "ludo":
        if st.get("turn") != me or st.get("finished"):
            return None
        if st.get("phase") == "roll":
            return view.dice.cx, view.dice.cy
        if st.get("phase") == "move" and not st.get("auto") and st.get("movable"):
            k = st["movable"][-1]
            u = view.geo[2]
            x, y = view.xy(view.color_of(me), view.tokens[me][k], k)
            return x, y - u * 0.3
        return None
    if game == "bingo":
        if st.get("phase") == "arrange":
            if not st["ready"][me]:
                b = view.b_ready
                return b.x + b.w / 2, b.y + b.h / 2
            return None
        if st.get("turn") == me and not st.get("winners"):
            marks = st.get("marks") or []
            for i, m in enumerate(marks):
                if not m:
                    x, y = view._cell_xy(i)
                    cell = view.geo[2]
                    return x + cell / 2, y + cell / 2
        return None
    return None


def main() -> None:
    games = sys.argv[1:] or list(GAME_IDS)
    results = [play(g) for g in games]
    print("ALL PASS" if all(results) else "SOME FAILED")
    sys.exit(0 if all(results) else 1)


if __name__ == "__main__":
    main()
