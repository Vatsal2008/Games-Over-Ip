"""Wire format shared by server and client.

One JSON object per line (UTF-8, ``\\n`` terminated). Every message has a string
field ``t``. See docs/ARCHITECTURE.md section 4 for the full message table.
"""
from __future__ import annotations

import json
import unicodedata
from typing import Any, Dict, List

MAX_LINE = 64 * 1024  # bytes; a peer sending a longer line is disconnected

# --- client -> server ---------------------------------------------------------
HELLO = "hello"          # {v, app, name, token?}
PING = "ping"            # {n}
ACT = "act"              # {a: {...game action...}}
CONFIGURE = "configure"  # host only: {game, humans, bots, options}
START = "start"          # host only: start / rematch
KICK = "kick"            # host only: {client}
LEAVE = "leave"          # {}

# --- server -> client ---------------------------------------------------------
WELCOME = "welcome"      # {you, host, room}
REJECT = "reject"        # {reason}
LOBBY = "lobby"          # {room}
BEGIN = "begin"          # {game, seat, seats, options}
STATE = "state"          # {s}
EVENT = "event"          # {e: {kind, ...}}
SEATS = "seats"          # {seats}
OVER = "over"            # {result: {winners, text, ...}}
ERROR = "error"          # {msg}
NOTICE = "notice"        # {msg}
PONG = "pong"            # {n}
BYE = "bye"              # {reason}

# --- client-local pseudo messages (queued by Client, never on the wire) -------
L_CONNECTED = "_connected"            # {welcome}
L_CONNECT_FAILED = "_connect_failed"  # {error}
L_DISCONNECTED = "_disconnected"      # {reason}


class ProtocolError(ValueError):
    """Malformed or oversized message."""


def encode(msg: Dict[str, Any]) -> bytes:
    """Serialise one message to a newline-terminated UTF-8 JSON line."""
    data = json.dumps(msg, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    if len(data) >= MAX_LINE:
        raise ProtocolError("message too large (%d bytes)" % len(data))
    return data + b"\n"


def decode(line: bytes) -> Dict[str, Any]:
    """Parse one line (without the trailing newline) into a message dict."""
    if len(line) > MAX_LINE:
        raise ProtocolError("message too large")
    try:
        obj = json.loads(line.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise ProtocolError("bad message: %s" % exc) from None
    if not isinstance(obj, dict) or not isinstance(obj.get("t"), str):
        raise ProtocolError("message must be a JSON object with a string 't' field")
    return obj


class LineBuffer:
    """Collects raw socket bytes and returns complete decoded messages."""

    def __init__(self, max_line: int = MAX_LINE) -> None:
        self.max_line = max_line
        self._buf = bytearray()

    def feed(self, data: bytes) -> List[Dict[str, Any]]:
        self._buf.extend(data)
        out: List[Dict[str, Any]] = []
        while True:
            nl = self._buf.find(b"\n")
            if nl < 0:
                break
            if nl > self.max_line:
                raise ProtocolError("message too large")
            line = bytes(self._buf[:nl]).strip()
            del self._buf[: nl + 1]
            if line:
                out.append(decode(line))
        if len(self._buf) > self.max_line:
            raise ProtocolError("message too large")
        return out


MAX_NAME = 16


def clean_name(name: Any, fallback: str = "Player") -> str:
    """Printable, single-spaced, at most MAX_NAME characters."""
    if not isinstance(name, str):
        return fallback
    kept = []
    for ch in unicodedata.normalize("NFC", name):
        cat = unicodedata.category(ch)
        if ch == " " or cat[0] not in ("C", "Z"):
            kept.append(ch)
    text = " ".join("".join(kept).split())[:MAX_NAME].strip()
    return text or fallback
