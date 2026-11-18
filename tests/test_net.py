"""Networking tests: netutil, RoomServer, Client and discovery.

Real sockets on 127.0.0.1 with port 0 and discovery=False, plus a tiny fake game
(a turn-based counter) injected through RoomServer(resolver=...).
"""
from __future__ import annotations

import errno
import json
import socket
import sys
import threading
import time
import unittest
from typing import Any, Callable, Dict, List, Optional
from unittest import mock

from goip import APP_ID, PROTOCOL_VERSION
from goip import netutil
from goip import protocol as P
from goip.client import Client, RejectedError, probe
from goip.discovery import Beacon, Scanner
from goip.games.base import ActionError, GameLogic, Option
from goip.server import RoomServer

LOCAL = "127.0.0.1"
WAIT = 4.0


# ============================================================== fake games
class CounterLogic(GameLogic):
    """Players take turns adding 1 or 2; whoever reaches the target wins."""

    ID = "counter"
    NAME = "Counter"
    TAGLINE = "Count to the target"
    SEAT_COUNTS = (2, 3, 4)
    OPTIONS = (
        Option("target", "Target", ((3, "3"), (6, "6"), (40, "40")), 6),
        Option("turn_timer", "Idle", ((0, "Off"), (1, "1s")), 0),
    )
    BOT_DELAY = 0.02

    def start(self) -> None:
        self.total = 0
        self.turn = 0
        self.emit("started", n=len(self.seats))
        self.push()
        self.schedule_actor(self.turn)

    def view_for(self, seat: Optional[int]) -> Dict[str, Any]:
        return {"total": self.total, "target": self.options["target"],
                "turn": None if self.finished else self.turn, "timer": self.time_left(),
                "phase": "play", "finished": self.finished, "me": seat,
                "secret": None if seat is None else "secret-%d" % seat}

    def on_action(self, seat: int, action: Dict[str, Any]) -> None:
        if action.get("a") == "boom":
            raise RuntimeError("kaboom")
        if seat != self.turn:
            raise ActionError("Not your turn")
        n = action.get("n")
        if action.get("a") != "add" or type(n) is not int or n not in (1, 2):
            raise ActionError("Add 1 or 2")
        self.clear_actor()
        self._add(seat, n)

    def bot_act(self, seat: int) -> None:
        self._add(seat, 1)

    def _add(self, seat: int, n: int) -> None:
        self.total += n
        self.emit("added", seat=seat, n=n, total=self.total)
        if self.total >= self.options["target"]:
            self.finish([seat], "%s wins" % self.name(seat), total=self.total)
            return
        self.turn = (self.turn + 1) % len(self.seats)
        self.push()
        self.schedule_actor(self.turn)


class DuoLogic(CounterLogic):
    ID = "duo"
    NAME = "Duo"
    SEAT_COUNTS = (2,)


class BrokenStartLogic(CounterLogic):
    ID = "broken"
    NAME = "Broken"

    def start(self) -> None:
        raise RuntimeError("cannot start")


GAMES = {"counter": CounterLogic, "duo": DuoLogic, "broken": BrokenStartLogic}


def resolver(game_id: str) -> Any:
    return GAMES[game_id]


# ================================================================ helpers
class Inbox:
    """Collects a Client's messages; wait() picks the first matching one."""

    def __init__(self, client: Client) -> None:
        self.client = client
        self.pending: List[Dict[str, Any]] = []
        self.log: List[Dict[str, Any]] = []

    def pump(self) -> None:
        msgs = self.client.poll()
        self.pending.extend(msgs)
        self.log.extend(msgs)

    def wait(self, t: str, pred: Optional[Callable[[Dict[str, Any]], bool]] = None,
             timeout: float = WAIT) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout
        while True:
            self.pump()
            for i, msg in enumerate(self.pending):
                if msg.get("t") == t and (pred is None or pred(msg)):
                    del self.pending[i]
                    return msg
            if time.monotonic() > deadline:
                raise AssertionError("no %r within %.1fs; pending=%r" % (t, timeout, [m.get("t") for m in self.pending]))
            time.sleep(0.005)

    def settle(self, seconds: float = 0.25) -> None:
        time.sleep(seconds)
        self.pump()

    def count(self, t: str) -> int:
        return sum(1 for m in self.log if m.get("t") == t)

    def index(self, msg: Dict[str, Any]) -> int:
        return next(i for i, m in enumerate(self.log) if m is msg)


class RawPeer:
    """A hand-driven TCP peer for protocol abuse tests."""

    def __init__(self, port: int, hello: bool = True, name: str = "Raw") -> None:
        self.sock = socket.create_connection((LOCAL, port), timeout=WAIT)
        self.buf = P.LineBuffer()
        self.msgs: List[Dict[str, Any]] = []
        if hello:
            self.send_msg({"t": "hello", "v": PROTOCOL_VERSION, "app": APP_ID, "name": name})

    def send_msg(self, msg: Dict[str, Any]) -> None:
        self.sock.sendall(P.encode(msg))

    def send_raw(self, data: bytes) -> None:
        try:
            self.sock.sendall(data)
        except OSError:
            pass

    def recv_msg(self, timeout: float = WAIT) -> Optional[Dict[str, Any]]:
        deadline = time.monotonic() + timeout
        while not self.msgs:
            self.sock.settimeout(max(0.01, deadline - time.monotonic()))
            try:
                data = self.sock.recv(65536)
            except socket.timeout:
                raise AssertionError("raw peer got nothing in %.1fs" % timeout) from None
            except OSError:
                return None
            if not data:
                return None
            self.msgs.extend(self.buf.feed(data))
        return self.msgs.pop(0)

    def wait_closed(self, timeout: float = WAIT) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            left = deadline - time.monotonic()
            if left <= 0:
                return False
            self.sock.settimeout(left)
            try:
                data = self.sock.recv(65536)
            except socket.timeout:
                return False
            except OSError:
                return True
            if not data:
                return True
            try:
                self.msgs.extend(self.buf.feed(data))
            except P.ProtocolError:
                pass

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:
            pass


def wait_until(pred: Callable[[], bool], timeout: float = WAIT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.01)
    return pred()


class NetCase(unittest.TestCase):
    def setUp(self) -> None:
        self._servers: List[RoomServer] = []
        self._clients: List[Client] = []
        self._peers: List[RawPeer] = []

    def tearDown(self) -> None:
        for c in self._clients:
            c.close()
        for p in self._peers:
            p.close()
        for srv in self._servers:
            srv.stop()
            self.assertFalse(srv.running, "server thread still running")

    # ------------------------------------------------------------ builders
    def server(self, humans: int = 2, bots: int = 0, game: str = "counter",
               options: Optional[Dict[str, Any]] = None, start: bool = True, **attrs: Any) -> RoomServer:
        srv = RoomServer(LOCAL, 0, "Asha", game, humans, bots, options, discovery=False,
                         resolver=resolver, seed=7)
        for key, value in attrs.items():
            setattr(srv, key, value)
        self._servers.append(srv)
        if start:
            srv.start()
        return srv

    def client(self, srv: RoomServer, name: str, host: bool = False, connect: bool = True) -> Any:
        c = Client(LOCAL, srv.address[1], name, token=srv.host_token if host else None)
        self._clients.append(c)
        if connect:
            c.connect(timeout=WAIT)
        return c, Inbox(c)

    def peer(self, srv: RoomServer, **kw: Any) -> RawPeer:
        p = RawPeer(srv.address[1], **kw)
        self._peers.append(p)
        return p

    def room_with_two(self, **kw: Any) -> Any:
        srv = self.server(**kw)
        host, hin = self.client(srv, "Asha", host=True)
        guest, gin = self.client(srv, "Ravi")
        hin.wait("lobby", lambda m: m["room"]["seats"][1]["kind"] == "human")
        return srv, host, hin, guest, gin

    def start_game(self, host: Client, *inboxes: Inbox) -> List[Dict[str, Any]]:
        host.send({"t": "start"})
        begins = [inbox.wait("begin") for inbox in inboxes]
        for inbox in inboxes:
            inbox.wait("state")
        return begins

    def play_until_over(self, client: Client, inbox: Inbox, seat: int, timeout: float = 8.0) -> Dict[str, Any]:
        deadline = time.monotonic() + timeout
        acted_at = None
        states = [m for m in inbox.log if m["t"] == "state"]
        if states and states[-1]["s"]["turn"] == seat and not states[-1]["s"]["finished"]:
            acted_at = states[-1]["s"]["total"]       # a state we already consumed
            client.act({"a": "add", "n": 1})
        while time.monotonic() < deadline:
            inbox.pump()
            while inbox.pending:
                msg = inbox.pending.pop(0)
                if msg["t"] == "over":
                    return msg
                if msg["t"] == "state":
                    s = msg["s"]
                    if s["turn"] == seat and not s["finished"] and s["total"] != acted_at:
                        acted_at = s["total"]
                        client.act({"a": "add", "n": 1})
            time.sleep(0.005)
        raise AssertionError("game did not finish")


# ================================================================ netutil
class NetutilTests(unittest.TestCase):
    def test_is_valid_ipv4(self) -> None:
        for good in ("192.168.1.5", "0.0.0.0", "255.255.255.255", "10.0.0.1", "127.0.0.1"):
            self.assertTrue(netutil.is_valid_ipv4(good), good)
        for bad in ("256.1.1.1", "01.2.3.4", "1.2.3", "1.2.3.4.5", " 1.2.3.4", "1.2.3.4 ", "1. 2.3.4",
                    "a.b.c.d", "", "1..2.3", "1.2.3.-4", "+1.2.3.4", "１.2.3.4", "1.2.3.4\n",
                    "1.2.3.0004", None, 123, b"1.2.3.4"):
            self.assertFalse(netutil.is_valid_ipv4(bad), repr(bad))

    def test_friendly_error_mapping(self) -> None:
        fe = netutil.friendly_error
        self.assertEqual(fe(ConnectionRefusedError(errno.ECONNREFUSED, "refused")), netutil.MSG_REFUSED)
        self.assertEqual(fe(socket.timeout("timed out")), netutil.MSG_TIMEOUT)
        self.assertEqual(fe(OSError(errno.ETIMEDOUT, "timed out")), netutil.MSG_TIMEOUT)
        self.assertEqual(fe(OSError(errno.EADDRINUSE, "in use"), "host"), netutil.MSG_IN_USE)
        self.assertEqual(fe(OSError(errno.EADDRNOTAVAIL, "nope"), "host"), netutil.MSG_NOT_HERE)
        self.assertEqual(fe(OSError(errno.ENETUNREACH, "unreachable")), netutil.MSG_UNREACH)
        self.assertEqual(fe(OSError(errno.EHOSTUNREACH, "unreachable")), netutil.MSG_UNREACH)
        self.assertEqual(fe(PermissionError(errno.EACCES, "denied"), "host"), netutil.MSG_DENIED_HOST)
        self.assertEqual(fe(ConnectionResetError(errno.ECONNRESET, "reset")), netutil.MSG_RESET)
        self.assertEqual(fe(socket.gaierror(11001, "no such host")), netutil.MSG_BAD_IP)
        self.assertEqual(fe(RejectedError("Room is full")), "Room is full")
        self.assertEqual(fe(OSError("weird")), netutil.MSG_GENERIC_CONNECT)
        self.assertEqual(fe(OSError("weird"), "host"), netutil.MSG_GENERIC_HOST)
        self.assertEqual(fe(ValueError("odd")), netutil.MSG_GENERIC_CONNECT)
        self.assertTrue(issubclass(RejectedError, ConnectionError))

    @unittest.skipUnless(sys.platform == "win32", "WinError codes")
    def test_friendly_error_winerror(self) -> None:
        fe = netutil.friendly_error
        self.assertEqual(fe(OSError(0, "x", None, 10061)), netutil.MSG_REFUSED)
        self.assertEqual(fe(OSError(0, "x", None, 10060)), netutil.MSG_TIMEOUT)
        self.assertEqual(fe(OSError(0, "x", None, 10065)), netutil.MSG_UNREACH)
        self.assertEqual(fe(OSError(0, "x", None, 10051)), netutil.MSG_UNREACH)
        self.assertEqual(fe(OSError(0, "x", None, 10049), "host"), netutil.MSG_NOT_HERE)
        self.assertEqual(fe(OSError(0, "x", None, 10048), "host"), netutil.MSG_IN_USE)
        self.assertEqual(fe(OSError(0, "x", None, 10013), "host"), netutil.MSG_DENIED_HOST)

    IPCONFIG_EN = (
        "\r\nWindows IP Configuration\r\n\r\n\r\n"
        "Ethernet adapter Ethernet:\r\n\r\n"
        "   Media State . . . . . . . . . . . : Media disconnected\r\n"
        "   Connection-specific DNS Suffix  . :\r\n\r\n"
        "Wireless LAN adapter Wi-Fi:\r\n\r\n"
        "   Connection-specific DNS Suffix  . : lan\r\n"
        "   Link-local IPv6 Address . . . . . : fe80::1c2b:3a4d:5e6f:7081%12\r\n"
        "   IPv4 Address. . . . . . . . . . . : 192.168.1.5(Preferred)\r\n"
        "   Subnet Mask . . . . . . . . . . . : 255.255.255.0\r\n"
        "   Default Gateway . . . . . . . . . : 192.168.1.1\r\n\r\n"
        "Ethernet adapter vEthernet (WSL):\r\n\r\n"
        "   IPv4 Address. . . . . . . . . . . : 172.22.48.1\r\n"
        "   Subnet Mask . . . . . . . . . . . : 255.255.240.0\r\n"
        "   Autoconfiguration IPv4 Address. . : 169.254.10.20\r\n"
    )

    def test_parse_ipconfig_locales(self) -> None:
        self.assertEqual(netutil.parse_ipconfig(self.IPCONFIG_EN),
                         {"192.168.1.5": "Wi-Fi", "172.22.48.1": "vEthernet (WSL)"})
        german = ("Windows-IP-Konfiguration\n\nDrahtlos-LAN-Adapter WLAN:\n\n"
                  "   Verbindungsspezifisches DNS-Suffix:\n"
                  "   IPv4-Adresse  . . . . . . . . . . : 10.0.0.23\n")
        self.assertEqual(netutil.parse_ipconfig(german), {"10.0.0.23": "WLAN"})
        french = ("Carte réseau sans fil Wi-Fi :\n\n"
                  "   Adresse IPv4. . . . . . . . . . . . . .: 192.168.0.42(préféré)\n")
        self.assertEqual(netutil.parse_ipconfig(french), {"192.168.0.42": "Carte réseau sans fil Wi-Fi"})
        self.assertEqual(netutil.parse_ipconfig(""), {})

    def test_local_ipv4s_order_and_filtering(self) -> None:
        with mock.patch.object(netutil, "_route_ip", return_value="192.168.1.5"), \
                mock.patch.object(netutil, "_hostname_ips",
                                  return_value=["172.22.48.1", "127.0.0.1", "169.254.3.4", "10.1.2.3", "192.168.1.5"]), \
                mock.patch.object(netutil, "_ipconfig_text", return_value=self.IPCONFIG_EN):
            got = netutil.local_ipv4s()
        self.assertEqual(got, [("192.168.1.5", "Wi-Fi"), ("10.1.2.3", "Network"), ("172.22.48.1", "vEthernet (WSL)")])
        with mock.patch.object(netutil, "_route_ip", return_value=None), \
                mock.patch.object(netutil, "_hostname_ips", return_value=["127.0.0.1"]), \
                mock.patch.object(netutil, "_ipconfig_text", return_value=""):
            self.assertEqual(netutil.local_ipv4s(), [])
            self.assertEqual(netutil.best_ipv4(), "127.0.0.1")
        with mock.patch.object(netutil, "_route_ip", return_value="10.9.8.7"), \
                mock.patch.object(netutil, "_hostname_ips", return_value=[]), \
                mock.patch.object(netutil, "_ipconfig_text", return_value=""):
            self.assertEqual(netutil.local_ipv4s(), [("10.9.8.7", "Main network")])
            self.assertEqual(netutil.best_ipv4(), "10.9.8.7")

    def test_local_ipv4s_real(self) -> None:
        got = netutil.local_ipv4s()
        self.assertIsInstance(got, list)
        ips = [ip for ip, _label in got]
        self.assertEqual(len(ips), len(set(ips)))
        for ip, label in got:
            self.assertTrue(netutil.is_valid_ipv4(ip))
            self.assertFalse(ip.startswith("127.") or ip.startswith("169.254."))
            self.assertTrue(isinstance(label, str) and label)
        self.assertTrue(netutil.is_valid_ipv4(netutil.best_ipv4()))


# ============================================================== handshake
class HandshakeTests(NetCase):
    def test_welcome_and_room_shape(self) -> None:
        srv = self.server(humans=2, bots=1)
        port = srv.address[1]
        self.assertNotEqual(port, 0)
        host, hin = self.client(srv, "Asha", host=True)
        w = host.welcome
        self.assertEqual(w["t"], "welcome")
        self.assertTrue(w["host"])
        self.assertTrue(host.is_host and host.connected)
        self.assertEqual(host.my_id, w["you"])
        room = w["room"]
        self.assertEqual(set(room), {"game", "game_name", "humans", "bots", "options", "state",
                                     "host_name", "address", "seats"})
        self.assertEqual(room["game"], "counter")
        self.assertEqual(room["game_name"], "Counter")
        self.assertEqual((room["humans"], room["bots"]), (2, 1))
        self.assertEqual(room["options"], {"target": 6, "turn_timer": 0})
        self.assertEqual(room["state"], "lobby")
        self.assertEqual(room["host_name"], "Asha")
        self.assertEqual(room["address"], "127.0.0.1:%d" % port)
        seats = room["seats"]
        self.assertEqual(len(seats), 3)
        for s in seats:
            self.assertEqual(set(s), {"seat", "kind", "name", "client", "color", "host"})
        self.assertEqual([s["seat"] for s in seats], [0, 1, 2])
        self.assertEqual([s["color"] for s in seats], CounterLogic.seat_colors(3))
        self.assertEqual(seats[0], {"seat": 0, "kind": "human", "name": "Asha", "client": w["you"],
                                    "color": seats[0]["color"], "host": True})
        self.assertEqual(seats[1]["kind"], "open")
        self.assertIsNone(seats[1]["name"])
        self.assertIsNone(seats[1]["client"])
        self.assertFalse(seats[1]["host"])
        self.assertEqual(seats[2]["kind"], "bot")
        self.assertTrue(seats[2]["name"].startswith("Bot "))
        json.dumps(room)

        guest, gin = self.client(srv, "Ravi")
        self.assertFalse(guest.is_host)
        self.assertNotEqual(guest.my_id, host.my_id)
        gseat = guest.welcome["room"]["seats"][1]
        self.assertEqual((gseat["kind"], gseat["name"], gseat["client"]), ("human", "Ravi", guest.my_id))
        lobby = hin.wait("lobby")
        self.assertEqual(lobby["room"]["seats"][1]["name"], "Ravi")
        self.assertTrue(wait_until(lambda: srv.room_info()["seats"][1]["name"] == "Ravi"))

    def test_host_token(self) -> None:
        srv = self.server(humans=3)
        fake, fin = self.client(srv, "Mallory")
        self.assertFalse(fake.is_host)
        seats = fake.welcome["room"]["seats"]
        self.assertEqual(seats[0]["kind"], "open")             # seat 0 is kept for the host
        self.assertEqual(seats[1]["name"], "Mallory")
        wrong = Client(LOCAL, srv.address[1], "Guess", token="0" * 32)
        self._clients.append(wrong)
        wrong.connect(timeout=WAIT)
        self.assertFalse(wrong.is_host)
        self.assertEqual(wrong.welcome["room"]["seats"][2]["name"], "Guess")
        self.assertEqual(srv.room_info()["seats"][0]["kind"], "open")
        host, hin = self.client(srv, "Asha", host=True)
        self.assertTrue(host.is_host)
        self.assertEqual(host.welcome["room"]["seats"][0]["client"], host.my_id)
        fake.send({"t": "start"})
        self.assertEqual(fin.wait("error")["msg"], "Only the host can start the game")

    def test_second_token_holder_is_not_host(self) -> None:
        srv = self.server(humans=3)
        host, _ = self.client(srv, "Asha", host=True)
        copy, _ = self.client(srv, "Copy", host=True)
        self.assertTrue(host.is_host)
        self.assertFalse(copy.is_host)

    def test_full_room_is_rejected(self) -> None:
        srv, host, hin, guest, gin = self.room_with_two()
        late = Client(LOCAL, srv.address[1], "Late")
        self._clients.append(late)
        with self.assertRaises(RejectedError) as cm:
            late.connect(timeout=WAIT)
        self.assertEqual(str(cm.exception), "Room is full")
        self.assertEqual(netutil.friendly_error(cm.exception), "Room is full")
        self.assertFalse(late.connected)
        later, lin = self.client(srv, "Later", connect=False)
        later.connect_async(timeout=WAIT)
        self.assertEqual(lin.wait("_connect_failed")["error"], "Room is full")
        lin.settle(0.1)
        self.assertEqual(lin.count("_disconnected"), 0)

    def test_single_seat_room_rejects_joiners(self) -> None:
        srv = self.server(humans=1, bots=1)
        with self.assertRaises(RejectedError):
            self.client(srv, "Ravi")
        host, _ = self.client(srv, "Asha", host=True)
        self.assertTrue(host.connected)

    def test_version_mismatch(self) -> None:
        srv = self.server()
        p = self.peer(srv, hello=False)
        p.send_msg({"t": "hello", "v": 999, "app": APP_ID, "name": "Old"})
        msg = p.recv_msg()
        self.assertEqual(msg, {"t": "reject", "reason": "Version mismatch: host has v%d, you have v999" % PROTOCOL_VERSION})
        self.assertTrue(p.wait_closed())
        p2 = self.peer(srv, hello=False)
        p2.send_msg({"t": "hello", "v": PROTOCOL_VERSION, "app": "other", "name": "X"})
        self.assertTrue(p2.recv_msg()["reason"].startswith("Version mismatch"))
        p3 = self.peer(srv, hello=False)
        p3.send_msg({"t": "hello", "app": APP_ID, "name": "X"})
        self.assertEqual(p3.recv_msg()["reason"], "Version mismatch: host has v%d, you have v?" % PROTOCOL_VERSION)

    def test_duplicate_names(self) -> None:
        srv = self.server(humans=4)
        self.client(srv, "Asha", host=True)
        self.client(srv, "Ravi")
        self.client(srv, "  Ravi ")
        last, _ = self.client(srv, "asha")
        names = [s["name"] for s in last.welcome["room"]["seats"]]
        self.assertEqual(names, ["Asha", "Ravi", "Ravi 2", "asha 2"])
        srv2 = self.server(humans=2)
        long_name = "Abcdefghijklmnop"
        self.client(srv2, long_name, host=True)
        dup, _ = self.client(srv2, long_name)
        self.assertEqual(dup.welcome["room"]["seats"][1]["name"], "Abcdefghijklmn 2")

    def test_client_api_edges(self) -> None:
        srv = self.server()
        c = Client(LOCAL, srv.address[1], "Asha", token=srv.host_token)
        self._clients.append(c)
        self.assertFalse(c.send({"t": "ping", "n": 1}))
        self.assertFalse(c.connected)
        self.assertIsNone(c.my_id)
        c.connect(timeout=WAIT)
        with self.assertRaises(RuntimeError):
            c.connect(timeout=WAIT)
        c.close()
        c.close()
        self.assertFalse(c.connected)
        self.assertFalse(c.send({"t": "ping", "n": 2}))
        closed = Client(LOCAL, srv.address[1], "Nope")
        closed.close()
        with self.assertRaises(OSError):
            closed.connect(timeout=WAIT)

    def test_probe(self) -> None:
        srv = self.server()
        ok, msg = probe(LOCAL, srv.address[1])
        self.assertTrue(ok, msg)
        s = socket.socket()
        s.bind((LOCAL, 0))
        free_port = s.getsockname()[1]
        s.close()
        ok, msg = probe(LOCAL, free_port, timeout=3)
        self.assertFalse(ok)
        self.assertIn(msg, (netutil.MSG_REFUSED, netutil.MSG_TIMEOUT))
        self.assertFalse(probe(LOCAL, 70000)[0])


# ================================================================ lobby
class LobbyTests(NetCase):
    def test_configure_validation(self) -> None:
        srv, host, hin, guest, gin = self.room_with_two()
        guest.send({"t": "configure", "game": "duo", "humans": 2, "bots": 0})
        self.assertEqual(gin.wait("error")["msg"], "Only the host can change the game")
        cases = [
            ({"game": "nope", "humans": 2, "bots": 0}, "Unknown game"),
            ({"game": 5, "humans": 2, "bots": 0}, "Unknown game"),
            ({"game": "counter", "humans": 0, "bots": 2}, "At least one person has to play"),
            ({"game": "counter", "humans": 3, "bots": 2}, "Counter is for 2 to 4 players"),
            ({"game": "duo", "humans": 1, "bots": 2}, "Duo is for 2 players"),
            ({"game": "counter", "humans": "2", "bots": 0}, "Pick how many people and bots"),
            ({"game": "counter", "humans": True, "bots": 1}, "Pick how many people and bots"),
            ({"game": "counter", "humans": 1, "bots": 1}, "2 people are here. Remove someone or pick more people."),
            ({"game": "counter", "humans": 2, "bots": 0, "options": "fast"}, "Those options aren't valid"),
        ]
        lobbies_before = hin.count("lobby")
        for fields, expected in cases:
            msg = {"t": "configure"}
            msg.update(fields)
            host.send(msg)
            self.assertEqual(hin.wait("error")["msg"], expected, fields)
        hin.settle(0.05)
        self.assertEqual(hin.count("lobby"), lobbies_before)

        host.send({"t": "configure", "game": "duo", "humans": 2, "bots": 0, "options": {"target": 3, "junk": 1}})
        room = hin.wait("lobby")["room"]
        self.assertEqual((room["game"], room["game_name"]), ("duo", "Duo"))
        self.assertEqual(room["options"], {"target": 3, "turn_timer": 0})
        groom = gin.wait("lobby", lambda m: m["room"]["game"] == "duo")["room"]
        self.assertEqual(groom["seats"][1]["client"], guest.my_id)
        self.assertEqual(groom["seats"][0]["client"], host.my_id)

        host.send({"t": "configure", "game": "counter", "humans": 2, "bots": 2, "options": {"target": 99}})
        room = hin.wait("lobby")["room"]
        self.assertEqual(room["options"]["target"], 6)
        self.assertEqual([s["kind"] for s in room["seats"]], ["human", "human", "bot", "bot"])
        self.assertEqual(len({s["name"] for s in room["seats"]}), 4)
        self.assertEqual([s["color"] for s in room["seats"]], CounterLogic.seat_colors(4))

        host.send({"t": "configure", "options": {"target": 40}})
        room = hin.wait("lobby")["room"]
        self.assertEqual((room["game"], room["humans"], room["bots"], room["options"]["target"]), ("counter", 2, 2, 40))

        host.send({"t": "configure", "humans": 4, "bots": 0})
        room = hin.wait("lobby")["room"]
        self.assertEqual([s["kind"] for s in room["seats"]], ["human", "human", "open", "open"])
        self.assertEqual(room["options"]["target"], 40)

    def test_kick(self) -> None:
        srv, host, hin, guest, gin = self.room_with_two()
        guest.send({"t": "kick", "client": host.my_id})
        self.assertEqual(gin.wait("error")["msg"], "Only the host can remove players")
        host.send({"t": "kick", "client": host.my_id})
        self.assertEqual(hin.wait("error")["msg"], "You can't remove yourself")
        host.send({"t": "kick", "client": 999})
        self.assertEqual(hin.wait("error")["msg"], "That player isn't here")
        host.send({"t": "kick", "client": guest.my_id})
        gone = gin.wait("_disconnected")
        self.assertEqual(gone["reason"], "You were removed by the host")
        self.assertTrue(gone.get("bye"))
        room = hin.wait("lobby", lambda m: m["room"]["seats"][1]["kind"] == "open")["room"]
        self.assertIsNone(room["seats"][1]["client"])
        gin.settle(0.3)
        self.assertEqual(gin.count("_disconnected"), 1)
        self.assertFalse(guest.connected)
        self.assertFalse(guest.send({"t": "ping", "n": 1}))
        again, _ = self.client(srv, "Ivy")
        self.assertEqual(again.welcome["room"]["seats"][1]["name"], "Ivy")

    def test_guest_leaving_lobby_frees_the_seat(self) -> None:
        srv, host, hin, guest, gin = self.room_with_two()
        guest.close()
        hin.wait("lobby", lambda m: m["room"]["seats"][1]["kind"] == "open")
        gin.settle(0.2)
        self.assertEqual(gin.count("_disconnected"), 0)      # none after close()

    def test_host_leaving_closes_the_room(self) -> None:
        srv, host, hin, guest, gin = self.room_with_two()
        host.close()
        gone = gin.wait("_disconnected")
        self.assertEqual(gone["reason"], "Host closed the room")
        self.assertTrue(wait_until(lambda: not srv.running, 3.0))
        hin.settle(0.1)
        self.assertEqual(hin.count("_disconnected"), 0)
        srv.stop()

    def test_stop_says_bye_to_everyone(self) -> None:
        srv, host, hin, guest, gin = self.room_with_two()
        srv.stop()
        self.assertFalse(srv.running)
        for inbox in (hin, gin):
            self.assertEqual(inbox.wait("_disconnected")["reason"], "Host closed the room")
        self.assertFalse(host.connected or guest.connected)

    def test_stop_is_idempotent(self) -> None:
        srv = self.server()
        self.client(srv, "Asha", host=True)
        srv.stop()
        srv.stop()
        threads = [threading.Thread(target=srv.stop) for _ in range(3)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(3)
        self.assertFalse(srv.running)
        idle = self.server(start=False)
        idle.stop()
        idle.stop()
        with self.assertRaises(RuntimeError):
            idle.start()

    def test_constructor_and_bind_errors(self) -> None:
        with self.assertRaises(ValueError):
            RoomServer(LOCAL, 0, "Asha", "nope", 2, 0, discovery=False, resolver=resolver)
        with self.assertRaises(ValueError):
            RoomServer(LOCAL, 0, "Asha", "counter", 0, 2, discovery=False, resolver=resolver)
        with self.assertRaises(ValueError):
            RoomServer(LOCAL, 0, "Asha", "duo", 2, 1, discovery=False, resolver=resolver)
        srv = self.server()
        dup = RoomServer(LOCAL, srv.address[1], "Asha", "counter", 2, 0, discovery=False, resolver=resolver)
        with self.assertRaises(OSError) as cm:
            dup.start()
        self.assertEqual(netutil.friendly_error(cm.exception, "host"), netutil.MSG_IN_USE)
        dup.stop()
        foreign = RoomServer("203.0.113.77", 0, "Asha", "counter", 2, 0, discovery=False, resolver=resolver)
        with self.assertRaises(OSError) as cm:
            foreign.start()
        self.assertEqual(netutil.friendly_error(cm.exception, "host"), netutil.MSG_NOT_HERE)

    def test_room_info_is_a_copy(self) -> None:
        srv = self.server()
        info = srv.room_info()
        info["seats"][0]["name"] = "Hacked"
        self.assertNotEqual(srv.room_info()["seats"][0]["name"], "Hacked")


# ================================================================ limits
class LimitTests(NetCase):
    def test_malformed_line_drops_only_that_client(self) -> None:
        srv = self.server(humans=3)
        host, hin = self.client(srv, "Asha", host=True)
        bad = self.peer(srv, name="Bad")
        self.assertEqual(bad.recv_msg()["t"], "welcome")
        hin.wait("lobby", lambda m: m["room"]["seats"][1]["kind"] == "human")
        bad.send_raw(b"{oops\n")
        self.assertTrue(bad.wait_closed())
        hin.wait("lobby", lambda m: m["room"]["seats"][1]["kind"] == "open")
        self.assertTrue(host.connected)
        host.send({"t": "configure", "options": {"target": 3}})
        self.assertEqual(hin.wait("lobby")["room"]["options"]["target"], 3)

    def test_oversized_line_drops_only_that_client(self) -> None:
        srv = self.server(humans=3)
        host, hin = self.client(srv, "Asha", host=True)
        big = self.peer(srv, name="Big")
        self.assertEqual(big.recv_msg()["t"], "welcome")
        hin.wait("lobby", lambda m: m["room"]["seats"][1]["kind"] == "human")
        big.send_raw(b"x" * (P.MAX_LINE + 8192))
        self.assertTrue(big.wait_closed())
        hin.wait("lobby", lambda m: m["room"]["seats"][1]["kind"] == "open")
        self.assertTrue(host.connected)

    def test_message_before_hello_is_dropped(self) -> None:
        srv = self.server()
        p = self.peer(srv, hello=False)
        p.send_msg({"t": "start"})
        self.assertTrue(p.wait_closed())

    def test_hello_timeout(self) -> None:
        srv = self.server(HELLO_TIMEOUT=0.3)
        t0 = time.monotonic()
        p = self.peer(srv, hello=False)
        self.assertTrue(p.wait_closed(3.0))
        self.assertGreater(time.monotonic() - t0, 0.25)

    def test_idle_timeout_and_pings(self) -> None:
        srv = self.server(humans=3, IDLE_TIMEOUT=0.6)
        host = Client(LOCAL, srv.address[1], "Asha", token=srv.host_token)
        host.PING_INTERVAL = 0.1
        self._clients.append(host)
        host.connect(timeout=WAIT)
        quiet = self.peer(srv, name="Quiet")
        self.assertEqual(quiet.recv_msg()["t"], "welcome")
        self.assertTrue(quiet.wait_closed(3.0))
        self.assertTrue(host.connected)          # its pings kept it alive
        self.assertIsNotNone(host.rtt)

    def test_max_clients(self) -> None:
        srv = self.server(MAX_CLIENTS=2)
        self.client(srv, "Asha", host=True)
        waiting = self.peer(srv, hello=False)
        self.assertTrue(wait_until(lambda: len(srv._conns) == 2))
        extra = self.peer(srv, hello=False)
        self.assertTrue(extra.wait_closed())
        if extra.msgs:
            self.assertEqual(extra.msgs[0], {"t": "reject", "reason": "Room is full"})
        waiting.close()

    def test_silent_host_is_detected_by_client(self) -> None:
        listener = socket.socket()
        listener.bind((LOCAL, 0))
        listener.listen(1)
        self.addCleanup(listener.close)
        port = listener.getsockname()[1]
        accepted: List[socket.socket] = []

        def fake_host() -> None:
            conn, _ = listener.accept()
            accepted.append(conn)
            conn.recv(4096)
            conn.sendall(P.encode({"t": "welcome", "you": 1, "host": False, "room": {}}))

        th = threading.Thread(target=fake_host, daemon=True)
        th.start()
        c = Client(LOCAL, port, "Ravi")
        c.SILENCE_TIMEOUT = 0.4
        c.PING_INTERVAL = 0.1
        self._clients.append(c)
        c.connect(timeout=WAIT)
        inbox = Inbox(c)
        self.assertEqual(inbox.wait("_disconnected")["reason"], "Lost connection to the host")
        inbox.settle(0.2)
        self.assertEqual(inbox.count("_disconnected"), 1)
        th.join(1)
        for s in accepted:
            s.close()


# ================================================================== play
class PlayTests(NetCase):
    def test_start_sends_begin_then_state(self) -> None:
        srv, host, hin, guest, gin = self.room_with_two(bots=1, options={"target": 40})
        host.send({"t": "start"})
        hb = hin.wait("begin")
        gb = gin.wait("begin")
        self.assertEqual((hb["seat"], gb["seat"]), (0, 1))
        self.assertEqual(hb["game"], "counter")
        self.assertEqual(hb["options"], {"target": 40, "turn_timer": 0})
        seats = hb["seats"]
        self.assertEqual(len(seats), 3)
        for s in seats:
            self.assertEqual(set(s), {"seat", "name", "bot", "color"})
        self.assertEqual([s["bot"] for s in seats], [False, False, True])
        self.assertEqual([s["name"] for s in seats[:2]], ["Asha", "Ravi"])
        for inbox, begin, seat in ((hin, hb, 0), (gin, gb, 1)):
            ev = inbox.wait("event", lambda m: m["e"]["kind"] == "started")
            st = inbox.wait("state")
            self.assertLess(inbox.index(begin), inbox.index(ev))
            self.assertLess(inbox.index(ev), inbox.index(st))
            self.assertEqual(st["s"]["turn"], 0)
            self.assertEqual(st["s"]["me"], seat)
            self.assertEqual(st["s"]["secret"], "secret-%d" % seat)
        self.assertTrue(wait_until(lambda: srv.room_info()["state"] == "playing"))
        host.send({"t": "start"})
        self.assertEqual(hin.wait("error")["msg"], "A game is already running")
        host.send({"t": "configure", "humans": 2, "bots": 2})
        self.assertEqual(hin.wait("error")["msg"], "Wait for this game to end first")

    def test_open_seats_become_bots_on_start(self) -> None:
        srv = self.server(humans=3, bots=0, options={"target": 40})
        host, hin = self.client(srv, "Asha", host=True)
        host.send({"t": "start"})
        seats = hin.wait("begin")["seats"]
        self.assertEqual([s["bot"] for s in seats], [False, True, True])
        self.assertTrue(all(s["name"].startswith("Bot ") for s in seats[1:]))
        self.assertEqual(len({s["name"] for s in seats}), 3)
        # a late friend replaces the first bot that is sitting in a person's seat
        late, lin = self.client(srv, "Ravi")
        begin = lin.wait("begin")
        self.assertEqual(begin["seat"], 1)
        self.assertFalse(begin["seats"][1]["bot"])
        self.assertEqual(begin["seats"][1]["name"], "Ravi")
        self.assertIn("replaced", hin.wait("notice")["msg"])

    def test_act_event_comes_before_state(self) -> None:
        srv, host, hin, guest, gin = self.room_with_two(options={"target": 40})
        self.start_game(host, hin, gin)
        host.act({"a": "add", "n": 2})
        for inbox in (hin, gin):
            ev = inbox.wait("event", lambda m: m["e"]["kind"] == "added")
            st = inbox.wait("state", lambda m: m["s"]["total"] == 2)
            self.assertEqual(ev["e"], {"kind": "added", "seat": 0, "n": 2, "total": 2})
            self.assertLess(inbox.index(ev), inbox.index(st))
            self.assertEqual(st["s"]["turn"], 1)

    def test_action_error_goes_only_to_the_actor(self) -> None:
        srv, host, hin, guest, gin = self.room_with_two(options={"target": 40})
        self.start_game(host, hin, gin)
        guest.act({"a": "add", "n": 1})
        self.assertEqual(gin.wait("error")["msg"], "Not your turn")
        guest.act("not a dict")             # type: ignore[arg-type]
        self.assertEqual(gin.wait("error")["msg"], "That move isn't allowed")
        host.act({"a": "add", "n": 1})
        hin.wait("state", lambda m: m["s"]["total"] == 1)
        self.assertEqual(hin.count("error"), 0)
        guest.act({"a": "add", "n": 7})
        self.assertEqual(gin.wait("error")["msg"], "Add 1 or 2")
        guest.act({"a": "add", "n": 2})
        hin.wait("state", lambda m: m["s"]["total"] == 3)
        self.assertEqual(hin.count("error"), 0)

    def test_act_in_lobby_is_an_error(self) -> None:
        srv, host, hin, guest, gin = self.room_with_two()
        guest.act({"a": "add", "n": 1})
        self.assertEqual(gin.wait("error")["msg"], "The game hasn't started yet")

    def test_exception_in_game_code_is_contained(self) -> None:
        srv, host, hin, guest, gin = self.room_with_two(options={"target": 40})
        self.start_game(host, hin, gin)
        with self.assertLogs("goip.server", level="ERROR") as logs:
            guest.act({"a": "boom"})
            err = gin.wait("error")
        self.assertEqual(err["msg"], "Something went wrong on the host")
        self.assertTrue(any("game code failed" in line for line in logs.output))
        self.assertTrue(srv.running)
        host.act({"a": "add", "n": 1})
        gin.wait("state", lambda m: m["s"]["total"] == 1)
        self.assertEqual(hin.count("error"), 0)

    def test_broken_start_returns_to_lobby(self) -> None:
        srv, host, hin, guest, gin = self.room_with_two(game="broken")
        with self.assertLogs("goip.server", level="ERROR"):
            host.send({"t": "start"})
            hin.wait("begin")
            self.assertEqual(hin.wait("error")["msg"], "Something went wrong on the host")
        over = gin.wait("over")
        self.assertEqual(over["result"]["winners"], [])
        self.assertEqual(gin.wait("lobby")["room"]["state"], "lobby")
        self.assertTrue(srv.running)

    def test_game_over_returns_to_lobby_and_rematch(self) -> None:
        srv, host, hin, guest, gin = self.room_with_two(options={"target": 3})
        self.start_game(host, hin, gin)
        host.act({"a": "add", "n": 2})
        gin.wait("state", lambda m: m["s"]["turn"] == 1)
        guest.act({"a": "add", "n": 1})
        for inbox in (hin, gin):
            over = inbox.wait("over")
            self.assertEqual(over["result"]["winners"], [1])
            self.assertEqual(over["result"]["text"], "Ravi wins")
            self.assertEqual(over["result"]["total"], 3)
            lobby = inbox.wait("lobby")
            self.assertLess(inbox.index(over), inbox.index(lobby))
            room = lobby["room"]
            self.assertEqual(room["state"], "lobby")
            self.assertEqual([s["kind"] for s in room["seats"]], ["human", "human"])
        final = [m for m in gin.log if m["t"] == "state"][-1]["s"]
        self.assertTrue(final["finished"])
        self.assertLess(gin.index([m for m in gin.log if m["t"] == "state"][-1]), gin.index(over))
        guest.send({"t": "start"})
        self.assertEqual(gin.wait("error")["msg"], "Only the host can start the game")
        host.send({"t": "start"})
        self.assertEqual(hin.wait("begin")["seat"], 0)
        self.assertEqual(gin.wait("begin")["seat"], 1)

    def test_disconnect_mid_game_bot_takes_over_and_finishes(self) -> None:
        srv, host, hin, guest, gin = self.room_with_two(options={"target": 6})
        self.start_game(host, hin, gin)
        guest.close()
        seats = hin.wait("seats")["seats"]
        self.assertEqual(seats[1]["bot"], True)
        self.assertEqual(seats[1]["name"], "Ravi")
        notice = hin.wait("notice")["msg"]
        self.assertIn("Ravi left", notice)
        self.assertIn("bot took over", notice)
        self.assertTrue(wait_until(lambda: srv.room_info()["seats"][1]["kind"] == "bot"))
        over = self.play_until_over(host, hin, 0)
        self.assertEqual(over["result"]["winners"], [1])       # the bot made the winning move
        room = hin.wait("lobby")["room"]
        self.assertEqual(room["state"], "lobby")
        self.assertEqual(room["seats"][1]["kind"], "open")
        self.assertIsNone(room["seats"][1]["name"])

    def test_reclaim_seat_with_same_name(self) -> None:
        srv, host, hin, guest, gin = self.room_with_two(options={"target": 40})
        self.start_game(host, hin, gin)
        guest.close()
        hin.wait("seats", lambda m: m["seats"][1]["bot"])
        hin.wait("notice")
        back, bin_ = self.client(srv, "Ravi")
        self.assertFalse(back.is_host)
        begin = bin_.wait("begin")
        self.assertEqual(begin["seat"], 1)
        self.assertFalse(begin["seats"][1]["bot"])
        st = bin_.wait("state")
        self.assertEqual(st["s"]["me"], 1)
        self.assertLess(bin_.index(begin), bin_.index(st))
        hin.wait("seats", lambda m: m["seats"][1]["bot"] is False)
        self.assertEqual(hin.wait("notice")["msg"], "Ravi is back")
        host.act({"a": "add", "n": 1})
        bin_.wait("state", lambda m: m["s"]["turn"] == 1)
        back.act({"a": "add", "n": 2})
        hin.wait("state", lambda m: m["s"]["total"] == 3)
        self.assertTrue(wait_until(lambda: srv.room_info()["seats"][1]["client"] == back.my_id))


# ============================================================= discovery
class DiscoveryTests(NetCase):
    @staticmethod
    def beacon_bytes(**fields: Any) -> bytes:
        msg = {"app": APP_ID, "v": PROTOCOL_VERSION, "ip": "192.168.1.5", "port": 50505, "host": "Asha",
               "game": "ludo", "game_name": "Ludo", "players": "1/4", "state": "lobby"}
        msg.update(fields)
        return json.dumps(msg).encode()

    def test_scanner_validates_and_dedupes(self) -> None:
        sc = Scanner(port=0)
        src = ("192.168.1.5", 50506)
        self.assertFalse(sc.feed(b"x" * 3000, src))
        self.assertFalse(sc.feed(b"{nope", src))
        self.assertFalse(sc.feed(b"[1, 2]", src))
        self.assertFalse(sc.feed(self.beacon_bytes(app="other"), src))
        self.assertFalse(sc.feed(self.beacon_bytes(port="50505"), src))
        self.assertFalse(sc.feed(self.beacon_bytes(port=0), src))
        self.assertEqual(sc.hosts(), [])
        self.assertTrue(sc.feed(self.beacon_bytes(), src))
        self.assertTrue(sc.feed(self.beacon_bytes(players="2/4"), src))
        hosts = sc.hosts()
        self.assertEqual(len(hosts), 1)
        h = hosts[0]
        self.assertEqual((h["ip"], h["port"], h["host"], h["game"], h["game_name"], h["players"], h["state"]),
                         ("192.168.1.5", 50505, "Asha", "ludo", "Ludo", "2/4", "lobby"))
        self.assertTrue(h["compatible"])
        time.sleep(0.01)
        self.assertTrue(sc.feed(self.beacon_bytes(ip="0.0.0.0", port=50600, host="Ravi\x00\n"), ("10.0.0.9", 50506)))
        hosts = sc.hosts()
        self.assertEqual([(x["ip"], x["port"]) for x in hosts], [("10.0.0.9", 50600), ("192.168.1.5", 50505)])
        self.assertEqual(hosts[0]["host"], "Ravi")
        self.assertTrue(sc.feed(self.beacon_bytes(v=99, port=50700), src))
        self.assertFalse([x for x in sc.hosts() if x["port"] == 50700][0]["compatible"])

    def test_scanner_forgets_old_rooms(self) -> None:
        sc = Scanner(port=0, window=0.05)
        sc.feed(self.beacon_bytes(), ("192.168.1.5", 50506))
        self.assertEqual(len(sc.hosts()), 1)
        time.sleep(0.1)
        self.assertEqual(sc.hosts(), [])

    def test_beacon_targets_and_payload(self) -> None:
        b = Beacon("192.168.1.5", lambda: {"ip": "192.168.1.5", "port": 50505, "app": "spoof"}, port=50506)
        self.assertEqual(b.targets(), [("255.255.255.255", 50506), ("192.168.1.255", 50506)])
        self.assertEqual(Beacon("127.0.0.1", dict, port=4).targets(), [("255.255.255.255", 4), ("127.0.0.1", 4)])
        self.assertEqual(Beacon("0.0.0.0", dict, port=4).targets(), [("255.255.255.255", 4)])
        data = json.loads(b.payload().decode())
        self.assertEqual(data, {"app": APP_ID, "v": PROTOCOL_VERSION, "ip": "192.168.1.5", "port": 50505})
        big = Beacon("192.168.1.5", lambda: {"pad": "x" * 3000})
        with self.assertLogs("goip.discovery", level="WARNING"):
            self.assertIsNone(big.payload())

    def test_beacon_reaches_scanner_on_loopback(self) -> None:
        sc = Scanner(port=0).start()
        self.addCleanup(sc.stop)
        if not sc.available:
            self.skipTest("UDP bind not allowed here")
        info = {"ip": LOCAL, "port": 50999, "host": "Asha", "game": "counter", "game_name": "Counter",
                "players": "1/2", "state": "lobby"}
        beacon = Beacon(LOCAL, lambda: info, interval=0.05, port=sc.port).start()
        self.addCleanup(beacon.stop)
        if not wait_until(lambda: bool(sc.hosts()), 3.0):
            self.skipTest("UDP datagrams are blocked on this machine")
        h = sc.hosts()[0]
        self.assertEqual((h["ip"], h["port"], h["host"], h["players"]), (LOCAL, 50999, "Asha", "1/2"))
        beacon.stop()
        sc.stop()
        self.assertFalse(beacon.running)
        self.assertFalse(sc.running)
        sc.stop()
        beacon.stop()

    def test_scanner_degrades_when_port_is_taken(self) -> None:
        blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(blocker.close)
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            blocker.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        blocker.bind(("0.0.0.0", 0))
        port = blocker.getsockname()[1]
        sc = Scanner(port=port).start()
        self.addCleanup(sc.stop)
        if sc.available:
            self.skipTest("this OS lets two sockets share the port")
        self.assertFalse(sc.running)
        self.assertEqual(sc.hosts(), [])
        sc.stop()

    def test_room_server_runs_a_beacon(self) -> None:
        srv = RoomServer(LOCAL, 0, "Asha", "counter", 2, 0, discovery=True, resolver=resolver)
        self._servers.append(srv)
        srv.start()
        self.assertIsNotNone(srv._beacon)
        self.assertTrue(wait_until(lambda: srv._beacon.running, 1.0))
        info = srv._beacon_info()
        self.assertEqual(info, {"ip": LOCAL, "port": srv.address[1], "host": "Asha", "game": "counter",
                                "game_name": "Counter", "players": "0/2", "state": "lobby"})
        self.client(srv, "Asha", host=True)
        self.assertTrue(wait_until(lambda: srv._beacon_info()["players"] == "1/2"))
        beacon = srv._beacon
        srv.stop()
        self.assertFalse(beacon.running)


if __name__ == "__main__":
    unittest.main()
