"""Colour themes and font selection.

Three themes:
    neon  - default: deep indigo gradient, vivid colours, glows and particles
    dim   - dark and low-brightness for dark lecture halls; no glow
    paper - stealth: plain light-grey "office notes" look, window title "Notes"
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional, Tuple

SeatPalette = Tuple[str, str, str]  # (main, light, dark)


@dataclass(frozen=True)
class Theme:
    key: str
    label: str
    title: str          # window title while this theme is active
    dark: bool
    bg_top: str         # window background gradient (top -> bottom)
    bg_bottom: str
    panel: str          # card / panel fill
    panel_hi: str       # hover / raised panel
    edge: str           # hairline borders
    text: str
    text_dim: str
    text_faint: str
    accent: str         # primary buttons, highlights
    accent_hi: str      # lighter end of the accent gradient
    accent2: str        # secondary accent (cyan in neon)
    good: str
    bad: str
    warn: str
    board_a: str        # board cells, colour 1
    board_b: str        # board cells, colour 2
    board_line: str
    shadow: str         # shadow colour (used with alpha)
    glow: bool          # draw glows, particles, decorative blobs
    seats: Dict[str, SeatPalette] = field(default_factory=dict)

    def seat(self, color: str) -> SeatPalette:
        return self.seats.get(color) or self.seats["red"]


NEON = Theme(
    key="neon", label="Neon", title="Games over IP", dark=True,
    bg_top="#141833", bg_bottom="#26124d",
    panel="#1f2347", panel_hi="#2a2f5e", edge="#363d78",
    text="#f1f2ff", text_dim="#aab0e0", text_faint="#6d73a8",
    accent="#7c5cff", accent_hi="#a08bff", accent2="#22d3ee",
    good="#34d399", bad="#fb7185", warn="#fbbf24",
    board_a="#f7f4ff", board_b="#e6e0fb", board_line="#c9c1ef",
    shadow="#05060f", glow=True,
    seats={
        "red": ("#f43f5e", "#fda4af", "#9f1239"),
        "blue": ("#3b82f6", "#93c5fd", "#1e3a8a"),
        "green": ("#22c55e", "#86efac", "#14532d"),
        "yellow": ("#facc15", "#fde68a", "#a16207"),
    },
)

DIM = Theme(
    key="dim", label="Dim", title="Games over IP", dark=True,
    bg_top="#121316", bg_bottom="#18191d",
    panel="#1e2025", panel_hi="#262930", edge="#30333b",
    text="#c9ccd3", text_dim="#8b9099", text_faint="#5d626b",
    accent="#5c6bc0", accent_hi="#7380cc", accent2="#4f9aa8",
    good="#5fa77f", bad="#c06a6a", warn="#c4a25a",
    board_a="#2b2e35", board_b="#24272d", board_line="#3a3e47",
    shadow="#000000", glow=False,
    seats={
        "red": ("#c25b5b", "#d98c8c", "#7a2f2f"),
        "blue": ("#5577b5", "#8ea6d4", "#2f4772"),
        "green": ("#5c9b6b", "#8fc19b", "#335c3d"),
        "yellow": ("#c9a84e", "#dec584", "#7d6527"),
    },
)

PAPER = Theme(
    key="paper", label="Paper", title="Notes", dark=False,
    bg_top="#f4f4f2", bg_bottom="#ecebe8",
    panel="#ffffff", panel_hi="#f7f7f5", edge="#d9d8d4",
    text="#2b2b2b", text_dim="#606060", text_faint="#9b9b9b",
    accent="#4a6fa5", accent_hi="#5a80b8", accent2="#6b8fb3",
    good="#4f8a5b", bad="#b05a52", warn="#a88a3c",
    board_a="#ffffff", board_b="#f1f0ec", board_line="#d6d4cf",
    shadow="#000000", glow=False,
    seats={
        "red": ("#c46a6a", "#e2aaaa", "#8a3c3c"),
        "blue": ("#5b7fb0", "#a3bad8", "#34507a"),
        "green": ("#5f9a6c", "#a5ccae", "#386143"),
        "yellow": ("#c8a64a", "#e3d08f", "#86692a"),
    },
)

THEMES: Dict[str, Theme] = {t.key: t for t in (NEON, DIM, PAPER)}


def get_theme(key: Optional[str]) -> Theme:
    return THEMES.get(key or "", NEON)


# ------------------------------------------------------------------ fonts
_TEXT_FAMILIES = ("Segoe UI", "SF Pro Text", "Helvetica Neue", "Inter", "Ubuntu",
                  "Cantarell", "Noto Sans", "DejaVu Sans", "Arial", "Helvetica")
_DISPLAY_FAMILIES = ("Segoe UI Black", "Segoe UI Semibold", "SF Pro Display", "Helvetica Neue",
                     "Ubuntu", "Noto Sans", "DejaVu Sans", "Arial Black", "Arial", "Helvetica")
_cache: Dict[str, str] = {}


def _pick(kind: str, choices: Tuple[str, ...], root=None) -> str:
    if kind in _cache:
        return _cache[kind]
    try:
        import tkinter.font as tkfont
        available = set(tkfont.families(root))
    except Exception:
        available = set()
    family = next((f for f in choices if f in available), "TkDefaultFont")
    _cache[kind] = family
    return family


def text_family(root=None) -> str:
    return _pick("text", _TEXT_FAMILIES, root)


def display_family(root=None) -> str:
    return _pick("display", _DISPLAY_FAMILIES, root)
