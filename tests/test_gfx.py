"""Tests for goip.gfx (rasteriser, PNG, cache) and goip.widgets (UI kit).

Pure maths tests always run. GUI tests create tk.Tk() and skip when there is no display.
The micro-benchmark prints its numbers to stderr (run with -v to see them next to the test).
"""
from __future__ import annotations

import ast
import math
import os
import struct
import sys
import time
import unittest
import zlib

from goip import gfx
from goip.gfx import Image

try:
    import tkinter as tk
except Exception:  # pragma: no cover - tkinter missing entirely
    tk = None  # type: ignore[assignment]

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# ------------------------------------------------------------------ helpers
def decode_png(data: bytes):
    """Minimal decoder for the PNGs gfx writes: 8-bit RGBA, filter 0. Checks every CRC."""
    assert data[:8] == b"\x89PNG\r\n\x1a\n", "bad signature"
    pos, idat, w, h, tags = 8, b"", 0, 0, []
    while pos < len(data):
        n, = struct.unpack(">I", data[pos:pos + 4])
        tag, body = data[pos + 4:pos + 8], data[pos + 8:pos + 8 + n]
        crc, = struct.unpack(">I", data[pos + 8 + n:pos + 12 + n])
        assert crc == zlib.crc32(tag + body) & 0xFFFFFFFF, "bad CRC in %r" % tag
        tags.append(tag)
        if tag == b"IHDR":
            w, h, depth, ctype, comp, filt, lace = struct.unpack(">IIBBBBB", body)
            assert (depth, ctype, comp, filt, lace) == (8, 6, 0, 0, 0)
        elif tag == b"IDAT":
            idat += body
        pos += 12 + n
    assert tags[0] == b"IHDR" and tags[-1] == b"IEND"
    raw = zlib.decompress(idat)
    stride = w * 4
    assert len(raw) == h * (stride + 1)
    out = bytearray()
    for y in range(h):
        assert raw[y * (stride + 1)] == 0
        out += raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)]
    return w, h, out


def alpha_sum(img: Image) -> float:
    return sum(img.buf[3::4]) / 255.0


def make_root():
    if tk is None:
        raise unittest.SkipTest("tkinter not available")
    try:
        root = tk.Tk()
    except Exception as e:  # no display
        raise unittest.SkipTest("no display: %s" % e)
    root.geometry("420x600+40+40")
    return root


def pump(root, seconds: float, until=None) -> None:
    end = time.perf_counter() + seconds
    while time.perf_counter() < end:
        root.update()
        if until is not None and until():
            return
        time.sleep(0.005)


# ------------------------------------------------------------------ colours
class ColourTests(unittest.TestCase):
    def test_parse_forms(self):
        self.assertEqual(gfx.parse("#fff"), (255, 255, 255, 255))
        self.assertEqual(gfx.parse("#0f08"), (0, 255, 0, 136))
        self.assertEqual(gfx.parse("#102030"), (16, 32, 48, 255))
        self.assertEqual(gfx.parse("#10203040"), (16, 32, 48, 64))
        self.assertEqual(gfx.parse("#ABCDEF"), (171, 205, 239, 255))
        self.assertEqual(gfx.parse((1, 2, 3)), (1, 2, 3, 255))
        self.assertEqual(gfx.parse((1, 2, 3, 4)), (1, 2, 3, 4))
        self.assertEqual(gfx.parse([300, -5, 2.6, 128.4]), (255, 0, 3, 128))
        self.assertEqual(gfx.parse("white"), (255, 255, 255, 255))
        for bad in ("#12", "#12345", "nope", (1, 2), "#ggg", None):
            with self.assertRaises(ValueError):
                gfx.parse(bad)  # type: ignore[arg-type]

    def test_mix_lighten_darken(self):
        self.assertEqual(gfx.mix("#000000", "#ffffff", 0), "#000000")
        self.assertEqual(gfx.mix("#000000", "#ffffff", 1), "#ffffff")
        self.assertEqual(gfx.mix("#000000", "#ffffff", 0.5), "#808080")
        self.assertEqual(gfx.mix("#ff0000", "#0000ff", 2), "#0000ff")      # clamped
        self.assertEqual(gfx.mix((10, 20, 30), "#000", -1), "#0a141e")
        self.assertEqual(gfx.lighten("#000000", 0.25), "#404040")
        self.assertEqual(gfx.darken("#ffffff", 0.25), "#bfbfbf")
        self.assertEqual(gfx.lighten("#123456", 0), "#123456")

    def test_with_alpha_and_hex(self):
        self.assertEqual(gfx.with_alpha("#102030", 99), (16, 32, 48, 99))
        self.assertEqual(gfx.with_alpha("#102030", 999), (16, 32, 48, 255))
        self.assertEqual(gfx.with_alpha((1, 2, 3, 4), -3), (1, 2, 3, 0))
        self.assertEqual(gfx.hexs((255, 0, 16, 7)), "#ff0010")
        self.assertGreater(gfx.luminance("#ffffff"), 0.99)
        self.assertLess(gfx.luminance("#000000"), 0.01)


# ------------------------------------------------------------------ compositing
class CompositeTests(unittest.TestCase):
    def test_over_maths(self):
        over = gfx.over
        self.assertEqual(over((0, 0, 255, 255), (255, 0, 0, 255)), (255, 0, 0, 255))   # opaque wins
        self.assertEqual(over((0, 0, 255, 255), (255, 0, 0, 0)), (0, 0, 255, 255))     # nothing
        self.assertEqual(over((0, 0, 0, 0), (10, 20, 30, 77)), (10, 20, 30, 77))       # onto empty
        r, g, b, a = over((0, 0, 255, 255), (255, 0, 0, 128))
        self.assertEqual(a, 255)
        self.assertAlmostEqual(r, 128, delta=1)
        self.assertAlmostEqual(b, 127, delta=1)
        # semi over semi: alpha = as + ad (1 - as); colour is the alpha-weighted mean
        r, g, b, a = over((0, 0, 255, 128), (255, 0, 0, 128))
        self.assertAlmostEqual(a, 128 + 128 * 127 / 255.0, delta=1)
        ws, wd = 128 / 255.0, (128 / 255.0) * (1 - 128 / 255.0)
        self.assertAlmostEqual(r, 255 * ws / (ws + wd), delta=1)
        self.assertAlmostEqual(b, 255 * wd / (ws + wd), delta=1)

    def test_image_over_matches_formula(self):
        img = Image(4, 1, fill=(0, 0, 255, 128))
        img.fill_rect(0, 0, 4, 1, (255, 0, 0, 128))
        want = gfx.over((0, 0, 255, 128), (255, 0, 0, 128))
        for x in range(4):
            self.assertEqual(img.get(x, 0), want)

    def test_span_with_mixed_destination(self):
        img = Image(6, 1)
        img.set(1, 0, "#00ff00")
        img.set(4, 0, (0, 0, 255, 100))
        img.fill_rect(0, 0, 6, 1, (255, 0, 0, 51))
        self.assertEqual(img.get(0, 0), (255, 0, 0, 51))
        self.assertEqual(img.get(1, 0), gfx.over((0, 255, 0, 255), (255, 0, 0, 51)))
        self.assertEqual(img.get(4, 0), gfx.over((0, 0, 255, 100), (255, 0, 0, 51)))

    def test_fill_and_clear_and_copy(self):
        img = Image(3, 2, fill="#11223344")
        self.assertEqual(img.get(2, 1), (0x11, 0x22, 0x33, 0x44))
        cp = img.copy()
        img.clear()
        self.assertEqual(img.get(0, 0), (0, 0, 0, 0))
        self.assertEqual(cp.get(0, 0), (0x11, 0x22, 0x33, 0x44))
        cp.scale_alpha(0.5)
        self.assertEqual(cp.get(1, 1)[3], 34)

    def test_blit(self):
        dst = Image(4, 4, fill="#000000")
        src = Image(2, 2, fill=(255, 255, 255, 128))
        dst.blit(src, 3, 3)                       # clipped to one pixel
        self.assertEqual(dst.get(3, 3)[0], 128)
        self.assertEqual(dst.get(2, 2), (0, 0, 0, 255))
        dst.blit(src, -10, -10)                   # fully outside: no-op


# ------------------------------------------------------------------ anti-aliasing
class CoverageTests(unittest.TestCase):
    def test_integer_rect_is_exact(self):
        img = Image(10, 10)
        img.fill_rect(2, 3, 4, 5, "#ff0000")
        for y in range(10):
            for x in range(10):
                inside = 2 <= x < 6 and 3 <= y < 8
                self.assertEqual(img.get(x, y)[3], 255 if inside else 0, (x, y))

    def test_half_pixel_edges(self):
        img = Image(4, 1)
        img.fill_rect(0.5, 0, 2, 1, "#ffffff")
        self.assertAlmostEqual(img.get(0, 0)[3], 128, delta=2)
        self.assertEqual(img.get(1, 0)[3], 255)
        self.assertAlmostEqual(img.get(2, 0)[3], 128, delta=2)
        self.assertEqual(img.get(3, 0)[3], 0)

    def test_circle_area_and_soft_edge(self):
        img = Image(64, 64)
        img.fill_circle(32.3, 31.7, 20, "#ffffff")
        self.assertAlmostEqual(alpha_sum(img), math.pi * 400, delta=math.pi * 400 * 0.01)
        alphas = set(img.buf[3::4])
        self.assertTrue(any(0 < a < 255 for a in alphas), "edges should be anti-aliased")
        self.assertEqual(img.get(32, 32)[3], 255)
        self.assertEqual(img.get(2, 2)[3], 0)

    def test_stroke_circle_area(self):
        img = Image(64, 64)
        img.stroke_circle(32, 32, 20, "#ffffff", 3)
        want = math.pi * (20 ** 2 - 17 ** 2)
        self.assertAlmostEqual(alpha_sum(img), want, delta=want * 0.03)
        self.assertEqual(img.get(32, 32)[3], 0)

    def test_rrect_area_and_stroke(self):
        img = Image(80, 60)
        img.fill_rrect(10, 10, 50, 30, 8, "#ffffff")
        want = 50 * 30 - (4 - math.pi) * 64
        self.assertAlmostEqual(alpha_sum(img), want, delta=want * 0.01)
        img2 = Image(80, 60)
        img2.stroke_rrect(10, 10, 50, 30, 8, "#ffffff", 2)
        self.assertEqual(img2.get(35, 25)[3], 0)        # hollow
        self.assertEqual(img2.get(35, 10)[3], 255)      # top edge row
        inner = 46 * 26 - (4 - math.pi) * 36
        self.assertAlmostEqual(alpha_sum(img2), want - inner, delta=(want - inner) * 0.04)

    def test_gradient_rows(self):
        img = Image(10, 21)
        img.fill_rect(0, 0, 10, 21, "#000000", grad_to="#ff0000")
        reds = [img.get(5, y)[0] for y in range(21)]
        self.assertLess(reds[0], 10)
        self.assertGreater(reds[-1], 245)
        self.assertEqual(reds, sorted(reds))
        self.assertAlmostEqual(reds[10], 128, delta=4)

    def test_polygon_area(self):
        img = Image(60, 60)
        img.fill_polygon([(5.5, 50.2), (50.1, 50.2), (27.3, 6.4)], "#ffffff")
        want = 0.5 * 44.6 * 43.8
        self.assertAlmostEqual(alpha_sum(img), want, delta=want * 0.01)
        sq = Image(10, 10)
        sq.fill_polygon([(2, 2), (7, 2), (7, 6), (2, 6)], "#ffffff")
        self.assertEqual(alpha_sum(sq), 20)
        self.assertEqual(sq.get(2, 2)[3], 255)
        self.assertEqual(sq.get(7, 2)[3], 0)
        # non-zero winding: a doubled square is not filled twice
        dbl = Image(10, 10)
        dbl.fill_polygon([(2, 2), (7, 2), (7, 6), (2, 6), (2, 2), (7, 2), (7, 6), (2, 6)], (255, 0, 0, 100))
        self.assertEqual(dbl.get(4, 4)[3], 100)

    def test_line_area_and_union(self):
        img = Image(80, 40)
        img.line(10, 20, 70, 20, 6, "#ffffff")
        want = 60 * 6 + math.pi * 9
        self.assertAlmostEqual(alpha_sum(img), want, delta=want * 0.02)
        img2 = Image(80, 40)
        img2.polyline([(10, 20), (70, 20), (12, 21)], 6, (255, 255, 255, 100))
        self.assertLessEqual(max(img2.buf[3::4]), 100, "overlapping segments must not double-blend")

    def test_soft_shapes_fall_off(self):
        img = Image(80, 80)
        img.soft_circle(40, 40, 20, (0, 0, 0, 200), 12)
        row = [img.get(x, 40)[3] for x in range(40, 80)]
        self.assertEqual(row[0], 200)
        self.assertEqual(row, sorted(row, reverse=True))
        self.assertEqual(row[-1], 0)
        # pixels 59 and 60 straddle the outline (x = 60): their mean is half the alpha
        self.assertAlmostEqual((img.get(59, 40)[3] + img.get(60, 40)[3]) / 2.0, 100, delta=3)
        rr = Image(80, 80)
        rr.soft_rrect(20, 20, 40, 40, 8, (0, 0, 0, 255), 10)
        self.assertEqual(rr.get(40, 40)[3], 255)
        self.assertEqual(rr.get(2, 2)[3], 0)

    def test_radial_circle(self):
        img = Image(41, 41)
        img.radial_circle(20.5, 20.5, 18, "#ffffff", "#000000")
        self.assertGreater(img.get(20, 20)[0], 240)
        self.assertLess(img.get(37, 20)[0], 40)
        self.assertEqual(img.get(20, 20)[3], 255)
        self.assertEqual(img.get(0, 0)[3], 0)

    def test_clipping_and_degenerate_shapes(self):
        img = Image(8, 8)
        img.fill_circle(-50, -50, 10, "#fff")
        img.fill_rrect(100, 100, 5, 5, 2, "#fff")
        img.fill_rrect(1, 1, 0, 5, 2, "#fff")
        img.fill_polygon([(0, 0), (1, 1)], "#fff")
        img.line(3, 3, 3, 3, 0, "#fff")
        img.stroke_circle(4, 4, 3, "#fff", 0)
        img.soft_circle(4, 4, 0.2, "#fff", 30)
        img.radial_circle(4, 4, 0, "#fff", "#000")
        self.assertEqual(alpha_sum(img) < 64, True)
        big = Image(8, 8)
        big.fill_circle(4, 4, 100, "#ffffff")
        self.assertEqual(alpha_sum(big), 64)
        self.assertEqual(Image(0, -3).w, 1)


# ------------------------------------------------------------------ PNG
class PngTests(unittest.TestCase):
    def test_round_trip(self):
        img = Image(17, 9)
        img.fill_circle(8, 4, 4, (200, 30, 90, 180))
        img.set(0, 0, (1, 2, 3, 4))
        img.set(16, 8, (250, 251, 252, 253))
        w, h, px = decode_png(img.png_bytes())
        self.assertEqual((w, h), (17, 9))
        self.assertEqual(bytes(px), bytes(img.buf))

    def test_save_png(self):
        import tempfile
        img = Image(5, 3, fill="#336699cc")
        fd, path = tempfile.mkstemp(suffix=".png")
        os.close(fd)
        try:
            img.save_png(path)
            with open(path, "rb") as f:
                w, h, px = decode_png(f.read())
            self.assertEqual((w, h), (5, 3))
            self.assertEqual(tuple(px[:4]), (0x33, 0x66, 0x99, 0xcc))
        finally:
            os.remove(path)

    def test_encode_rejects_wrong_size(self):
        with self.assertRaises(ValueError):
            gfx.encode_png(2, 2, b"\x00" * 15)


# ------------------------------------------------------------------ performance
class PerfTests(unittest.TestCase):
    """Generous limits (spec budget: 15 ms sprite, 400 ms board); numbers go to stderr."""

    @staticmethod
    def best(fn, n=3) -> float:
        out = 1e9
        for _ in range(n):
            t = time.perf_counter()
            fn()
            out = min(out, time.perf_counter() - t)
        return out * 1000.0

    def test_benchmark(self):
        from goip.widgets import paint_die, paint_token

        def sprite():
            img = Image(64, 64)
            img.soft_circle(32, 36, 26, (0, 0, 0, 90), 8)
            img.fill_circle(32, 32, 26, "#f43f5e", grad_to="#9f1239")
            img.stroke_circle(32, 32, 26, (255, 255, 255, 120), 2)
            img.radial_circle(32, 30, 14, "#fda4af", "#f43f5e")
            img.line(16, 48, 48, 48, 3, "#ffffff")

        def board():
            img = Image(360, 360)
            img.soft_rrect(4, 8, 352, 348, 18, (0, 0, 0, 120), 16)
            img.fill_rrect(0, 0, 360, 352, 18, "#f7f4ff", grad_to="#e6e0fb")
            for i in range(10):
                for j in range(10):
                    if (i + j) % 2:
                        img.fill_rrect(4 + i * 35.2, 4 + j * 35.2, 34, 34, 6, "#e6e0fb")
            img.fill_polygon([(40, 300), (300, 40), (320, 60), (60, 320)], (60, 160, 90, 200))
            img.polyline([(30, 30), (180, 200), (330, 40)], 8, (200, 60, 60, 220))

        res = {
            "sprite64": self.best(sprite),
            "pawn64": self.best(lambda: paint_token(Image(64, 64), "#f43f5e", "#fda4af", "#9f1239", "pawn")),
            "disc64": self.best(lambda: paint_token(Image(64, 64), "#f43f5e", "#fda4af", "#9f1239", "disc")),
            "die64": self.best(lambda: paint_die(Image(64, 64), 5, "#ffffff", "#262636")),
            "board360": self.best(board, 2),
            "png360": self.best(lambda: Image(360, 360, fill="#123456").png_bytes(1)),
        }
        sys.stderr.write("\n  gfx benchmark (ms): %s\n" % ", ".join("%s=%.1f" % kv for kv in res.items()))
        for k in ("sprite64", "pawn64", "disc64", "die64"):
            self.assertLess(res[k], 60, k)
        self.assertLess(res["board360"], 1500)
        self.assertLess(res["png360"], 300)


# ------------------------------------------------------------------ source rules
class SourceRuleTests(unittest.TestCase):
    def test_py38_syntax_and_future_import(self):
        for rel in ("goip/gfx.py", "goip/widgets.py", "tools/snapshot.py", "tools/gallery.py"):
            with open(os.path.join(ROOT, rel), encoding="utf-8") as f:
                src = f.read()
            ast.parse(src, rel, feature_version=(3, 8))
            self.assertIn("from __future__ import annotations", src, rel)


# ------------------------------------------------------------------ Tk: photos and cache
class TkTestCase(unittest.TestCase):
    """Shared root per class; any exception raised inside a Tk callback fails the test."""
    root = None
    callback_errors: list = []

    @classmethod
    def setUpClass(cls):
        cls.root = make_root()
        cls.callback_errors = []

        def report(exc, val, tb):
            import traceback
            cls.callback_errors.append("".join(traceback.format_exception(exc, val, tb)))

        cls.root.report_callback_exception = report  # type: ignore[assignment]

    def tearDown(self):
        errs = list(self.callback_errors)
        del self.callback_errors[:]
        self.assertEqual(errs, [], "exceptions in Tk callbacks:\n" + "\n".join(errs))

    @classmethod
    def tearDownClass(cls):
        if cls.root is not None:
            try:
                cls.root.destroy()
            except Exception:
                pass
            cls.root = None


class PhotoTests(TkTestCase):
    def test_to_photo_keeps_colour_and_alpha(self):
        img = Image(6, 4)
        img.fill_rect(0, 0, 3, 4, "#ff8000")
        img.fill_rect(3, 0, 3, 4, (0, 0, 255, 128))
        ph = img.to_photo(self.root)
        self.assertEqual((ph.width(), ph.height()), (6, 4))
        self.assertEqual(tuple(ph.get(1, 1)), (255, 128, 0))
        self.assertFalse(ph.transparency_get(1, 1))
        self.assertFalse(ph.transparency_get(4, 1))      # half-transparent is not "transparent"
        blank = Image(2, 2).to_photo(self.root)
        self.assertTrue(blank.transparency_get(0, 0))

    @unittest.skipUnless(sys.platform == "win32", "PrintWindow check is Windows only")
    def test_alpha_displays_on_screen(self):
        """Composite a 50% red / 25% blue sprite on a black canvas and read the screen back."""
        sys.path.insert(0, ROOT)
        from tools.snapshot import grab
        top = tk.Toplevel(self.root)
        top.geometry("120x80+60+60")
        c = tk.Canvas(top, width=120, height=80, bg="#000000", highlightthickness=0, bd=0)
        c.pack()
        img = Image(40, 40)
        img.fill_rect(0, 0, 20, 40, (255, 0, 0, 128))
        img.fill_rect(20, 0, 20, 40, (0, 0, 255, 64))
        ph = img.to_photo(top)
        c.create_image(10, 10, image=ph, anchor="nw")
        try:
            pump(self.root, 0.3)
            w, h, rgba = grab(top)

            def px(x, y):
                j = (y * w + x) * 4
                return tuple(rgba[j:j + 3])

            r, g, b = px(20, 30)
            self.assertAlmostEqual(r, 128, delta=3)
            self.assertEqual((g, b), (0, 0))
            r, g, b = px(40, 30)
            self.assertAlmostEqual(b, 64, delta=3)
            self.assertEqual(px(100, 70), (0, 0, 0))
        finally:
            top.destroy()


class SpriteCacheTests(TkTestCase):
    def test_hits_misses_and_keys(self):
        cache = gfx.SpriteCache(self.root, max_items=10)
        calls = []

        def paint(img):
            calls.append((img.w, img.h))
            img.fill_circle(img.w / 2, img.h / 2, 3, "#fff")

        a = cache.get(("dot", "neon"), 8, 8, paint)
        b = cache.get(("dot", "neon"), 8, 8, paint)
        self.assertIs(a, b)
        self.assertEqual(calls, [(8, 8)])
        cache.get(("dot", "neon"), 9, 8, paint)           # size is part of the key
        cache.get(("dot", "paper"), 8, 8, paint)
        self.assertEqual(len(calls), 3)
        self.assertEqual((cache.hits, cache.misses), (1, 3))
        self.assertIn((("dot", "neon"), 9, 8), cache)
        cache.clear()
        self.assertEqual(len(cache), 0)

    def test_lru_eviction(self):
        cache = gfx.SpriteCache(self.root, max_items=3)
        noop = lambda img: None  # noqa: E731
        for k in "abc":
            cache.get(k, 4, 4, noop)
        cache.get("a", 4, 4, noop)            # refresh "a"
        cache.get("d", 4, 4, noop)            # evicts "b", the least recently used
        self.assertEqual(len(cache), 3)
        self.assertNotIn(("b", 4, 4), cache)
        self.assertIn(("a", 4, 4), cache)
        self.assertIn(("d", 4, 4), cache)

    def test_clear_keeps_shown_images_alive(self):
        import gc
        cache = gfx.SpriteCache(self.root)
        c = tk.Canvas(self.root)
        item = c.create_image(0, 0, anchor="nw",
                              image=cache.get("shown", 4, 4, lambda img: img.fill_rect(0, 0, 4, 4, "#fff")))
        shown = c.itemcget(item, "image")
        hidden = cache.get("hidden", 4, 4, lambda img: None).name
        cache.clear()
        gc.collect()
        names = self.root.tk.splitlist(self.root.tk.call("image", "names"))
        self.assertIn(shown, names)
        self.assertNotIn(hidden, names)
        c.destroy()

    def test_painter_error_gives_blank(self):
        cache = gfx.SpriteCache(self.root)

        def boom(img):
            raise RuntimeError("bad painter")

        import logging
        logging.disable(logging.CRITICAL)
        try:
            ph = cache.get("boom", 3, 2, boom)
        finally:
            logging.disable(logging.NOTSET)
        self.assertEqual((ph.width(), ph.height()), (3, 2))
        self.assertTrue(ph.transparency_get(1, 1))


# ------------------------------------------------------------------ Tk: animator
class AnimatorTests(TkTestCase):
    def setUp(self):
        from goip.widgets import Animator
        self.anim = Animator(self.root)

    def tearDown(self):
        self.anim.stop()
        super().tearDown()

    def test_runs_to_completion(self):
        seen, done = [], []
        self.anim.tween(0.12, seen.append, "out_cubic", done=lambda: done.append(1))
        self.assertTrue(self.anim.busy())
        pump(self.root, 1.5, until=lambda: bool(done))
        self.assertEqual(done, [1])
        self.assertEqual(seen[-1], 1.0)
        self.assertEqual(seen.count(1.0), 1)
        self.assertTrue(all(0.0 <= t <= 1.0 for t in seen))
        self.assertEqual(seen, sorted(seen))
        self.assertGreater(len(seen), 2)
        self.assertFalse(self.anim.busy())
        self.assertIsNone(self.anim._job, "loop must stop when no tweens are left")

    def test_all_eases(self):
        from goip.widgets import EASES
        for name in ("linear", "in_quad", "out_quad", "out_cubic", "in_out_cubic", "out_back",
                     "out_bounce", "out_elastic"):
            f = EASES[name]
            self.assertAlmostEqual(f(0.0), 0.0, places=6, msg=name)
            self.assertAlmostEqual(f(1.0), 1.0, places=6, msg=name)

    def test_failing_step_is_dropped(self):
        import logging
        good, bad_calls = [], []

        def bad(t):
            bad_calls.append(t)
            raise ValueError("boom")

        logging.disable(logging.CRITICAL)
        try:
            self.anim.tween(0.1, bad)
            self.anim.tween(0.1, good.append)
            pump(self.root, 1.0, until=lambda: not self.anim.busy())
        finally:
            logging.disable(logging.NOTSET)
        self.assertEqual(len(bad_calls), 1)
        self.assertEqual(good[-1], 1.0)
        self.assertFalse(self.anim.busy())

    def test_cancel_owner_and_busy(self):
        owner, other = object(), object()
        hits = []
        h = self.anim.tween(5, hits.append, owner=owner)
        self.anim.tween(5, hits.append, owner=other)
        self.assertTrue(self.anim.busy(owner))
        self.anim.cancel_owner(owner)
        self.assertFalse(self.anim.busy(owner))
        self.assertTrue(self.anim.busy(other))
        self.anim.cancel(h)                        # already gone: harmless
        self.anim.cancel_owner(other)
        self.assertFalse(self.anim.busy())

    def test_pause_resume(self):
        seen = []
        self.anim.tween(0.15, seen.append)
        self.anim.pause()
        pump(self.root, 0.3)
        self.assertEqual(seen, [])
        self.assertTrue(self.anim.busy())
        self.anim.resume()
        pump(self.root, 1.5, until=lambda: not self.anim.busy())
        self.assertEqual(seen[-1], 1.0)

    def test_zero_duration_and_delay(self):
        seen = []
        self.anim.tween(0, seen.append)
        self.anim.tween(0.05, lambda t: seen.append(("late", t)), delay=0.1)
        pump(self.root, 1.0, until=lambda: not self.anim.busy())
        self.assertEqual(seen[0], 1.0)
        self.assertEqual(seen[-1], ("late", 1.0))


# ------------------------------------------------------------------ Tk: widgets
class WidgetTests(TkTestCase):
    def setUp(self):
        from goip import widgets as W
        from goip.theme import NEON
        self.W = W
        self.canvas = tk.Canvas(self.root, width=420, height=600, highlightthickness=0, bd=0)
        self.canvas.pack()
        self.ui = W.UI(self.root, NEON)
        self.root.update()

    def tearDown(self):
        self.ui.anim.stop()
        self.canvas.destroy()
        super().tearDown()

    def hover(self, x, y):
        self.canvas.event_generate("<Motion>", x=x, y=y)
        self.root.update()

    def test_hover_cursor_and_states(self):
        b = self.W.Button(self.ui, self.canvas, "Go", lambda: None).place(20, 20, 120, 44)
        dd = self.W.Dropdown(self.ui, self.canvas, [(1, "One")], 1).place(20, 100, 120, 40)
        seg = self.W.Segmented(self.ui, self.canvas, [(1, "A"), (2, "B")], 1).place(20, 160, 120, 36)
        self.root.update()
        self.hover(60, 40)
        self.assertEqual(self.canvas.cget("cursor"), "hand2")
        self.assertEqual(b._state, "hover")
        self.hover(300, 400)
        self.assertEqual(self.canvas.cget("cursor"), "")
        self.assertEqual(b._state, "normal")
        self.hover(60, 120)
        self.assertEqual(self.canvas.cget("cursor"), "hand2")
        self.hover(60, 178)
        self.hover(300, 400)
        self.assertEqual(self.canvas.cget("cursor"), "")
        for w in (b, dd, seg):
            w.set_visible(False)
            w.set_visible(True)
            w.destroy()

    def all_widgets(self):
        W, ui, c = self.W, self.ui, self.canvas
        return [
            W.Button(ui, c, "Host a game", lambda: None),
            W.Button(ui, c, "Join", None, kind="secondary", icon="+"),
            W.Button(ui, c, "", None, kind="ghost",
                     icon=lambda img, col: img.fill_circle(img.w / 2, img.h / 2, img.w / 3, col)),
            W.Button(ui, c, "Leave", None, kind="danger"),
            W.Panel(ui, c), W.Panel(ui, c, kind="inset"), W.Panel(ui, c, radius=4, kind="glass"),
            W.TextField(ui, c, "Asha", "Your name"),
            W.Segmented(ui, c, [(1, "One"), (2, "Two"), (3, "Three")], 2),
            W.Segmented(ui, c, [], None),
            W.Dropdown(ui, c, [("a", "192.168.1.5"), ("b", "10.0.0.2")], "a"),
            W.PlayerChip(ui, c),
        ]

    def test_ui_context(self):
        ui = self.W.UI(self.root, scale=1.5)
        f = ui.font(14, bold=True)
        self.assertEqual(f[1], -21)
        self.assertEqual(f[2], "bold")
        self.assertIs(ui.font(14, bold=True), f)
        self.assertEqual(ui.px(10), 15)
        self.assertEqual(ui.px(0.1), 1)
        ui.sprites.get("x", 2, 2, lambda img: None)
        from goip.theme import PAPER
        ui.set_theme(PAPER)
        self.assertIs(ui.theme, PAPER)
        self.assertEqual(len(ui.sprites), 0)
        text, font = ui.fit("A very long player name indeed", 14, 60)
        self.assertLessEqual(ui.measure(text, font), 60)
        self.assertTrue(text.endswith("…"))

    def test_every_theme_place_tiny_and_destroy(self):
        from goip.theme import THEMES
        for th in THEMES.values():
            self.ui.set_theme(th)
            ws = self.all_widgets()
            for size in [(0, 0), (1, 1), (3, 2), (12, 7), (40, 18), (380, 56)]:
                for w in ws:
                    w.place(4, 4, *size)
                    if isinstance(w, self.W.PlayerChip):
                        w.update("Mia", "green", bot=True, active=True, subtitle="Rolling", timer=9, me=True)
                self.root.update()
            for s in (1, 6, 64):
                d = self.W.Dice(self.ui, self.canvas, s)
                d.place(40, 40)
                d.set_color("blue")
                d.set_active(True)
                d.roll(3)
                self.root.update()
                ws.append(d)
            for w in ws:
                if hasattr(w, "set_visible"):
                    w.set_visible(False)
                    w.set_visible(True)
                w.destroy()
                w.destroy()                          # idempotent
            self.root.update()
            self.assertEqual(self.canvas.find_all(), (), th.key)
            self.assertEqual(self.root.tk.call("after", "info"), "", th.key)
            self.assertFalse(self.ui.anim.busy(), th.key)

    def test_background(self):
        W, c = self.W, self.canvas
        c.create_rectangle(0, 0, 10, 10, fill="red", tags=("thing",))
        W.draw_background(c, self.ui, 420, 600)
        W.draw_background(c, self.ui, 420, 600)
        bg = c.find_withtag("bg")
        self.assertEqual(len(bg), 1)
        self.assertEqual(c.find_all()[0], bg[0])      # lowest
        self.assertEqual(len(self.ui._bg), 1)         # cached per (theme, size)
        t = time.perf_counter()
        for i in range(5):
            W.draw_background(c, self.ui, 300 + i * 7, 500 + i * 3)
        per = (time.perf_counter() - t) / 5 * 1000
        sys.stderr.write("\n  draw_background new size: %.1f ms\n" % per)
        self.assertLess(per, 250)
        W.draw_background(c, self.ui, 1, 1)

    def test_button_click_fires_on_release_inside(self):
        clicks = []
        b = self.W.Button(self.ui, self.canvas, "Go", lambda: clicks.append(1)).place(20, 20, 120, 44)
        self.root.update()
        c = self.canvas

        def press_release(x0, y0, x1, y1):
            c.event_generate("<Motion>", x=x0, y=y0)
            c.event_generate("<ButtonPress-1>", x=x0, y=y0)
            c.event_generate("<Motion>", x=x1, y=y1)
            c.event_generate("<ButtonRelease-1>", x=x1, y=y1)
            self.root.update()

        press_release(60, 40, 70, 42)
        self.assertEqual(clicks, [1])
        press_release(60, 40, 300, 300)               # dragged off: no click
        self.assertEqual(clicks, [1])
        b.set_enabled(False)
        press_release(60, 40, 60, 40)
        self.assertEqual(clicks, [1])
        b.set_enabled(True)
        b.set_visible(False)
        press_release(60, 40, 60, 40)
        self.assertEqual(clicks, [1])
        b.set_visible(True)
        b.set_text("Again")
        press_release(60, 40, 60, 40)
        self.assertEqual(clicks, [1, 1])
        b.destroy()

    def test_text_field(self):
        W = self.W
        entered = []
        tf = W.TextField(self.ui, self.canvas, "", "Your name", on_enter=entered.append, max_len=5)
        tf.place(10, 10, 200, 44)
        self.root.update()
        self.assertEqual(tf.value, "")                # placeholder is not a value
        self.assertEqual(tf.entry.get(), "Your name")
        tf.value = "Ravindra"
        self.assertEqual(tf.value, "Ravin")
        tf.set_error("Too short")
        self.assertTrue(any(self.canvas.type(i) == "text" for i in self.canvas.find_withtag(tf.tag)))
        tf.entry.insert("end", "x")                   # typing clears the error...
        self.assertIsNone(tf.error)
        self.assertEqual(tf.value, "Ravin")           # ...and max_len holds
        tf._enter()
        self.assertEqual(entered, ["Ravin"])
        tf.destroy()
        self.assertEqual(self.canvas.find_all(), ())

    def test_segmented_and_dropdown(self):
        W = self.W
        changes = []
        seg = W.Segmented(self.ui, self.canvas, [("a", "A"), ("b", "B"), ("c", "C")], "a", changes.append)
        seg.place(10, 10, 300, 40)
        self.root.update()
        self.canvas.event_generate("<Motion>", x=260, y=30)
        self.canvas.event_generate("<ButtonPress-1>", x=260, y=30)
        self.canvas.event_generate("<ButtonRelease-1>", x=260, y=30)
        pump(self.root, 0.5, until=lambda: not self.ui.anim.busy())
        self.assertEqual(seg.value, "c")
        self.assertEqual(changes, ["c"])
        seg.value = "b"                               # programmatic: no callback
        self.assertEqual(changes, ["c"])
        seg.set_choices([(1, "One")], 1)
        self.assertEqual(seg.value, 1)
        dd = W.Dropdown(self.ui, self.canvas, [(1, "One"), (2, "Two")], 1, changes.append).place(10, 80, 200, 40)
        dd._pick(2)
        self.assertEqual((dd.value, changes[-1]), (2, 2))
        dd.set_choices([(3, "Three")], 3)
        self.assertEqual(dd.label(), "Three")
        seg.destroy()
        dd.destroy()
        self.assertEqual(self.canvas.find_all(), ())

    def test_dice_roll_lands(self):
        d = self.W.Dice(self.ui, self.canvas, 56).place(100, 100)
        done = []
        d.roll(4, done=lambda: done.append(d.value))
        pump(self.root, 2.0, until=lambda: bool(done))
        self.assertEqual(done, [4])
        clicks = []
        d.bind_click(lambda: clicks.append(1))
        d._on_click(None)
        self.assertEqual(clicks, [1])
        d.show(6)
        self.assertEqual(d.value, 6)
        d.destroy()

    def test_player_chip_countdown(self):
        chip = self.W.PlayerChip(self.ui, self.canvas).place(10, 10, 300, 56)
        chip.update("Asha", "red", active=True, timer=3)
        self.root.update()
        arcs = [i for i in self.canvas.find_withtag(chip.tag) if self.canvas.type(i) == "arc"]
        self.assertEqual(len(arcs), 1)
        e0 = float(self.canvas.itemcget(arcs[0], "extent"))
        pump(self.root, 0.5)
        e1 = float(self.canvas.itemcget(arcs[0], "extent"))
        self.assertGreater(e1, e0, "the ring counts down locally")   # extents are negative
        chip.update("Asha", "red", active=False)
        self.root.update()
        self.assertFalse([i for i in self.canvas.find_withtag(chip.tag) if self.canvas.type(i) == "arc"])
        chip.update("Asha", "red", active=True, timer=20)
        chip.destroy()
        self.root.update()
        self.assertEqual(self.root.tk.call("after", "info"), "")

    def test_toaster_and_confetti_clean_up(self):
        W = self.W
        t = W.Toaster(self.ui, self.canvas)
        t.show("Ravi joined", "good", seconds=0.1)
        t.show("Not your turn yet", "bad", seconds=0.1)
        self.assertTrue(self.canvas.find_withtag(t.tag))
        handle = W.confetti(self.ui, self.canvas, 200, 300, count=30)
        self.assertIsNotNone(handle)
        pump(self.root, 0.3)
        self.assertGreater(len(self.canvas.find_all()), 30)
        pump(self.root, 2.5, until=lambda: not self.canvas.find_all())
        self.assertEqual(self.canvas.find_all(), ())
        t.destroy()
        from goip.theme import DIM
        self.ui.set_theme(DIM)
        self.assertIsNone(W.confetti(self.ui, self.canvas, 10, 10))

    def test_painters_draw_something(self):
        from goip.theme import THEMES
        for th in THEMES.values():
            for style in ("pawn", "disc"):
                for color in ("red", "green", "yellow", "blue"):
                    img = Image(48, 48)
                    self.W.paint_token(img, *th.seat(color), style=style)
                    self.assertGreater(alpha_sum(img), 400, (th.key, style, color))
                    self.assertEqual(img.get(0, 0)[3], 0)
            for v in range(1, 7):
                img = Image(40, 40)
                self.W.paint_die(img, v, th.board_a, th.text)
                self.assertGreater(alpha_sum(img), 600)
        tiny = Image(2, 2)
        self.W.paint_token(tiny, "#f00", "#faa", "#800")
        self.W.paint_die(tiny, 9, "#fff", "#000")


if __name__ == "__main__":
    unittest.main()
