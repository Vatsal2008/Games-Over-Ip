"""Client: one TCP connection to a RoomServer.

    client = Client("192.168.1.5", 50505, "Ravi")
    client.connect_async()          # or: welcome = client.connect(timeout=5)
    ...every ~15 ms on the Tk thread:
    for msg in client.poll():       # decoded server messages + local "_..." pseudo messages
        ...
    client.act({"a": "roll"})
    client.close()

A reader thread decodes server messages onto a queue; poll() drains it without
blocking. Pseudo messages (never on the wire):
    {"t": "_connected", "welcome": {...}}       connect_async() succeeded
    {"t": "_connect_failed", "error": "..."}    connect_async() failed (friendly text)
    {"t": "_disconnected", "reason": "..."}     exactly once per connection, never after close()
A server "bye" is not queued as itself: it becomes the single "_disconnected"
(with "bye": True and the host's reason).
"""
from __future__ import annotations

import errno
import logging
import queue
import socket
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from . import APP_ID, PROTOCOL_VERSION
from . import protocol as P
from .netutil import RejectedError, friendly_error

log = logging.getLogger(__name__)

__all__ = ["Client", "RejectedError", "probe"]

NOT_GOIP = "That isn't a Games over IP room, or it runs a different version."
LOST = "Lost connection to the host"
HOST_CLOSED_CONN = "The host closed the connection"


def _quiet_close(sock: Optional[socket.socket]) -> None:
    if sock is None:
        return
    try:
        sock.shutdown(socket.SHUT_RDWR)
    except OSError:
        pass
    try:
        sock.close()
    except OSError:
        pass


def _reason(value: Any, fallback: str) -> str:
    return value if isinstance(value, str) and value.strip() else fallback


def _remaining(deadline: float) -> float:
    left = deadline - time.monotonic()
    if left <= 0:
        raise socket.timeout("timed out")
    return left


class Client:
    """Connection to a room. Safe to use from the Tk thread; see module docstring."""

    PING_INTERVAL = 3.0        # seconds between pings
    SILENCE_TIMEOUT = 12.0     # nothing received for this long -> disconnected
    RECV_TIMEOUT = 0.5         # reader wake-up period

    def __init__(self, ip: str, port: int, name: str, token: Optional[str] = None) -> None:
        self.ip = ip
        self.port = int(port)
        self.name = name
        self.token = token
        self.rtt: Optional[float] = None          # last ping round trip (seconds)
        self._q: "queue.Queue[Dict[str, Any]]" = queue.Queue()
        self._lock = threading.RLock()
        self._sock: Optional[socket.socket] = None
        self._welcome: Optional[Dict[str, Any]] = None
        self._buf = P.LineBuffer()
        self._started = False      # connect() was called
        self._closed = False       # the user called close()
        self._down = False         # connection is over (reported or closed)
        self._reader: Optional[threading.Thread] = None
        self._last_recv = 0.0
        self._last_ping = 0.0
        self._ping_n = 0
        self._pings: Dict[int, float] = {}

    # ------------------------------------------------------------ properties
    @property
    def connected(self) -> bool:
        return self._welcome is not None and not self._down

    @property
    def welcome(self) -> Optional[Dict[str, Any]]:
        return self._welcome

    @property
    def my_id(self) -> Optional[int]:
        w = self._welcome
        return w.get("you") if w else None

    @property
    def is_host(self) -> bool:
        w = self._welcome
        return bool(w.get("host")) if w else False

    @property
    def closed(self) -> bool:
        return self._closed

    # ------------------------------------------------------------ connecting
    def connect(self, timeout: float = 5.0) -> Dict[str, Any]:
        """Blocking connect + hello. Returns the welcome message.

        Raises RejectedError(reason) if the host says no, OSError on network errors.
        Does not queue "_connected" (you already have the welcome).
        """
        welcome, rest = self._handshake(timeout)
        self._go_live(rest)
        return welcome

    def connect_async(self, timeout: float = 5.0) -> threading.Thread:
        """connect() in a thread; queues "_connected" or "_connect_failed"."""

        def run() -> None:
            try:
                welcome, rest = self._handshake(timeout)
            except Exception as exc:
                with self._lock:
                    if not self._closed:
                        self._q.put({"t": P.L_CONNECT_FAILED, "error": friendly_error(exc, "connect")})
                log.info("connect to %s:%s failed: %r", self.ip, self.port, exc)
                return
            with self._lock:
                if self._closed:
                    return
                self._q.put({"t": P.L_CONNECTED, "welcome": welcome})
            self._go_live(rest)

        thread = threading.Thread(target=run, name="goip-connect", daemon=True)
        thread.start()
        return thread

    def _handshake(self, timeout: float) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
        with self._lock:
            if self._closed:
                raise ConnectionAbortedError(errno.ECONNABORTED, "Connection cancelled")
            if self._started:
                raise RuntimeError("Client.connect() can only be called once")
            self._started = True
        timeout = max(0.1, float(timeout))
        deadline = time.monotonic() + timeout
        sock = socket.create_connection((self.ip, self.port), timeout=timeout)
        with self._lock:
            if self._closed:
                _quiet_close(sock)
                raise ConnectionAbortedError(errno.ECONNABORTED, "Connection cancelled")
            self._sock = sock
        try:
            try:
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            except OSError:
                pass
            hello: Dict[str, Any] = {"t": P.HELLO, "v": PROTOCOL_VERSION, "app": APP_ID, "name": self.name}
            if self.token:
                hello["token"] = self.token
            sock.settimeout(_remaining(deadline))
            sock.sendall(P.encode(hello))
            buf = P.LineBuffer()
            while True:
                sock.settimeout(_remaining(deadline))
                data = sock.recv(65536)
                if not data:
                    raise ConnectionResetError(errno.ECONNRESET, HOST_CLOSED_CONN)
                try:
                    msgs = buf.feed(data)
                except P.ProtocolError:
                    raise RejectedError(NOT_GOIP) from None
                for i, msg in enumerate(msgs):
                    t = msg["t"]
                    if t == P.WELCOME:
                        if not isinstance(msg.get("room"), dict) or type(msg.get("you")) is not int:
                            raise RejectedError(NOT_GOIP)
                        with self._lock:
                            if self._closed:
                                raise ConnectionAbortedError(errno.ECONNABORTED, "Connection cancelled")
                            sock.settimeout(self.RECV_TIMEOUT)
                            self._buf = buf
                            self._last_recv = self._last_ping = time.monotonic()
                            self._welcome = msg
                        log.info("connected to %s:%s as client %s", self.ip, self.port, msg.get("you"))
                        return msg, msgs[i + 1:]
                    if t == P.REJECT:
                        raise RejectedError(_reason(msg.get("reason"), "The host didn't let you in."))
                    if t == P.BYE:
                        raise RejectedError(_reason(msg.get("reason"), "The host closed the room."))
        except BaseException:
            with self._lock:
                if self._sock is sock:
                    self._sock = None
                self._down = True
            _quiet_close(sock)
            raise

    def _go_live(self, rest: List[Dict[str, Any]]) -> None:
        for msg in rest:
            if not self._handle(msg):
                return
        with self._lock:
            if self._down:
                return
            self._reader = threading.Thread(target=self._read_loop, name="goip-client", daemon=True)
            self._reader.start()

    # --------------------------------------------------------------- reading
    def _read_loop(self) -> None:
        sock = self._sock
        if sock is None:
            return
        while not self._down:
            try:
                data: Optional[bytes] = sock.recv(65536)
            except socket.timeout:
                data = None
            except OSError:
                self._lost(LOST)
                return
            now = time.monotonic()
            if data is not None:
                if not data:
                    self._lost(HOST_CLOSED_CONN)
                    return
                self._last_recv = now
                try:
                    msgs = self._buf.feed(data)
                except P.ProtocolError:
                    log.warning("bad data from the host")
                    self._lost(LOST)
                    return
                for msg in msgs:
                    if not self._handle(msg):
                        return
            if now - self._last_recv > self.SILENCE_TIMEOUT:
                self._lost(LOST)
                return
            if now - self._last_ping >= self.PING_INTERVAL:
                self._ping(now)

    def _handle(self, msg: Dict[str, Any]) -> bool:
        """Queue one server message. Returns False once the connection is over."""
        t = msg.get("t")
        if t == P.PONG:
            sent = self._pings.pop(msg.get("n"), None) if isinstance(msg.get("n"), int) else None
            if sent is not None:
                self.rtt = time.monotonic() - sent
            return True
        if t == P.BYE:
            self._lost(_reason(msg.get("reason"), HOST_CLOSED_CONN), bye=True)
            return False
        if self._down:
            return False
        self._q.put(msg)
        return True

    def _ping(self, now: float) -> None:
        self._last_ping = now
        self._ping_n += 1
        self._pings[self._ping_n] = now
        if len(self._pings) > 8:
            for n in sorted(self._pings)[:-8]:
                del self._pings[n]
        self.send({"t": P.PING, "n": self._ping_n})

    def _lost(self, reason: str, bye: bool = False) -> None:
        with self._lock:
            if self._down:
                return
            self._down = True
            sock = self._sock
            if not self._closed:
                msg: Dict[str, Any] = {"t": P.L_DISCONNECTED, "reason": reason}
                if bye:
                    msg["bye"] = True
                self._q.put(msg)
        log.info("disconnected: %s", reason)
        _quiet_close(sock)

    # --------------------------------------------------------------- public
    def poll(self) -> List[Dict[str, Any]]:
        """All queued messages, oldest first (never blocks)."""
        out: List[Dict[str, Any]] = []
        while True:
            try:
                msg = self._q.get_nowait()
            except queue.Empty:
                break
            if self._closed and msg.get("t") in (P.L_DISCONNECTED, P.L_CONNECT_FAILED):
                continue
            out.append(msg)
        return out

    def send(self, msg: Dict[str, Any]) -> bool:
        """Send one message (thread-safe). Returns False if not connected; errors disconnect."""
        try:
            data = P.encode(msg)
        except (P.ProtocolError, TypeError, ValueError):
            log.exception("can't send %r", msg.get("t") if isinstance(msg, dict) else msg)
            return False
        with self._lock:
            sock = self._sock
            if self._down or sock is None or self._welcome is None:
                return False
            try:
                sock.sendall(data)
                return True
            except OSError as exc:
                log.info("send failed: %s", exc)
                self._lost(LOST)
                return False

    def act(self, action: Dict[str, Any]) -> bool:
        """Send a game action, e.g. act({"a": "roll"})."""
        return self.send({"t": P.ACT, "a": action})

    def close(self) -> None:
        """Leave (best effort) and close. Idempotent; no "_disconnected" follows."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            sock = self._sock
            live = self._welcome is not None and not self._down
            self._down = True
        if sock is not None:
            if live:
                try:
                    sock.settimeout(0.5)
                    sock.sendall(P.encode({"t": P.LEAVE}))
                except OSError:
                    pass
            _quiet_close(sock)
        reader = self._reader
        if reader is not None and reader is not threading.current_thread() and reader.is_alive():
            reader.join(1.0)

    def __enter__(self) -> "Client":
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()


def probe(ip: str, port: int, timeout: float = 2.0) -> Tuple[bool, str]:
    """Quick check: can we open a TCP connection to ip:port? -> (ok, message)."""
    try:
        sock = socket.create_connection((ip, int(port)), timeout=timeout)
    except (OSError, ValueError, OverflowError) as exc:
        if isinstance(exc, ValueError) and not isinstance(exc, OSError):
            return False, "That port number isn't valid."
        return False, friendly_error(exc, "connect")
    _quiet_close(sock)
    return True, "Found a host at %s:%s" % (ip, port)
