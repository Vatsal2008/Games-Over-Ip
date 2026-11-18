"""User preferences, stored as JSON in ~/.games_over_ip.json (best effort, never fatal)."""
from __future__ import annotations

import json
import logging
import os
from typing import Any, Dict, Optional

from . import DEFAULT_PORT, SETTINGS_FILE, WINDOW_SIZES
from .protocol import clean_name
from .theme import THEMES

log = logging.getLogger(__name__)

OPACITIES = (100, 90, 80, 70)
BOSS_ACTIONS = ("minimize", "notes")
MAX_NOTES = 20000

DEFAULTS: Dict[str, Any] = {
    "name": "",
    "theme": "neon",
    "size": "M",
    "opacity": 100,
    "boss": "minimize",
    "host_ip": "",
    "join_ip": "",
    "port": DEFAULT_PORT,
    "game": "ludo",
    "notes": "",
    "rejoin": "",   # "ip:port|key" of the last room joined (gets your seat back)
}


def settings_path() -> str:
    """~/.games_over_ip.json, or the file named by $GOIP_SETTINGS (used by tests and dev tools)."""
    return os.environ.get("GOIP_SETTINGS") or os.path.join(os.path.expanduser("~"), SETTINGS_FILE)


def default_name() -> str:
    for var in ("USERNAME", "USER", "LOGNAME"):
        value = os.environ.get(var)
        if value:
            return clean_name(value[:12], "Player")
    return "Player"


class Settings(dict):
    def __init__(self, file: Optional[str] = None) -> None:
        super().__init__(DEFAULTS)
        self.file = file or settings_path()
        self.load()

    def load(self) -> None:
        data: Any = {}
        try:
            with open(self.file, "r", encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = {}
        if isinstance(data, dict):
            for key, default in DEFAULTS.items():
                value = data.get(key)
                if value is not None and type(value) is type(default):
                    self[key] = value
        self._sanitise()

    def _sanitise(self) -> None:
        self["name"] = clean_name(self["name"], "") or default_name()
        if self["theme"] not in THEMES:
            self["theme"] = "neon"
        if self["size"] not in WINDOW_SIZES:
            self["size"] = "M"
        if self["opacity"] not in OPACITIES:
            self["opacity"] = 100
        if self["boss"] not in BOSS_ACTIONS:
            self["boss"] = "minimize"
        if not 1024 <= self["port"] <= 65535:
            self["port"] = DEFAULT_PORT
        self["notes"] = self["notes"][:MAX_NOTES]

    def save(self) -> None:
        self._sanitise()
        tmp = self.file + ".tmp"
        try:
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(dict(self), fh, indent=2, ensure_ascii=False)
            os.replace(tmp, self.file)
        except OSError as exc:
            log.warning("could not save settings: %s", exc)
