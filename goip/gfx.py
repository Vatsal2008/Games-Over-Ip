"""Pure-Python RGBA rasteriser -> PNG -> tk.PhotoImage, plus an LRU sprite cache.

Images are straight-alpha RGBA in a bytearray. Every primitive composites with the
"over" operator and gets anti-aliased edges from signed-distance coverage. Rows are
split into spans: interior runs are written with bytearray slice assignment and only
edge pixels get per-pixel maths, which keeps sprites fast enough in plain Python.

Strokes (stroke_rrect, stroke_circle) are drawn *inside* the outline: the band from
the shape edge inward by ``width``. Soft shapes fade with a smoothstep from
``blur/2`` inside the outline to ``blur/2`` outside it.
"""
from __future__ import annotations

import base64
import logging
import math
import struct
import sys
import zlib
from collections import OrderedDict
from typing import Any, Callable, Dict, Hashable, List, Optional, Sequence, Tuple, Union

log = logging.getLogger(__name__)

RGBA = Tuple[int, int, int, int]
ColorLike = Union[str, Sequence[float]]

_ORDER = sys.byteorder
_sqrt = math.sqrt
_ceil = math.ceil
_NAMED = {"white": (255, 255, 255, 255), "black": (0, 0, 0, 255),
          "transparent": (0, 0, 0, 0), "none": (0, 0, 0, 0)}
_pcache: Dict[Any, RGBA] = {}


# ------------------------------------------------------------------ colours
def parse(color: ColorLike) -> RGBA:
    """'#rgb' '#rgba' '#rrggbb' '#rrggbbaa' (r,g,b) (r,g,b,a) -> (r, g, b, a) ints."""
    if isinstance(color, str):
        hit = _pcache.get(color)
        if hit is not None:
            return hit
        s = color.strip().lower()
        if s in _NAMED:
            out = _NAMED[s]
        else:
            if s.startswith("#"):
                s = s[1:]
            try:
                if len(s) in (3, 4):
                    vals = [int(ch * 2, 16) for ch in s]
                elif len(s) in (6, 8):
                    vals = [int(s[i:i + 2], 16) for i in range(0, len(s), 2)]
                else:
                    raise ValueError(s)
            except ValueError:
                raise ValueError("bad colour %r" % (color,)) from None
            if len(vals) == 3:
                vals.append(255)
            out = (vals[0], vals[1], vals[2], vals[3])
        if len(_pcache) < 4096:
            _pcache[color] = out
        return out
    if isinstance(color, (tuple, list)) and len(color) in (3, 4):
        v = [max(0, min(255, int(round(c)))) for c in color]
        if len(v) == 3:
            v.append(255)
        return (v[0], v[1], v[2], v[3])
    raise ValueError("bad colour %r" % (color,))


def hexs(color: ColorLike) -> str:
    """Any colour -> '#rrggbb' (alpha dropped)."""
    r, g, b, _a = parse(color)
    return "#%02x%02x%02x" % (r, g, b)


def mix(c1: ColorLike, c2: ColorLike, t: float) -> str:
    """Linear blend of two colours, t=0 -> c1, t=1 -> c2. Returns '#rrggbb'."""
    a = parse(c1)
    b = parse(c2)
    t = 0.0 if t < 0 else 1.0 if t > 1 else float(t)
    return "#%02x%02x%02x" % tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))


def mix_rgba(c1: ColorLike, c2: ColorLike, t: float) -> RGBA:
    """Like mix() but keeps (and blends) alpha."""
    a = parse(c1)
    b = parse(c2)
    t = 0.0 if t < 0 else 1.0 if t > 1 else float(t)
    v = [int(round(a[i] + (b[i] - a[i]) * t)) for i in range(4)]
    return (v[0], v[1], v[2], v[3])


def lighten(c: ColorLike, t: float) -> str:
    return mix(c, "#ffffff", t)


def darken(c: ColorLike, t: float) -> str:
    return mix(c, "#000000", t)


def with_alpha(c: ColorLike, a: float) -> RGBA:
    r, g, b, _ = parse(c)
    return (r, g, b, max(0, min(255, int(round(a)))))


def luminance(c: ColorLike) -> float:
    """Relative luminance 0..1 (sRGB approximation), handy for picking text colours."""
    r, g, b, _ = parse(c)
    return (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255.0


def over(dst: Sequence[int], src: Sequence[int]) -> RGBA:
    """Straight-alpha 'src over dst' for one pixel (integer maths, rounded)."""
    return _over(dst[0], dst[1], dst[2], dst[3], src[0], src[1], src[2], src[3])


def _over(dr: int, dg: int, db: int, da: int, r: int, g: int, b: int, a: int) -> RGBA:
    if a >= 255 or da == 0:
        return (r, g, b, a)
    if a <= 0:
        return (dr, dg, db, da)
    ws = a * 255
    wd = da * (255 - a)
    den = ws + wd
    h = den >> 1
    return ((r * ws + dr * wd + h) // den, (g * ws + dg * wd + h) // den,
            (b * ws + db * wd + h) // den, (den + 127) // 255)


def _blend(buf: bytearray, j: int, r: int, g: int, b: int, a: int) -> None:
    da = buf[j + 3]
    if a >= 255 or da == 0:
        buf[j] = r
        buf[j + 1] = g
        buf[j + 2] = b
        buf[j + 3] = a
        return
    ws = a * 255
    wd = da * (255 - a)
    den = ws + wd
    h = den >> 1
    buf[j] = (r * ws + buf[j] * wd + h) // den
    buf[j + 1] = (g * ws + buf[j + 1] * wd + h) // den
    buf[j + 2] = (b * ws + buf[j + 2] * wd + h) // den
    buf[j + 3] = (den + 127) // 255


def _pack(r: int, g: int, b: int, a: int) -> int:
    return int.from_bytes(bytes((r, g, b, a)), _ORDER)


def _cut(v: float) -> int:
    """First pixel index whose centre is >= v."""
    return int(_ceil(v - 0.5))


def _hext(hw: float, hh: float, r: float, dy: float) -> float:
    """Half-width of a rounded rect (half sizes hw, hh, corner r) at |dy|; -1 if outside."""
    if hw < 0 or hh < 0 or dy > hh:
        return -1.0
    t = dy - (hh - r)
    if t <= 0:
        return hw
    q = r * r - t * t
    return hw - r + (_sqrt(q) if q > 0 else 0.0)


def bezier(p0, p1, p2, p3, n: int = 12) -> List[Tuple[float, float]]:
    """Points along a cubic Bezier curve (n+1 points, both ends included)."""
    out = []
    for i in range(n + 1):
        t = i / float(n)
        u = 1 - t
        a, b, c, d = u * u * u, 3 * u * u * t, 3 * u * t * t, t * t * t
        out.append((a * p0[0] + b * p1[0] + c * p2[0] + d * p3[0],
                    a * p0[1] + b * p1[1] + c * p2[1] + d * p3[1]))
    return out


def rrect_points(x: float, y: float, w: float, h: float, r: float, angle: float = 0.0,
                 cx: Optional[float] = None, cy: Optional[float] = None, seg: int = 6):
    """Outline of a rounded rect as polygon points, optionally rotated (degrees) about (cx, cy)."""
    r = max(0.0, min(r, w / 2.0, h / 2.0))
    pts = []
    corners = ((x + w - r, y + r, -90), (x + w - r, y + h - r, 0), (x + r, y + h - r, 90), (x + r, y + r, 180))
    for ccx, ccy, start in corners:
        for i in range(seg + 1):
            a = math.radians(start + 90.0 * i / seg)
            pts.append((ccx + r * math.cos(a), ccy + r * math.sin(a)))
    if angle:
        cx = x + w / 2.0 if cx is None else cx
        cy = y + h / 2.0 if cy is None else cy
        ca, sa = math.cos(math.radians(angle)), math.sin(math.radians(angle))
        pts = [(cx + (px - cx) * ca - (py - cy) * sa, cy + (px - cx) * sa + (py - cy) * ca) for px, py in pts]
    return pts


# ------------------------------------------------------------------ PNG
def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def encode_png(w: int, h: int, rgba: Union[bytes, bytearray], level: int = 6) -> bytes:
    """8-bit RGBA PNG (filter 0 on every row)."""
    stride = w * 4
    if len(rgba) != stride * h:
        raise ValueError("pixel buffer has the wrong size")
    mv = memoryview(rgba)
    raw = b"".join(b"\x00" + mv[y * stride:(y + 1) * stride].tobytes() for y in range(h))
    ihdr = struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", zlib.compress(raw, level))
            + _chunk(b"IEND", b""))


# ------------------------------------------------------------------ Image
class Image:
    """Straight-alpha RGBA image with anti-aliased, 'over'-composited primitives."""

    __slots__ = ("w", "h", "buf", "_m32")

    def __init__(self, w: int, h: int, fill: Optional[ColorLike] = None) -> None:
        self.w = max(1, int(w))
        self.h = max(1, int(h))
        self.buf = bytearray(self.w * self.h * 4)
        self._m32 = memoryview(self.buf).cast("I")
        if fill is not None:
            self.buf[:] = bytes(parse(fill)) * (self.w * self.h)

    # ---------------------------------------------------------- pixel access
    def get(self, x: int, y: int) -> RGBA:
        j = (int(y) * self.w + int(x)) * 4
        b = self.buf
        return (b[j], b[j + 1], b[j + 2], b[j + 3])

    def set(self, x: int, y: int, color: ColorLike) -> None:
        if 0 <= x < self.w and 0 <= y < self.h:
            j = (int(y) * self.w + int(x)) * 4
            self.buf[j:j + 4] = bytes(parse(color))

    def clear(self, color: Optional[ColorLike] = None) -> None:
        self.buf[:] = bytes(parse(color) if color is not None else (0, 0, 0, 0)) * (self.w * self.h)

    def copy(self) -> "Image":
        out = Image(self.w, self.h)
        out.buf[:] = self.buf
        return out

    def scale_alpha(self, f: float) -> None:
        """Multiply every pixel's alpha by f (0..1). Used for fade frames."""
        f = max(0.0, min(1.0, float(f)))
        table = bytes(int(i * f + 0.5) for i in range(256))
        self.buf[3::4] = self.buf[3::4].translate(table)

    def blit(self, src: "Image", x: int, y: int) -> None:
        """Composite another image at integer offset (x, y)."""
        x, y = int(x), int(y)
        sx0, sy0 = max(0, -x), max(0, -y)
        sx1, sy1 = min(src.w, self.w - x), min(src.h, self.h - y)
        if sx0 >= sx1 or sy0 >= sy1:
            return
        sb, db = src.buf, self.buf
        for sy in range(sy0, sy1):
            so = (sy * src.w) * 4
            do = ((sy + y) * self.w + x) * 4
            for sx in range(sx0, sx1):
                i = so + sx * 4
                a = sb[i + 3]
                if a:
                    _blend(db, do + sx * 4, sb[i], sb[i + 1], sb[i + 2], a)

    # ---------------------------------------------------------- span helpers
    def _span(self, off: int, x0: int, x1: int, r: int, g: int, b: int, a: int) -> None:
        """Composite a constant colour over pixels x0..x1 (inclusive) of the row at `off`."""
        n = x1 - x0 + 1
        if n <= 0 or a <= 0:
            return
        buf = self.buf
        i0 = (off + x0) * 4
        i1 = i0 + 4 * n
        if a >= 255:
            buf[i0:i1] = bytes((r, g, b, 255)) * n
            return
        first = buf[i0:i0 + 4]
        if n == 1 or buf[i0:i1] == first * n:
            buf[i0:i1] = bytes(_over(first[0], first[1], first[2], first[3], r, g, b, a)) * n
            return
        m = self._m32
        memo: Dict[int, int] = {}
        for k in range(off + x0, off + x1 + 1):
            p = m[k]
            q = memo.get(p)
            if q is None:
                j = k * 4
                q = _pack(*_over(buf[j], buf[j + 1], buf[j + 2], buf[j + 3], r, g, b, a))
                memo[p] = q
            m[k] = q

    def _segments(self, cx: float, segs, yc: float, row_off: int, col, pix: Callable) -> None:
        """Paint symmetric |dx| segments: cov float -> uniform span, None -> pix(a, b, ...)."""
        W1 = self.w - 1
        r, g, b, a = col
        for b0, b1, cov in segs:
            if cov is not None and cov <= 0.002:
                continue
            if b0 <= 0:
                rngs = ((_cut(cx - b1), _cut(cx + b1) - 1),)
            else:
                rngs = ((_cut(cx - b1), _cut(cx - b0) - 1), (_cut(cx + b0), _cut(cx + b1) - 1))
            for lo, hi in rngs:
                if lo < 0:
                    lo = 0
                if hi > W1:
                    hi = W1
                if lo > hi:
                    continue
                if cov is None:
                    pix(lo, hi, yc, row_off, r, g, b, a)
                else:
                    self._span(row_off, lo, hi, r, g, b, a if cov >= 0.998 else int(a * cov + 0.5))

    # ---------------------------------------------------------- rounded rects
    def _rrect(self, x: float, y: float, w: float, h: float, r: float, color: ColorLike,
               grad_to: Optional[ColorLike] = None, s: float = 0.5, smooth: bool = False,
               ring: float = 0.0) -> None:
        if w <= 0 or h <= 0:
            return
        hw, hh = w / 2.0, h / 2.0
        cx, cy = x + hw, y + hh
        r = max(0.0, min(float(r), hw, hh))
        c0 = parse(color)
        c1 = parse(grad_to) if grad_to is not None else c0
        if c0[3] == 0 and c1[3] == 0:
            return
        inner = None
        if ring > 0:
            ihw, ihh = hw - ring, hh - ring
            if ihw > 0 and ihh > 0:
                inner = (ihw, ihh, max(0.0, r - ring))
        inv = 1.0 / (2.0 * s)
        ss = s
        ext = s + 1
        y0 = max(0, int(math.floor(cy - hh - ext)))
        y1 = min(self.h - 1, int(math.ceil(cy + hh + ext)))
        band = hw - max(r, s)
        sqrt = _sqrt
        buf = self.buf

        def cov_of(sd: float) -> float:
            t = (ss - sd) * inv
            if t <= 0:
                return 0.0
            if t >= 1:
                return 1.0
            return t * t * (3 - 2 * t) if smooth else t

        def info(hw_: float, hh_: float, r_: float, band_: float, dy: float):
            oe = _hext(hw_ + ss, hh_ + ss, r_ + ss, dy)
            if oe < 0:
                return None
            fe = _hext(hw_ - ss, hh_ - ss, r_ - ss if r_ > ss else 0.0, dy)
            if fe >= 0:
                return oe, max(fe, band_), 1.0
            return oe, band_, cov_of(dy - hh_)

        st = {}

        def pix(lo: int, hi: int, yc: float, off: int, cr: int, cg: int, cb: int, ca: int) -> None:
            qy, qyy, rr, qxo = st["o"]
            ino = st["i"]
            for k in range(lo, hi + 1):
                dx = k + 0.5 - cx
                if dx < 0:
                    dx = -dx
                qx = dx - qxo
                sd = (sqrt(qx * qx + qyy) if (qx > 0 and qy > 0) else (qx if qx > qy else qy)) - rr
                cv = cov_of(sd)
                if ino is not None and cv > 0:
                    iqy, iqyy, irr, iqxo = ino
                    qx = dx - iqxo
                    sd = (sqrt(qx * qx + iqyy) if (qx > 0 and iqy > 0) else (qx if qx > iqy else iqy)) - irr
                    cv -= cov_of(sd)
                if cv <= 0.002:
                    continue
                aa = int(ca * cv + 0.5) if cv < 0.998 else ca
                if aa > 0:
                    _blend(buf, (off + k) * 4, cr, cg, cb, aa)

        ih_band = (inner[0] - max(inner[2], s)) if inner else -1.0
        gy, gh = y, h
        for py in range(y0, y1 + 1):
            yc = py + 0.5
            dy = abs(yc - cy)
            o = info(hw, hh, r, band, dy)
            if o is None:
                continue
            o_out, o_known, o_kc = o
            qy = dy - (hh - r)
            st["o"] = (qy, qy * qy if qy > 0 else 0.0, r, hw - r)
            io = None
            if inner is not None:
                io = info(inner[0], inner[1], inner[2], ih_band, dy)
            if io is None:
                st["i"] = None
                segs = []
                if o_known > 0:
                    segs.append((0.0, o_known, o_kc))
                segs.append((max(o_known, 0.0), o_out, None))
            else:
                i_out, i_known, i_kc = io
                iqy = dy - (inner[1] - inner[2])
                st["i"] = (iqy, iqy * iqy if iqy > 0 else 0.0, inner[2], inner[0] - inner[2])
                i_full = i_known if i_kc >= 1.0 else -1.0
                segs = []
                p = max(i_full, 0.0)
                ub = min(o_known, i_known)
                if ub > p:
                    segs.append((p, ub, o_kc - i_kc))
                    p = ub
                if i_out > p:
                    segs.append((p, i_out, None))
                    p = i_out
                if o_known > p:
                    segs.append((p, o_known, o_kc))
                    p = o_known
                if o_out > p:
                    segs.append((p, o_out, None))
            if c1 is c0:
                col = c0
            else:
                t = (yc - gy) / gh
                t = 0.0 if t < 0 else 1.0 if t > 1 else t
                col = (int(c0[0] + (c1[0] - c0[0]) * t + 0.5), int(c0[1] + (c1[1] - c0[1]) * t + 0.5),
                       int(c0[2] + (c1[2] - c0[2]) * t + 0.5), int(c0[3] + (c1[3] - c0[3]) * t + 0.5))
            self._segments(cx, segs, yc, py * self.w, col, pix)

    def fill_rrect(self, x: float, y: float, w: float, h: float, r: float, color: ColorLike,
                   grad_to: Optional[ColorLike] = None) -> None:
        """Rounded rect; grad_to = bottom colour of a vertical gradient."""
        self._rrect(x, y, w, h, r, color, grad_to)

    def fill_rect(self, x: float, y: float, w: float, h: float, color: ColorLike,
                  grad_to: Optional[ColorLike] = None) -> None:
        self._rrect(x, y, w, h, 0.0, color, grad_to)

    def stroke_rrect(self, x: float, y: float, w: float, h: float, r: float, color: ColorLike,
                     width: float) -> None:
        """Outline drawn inside the rect: the band from the edge inward by `width`."""
        if width <= 0:
            return
        self._rrect(x, y, w, h, r, color, None, ring=float(width))

    def fill_circle(self, cx: float, cy: float, r: float, color: ColorLike,
                    grad_to: Optional[ColorLike] = None) -> None:
        self._rrect(cx - r, cy - r, 2 * r, 2 * r, r, color, grad_to)

    def stroke_circle(self, cx: float, cy: float, r: float, color: ColorLike, width: float) -> None:
        """Ring between radius r - width and r."""
        if width <= 0:
            return
        self._rrect(cx - r, cy - r, 2 * r, 2 * r, r, color, None, ring=float(width))

    def soft_rrect(self, x: float, y: float, w: float, h: float, r: float, color: ColorLike,
                   blur: float) -> None:
        """Blurred rounded rect (shadows, glows): smoothstep falloff across `blur` px."""
        if blur <= 1.0:
            self._rrect(x, y, w, h, r, color)
        else:
            self._rrect(x, y, w, h, r, color, None, s=blur / 2.0, smooth=True)

    def soft_circle(self, cx: float, cy: float, r: float, color: ColorLike, blur: float) -> None:
        self.soft_rrect(cx - r, cy - r, 2 * r, 2 * r, r, color, blur)

    # ---------------------------------------------------------- ellipses
    def _ellipse(self, cx: float, cy: float, rx: float, ry: float, color: ColorLike,
                 s: float, smooth: bool) -> None:
        if rx <= 0 or ry <= 0:
            return
        col = parse(color)
        if col[3] == 0:
            return
        m = min(rx, ry)
        k_in = 1.0 - s / m
        k_out = 1.0 + s / m
        inv = 1.0 / (2.0 * s)
        y0 = max(0, int(math.floor(cy - ry * k_out - 1)))
        y1 = min(self.h - 1, int(math.ceil(cy + ry * k_out + 1)))
        buf = self.buf
        sqrt = _sqrt
        st = [0.0]

        def pix(lo: int, hi: int, yc: float, off: int, cr: int, cg: int, cb: int, ca: int) -> None:
            dyn = st[0]
            gy = (dyn / ry) ** 2
            for k in range(lo, hi + 1):
                dxn = (k + 0.5 - cx) / rx
                q = sqrt(dxn * dxn + dyn * dyn)
                if q <= 1e-9:
                    cv = 1.0
                else:
                    g = sqrt((dxn / rx) ** 2 + gy) / q
                    t = (s - (q - 1.0) / g) * inv
                    if t <= 0:
                        continue
                    cv = 1.0 if t >= 1 else (t * t * (3 - 2 * t) if smooth else t)
                aa = int(ca * cv + 0.5)
                if aa > 0:
                    _blend(buf, (off + k) * 4, cr, cg, cb, aa)

        for py in range(y0, y1 + 1):
            yc = py + 0.5
            dyn = (yc - cy) / ry
            ad = abs(dyn)
            if ad >= k_out:
                continue
            st[0] = dyn
            oe = rx * _sqrt(k_out * k_out - dyn * dyn)
            ie = rx * _sqrt(k_in * k_in - dyn * dyn) if (k_in > 0 and ad < k_in) else -1.0
            segs = []
            if ie > 0:
                segs.append((0.0, ie, 1.0))
            segs.append((max(ie, 0.0), oe, None))
            self._segments(cx, segs, yc, py * self.w, col, pix)

    def fill_ellipse(self, cx: float, cy: float, rx: float, ry: float, color: ColorLike) -> None:
        self._ellipse(cx, cy, rx, ry, color, 0.5, False)

    def soft_ellipse(self, cx: float, cy: float, rx: float, ry: float, color: ColorLike, blur: float) -> None:
        if blur <= 1.0:
            self._ellipse(cx, cy, rx, ry, color, 0.5, False)
        else:
            self._ellipse(cx, cy, rx, ry, color, blur / 2.0, True)

    # ---------------------------------------------------------- radial gradient
    def radial_circle(self, cx: float, cy: float, r: float, inner: ColorLike, outer: ColorLike,
                      focus: Optional[Tuple[float, float]] = None) -> None:
        """Disc whose colour runs from `inner` (at the centre, or `focus`) to `outer` at the rim."""
        if r <= 0:
            return
        ci, co = parse(inner), parse(outer)
        fx, fy = focus if focus is not None else (cx, cy)
        reach = r + math.hypot(fx - cx, fy - cy)
        lut = []
        for i in range(256):
            t = i / 255.0
            lut.append(tuple(int(ci[c] + (co[c] - ci[c]) * t + 0.5) for c in range(4)))
        opaque = ci[3] == 255 and co[3] == 255
        packed = [_pack(*c) for c in lut] if opaque else None
        buf, m = self.buf, self._m32
        W = self.w
        sqrt = _sqrt
        scale = 255.0 / reach
        y0 = max(0, int(math.floor(cy - r - 1)))
        y1 = min(self.h - 1, int(math.ceil(cy + r + 1)))
        ro2 = (r + 0.5) ** 2
        for py in range(y0, y1 + 1):
            yc = py + 0.5
            dy = yc - cy
            if dy * dy >= ro2:
                continue
            oe = sqrt(ro2 - dy * dy)
            ie = sqrt((r - 0.5) ** 2 - dy * dy) if (r > 0.5 and abs(dy) < r - 0.5) else -1.0
            lo = max(0, _cut(cx - oe))
            hi = min(W - 1, _cut(cx + oe) - 1)
            fdy2 = (yc - fy) ** 2
            off = py * W
            for k in range(lo, hi + 1):
                px = k + 0.5
                dx = px - cx
                ti = int(sqrt((px - fx) ** 2 + fdy2) * scale)
                if ti > 255:
                    ti = 255
                if abs(dx) <= ie:
                    if opaque:
                        m[off + k] = packed[ti]
                        continue
                    cv = 1.0
                else:
                    cv = r + 0.5 - sqrt(dx * dx + dy * dy)
                    if cv <= 0:
                        continue
                    if cv > 1:
                        cv = 1.0
                c = lut[ti]
                aa = int(c[3] * cv + 0.5)
                if aa > 0:
                    _blend(buf, (off + k) * 4, c[0], c[1], c[2], aa)

    # ---------------------------------------------------------- lines
    def line(self, x1: float, y1: float, x2: float, y2: float, width: float, color: ColorLike) -> None:
        """Anti-aliased line with round caps."""
        self.polyline([(x1, y1), (x2, y2)], width, color)

    def polyline(self, points: Sequence[Tuple[float, float]], width: float, color: ColorLike) -> None:
        """Connected round-capped segments; overlaps are blended once (union coverage)."""
        pts = [(float(p[0]), float(p[1])) for p in points]
        if not pts or width <= 0:
            return
        if len(pts) == 1:
            pts = pts * 2
        col = parse(color)
        if col[3] == 0:
            return
        R = width / 2.0
        segs = []
        for i in range(len(pts) - 1):
            (ax, ay), (bx, by) = pts[i], pts[i + 1]
            dx, dy = bx - ax, by - ay
            L = math.hypot(dx, dy)
            ux, uy = (dx / L, dy / L) if L > 1e-9 else (1.0, 0.0)
            segs.append((ax, ay, bx, by, dx, dy, L, ux, uy, min(ay, by) - R - 1, max(ay, by) + R + 1))
        y0 = max(0, int(math.floor(min(s[9] for s in segs))))
        y1 = min(self.h - 1, int(math.ceil(max(s[10] for s in segs))))
        W1 = self.w - 1
        cr, cg, cb, ca = col
        buf = self.buf
        sqrt = _sqrt
        for py in range(y0, y1 + 1):
            yc = py + 0.5
            act = [s for s in segs if s[9] <= yc <= s[10]]
            if not act:
                continue
            outs, ins = [], []
            for sg in act:
                iv = _cap_iv(sg, yc, R + 0.5)
                if iv is None:
                    continue
                lo, hi = max(0, _cut(iv[0])), min(W1, _cut(iv[1]) - 1)
                if lo <= hi:
                    outs.append([lo, hi])
                if R > 0.5:
                    iv = _cap_iv(sg, yc, R - 0.5)
                    if iv is not None:
                        lo, hi = max(0, _cut(iv[0])), min(W1, _cut(iv[1]) - 1)
                        if lo <= hi:
                            ins.append([lo, hi])
            if not outs:
                continue
            outs = _merge(outs)
            ins = _merge(ins)
            off = py * self.w
            for lo, hi in outs:
                x = lo
                for a, b in ins:
                    if b < x or a > hi:
                        continue
                    a = max(a, x)
                    if a > x:
                        self._cap_pix(x, a - 1, yc, off, act, R, cr, cg, cb, ca, buf, sqrt)
                    self._span(off, a, min(b, hi), cr, cg, cb, ca)
                    x = min(b, hi) + 1
                if x <= hi:
                    self._cap_pix(x, hi, yc, off, act, R, cr, cg, cb, ca, buf, sqrt)

    @staticmethod
    def _cap_pix(lo, hi, yc, off, act, R, cr, cg, cb, ca, buf, sqrt) -> None:
        for k in range(lo, hi + 1):
            px = k + 0.5
            best = 1e18
            for ax, ay, _bx, _by, dx, dy, L, _ux, _uy, _y0, _y1 in act:
                if L > 1e-9:
                    t = ((px - ax) * dx + (yc - ay) * dy) / (L * L)
                    t = 0.0 if t < 0 else 1.0 if t > 1 else t
                    ex, ey = px - ax - t * dx, yc - ay - t * dy
                else:
                    ex, ey = px - ax, yc - ay
                d2 = ex * ex + ey * ey
                if d2 < best:
                    best = d2
            cv = R + 0.5 - sqrt(best)
            if cv <= 0.002:
                continue
            aa = ca if cv >= 0.998 else int(ca * cv + 0.5)
            if aa > 0:
                _blend(buf, (off + k) * 4, cr, cg, cb, aa)

    # ---------------------------------------------------------- polygons
    def fill_polygon(self, points: Sequence[Tuple[float, float]], color: ColorLike) -> None:
        """Non-zero winding fill; vertical supersampling + exact horizontal coverage."""
        pts = [(float(p[0]), float(p[1])) for p in points]
        if len(pts) < 3:
            return
        cr, cg, cb, ca = parse(color)
        if ca == 0:
            return
        W, H = self.w, self.h
        ys = [p[1] for p in pts]
        ymin_f, ymax_f = min(ys), max(ys)
        y0 = max(0, int(math.floor(ymin_f)))
        y1 = min(H - 1, int(math.ceil(ymax_f)))
        edges = []
        n = len(pts)
        for i in range(n):
            xa, ya = pts[i]
            xb, yb = pts[(i + 1) % n]
            if ya == yb:
                continue
            d = 1
            if ya > yb:
                xa, ya, xb, yb, d = xb, yb, xa, ya, -1
            edges.append((ya, yb, xa, (xb - xa) / (yb - ya), d))
        if not edges:
            return
        ss = 8 if (ymax_f - ymin_f) <= 160 else 4
        wsub = 1.0 / ss
        offs = [(k + 0.5) / ss for k in range(ss)]
        buf = self.buf
        for py in range(y0, y1 + 1):
            row = [e for e in edges if e[0] < py + 1 and e[1] > py]
            if not row:
                continue
            part: Dict[int, float] = {}
            diff: Dict[int, float] = {}
            for of in offs:
                sy = py + of
                xs = [(e[2] + (sy - e[0]) * e[3], e[4]) for e in row if e[0] <= sy < e[1]]
                if len(xs) < 2:
                    continue
                xs.sort()
                wind = 0
                for i in range(len(xs) - 1):
                    wind += xs[i][1]
                    if wind == 0:
                        continue
                    xa = xs[i][0]
                    xb = xs[i + 1][0]
                    if xa < 0:
                        xa = 0.0
                    if xb > W:
                        xb = float(W)
                    if xb <= xa:
                        continue
                    ia, ib = int(xa), int(xb)
                    if ia == ib:
                        part[ia] = part.get(ia, 0.0) + (xb - xa) * wsub
                        continue
                    part[ia] = part.get(ia, 0.0) + (ia + 1 - xa) * wsub
                    if ia + 1 < ib:
                        diff[ia + 1] = diff.get(ia + 1, 0.0) + wsub
                        diff[ib] = diff.get(ib, 0.0) - wsub
                    if ib < W and xb > ib:
                        part[ib] = part.get(ib, 0.0) + (xb - ib) * wsub
            if not part and not diff:
                continue
            off = py * W
            run = 0.0
            prev = None
            for k in sorted(set(part) | set(diff)):
                if prev is not None and k > prev + 1 and run > 0.002:
                    self._span(off, prev + 1, min(k - 1, W - 1), cr, cg, cb,
                               ca if run >= 0.998 else int(ca * run + 0.5))
                run += diff.get(k, 0.0)
                if -1e-9 < run < 1e-9:
                    run = 0.0
                c = run + part.get(k, 0.0)
                if c > 0.002 and k < W:
                    aa = ca if c >= 0.998 else int(ca * c + 0.5)
                    if aa > 0:
                        _blend(buf, (off + k) * 4, cr, cg, cb, aa)
                prev = k

    # ---------------------------------------------------------- output
    def png_bytes(self, level: int = 6) -> bytes:
        return encode_png(self.w, self.h, self.buf, level)

    def save_png(self, path: str) -> None:
        with open(path, "wb") as f:
            f.write(self.png_bytes(6))

    def to_photo(self, master: Any = None):
        """tk.PhotoImage (PNG via base64, alpha kept). Keep a reference while it is shown."""
        import tkinter as tk
        data = base64.b64encode(self.png_bytes(1)).decode("ascii")
        return tk.PhotoImage(master=master, data=data, format="png")


def _cap_iv(sg, yc: float, R: float) -> Optional[Tuple[float, float]]:
    """x-interval where the horizontal line y=yc is within R of segment sg (a capsule)."""
    ax, ay, bx, by, _dx, _dy, L, ux, uy = sg[:9]
    lo, hi = math.inf, -math.inf
    for px, py in ((ax, ay), (bx, by)):
        d = yc - py
        if -R <= d <= R:
            h = _sqrt(R * R - d * d)
            if px - h < lo:
                lo = px - h
            if px + h > hi:
                hi = px + h
    if L > 1e-9:
        a_lo, a_hi = -math.inf, math.inf
        ok = True
        b = (yc - ay) * uy
        if abs(ux) > 1e-12:
            t0, t1 = ax - b / ux, ax + (L - b) / ux
            a_lo, a_hi = min(t0, t1), max(t0, t1)
        elif not (0 <= b <= L):
            ok = False
        c = (yc - ay) * ux
        if ok and abs(uy) > 1e-12:
            t0, t1 = ax + (R + c) / uy, ax + (-R + c) / uy
            a_lo, a_hi = max(a_lo, min(t0, t1)), min(a_hi, max(t0, t1))
        elif ok and not (-R <= c <= R):
            ok = False
        if ok and a_lo <= a_hi:
            lo, hi = min(lo, a_lo), max(hi, a_hi)
    return None if lo > hi else (lo, hi)


def _merge(ivs: List[List[int]]) -> List[List[int]]:
    if len(ivs) < 2:
        return ivs
    ivs.sort()
    out = [ivs[0][:]]
    for a, b in ivs[1:]:
        if a <= out[-1][1] + 1:
            if b > out[-1][1]:
                out[-1][1] = b
        else:
            out.append([a, b])
    return out


# ------------------------------------------------------------------ cache
class SpriteCache:
    """LRU of tk.PhotoImage keyed by (key, w, h). Images still shown on a canvas are kept."""

    def __init__(self, master: Any = None, max_items: int = 600) -> None:
        self.master = master
        self.max_items = max(1, int(max_items))
        self._d: "OrderedDict[Tuple[Hashable, int, int], Any]" = OrderedDict()
        self._retired: List[Any] = []
        self.hits = 0
        self.misses = 0

    def __len__(self) -> int:
        return len(self._d)

    def __contains__(self, k: Tuple[Hashable, int, int]) -> bool:
        return k in self._d

    def get(self, key: Hashable, w: int, h: int, paint: Callable[[Image], None]):
        """Cached photo; on a miss, paint(img) draws into a fresh transparent Image(w, h)."""
        w, h = max(1, int(round(w))), max(1, int(round(h)))
        k = (key, w, h)
        ph = self._d.get(k)
        if ph is not None:
            self._d.move_to_end(k)
            self.hits += 1
            return ph
        self.misses += 1
        img = Image(w, h)
        try:
            paint(img)
        except Exception:
            log.exception("sprite painter failed for %r", key)
            img = Image(w, h)
        ph = img.to_photo(self.master)
        self.put(key, w, h, ph)
        return ph

    def peek(self, key: Hashable, w: int, h: int):
        k = (key, max(1, int(round(w))), max(1, int(round(h))))
        ph = self._d.get(k)
        if ph is not None:
            self._d.move_to_end(k)
        return ph

    def put(self, key: Hashable, w: int, h: int, photo: Any) -> None:
        self._d[(key, int(w), int(h))] = photo
        self._trim()

    def clear(self) -> None:
        """Forget every sprite. Photos still shown on a canvas stay alive until the next
        clear(), so items nobody keeps a reference to do not go blank mid-redraw."""
        self._retired = [ph for ph in self._d.values() if _in_use(ph)]
        self._d.clear()

    def _trim(self) -> None:
        excess = len(self._d) - self.max_items
        if excess <= 0:
            return
        doomed = []
        for scanned, (k, ph) in enumerate(self._d.items()):
            if len(doomed) >= excess or scanned >= excess + 64:
                break
            if not _in_use(ph):
                doomed.append(k)
        for k in doomed:
            del self._d[k]


def _in_use(photo: Any) -> bool:
    """True while a canvas item or widget displays this tk image."""
    try:
        return bool(int(photo.tk.call("image", "inuse", photo.name)))
    except Exception:
        return False
