"""Tests for goip.protocol (framing, limits, clean_name)."""
from __future__ import annotations

import json
import unittest

from goip import protocol as P


class EncodeDecodeTests(unittest.TestCase):
    def test_roundtrip_is_one_compact_line(self) -> None:
        msg = {"t": "act", "a": {"a": "move", "token": 2}}
        data = P.encode(msg)
        self.assertTrue(data.endswith(b"\n"))
        self.assertEqual(data.count(b"\n"), 1)
        self.assertNotIn(b" ", data)
        self.assertEqual(P.decode(data[:-1]), msg)

    def test_unicode_is_kept_as_utf8(self) -> None:
        msg = {"t": "notice", "msg": "Asha left — a bot took over अ"}
        data = P.encode(msg)
        self.assertIn("—".encode("utf-8"), data)
        self.assertEqual(P.decode(data.strip()), msg)

    def test_nan_is_refused(self) -> None:
        with self.assertRaises(ValueError):
            P.encode({"t": "state", "s": {"x": float("nan")}})

    def test_too_large_to_encode(self) -> None:
        with self.assertRaises(P.ProtocolError):
            P.encode({"t": "state", "s": "x" * P.MAX_LINE})

    def test_decode_rejects_bad_input(self) -> None:
        for bad in (b"{nope", b"[1, 2]", b'"t"', b"{}", b'{"t": 5}', b"\xff\xfe", b"null"):
            with self.subTest(bad=bad):
                with self.assertRaises(P.ProtocolError):
                    P.decode(bad)

    def test_decode_rejects_oversized(self) -> None:
        line = json.dumps({"t": "x", "pad": "y" * P.MAX_LINE}).encode()
        with self.assertRaises(P.ProtocolError):
            P.decode(line)

    def test_protocol_error_is_value_error(self) -> None:
        self.assertTrue(issubclass(P.ProtocolError, ValueError))


class LineBufferTests(unittest.TestCase):
    def test_split_across_chunks(self) -> None:
        buf = P.LineBuffer()
        data = P.encode({"t": "ping", "n": 1}) + P.encode({"t": "ping", "n": 2})
        got = []
        for i in range(len(data)):
            got.extend(buf.feed(data[i:i + 1]))
        self.assertEqual(got, [{"t": "ping", "n": 1}, {"t": "ping", "n": 2}])

    def test_many_in_one_chunk_and_blank_lines(self) -> None:
        buf = P.LineBuffer()
        data = b"\n" + P.encode({"t": "a"}) + b"\r\n" + P.encode({"t": "b"}) + b'{"t":"c"'
        self.assertEqual(buf.feed(data), [{"t": "a"}, {"t": "b"}])
        self.assertEqual(buf.feed(b"}\n"), [{"t": "c"}])

    def test_crlf_lines(self) -> None:
        buf = P.LineBuffer()
        self.assertEqual(buf.feed(b'{"t":"a"}\r\n'), [{"t": "a"}])

    def test_oversized_line_without_newline(self) -> None:
        buf = P.LineBuffer(max_line=100)
        with self.assertRaises(P.ProtocolError):
            buf.feed(b"x" * 101)

    def test_oversized_line_with_newline(self) -> None:
        buf = P.LineBuffer(max_line=100)
        with self.assertRaises(P.ProtocolError):
            buf.feed(b"x" * 150 + b"\n")

    def test_line_at_limit_is_fine(self) -> None:
        buf = P.LineBuffer(max_line=100)
        line = b'{"t":"a","p":"' + b"y" * (100 - 16) + b'"}'
        self.assertEqual(len(line), 100)
        self.assertEqual(len(buf.feed(line + b"\n")), 1)

    def test_bad_json_raises(self) -> None:
        buf = P.LineBuffer()
        with self.assertRaises(P.ProtocolError):
            buf.feed(b"{oops\n")


class CleanNameTests(unittest.TestCase):
    def test_basic(self) -> None:
        self.assertEqual(P.clean_name("  Asha  "), "Asha")
        self.assertEqual(P.clean_name("Ravi   Kumar"), "Ravi Kumar")

    def test_control_and_format_chars_removed(self) -> None:
        self.assertEqual(P.clean_name("A\x00s​h\ta\n"), "Asha")

    def test_length_limit(self) -> None:
        name = P.clean_name("x" * 40)
        self.assertEqual(len(name), P.MAX_NAME)

    def test_fallbacks(self) -> None:
        self.assertEqual(P.clean_name(""), "Player")
        self.assertEqual(P.clean_name("   "), "Player")
        self.assertEqual(P.clean_name(None), "Player")
        self.assertEqual(P.clean_name(42, fallback="Host"), "Host")

    def test_unicode_letters_kept(self) -> None:
        self.assertEqual(P.clean_name("अशा"), "अशा")
        self.assertEqual(P.clean_name("Zoë \U0001F600"), "Zoë \U0001F600")


if __name__ == "__main__":
    unittest.main()
