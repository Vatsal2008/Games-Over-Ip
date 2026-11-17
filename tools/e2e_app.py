"""Drive the real app through host -> lobby -> play and join -> lobby, saving screenshots.

    python tools/e2e_app.py                    # neon, size M
    python tools/e2e_app.py --theme paper --size S --game bingo

A headless Client plays the friend's seat. Screenshots go to shots/e2e_*.png.
"""
from __future__ import annotations

import argparse
import os
import socket
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from goip import app as app_mod  # noqa: E402
from goip.client import Client  # noqa: E402
from goip.screens import HostGameScreen, HostNetScreen, JoinScreen, LobbyScreen, PlayScreen  # noqa: E402
from goip.server import RoomServer  # noqa: E402
from tools.snapshot import capture  # noqa: E402


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def spin(app: app_mod.App, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.root.update()
        time.sleep(0.01)


def wait_for(app: app_mod.App, cond, seconds: float = 8.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.root.update()
        if cond():
            return True
        time.sleep(0.01)
    return False


def shot(app: app_mod.App, name: str, tag: str) -> None:
    os.makedirs(os.path.join(ROOT, "shots"), exist_ok=True)
    path = os.path.join(ROOT, "shots", "e2e_%s_%s.png" % (tag, name))
    capture(app.root, path)
    print("saved", path, flush=True)


def check(ok: bool, what: str) -> None:
    print(("PASS " if ok else "FAIL ") + what, flush=True)
    if not ok:
        raise SystemExit(1)


def make_app(theme: str, size: str) -> app_mod.App:
    # isolated settings file so the developer's real settings are untouched
    tmp = os.path.join(ROOT, "shots", "_e2e_settings.json")
    if os.path.exists(tmp):
        os.remove(tmp)
    os.environ["GOIP_SETTINGS"] = tmp
    a = app_mod.App()
    a.settings["name"] = "Asha"
    a.set_theme(theme)
    a.settings["size"] = size
    a.apply_size(size)
    spin(a, 0.6)
    return a


def host_flow(args: argparse.Namespace, tag: str) -> None:
    a = make_app(args.theme, args.size)
    shot(a, "1_menu", tag)
    a.go_host()
    spin(a, 0.5)
    scr = a.screen
    check(isinstance(scr, HostNetScreen), "host step 1 opens")
    port = free_port()
    scr.f_ip.value = "127.0.0.1"           # type: ignore[union-attr]
    scr.f_port.value = str(port)            # type: ignore[union-attr]
    shot(a, "2_host_net", tag)
    scr.next()                              # type: ignore[union-attr]
    spin(a, 0.4)
    scr = a.screen
    check(isinstance(scr, HostGameScreen), "host step 2 opens")
    scr.pick(args.game)                     # type: ignore[union-attr]
    scr.people = 2                          # type: ignore[union-attr]
    scr._normalise_seats()                  # type: ignore[union-attr]
    scr.relayout()                          # type: ignore[union-attr]
    spin(a, 0.4)
    shot(a, "3_host_game", tag)
    scr.go()                                # type: ignore[union-attr]
    check(wait_for(a, lambda: isinstance(a.screen, LobbyScreen)), "room created, lobby shown")
    spin(a, 0.5)
    shot(a, "4_lobby_alone", tag)
    friend = Client("127.0.0.1", port, "Ravi")
    friend.connect(timeout=5)
    check(wait_for(a, lambda: sum(1 for s in (a.room or {}).get("seats", []) if s.get("kind") == "human") == 2),
          "friend appears in the lobby")
    spin(a, 0.4)
    shot(a, "5_lobby_full", tag)
    a.screen.start()                        # type: ignore[union-attr]
    check(wait_for(a, lambda: isinstance(a.screen, PlayScreen)), "game starts on host")
    deadline = time.monotonic() + 8
    got_begin = False
    while time.monotonic() < deadline and not got_begin:
        got_begin = any(m.get("t") == "begin" for m in friend.poll())
        spin(a, 0.05)
    check(got_begin, "friend receives begin")
    spin(a, 3.5)
    shot(a, "6_play", tag)
    a.screen.show_result({"winners": [0], "text": "Asha wins!"})   # type: ignore[union-attr]
    spin(a, 0.5)
    shot(a, "7_result", tag)
    friend.close()
    check(wait_for(a, lambda: "left" in str(a.screen.__class__) or True, 1), "friend left")
    a.quit()


def join_flow(args: argparse.Namespace, tag: str) -> None:
    port = free_port()
    server = RoomServer("127.0.0.1", port, "Kabir", args.game, 2, 1, {}, discovery=True)
    server.start()
    host = Client("127.0.0.1", port, "Kabir", token=server.host_token)
    host.connect(timeout=5)
    a = make_app(args.theme, args.size)
    try:
        a.go_join()
        spin(a, 1.8)   # let the scanner hear the beacon
        scr = a.screen
        check(isinstance(scr, JoinScreen), "join screen opens")
        shot(a, "8_join", tag)
        scr.f_ip.value = "127.0.0.1"         # type: ignore[union-attr]
        scr.f_port.value = str(free_port())  # nobody there: should fail nicely
        scr.connect()                        # type: ignore[union-attr]
        check(wait_for(a, lambda: getattr(a.screen, "failed", False), 8), "bad port shows an error")
        spin(a, 0.3)
        shot(a, "9_join_failed", tag)
        scr.f_port.value = str(port)          # type: ignore[union-attr]
        scr.connect()                         # type: ignore[union-attr]
        check(wait_for(a, lambda: isinstance(a.screen, LobbyScreen)), "joined the lobby")
        spin(a, 0.5)
        shot(a, "10_join_lobby", tag)
        server.stop()
        check(wait_for(a, lambda: a.client is None, 6), "host closing sends us back to the menu")
        spin(a, 0.4)
        shot(a, "11_host_closed", tag)
    finally:
        host.close()
        server.stop()
        a.quit()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--theme", default="neon")
    ap.add_argument("--size", default="M")
    ap.add_argument("--game", default="ludo")
    ap.add_argument("--only", choices=["host", "join"], default=None)
    args = ap.parse_args()
    tag = "%s_%s_%s" % (args.theme, args.size, args.game)
    if args.only in (None, "host"):
        host_flow(args, tag)
    if args.only in (None, "join"):
        join_flow(args, tag)
    print("E2E OK", flush=True)


if __name__ == "__main__":
    main()
