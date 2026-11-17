"""RoomServer: the authoritative TCP lobby and game host.

    server = RoomServer("192.168.1.5", 50505, "Asha", "ludo", humans=2, bots=2)
    server.start()            # binds now (raises OSError), then runs in a daemon thread
    ...                       # the host UI connects a Client with server.host_token
    server.stop()             # thread-safe, idempotent

Everything (sockets, room rules, timers and ALL game code) runs on the server
thread, so games never need locks. See docs/ARCHITECTURE.md sections 3-5.
"""
from __future__ import annotations

import copy
import heapq
import hmac
import itertools
import logging
import random
import secrets
import select
import selectors
import socket
import threading
import time
from contextlib import suppress
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import APP_ID, PROTOCOL_VERSION
from . import protocol as P
from .games import logic_class
from .games.base import ActionError, GameHost, Seat
from .netutil import best_ipv4

log = logging.getLogger(__name__)

BOT_NAMES = ("Mia", "Leo", "Zara", "Kai", "Nova", "Ravi", "Ivy", "Max", "Luna", "Arjun", "Tara", "Finn")

MSG_FULL = "Room is full"
MSG_RUNNING = "A game is running. Try again when it ends."
MSG_HOST_CLOSED = "Host closed the room"
MSG_KICKED = "You were removed by the host"
MSG_CRASH = "Something went wrong on the host"


class _Timer:
    __slots__ = ("when", "fn", "cancelled")

    def __init__(self, when: float, fn: Callable[[], None]) -> None:
        self.when = when
        self.fn: Optional[Callable[[], None]] = fn
        self.cancelled = False


class _RoomSeat:
    __slots__ = ("index", "kind", "name", "client", "color", "was_name")

    def __init__(self, index: int, kind: str, name: Optional[str], client: Optional[int], color: str) -> None:
        self.index, self.kind, self.name = index, kind, name    # kind: "human" | "open" | "bot"
        self.client, self.color = client, color                 # client id holding the seat
        self.was_name: Optional[str] = None   # human who left mid-game (can reclaim)

    def to_dict(self) -> Dict[str, Any]:
        return {"seat": self.index, "kind": self.kind, "name": self.name, "client": self.client,
                "color": self.color, "host": self.index == 0}


class _Conn:
    def __init__(self, sock: socket.socket, cid: int, addr: Any, now: float) -> None:
        self.sock, self.cid, self.addr = sock, cid, addr
        self.buf = P.LineBuffer()
        self.out = bytearray()
        self.name = ""
        self.is_host = False
        self.seat: Optional[int] = None
        self.ready = False            # hello accepted
        self.opened = self.last_recv = now
        self.closing = False          # flush what's queued, then close
        self.close_by = 0.0
        self.dead = self.want_write = False


def _fmt_counts(counts: Tuple[int, ...]) -> str:
    vals = sorted(set(int(c) for c in counts))
    if len(vals) == 1:
        return str(vals[0])
    if vals == list(range(vals[0], vals[-1] + 1)):
        return "%d to %d" % (vals[0], vals[-1])
    return ", ".join(str(v) for v in vals[:-1]) + " or %d" % vals[-1]


class RoomServer(GameHost):
    """One room: lobby, seats, and the running game. Implements GameHost for the game."""

    HELLO_TIMEOUT = 5.0         # seconds to send hello after connecting
    IDLE_TIMEOUT = 15.0         # seconds without any bytes from a client
    MAX_CLIENTS = 8             # sockets, including ones still saying hello
    MAX_OUTBUF = 1 << 20        # bytes queued per client before we give up on it
    TICK = 0.25                 # housekeeping period (seconds)
    CLOSE_GRACE = 2.0           # seconds to flush a reject / bye before closing anyway

    def __init__(self, bind_ip: str, port: int, host_name: str, game_id: str, humans: int, bots: int,
                 options: Optional[Dict[str, Any]] = None, *, discovery: bool = True,
                 resolver: Optional[Callable[[str], Any]] = None, seed: Optional[int] = None) -> None:
        self.bind_ip = bind_ip or "0.0.0.0"
        self.port = int(port)
        self.host_name = P.clean_name(host_name, "Host")
        self.host_token = secrets.token_hex(16)
        self.address: Tuple[str, int] = (self.bind_ip, self.port)
        self._resolver = resolver or logic_class
        self._rng = random.Random(seed)
        self._discovery = discovery

        cls = self._resolve(game_id)
        problem = self._check_counts(cls, humans, bots)
        if problem:
            raise ValueError(problem)
        self.game_id: str = game_id
        self.logic_cls: Any = cls
        self.humans: int = humans
        self.bots: int = bots
        self.options: Dict[str, Any] = cls.clean_options(options if isinstance(options, dict) else {})
        self.state = "lobby"
        self.logic: Any = None

        names = list(BOT_NAMES)
        self._rng.shuffle(names)
        self._bot_names = names
        self._seats: List[_RoomSeat] = []
        self._conns: Dict[int, _Conn] = {}
        self._cids = itertools.count(1)
        self._host_cid: Optional[int] = None
        self._doomed: List[_Conn] = []
        self._timers: List[Tuple[float, int, _Timer]] = []
        self._tseq = itertools.count()

        self._sel: Optional[selectors.BaseSelector] = None
        self._lsock: Optional[socket.socket] = None
        self._wake_r: Optional[socket.socket] = None
        self._wake_w: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop_evt = threading.Event()
        self._stop_lock = threading.Lock()
        self._beacon: Any = None
        self._shown_ip = self.bind_ip

        self._relayout()
        self._snapshot = self._build_room()

    # ================================================================ public API
    def start(self) -> "RoomServer":
        """Bind and listen (raises OSError on failure), then serve in a daemon thread."""
        if self._thread is not None:
            raise RuntimeError("RoomServer.start() called twice")
        if self._stop_evt.is_set():
            raise RuntimeError("RoomServer was stopped")
        lsock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):       # Windows: refuse to share the port
                lsock.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
            else:
                lsock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            lsock.bind((self.bind_ip, self.port))
            lsock.listen(16)
            lsock.setblocking(False)
            wake_r, wake_w = socket.socketpair()
        except BaseException:
            lsock.close()
            raise
        wake_r.setblocking(False)
        wake_w.setblocking(False)
        self._lsock, self._wake_r, self._wake_w = lsock, wake_r, wake_w
        self.address = (lsock.getsockname()[0], lsock.getsockname()[1])
        self._shown_ip = self.bind_ip if self.bind_ip not in ("", "0.0.0.0") else best_ipv4()
        sel = selectors.DefaultSelector()
        sel.register(lsock, selectors.EVENT_READ, "listen")
        sel.register(wake_r, selectors.EVENT_READ, "wake")
        self._sel = sel
        self._snapshot = self._build_room()
        if self._discovery:
            try:
                from .discovery import Beacon
                self._beacon = Beacon(self.bind_ip, self._beacon_info).start()
            except Exception:
                log.exception("could not start the discovery beacon")
                self._beacon = None
        self._thread = threading.Thread(target=self._run, name="goip-server", daemon=True)
        self._thread.start()
        log.info("room open on %s:%d (%s)", self.address[0], self.address[1], self.game_id)
        return self

    def stop(self) -> None:
        """Close the room (bye to everyone). Thread-safe and idempotent; joins up to 2 s."""
        with self._stop_lock:
            first = not self._stop_evt.is_set()
            self._stop_evt.set()
        thread = self._thread
        if thread is None:
            return
        if first:
            self._wake()
        if thread is not threading.current_thread():
            thread.join(2.0)

    @property
    def running(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    def room_info(self) -> Dict[str, Any]:
        """The room dict (spec section 4). Safe to call from any thread."""
        t = self._thread
        if t is None or not t.is_alive() or threading.current_thread() is t:
            return self._build_room()
        return copy.deepcopy(self._snapshot)

    # ============================================================ GameHost API
    def now(self) -> float:
        return time.monotonic()

    def push_state(self) -> None:
        logic = self.logic
        if logic is None:
            return
        cache: Dict[Optional[int], bytes] = {}
        for conn in self._ready():
            seat = conn.seat
            if seat not in cache:
                cache[seat] = P.encode({"t": P.STATE, "s": logic.view_for(seat)})
            self._send_raw(conn, cache[seat])

    def send_event(self, event: Dict[str, Any], to_seat: Optional[int] = None) -> None:
        if self.logic is None:
            return
        data = P.encode({"t": P.EVENT, "e": event})
        for conn in self._ready():
            if to_seat is None or conn.seat == to_seat:
                self._send_raw(conn, data)

    def call_later(self, delay: float, fn: Callable[[], None]) -> _Timer:
        timer = _Timer(self.now() + max(0.0, float(delay)), fn)
        heapq.heappush(self._timers, (timer.when, next(self._tseq), timer))
        return timer

    def cancel(self, handle: Any) -> None:
        if isinstance(handle, _Timer):
            handle.cancelled = True
            handle.fn = None

    def game_over(self, result: Dict[str, Any]) -> None:
        if self.state != "playing":
            return
        logic, self.logic = self.logic, None
        self.state = "lobby"
        if logic is not None and not getattr(logic, "finished", True):
            try:
                logic.stop()
            except Exception:
                log.exception("logic.stop() failed")
        try:
            data = P.encode({"t": P.OVER, "result": result})
        except (TypeError, ValueError):
            log.exception("game result is not JSON-safe")
            data = P.encode({"t": P.OVER, "result": {"winners": [], "text": "Game over"}})
        for conn in self._ready():
            self._send_raw(conn, data)
        self._relayout()
        self._broadcast_lobby()

    def notice(self, text: str) -> None:
        self._broadcast({"t": P.NOTICE, "msg": str(text)})

    # ============================================================ server loop
    def _run(self) -> None:
        try:
            while not self._stop_evt.is_set():
                assert self._sel is not None
                try:
                    events = self._sel.select(self._select_timeout())
                except (OSError, ValueError) as exc:
                    log.warning("select failed: %s", exc)
                    events = []
                    time.sleep(0.05)
                for key, mask in events:
                    if self._stop_evt.is_set():
                        break
                    data = key.data
                    if data == "listen":
                        self._accept()
                    elif data == "wake":
                        self._drain_wake()
                    else:
                        conn = data
                        if not conn.dead and mask & selectors.EVENT_READ:
                            self._on_readable(conn)
                        if not conn.dead and mask & selectors.EVENT_WRITE:
                            self._flush(conn)
                    self._reap()
                if self._stop_evt.is_set():
                    break
                self._run_timers()
                self._housekeeping()
                self._snapshot = self._build_room()
        except Exception:
            log.exception("room server crashed")
        finally:
            self._shutdown()

    def _select_timeout(self) -> float:
        timeout = self.TICK
        if self._timers:
            timeout = min(timeout, max(0.0, self._timers[0][0] - self.now()))
        return timeout

    def _wake(self) -> None:
        w = self._wake_w
        if w is not None:
            with suppress(OSError):
                w.send(b"x")

    def _drain_wake(self) -> None:
        r = self._wake_r
        with suppress(OSError):
            while r is not None and r.recv(4096):
                pass

    def _accept(self) -> None:
        assert self._lsock is not None and self._sel is not None
        for _ in range(16):
            try:
                sock, addr = self._lsock.accept()
            except (BlockingIOError, InterruptedError):
                return
            except OSError as exc:
                log.warning("accept failed: %s", exc)
                return
            sock.setblocking(False)
            if len(self._conns) >= self.MAX_CLIENTS:
                log.info("refusing %s: too many connections", addr)
                with suppress(OSError):
                    sock.send(P.encode({"t": P.REJECT, "reason": MSG_FULL}))
                self._close_sock(sock)
                continue
            with suppress(OSError):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            conn = _Conn(sock, next(self._cids), addr, self.now())
            self._conns[conn.cid] = conn
            self._sel.register(sock, selectors.EVENT_READ, conn)
            log.debug("connection %d from %s", conn.cid, addr)

    def _on_readable(self, conn: _Conn) -> None:
        try:
            data = conn.sock.recv(65536)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            self._drop(conn)
            return
        if not data:
            self._drop(conn)
            return
        conn.last_recv = self.now()
        if conn.closing:
            return
        try:
            msgs = conn.buf.feed(data)
        except P.ProtocolError as exc:
            log.info("dropping client %d: %s", conn.cid, exc)
            self._drop(conn)
            return
        for msg in msgs:
            if conn.dead or conn.closing or self._stop_evt.is_set():
                break
            try:
                self._dispatch(conn, msg)
            except Exception:
                log.exception("error handling %r from client %d", msg.get("t"), conn.cid)
                if not conn.dead:
                    self._send(conn, {"t": P.ERROR, "msg": MSG_CRASH})
            self._reap()

    def _run_timers(self) -> None:
        now = self.now()
        ran = 0
        while self._timers and self._timers[0][0] <= now and ran < 500:
            _when, _seq, timer = heapq.heappop(self._timers)
            fn = timer.fn
            if timer.cancelled or fn is None:
                continue
            timer.cancelled = True
            timer.fn = None
            ran += 1
            self._game_call(None, fn)
            self._reap()
            if self._stop_evt.is_set():
                return

    def _housekeeping(self) -> None:
        now = self.now()
        for conn in list(self._conns.values()):
            if conn.dead:
                continue
            if conn.closing:
                if now > conn.close_by:
                    self._doom(conn)
            elif not conn.ready and now - conn.opened > self.HELLO_TIMEOUT:
                log.info("client %d sent no hello in time", conn.cid)
                self._doom(conn)
            elif now - conn.last_recv > self.IDLE_TIMEOUT:
                log.info("client %d went silent", conn.cid)
                self._doom(conn)
        self._reap()

    def _shutdown(self) -> None:
        self._stop_evt.set()
        logic, self.logic = self.logic, None
        if logic is not None:
            try:
                logic.stop()
            except Exception:
                log.exception("logic.stop() failed")
        bye = P.encode({"t": P.BYE, "reason": MSG_HOST_CLOSED})
        conns = [c for c in self._conns.values() if not c.dead]
        for conn in conns:
            if conn.ready and not conn.closing:
                conn.out += bye
        deadline = time.monotonic() + 0.5
        pending = [c for c in conns if c.out]
        while pending and time.monotonic() < deadline:
            try:
                _r, writable, _x = select.select([], [c.sock for c in pending], [], 0.05)
            except (OSError, ValueError):
                break
            for conn in pending:
                if conn.sock in writable:
                    try:
                        sent = conn.sock.send(conn.out)
                        del conn.out[:sent]
                    except (BlockingIOError, InterruptedError):
                        pass
                    except OSError:
                        conn.out.clear()
            pending = [c for c in pending if c.out]
        for conn in conns:
            conn.dead = True
            self._close_sock(conn.sock)
        self._conns.clear()
        if self._sel is not None:
            with suppress(Exception):
                self._sel.close()
        for sock in (self._lsock, self._wake_r, self._wake_w):
            if sock is not None:
                with suppress(OSError):
                    sock.close()
        self._snapshot = self._build_room()
        beacon, self._beacon = self._beacon, None
        if beacon is not None:
            beacon.stop()
        log.info("room closed")

    # ======================================================== sending helpers
    def _ready(self) -> List[_Conn]:
        return [c for c in self._conns.values() if c.ready and not c.dead and not c.closing]

    def _send(self, conn: _Conn, msg: Dict[str, Any]) -> None:
        self._send_raw(conn, P.encode(msg))

    def _send_raw(self, conn: _Conn, data: bytes) -> None:
        if conn.dead:
            return
        conn.out += data
        if len(conn.out) > self.MAX_OUTBUF:
            log.info("client %d is too slow; dropping it", conn.cid)
            conn.out.clear()
            self._doom(conn)
            return
        self._flush(conn)

    def _broadcast(self, msg: Dict[str, Any], exclude: Optional[_Conn] = None) -> None:
        data = P.encode(msg)
        for conn in self._ready():
            if conn is not exclude:
                self._send_raw(conn, data)

    def _broadcast_lobby(self, exclude: Optional[_Conn] = None) -> None:
        self._broadcast({"t": P.LOBBY, "room": self._build_room()}, exclude)

    def _flush(self, conn: _Conn) -> None:
        if conn.dead:
            return
        if conn.out:
            try:
                sent = conn.sock.send(conn.out)
                del conn.out[:sent]
            except (BlockingIOError, InterruptedError):
                pass
            except OSError:
                conn.out.clear()
                self._doom(conn)
                return
        want = bool(conn.out)
        if want != conn.want_write and self._sel is not None:
            conn.want_write = want
            events = selectors.EVENT_READ | (selectors.EVENT_WRITE if want else 0)
            with suppress(KeyError, ValueError, OSError):
                self._sel.modify(conn.sock, events, conn)
        if not conn.out and conn.closing:
            self._doom(conn)

    def _close_after_flush(self, conn: _Conn) -> None:
        conn.closing = True
        conn.close_by = self.now() + self.CLOSE_GRACE
        self._flush(conn)

    def _reject(self, conn: _Conn, reason: str) -> None:
        log.info("rejecting client %d: %s", conn.cid, reason)
        self._send(conn, {"t": P.REJECT, "reason": reason})
        self._close_after_flush(conn)

    def _error(self, conn: _Conn, text: str) -> None:
        self._send(conn, {"t": P.ERROR, "msg": text})

    # ====================================================== dropping clients
    def _doom(self, conn: _Conn) -> None:
        """Drop later (never re-enter game code from inside a send)."""
        if not conn.dead and conn not in self._doomed:
            self._doomed.append(conn)

    def _reap(self) -> None:
        while self._doomed:
            self._drop(self._doomed.pop(0))

    @staticmethod
    def _close_sock(sock: socket.socket) -> None:
        with suppress(OSError):
            sock.shutdown(socket.SHUT_WR)
        with suppress(OSError):
            for _ in range(4):        # unread input would turn our FIN into a reset
                if not sock.recv(65536):
                    break
        with suppress(OSError):
            sock.close()

    def _drop(self, conn: _Conn) -> None:
        if conn.dead:
            return
        conn.dead = True
        if self._sel is not None:
            with suppress(KeyError, ValueError, OSError):
                self._sel.unregister(conn.sock)
        self._close_sock(conn.sock)
        self._conns.pop(conn.cid, None)
        if not conn.ready:
            return
        log.info("client %d (%s) left", conn.cid, conn.name)
        if conn.is_host:
            self._stop_evt.set()       # the loop ends and _shutdown says bye to everyone
            return
        self._release_seat(conn)

    def _release_seat(self, conn: _Conn) -> None:
        idx, conn.seat = conn.seat, None
        if idx is None or idx >= len(self._seats):
            return
        seat = self._seats[idx]
        if seat.client != conn.cid:
            return
        logic = self.logic
        if self.state == "playing" and logic is not None:
            seat.kind, seat.client, seat.was_name = "bot", None, seat.name
            logic.seats[idx].is_bot = True
            self._broadcast({"t": P.SEATS, "seats": self._public_seats()})
            self._broadcast({"t": P.NOTICE, "msg": "%s left — a bot took over" % seat.name})
            self._game_call(None, lambda: logic.on_seat_changed(idx))
        else:
            seat.kind, seat.name, seat.client, seat.was_name = "open", None, None, None
            self._broadcast_lobby()

    # ======================================================== message handling
    def _dispatch(self, conn: _Conn, msg: Dict[str, Any]) -> None:
        t = msg["t"]
        if not conn.ready:
            if t == P.HELLO:
                self._on_hello(conn, msg)
            else:
                log.info("client %d spoke before hello", conn.cid)
                self._drop(conn)
            return
        handler = self._HANDLERS.get(t)
        if handler is None:
            log.debug("ignoring %r from client %d", t, conn.cid)
            return
        handler(self, conn, msg)

    def _on_hello(self, conn: _Conn, msg: Dict[str, Any]) -> None:
        if msg.get("app") != APP_ID or msg.get("v") != PROTOCOL_VERSION:
            theirs = msg.get("v")
            shown = str(theirs)[:12] if isinstance(theirs, (int, str)) and not isinstance(theirs, bool) else "?"
            self._reject(conn, "Version mismatch: host has v%s, you have v%s" % (PROTOCOL_VERSION, shown))
            return
        name = P.clean_name(msg.get("name"))
        token = msg.get("token")
        is_host = (self._host_cid is None and isinstance(token, str)
                   and hmac.compare_digest(token.encode("utf-8"), self.host_token.encode("utf-8")))
        if self.state == "playing":
            idx = self._find_reclaim(name)
            if idx is None:
                self._reject(conn, MSG_RUNNING)
            else:
                self._reclaim(conn, idx)
            return
        if is_host:
            idx = 0
        else:
            idx = next((s.index for s in self._seats[1:self.humans] if s.kind == "open"), None)
        if idx is None:
            self._reject(conn, MSG_FULL)
            return
        name = self._unique_name(name)
        conn.ready, conn.name, conn.is_host, conn.seat = True, name, is_host, idx
        seat = self._seats[idx]
        seat.kind, seat.name, seat.client, seat.was_name = "human", name, conn.cid, None
        if is_host:
            self._host_cid = conn.cid
            self.host_name = name
        log.info("client %d joined as %r (seat %d%s)", conn.cid, name, idx, ", host" if is_host else "")
        self._send(conn, {"t": P.WELCOME, "you": conn.cid, "host": is_host, "room": self._build_room()})
        self._broadcast_lobby(exclude=conn)

    def _find_reclaim(self, name: str) -> Optional[int]:
        key = name.casefold()
        for seat in self._seats:
            if seat.kind == "bot" and seat.client is None and seat.was_name and seat.was_name.casefold() == key:
                return seat.index
        return None

    def _reclaim(self, conn: _Conn, idx: int) -> None:
        logic = self.logic
        seat = self._seats[idx]
        name = seat.was_name or seat.name or "Player"
        seat.kind, seat.client, seat.name, seat.was_name = "human", conn.cid, name, None
        conn.ready, conn.name, conn.seat = True, name, idx
        logic.seats[idx].is_bot = False
        logic.seats[idx].name = name
        log.info("client %d reclaimed seat %d as %r", conn.cid, idx, name)
        self._send(conn, {"t": P.WELCOME, "you": conn.cid, "host": False, "room": self._build_room()})
        self._send(conn, self._begin_msg(idx))
        self._broadcast({"t": P.SEATS, "seats": self._public_seats()}, exclude=conn)
        self._broadcast({"t": P.NOTICE, "msg": "%s is back" % name}, exclude=conn)
        self._game_call(None, lambda: logic.on_seat_changed(idx))
        self._game_call(None, self.push_state)

    def _unique_name(self, name: str) -> str:
        taken = {c.name.casefold() for c in self._conns.values() if c.ready and not c.dead}
        taken.update(s.name.casefold() for s in self._seats if s.name)
        if name.casefold() not in taken:
            return name
        for n in itertools.count(2):
            suffix = " %d" % n
            candidate = name[: P.MAX_NAME - len(suffix)].rstrip() + suffix
            if candidate.casefold() not in taken:
                return candidate
        return name  # pragma: no cover

    def _on_ping(self, conn: _Conn, msg: Dict[str, Any]) -> None:
        n = msg.get("n")
        self._send(conn, {"t": P.PONG, "n": n if isinstance(n, (int, float, str)) else None})

    def _on_act(self, conn: _Conn, msg: Dict[str, Any]) -> None:
        logic = self.logic
        if self.state != "playing" or logic is None:
            self._error(conn, "The game hasn't started yet")
            return
        seat = conn.seat
        if seat is None:
            self._error(conn, "You're not playing in this game")
            return
        action = msg.get("a")
        if not isinstance(action, dict):
            self._error(conn, "That move isn't allowed")
            return
        self._game_call(conn, lambda: logic.on_action(seat, action))

    def _on_configure(self, conn: _Conn, msg: Dict[str, Any]) -> None:
        if not conn.is_host:
            self._error(conn, "Only the host can change the game")
            return
        if self.state != "lobby":
            self._error(conn, "Wait for this game to end first")
            return
        game = msg.get("game", self.game_id)
        try:
            cls = self._resolve(game)
        except ValueError:
            self._error(conn, "Unknown game")
            return
        humans = msg.get("humans", self.humans)
        bots = msg.get("bots", self.bots)
        problem = self._check_counts(cls, humans, bots)
        if problem:
            self._error(conn, problem)
            return
        present = sum(1 for s in self._seats if s.kind == "human")
        if present > humans:
            self._error(conn, "%d people are here. Remove someone or pick more people." % present)
            return
        options = msg.get("options")
        if options is None:
            options = self.options if game == self.game_id else {}
        if not isinstance(options, dict):
            self._error(conn, "Those options aren't valid")
            return
        self.options = cls.clean_options(options)
        self.game_id, self.logic_cls, self.humans, self.bots = game, cls, humans, bots
        self._relayout()
        self._broadcast_lobby()

    def _on_start(self, conn: _Conn, msg: Dict[str, Any]) -> None:
        if not conn.is_host:
            self._error(conn, "Only the host can start the game")
            return
        if self.state != "lobby":
            self._error(conn, "A game is already running")
            return
        self._start_game(conn)

    def _on_kick(self, conn: _Conn, msg: Dict[str, Any]) -> None:
        if not conn.is_host:
            self._error(conn, "Only the host can remove players")
            return
        cid = msg.get("client")
        target = self._conns.get(cid) if type(cid) is int else None
        if target is conn:
            self._error(conn, "You can't remove yourself")
            return
        if target is None or not target.ready or target.dead or target.closing:
            self._error(conn, "That player isn't here")
            return
        log.info("host removed client %d (%s)", target.cid, target.name)
        self._send(target, {"t": P.BYE, "reason": MSG_KICKED})
        self._close_after_flush(target)
        self._release_seat(target)

    _HANDLERS: Dict[str, Callable[..., None]] = {
        P.PING: _on_ping,
        P.ACT: _on_act,
        P.CONFIGURE: _on_configure,
        P.START: _on_start,
        P.KICK: _on_kick,
        P.LEAVE: lambda self, conn, msg: self._drop(conn),     # polite disconnect
        P.HELLO: lambda self, conn, msg: None,                 # a second hello is ignored
    }

    # ============================================================== game flow
    def _start_game(self, conn: Optional[_Conn]) -> None:
        cls = self.logic_cls
        n = len(self._seats)
        colors = list(cls.seat_colors(n))
        extra = 0
        for seat in self._seats:
            if seat.kind == "open":
                seat.kind = "bot"
                seat.name = self._bot_name(self.bots + extra)
                extra += 1
            seat.was_name = None
            if seat.index < len(colors):
                seat.color = colors[seat.index]
        seats = [Seat(s.index, s.name or "Player", s.kind == "bot", s.color) for s in self._seats]
        try:
            logic = cls(self, seats, dict(self.options), random.Random(self._rng.getrandbits(64)))
        except Exception:
            log.exception("could not create %s", self.game_id)
            if conn is not None:
                self._error(conn, MSG_CRASH)
            self._relayout()
            self._broadcast_lobby()
            return
        self.logic = logic
        self.state = "playing"
        log.info("game %s started with %d seats", self.game_id, n)
        for c in self._ready():
            self._send(c, self._begin_msg(c.seat))
        ok = self._game_call(conn, logic.start)
        if not ok and self.logic is logic:
            self.game_over({"winners": [], "text": "The game couldn't start"})

    def _begin_msg(self, seat: Optional[int]) -> Dict[str, Any]:
        return {"t": P.BEGIN, "game": self.game_id, "seat": seat, "seats": self._public_seats(),
                "options": dict(self.options)}

    def _public_seats(self) -> List[Dict[str, Any]]:
        if self.logic is not None:
            return [s.public() for s in self.logic.seats]
        return [{"seat": s.index, "name": s.name, "bot": s.kind == "bot", "color": s.color} for s in self._seats]

    def _game_call(self, conn: Optional[_Conn], fn: Callable[[], None]) -> bool:
        """Run game code; ActionError -> error to the actor; crashes are contained."""
        try:
            fn()
            return True
        except ActionError as exc:
            if conn is not None and not conn.dead:
                self._error(conn, str(exc) or "That move isn't allowed")
            else:
                log.warning("ActionError outside an action: %s", exc)
            return True
        except Exception:
            log.exception("game code failed (%s)", self.game_id)
            targets = [conn] if conn is not None else self._ready()
            for c in targets:
                if not c.dead:
                    self._error(c, MSG_CRASH)
            return False

    # ============================================================ room model
    def _resolve(self, game_id: Any) -> Any:
        if not isinstance(game_id, str):
            raise ValueError("unknown game %r" % (game_id,))
        try:
            cls = self._resolver(game_id)
        except Exception:
            raise ValueError("unknown game %r" % (game_id,)) from None
        if cls is None or not hasattr(cls, "SEAT_COUNTS"):
            raise ValueError("unknown game %r" % (game_id,))
        return cls

    @staticmethod
    def _check_counts(cls: Any, humans: Any, bots: Any) -> Optional[str]:
        if type(humans) is not int or type(bots) is not int:
            return "Pick how many people and bots"
        if humans < 1:
            return "At least one person has to play"
        if bots < 0:
            return "Bots can't be negative"
        if humans + bots not in tuple(cls.SEAT_COUNTS):
            return "%s is for %s players" % (getattr(cls, "NAME", "") or "This game", _fmt_counts(cls.SEAT_COUNTS))
        return None

    def _bot_name(self, j: int) -> str:
        names = self._bot_names
        base = names[j % len(names)]
        if j >= len(names):
            base = "%s %d" % (base, j // len(names) + 1)
        return "Bot " + base

    def _relayout(self) -> None:
        """Rebuild lobby seats for the current config, keeping connected people seated."""
        n = self.humans + self.bots
        colors = list(self.logic_cls.seat_colors(n))
        seated = [c for c in self._conns.values()
                  if c.ready and not c.dead and not c.closing and c.seat is not None]
        seated.sort(key=lambda c: (not c.is_host, c.seat))
        slots: List[Optional[_Conn]] = [None] * self.humans
        pending: List[_Conn] = []
        for c in seated:
            if c.is_host:
                slots[0] = c
            elif c.seat is not None and 1 <= c.seat < self.humans and slots[c.seat] is None:
                slots[c.seat] = c
            else:
                pending.append(c)
        for c in pending:
            free = next((i for i in range(1, self.humans) if slots[i] is None), None)
            if free is None:
                c.seat = None
                continue
            slots[free] = c
        seats: List[_RoomSeat] = []
        for i in range(n):
            color = colors[i] if i < len(colors) else "red"
            if i < self.humans:
                c = slots[i]
                if c is not None:
                    c.seat = i
                    seats.append(_RoomSeat(i, "human", c.name, c.cid, color))
                else:
                    seats.append(_RoomSeat(i, "open", None, None, color))
            else:
                seats.append(_RoomSeat(i, "bot", self._bot_name(i - self.humans), None, color))
        self._seats = seats

    def _build_room(self) -> Dict[str, Any]:
        return {
            "game": self.game_id,
            "game_name": getattr(self.logic_cls, "NAME", self.game_id),
            "humans": self.humans,
            "bots": self.bots,
            "options": dict(self.options),
            "state": self.state,
            "host_name": self.host_name,
            "address": "%s:%d" % (self._shown_ip, self.address[1]),
            "seats": [s.to_dict() for s in self._seats],
        }

    def _beacon_info(self) -> Dict[str, Any]:
        room = self._snapshot
        joined = sum(1 for s in room["seats"] if s["kind"] == "human")
        return {"ip": self._shown_ip, "port": self.address[1], "host": room["host_name"],
                "game": room["game"], "game_name": room["game_name"],
                "players": "%d/%d" % (joined, room["humans"]), "state": room["state"]}
