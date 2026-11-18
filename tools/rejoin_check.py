"""Two real app processes: a host and a joiner. The joiner leaves mid-game, a bot plays
for them, then they rejoin and must continue from the bot's progress.

    python tools/rejoin_check.py [game]       # game: snakes (default), ludo, bingo, tictactoe

Both apps use the same name on purpose (like two windows on one PC). Screenshots go to shots/.
"""
from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

NAME = "TestAri"


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def spin(app, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.root.update()
        time.sleep(0.01)


def wait_for(app, cond, seconds: float = 10.0) -> bool:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.root.update()
        if cond():
            return True
        time.sleep(0.01)
    return False


def host_main(port: int, game: str, seconds: float) -> None:
    os.environ["GOIP_SETTINGS"] = os.path.join(tempfile.mkdtemp(), "host.json")
    from goip.app import App
    from goip.screens import HostGameScreen, LobbyScreen, PlayScreen
    a = App()
    a.settings["name"] = NAME
    a.host_net = (NAME, "127.0.0.1", port)
    a.show(HostGameScreen, mode="create")
    scr = a.screen
    scr.pick(game)
    scr.people, scr.bots = 2, 0
    scr._normalise_seats()
    scr.go()
    assert wait_for(a, lambda: isinstance(a.screen, LobbyScreen)), "host: no lobby"
    print("HOST room ready", flush=True)
    assert wait_for(a, lambda: sum(s["kind"] == "human" for s in a.room["seats"]) == 2, 30), "host: nobody joined"
    a.screen.start()
    assert wait_for(a, lambda: isinstance(a.screen, PlayScreen)), "host: no play screen"
    print("HOST game started", flush=True)
    from tools.click_play import click, pick_target
    end = time.monotonic() + seconds
    while time.monotonic() < end and a.server is not None:
        spin(a, 0.4)
        scr = a.screen
        view = getattr(scr, "view", None)
        if isinstance(scr, PlayScreen) and view is not None and view.state and not getattr(view, "busy", False):
            target = pick_target(game, view, view.state)      # the host plays its own turns
            if target is not None:
                click(scr.vcanvas, *target)
    a.quit()


def joiner_main(port: int, game: str) -> int:
    os.environ["GOIP_SETTINGS"] = os.path.join(tempfile.mkdtemp(), "joiner.json")
    from goip.app import App
    from goip.screens import JoinScreen, MenuScreen, PlayScreen
    from tools.snapshot import capture
    a = App()
    a.settings["name"] = NAME
    ok = True

    def check(cond: bool, what: str) -> None:
        nonlocal ok
        print(("PASS " if cond else "FAIL ") + what, flush=True)
        ok = ok and cond

    def progress(st):
        return {k: st.get(k) for k in ("pos", "tokens", "last_roll", "marks", "board", "called") if k in st}

    try:
        a.go_join()
        a.screen.f_ip.value, a.screen.f_port.value = "127.0.0.1", str(port)
        a.screen.connect()
        check(wait_for(a, lambda: isinstance(a.screen, PlayScreen), 30), "joined and game started")
        seat = a.begin["seat"]
        my_name = a.begin["seats"][seat]["name"]
        check(wait_for(a, lambda: a.screen.view.state is not None), "got first state")
        before = progress(a.screen.view.state)
        a.leave()
        check(isinstance(a.screen, MenuScreen), "left to the menu")
        spin(a, 25.0 if game == "ludo" else 9.0)       # the bot plays our seat meanwhile
        a.go_join()
        check(isinstance(a.screen, JoinScreen), "join screen again")
        a.screen.connect()
        back = wait_for(a, lambda: isinstance(a.screen, PlayScreen), 15)
        check(back, "rejoined the running game")
        if not back:
            print("   join screen says:", getattr(a.screen, "error_text", "?"), flush=True)
            return 1
        check(a.begin["seat"] == seat, "same seat (%s)" % seat)
        check(a.begin["seats"][seat]["name"] == my_name and not a.begin["seats"][seat]["bot"],
              "seat is mine again, not a bot")
        check(wait_for(a, lambda: a.screen.view.state is not None), "got state after rejoin")
        spin(a, 1.5)
        after = progress(a.screen.view.state)
        print("   before leaving:", before, flush=True)
        print("   after rejoin  :", after, flush=True)
        check(after != before, "bot's progress is on the board (state moved on)")
        os.makedirs(os.path.join(ROOT, "shots"), exist_ok=True)
        capture(a.root, os.path.join(ROOT, "shots", "rejoin_%s.png" % game))
    finally:
        a.quit()
    return 0 if ok else 1


def main() -> None:
    if len(sys.argv) > 2 and sys.argv[1] == "--host":
        host_main(int(sys.argv[2]), sys.argv[3], float(sys.argv[4]))
        return
    game = sys.argv[1] if len(sys.argv) > 1 else "snakes"
    port = free_port()
    host = subprocess.Popen([sys.executable, os.path.abspath(__file__), "--host", str(port), game, "60"],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    try:
        line = ""
        end = time.monotonic() + 20
        while time.monotonic() < end and "room ready" not in line:
            line = host.stdout.readline()
        code = joiner_main(port, game)
    finally:
        host.terminate()
        try:
            out = host.communicate(timeout=10)[0]
        except subprocess.TimeoutExpired:
            host.kill()
            out = ""
        bad = [l for l in out.splitlines() if "Traceback" in l or "Error" in l or "FAIL" in l or "assert" in l]
        if bad:
            print("HOST:", *bad[:8], sep="\n  ")
    print("REJOIN OK" if code == 0 else "REJOIN FAILED")
    sys.exit(code)


if __name__ == "__main__":
    main()
