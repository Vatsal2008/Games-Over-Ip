"""Game registry.

Each game lives in ``goip/games/<id>/`` with:
    logic.py  -> class Logic(GameLogic)       (server side, no tkinter)
    view.py   -> class View(GameView)         (client side)
                 def paint_icon(img, theme)   (draws the game-card icon into a gfx.Image)
Modules are imported lazily so a headless host never needs a game's view.
"""
from __future__ import annotations

import importlib
from types import ModuleType
from typing import List, Type

GAME_IDS = ("tictactoe", "snakes", "ludo", "bingo")


def logic_class(game_id: str) -> Type:
    if game_id not in GAME_IDS:
        raise KeyError("unknown game %r" % (game_id,))
    return importlib.import_module("goip.games.%s.logic" % game_id).Logic


def view_module(game_id: str) -> ModuleType:
    if game_id not in GAME_IDS:
        raise KeyError("unknown game %r" % (game_id,))
    return importlib.import_module("goip.games.%s.view" % game_id)


def view_class(game_id: str) -> Type:
    return view_module(game_id).View


def all_logics() -> List[Type]:
    return [logic_class(g) for g in GAME_IDS]
