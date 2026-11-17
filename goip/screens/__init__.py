"""App screens. Each is a Screen subclass drawing on its own full-window canvas."""
from __future__ import annotations

from .host import HostGameScreen, HostNetScreen
from .join import JoinScreen
from .lobby import LobbyScreen
from .menu import MenuScreen
from .play import PlayScreen
from .prefs import PrefsScreen

__all__ = ["HostGameScreen", "HostNetScreen", "JoinScreen", "LobbyScreen", "MenuScreen", "PlayScreen",
           "PrefsScreen"]
