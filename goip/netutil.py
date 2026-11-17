"""Local IPv4 addresses, IP validation and friendly network error text.

    local_ipv4s()  -> [(ip, label), ...]   best first, e.g. [("192.168.1.5", "Wi-Fi")]
    best_ipv4()    -> "192.168.1.5"        (falls back to "127.0.0.1")
    is_valid_ipv4("192.168.1.5") -> True
    friendly_error(exc, action="connect" | "host") -> short sentence for the UI
"""
from __future__ import annotations

import errno
import logging
import re
import socket
import subprocess
import sys
from typing import Dict, FrozenSet, List, Optional, Tuple

log = logging.getLogger(__name__)

__all__ = ["RejectedError", "local_ipv4s", "best_ipv4", "is_valid_ipv4", "friendly_error",
           "parse_ipconfig"]


class RejectedError(ConnectionError):
    """The host answered but refused us (room full, version mismatch, ...)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason

    def __str__(self) -> str:
        return self.reason


# ----------------------------------------------------------------- validation
_PART = re.compile(r"(?:0|[1-9][0-9]{0,2})\Z", re.ASCII)
_QUAD = re.compile(r"[0-9]{1,3}(?:\.[0-9]{1,3}){3}", re.ASCII)


def is_valid_ipv4(s: object) -> bool:
    """Dotted quad, each part 0-255, no leading zeros, no spaces or signs."""
    if not isinstance(s, str):
        return False
    parts = s.split(".")
    if len(parts) != 4:
        return False
    for part in parts:
        if not _PART.match(part) or int(part) > 255:
            return False
    return True


def _usable(ip: Optional[str]) -> bool:
    if not is_valid_ipv4(ip):
        return False
    assert ip is not None
    first = int(ip.split(".", 1)[0])
    if ip == "0.0.0.0" or first == 127 or first >= 224:
        return False
    return not ip.startswith("169.254.")


# ------------------------------------------------------------ address sources
def _route_ip() -> Optional[str]:
    """The address the OS would use for internet traffic (connect() on UDP sends nothing)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def _hostname_ips() -> List[str]:
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except (OSError, UnicodeError):
        return []
    return [info[4][0] for info in infos]


def _ipconfig_text() -> str:
    """Raw `ipconfig` output on Windows ('' elsewhere or on any failure)."""
    if sys.platform != "win32":
        return ""
    flags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    try:
        proc = subprocess.run(["ipconfig"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                              stderr=subprocess.DEVNULL, timeout=3, creationflags=flags)
    except (OSError, subprocess.SubprocessError, ValueError) as exc:
        log.debug("ipconfig failed: %s", exc)
        return ""
    data = proc.stdout or b""
    for enc in ("oem", "mbcs", "utf-8"):
        try:
            return data.decode(enc)
        except (LookupError, UnicodeDecodeError):
            continue
    return data.decode("latin-1")


_ADAPTER = re.compile(r"adapter\s+(.+)$", re.IGNORECASE)


def _adapter_label(header: str) -> str:
    m = _ADAPTER.search(header)
    label = (m.group(1) if m else header).strip()
    return label[:40] or "Network"


def parse_ipconfig(text: str) -> Dict[str, str]:
    """Map IPv4 -> adapter label from `ipconfig` output (locale tolerant, best effort).

    Adapter headers are unindented lines ending with ':'; address lines are
    indented and contain 'IPv4'. Loopback and 169.254.x addresses are skipped.
    """
    labels: Dict[str, str] = {}
    current: Optional[str] = None
    for raw in text.splitlines():
        line = raw.rstrip()
        if not line:
            continue
        if not line[0].isspace():
            current = _adapter_label(line[:-1].strip()) if line.endswith(":") else None
            continue
        if current is None or "IPv4" not in line:
            continue
        for ip in _QUAD.findall(line):
            if _usable(ip):
                labels.setdefault(ip, current)
                break
    return labels


_VIRTUAL = ("vethernet", "virtualbox", "vmware", "hyper-v", "wsl", "bluetooth", "loopback")


def _is_virtual(label: str) -> bool:
    low = label.lower()
    return any(word in low for word in _VIRTUAL)


def local_ipv4s() -> List[Tuple[str, str]]:
    """This PC's LAN IPv4 addresses as (ip, label), best first.

    The route-preferred address comes first, then the others (virtual adapters
    such as WSL / Hyper-V / VirtualBox last). Loopback and 169.254.x are left out.
    """
    labels = parse_ipconfig(_ipconfig_text())
    route = _route_ip()
    candidates: List[str] = []
    if route:
        candidates.append(route)
    candidates.extend(_hostname_ips())
    candidates.extend(labels)
    seen = set()
    result: List[Tuple[str, str]] = []
    for ip in candidates:
        if ip in seen or not _usable(ip):
            continue
        seen.add(ip)
        label = labels.get(ip) or ("Main network" if ip == route else "Network")
        result.append((ip, label))
    head = result[:1] if (result and result[0][0] == route) else []
    rest = result[len(head):]
    rest.sort(key=lambda pair: _is_virtual(pair[1]))   # stable: keeps the original order otherwise
    return head + rest


def best_ipv4() -> str:
    """The most likely LAN address of this PC, or '127.0.0.1' when offline."""
    route = _route_ip()
    if _usable(route):
        assert route is not None
        return route
    for ip in _hostname_ips():
        if _usable(ip):
            return ip
    found = local_ipv4s()
    return found[0][0] if found else "127.0.0.1"


# -------------------------------------------------------------- error wording
def _codes(*names: str, win: Tuple[int, ...] = ()) -> FrozenSet[int]:
    values = set(win)
    for name in names:
        value = getattr(errno, name, None)
        if isinstance(value, int):
            values.add(value)
    return frozenset(values)


_REFUSED = _codes("ECONNREFUSED", win=(10061,))
_TIMEOUT = _codes("ETIMEDOUT", win=(10060,))
_UNREACH = _codes("ENETUNREACH", "EHOSTUNREACH", "ENETDOWN", "EHOSTDOWN", win=(10050, 10051, 10064, 10065))
_NOT_AVAIL = _codes("EADDRNOTAVAIL", win=(10049,))
_IN_USE = _codes("EADDRINUSE", win=(10048,))
_DENIED = _codes("EACCES", "EPERM", win=(10013,))
_RESET = _codes("ECONNRESET", "ECONNABORTED", "EPIPE", "ESHUTDOWN", win=(10053, 10054, 10058))

MSG_REFUSED = "Nobody is hosting at that address. Check the IP and that the host created a room."
MSG_TIMEOUT = ("No answer from that IP. Check you're on the same Wi-Fi, and that the host "
               "allowed Python in the Windows Firewall prompt.")
MSG_UNREACH = "Can't reach that IP from here. Make sure you're on the same Wi-Fi as the host."
MSG_NOT_HERE = "That IP isn't on this PC. Pick one from the list - it's the IPv4 Address that ipconfig shows."
MSG_BAD_TARGET = "Can't connect to that address. Check the IP."
MSG_IN_USE = "That port is already in use - is another room open? Try another port."
MSG_DENIED_HOST = "This PC blocked that port. Try another port, like 50600."
MSG_DENIED_CONNECT = "This PC blocked the connection. Check your firewall or antivirus."
MSG_RESET = "The host closed the connection."
MSG_BAD_IP = "That doesn't look like a valid IP address."
MSG_BAD_PORT = "That port number isn't valid. Use one from 1024 to 65535."
MSG_GENERIC_HOST = "Couldn't create the room. Check the IP and port."
MSG_GENERIC_CONNECT = "Couldn't connect. Check the IP and port, and that you're on the same Wi-Fi."


def _exc_codes(exc: BaseException) -> set:
    found = set()
    for attr in ("winerror", "errno"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            found.add(value)
    return found


def friendly_error(exc: BaseException, action: str = "connect") -> str:
    """Short, friendly sentence for a network failure. action: 'connect' or 'host'."""
    hosting = action == "host"
    if isinstance(exc, RejectedError):
        return exc.reason or "The host didn't let you in."
    if isinstance(exc, socket.gaierror):
        return MSG_BAD_IP
    if isinstance(exc, OverflowError):
        return MSG_BAD_PORT
    codes = _exc_codes(exc)
    if isinstance(exc, (socket.timeout, TimeoutError)) or codes & _TIMEOUT:
        return MSG_TIMEOUT
    if isinstance(exc, ConnectionRefusedError) or codes & _REFUSED:
        return MSG_REFUSED
    if codes & _IN_USE:
        return MSG_IN_USE
    if codes & _NOT_AVAIL:
        return MSG_NOT_HERE if hosting else MSG_BAD_TARGET
    if codes & _UNREACH:
        return MSG_UNREACH
    if isinstance(exc, PermissionError) or codes & _DENIED:
        return MSG_DENIED_HOST if hosting else MSG_DENIED_CONNECT
    if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)) or codes & _RESET:
        return MSG_RESET
    log.debug("unmapped network error: %r", exc)
    return MSG_GENERIC_HOST if hosting else MSG_GENERIC_CONNECT
