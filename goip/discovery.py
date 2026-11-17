"""LAN discovery: the host's UDP Beacon and the join screen's Scanner.

The Beacon broadcasts one small JSON datagram about once per second:
    {"app": "goip", "v": 1, "ip": "192.168.1.5", "port": 50505, "host": "Asha",
     "game": "ludo", "game_name": "Ludo", "players": "2/4", "state": "lobby"}
The Scanner listens on DISCOVERY_PORT and remembers rooms heard recently.
Neither ever raises out of its thread; when the OS blocks UDP they just do nothing.
"""
from __future__ import annotations

import json
import logging
import socket
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from . import APP_ID, DISCOVERY_PORT, PROTOCOL_VERSION
from .netutil import is_valid_ipv4

log = logging.getLogger(__name__)

MAX_DATAGRAM = 2048     # bytes; bigger datagrams are ignored
SEEN_WINDOW = 4.0       # seconds a room stays listed after its last beacon


def _close_quietly(sock: Optional[socket.socket]) -> None:
    if sock is not None:
        try:
            sock.close()
        except OSError:
            pass


class Beacon:
    """Advertises a room: sends info_fn() as JSON every `interval` seconds."""

    def __init__(self, bind_ip: str, info_fn: Callable[[], Dict[str, Any]], interval: float = 1.0,
                 port: int = DISCOVERY_PORT) -> None:
        self.bind_ip = bind_ip if bind_ip else "0.0.0.0"
        self.info_fn = info_fn
        self.interval = max(0.05, float(interval))
        self.port = int(port)
        self.sent = 0                      # datagrams sent successfully (for tests / debugging)
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._thread: Optional[threading.Thread] = None

    @property
    def running(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    def start(self) -> "Beacon":
        with self._lock:
            if self._thread is not None:
                return self
            self._thread = threading.Thread(target=self._run, name="goip-beacon", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(1.5)

    # ------------------------------------------------------------------ parts
    def targets(self) -> List[Tuple[str, int]]:
        """Limited broadcast, the /24 directed broadcast of bind_ip, and loopback for 127.x."""
        out = [("255.255.255.255", self.port)]
        ip = self.bind_ip
        if is_valid_ipv4(ip) and ip != "0.0.0.0":
            if ip.startswith("127."):
                out.append(("127.0.0.1", self.port))
            else:
                directed = ".".join(ip.split(".")[:3] + ["255"])
                out.append((directed, self.port))
        return out

    def payload(self) -> Optional[bytes]:
        info = self.info_fn() or {}
        msg: Dict[str, Any] = dict(info)
        msg["app"] = APP_ID
        msg["v"] = PROTOCOL_VERSION
        data = json.dumps(msg, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        if len(data) > MAX_DATAGRAM:
            log.warning("beacon payload too large (%d bytes); not sent", len(data))
            return None
        return data

    def _open(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            sock.bind((self.bind_ip, 0))
        except OSError:
            sock.close()
            raise
        return sock

    def _run(self) -> None:
        sock: Optional[socket.socket] = None
        failures = 0
        while not self._stop.is_set():
            try:
                if sock is None:
                    sock = self._open()
                data = self.payload()
                if data:
                    for target in self.targets():
                        try:
                            sock.sendto(data, target)
                            self.sent += 1
                        except OSError as exc:
                            log.debug("beacon to %s failed: %s", target, exc)
                failures = 0
            except Exception as exc:            # never let the thread die noisily
                failures += 1
                if failures == 1:
                    log.info("beacon problem: %s", exc)
                _close_quietly(sock)
                sock = None
            self._stop.wait(self.interval)
        _close_quietly(sock)


class Scanner:
    """Listens for beacons; hosts() lists rooms heard in the last few seconds.

    If the port can't be bound (blocked, or not supported) the scanner is a
    harmless no-op: `available` is False and hosts() stays empty.
    """

    def __init__(self, port: int = DISCOVERY_PORT, window: float = SEEN_WINDOW) -> None:
        self.port = int(port)
        self.window = float(window)
        self.available = False
        self._lock = threading.Lock()
        self._seen: Dict[Tuple[str, int], Dict[str, Any]] = {}
        self._sock: Optional[socket.socket] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    @property
    def running(self) -> bool:
        t = self._thread
        return t is not None and t.is_alive()

    def start(self) -> "Scanner":
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return self
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
                sock.bind(("0.0.0.0", self.port))
                sock.settimeout(0.5)
                self.port = sock.getsockname()[1]
            except OSError as exc:
                log.info("discovery scanner disabled: %s", exc)
                sock.close()
                self.available = False
                return self
            self.available = True
            self._sock = sock
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._run, args=(sock, self._stop),
                                            name="goip-scanner", daemon=True)
            self._thread.start()
        return self

    def stop(self) -> None:
        with self._lock:
            self._stop.set()
            sock, self._sock = self._sock, None
            t = self._thread
        _close_quietly(sock)
        if t is not None and t is not threading.current_thread():
            t.join(1.5)

    def hosts(self) -> List[Dict[str, Any]]:
        """Rooms heard in the last `window` seconds, newest room first, one per (ip, port)."""
        now = time.monotonic()
        with self._lock:
            for key in [k for k, e in self._seen.items() if now - e["seen"] > self.window]:
                del self._seen[key]
            items = sorted(self._seen.values(), key=lambda e: e["first_seen"], reverse=True)
            return [dict(e) for e in items]

    def clear(self) -> None:
        with self._lock:
            self._seen.clear()

    # -------------------------------------------------------------- internals
    def feed(self, data: bytes, addr: Tuple[str, int]) -> bool:
        """Validate one datagram and remember the room. Returns True if accepted."""
        if not isinstance(data, (bytes, bytearray)) or len(data) > MAX_DATAGRAM:
            return False
        try:
            obj = json.loads(bytes(data).decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return False
        if not isinstance(obj, dict) or obj.get("app") != APP_ID:
            return False
        port = obj.get("port")
        if type(port) is not int or not 1 <= port <= 65535:
            return False
        source = addr[0] if addr else ""
        ip = obj.get("ip")
        if not is_valid_ipv4(ip) or ip == "0.0.0.0":
            ip = source
        if not is_valid_ipv4(ip):
            return False
        version = obj.get("v")
        now = time.monotonic()
        entry = {
            "ip": ip,
            "port": port,
            "host": _text(obj.get("host"), 16) or "Host",
            "game": _text(obj.get("game"), 24),
            "game_name": _text(obj.get("game_name"), 32),
            "players": _text(obj.get("players"), 8),
            "state": _text(obj.get("state"), 16),
            "v": version if type(version) is int else None,
            "compatible": version == PROTOCOL_VERSION and type(version) is int,
            "from": source,
            "seen": now,
            "first_seen": now,
        }
        key = (ip, port)
        with self._lock:
            old = self._seen.get(key)
            if old is not None and now - old["seen"] <= self.window:
                entry["first_seen"] = old["first_seen"]
            self._seen[key] = entry
        return True

    def _run(self, sock: socket.socket, stop: threading.Event) -> None:
        errors = 0
        while not stop.is_set():
            try:
                data, addr = sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError as exc:
                if stop.is_set():
                    break
                errors += 1          # e.g. WSAEMSGSIZE for an oversized datagram
                if errors > 100:
                    log.info("discovery scanner stopped: %s", exc)
                    break
                time.sleep(0.05)
                continue
            try:
                self.feed(data, addr)
            except Exception:       # defensive: bad data must never kill the thread
                log.debug("bad beacon ignored", exc_info=True)
        _close_quietly(sock)


def _text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    kept = "".join(ch for ch in value if ch.isprintable())
    return " ".join(kept.split())[:limit]
