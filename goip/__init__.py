"""Games over IP - LAN party games in pure Python + tkinter (standard library only)."""
from __future__ import annotations

__version__ = "1.0.0"

APP_ID = "goip"
APP_NAME = "Games over IP"
PROTOCOL_VERSION = 1

DEFAULT_PORT = 50505        # TCP: game rooms
DISCOVERY_PORT = 50506      # UDP: "who is hosting?" beacons

# Window presets (logical pixels at 96 DPI; app.py multiplies by the DPI factor).
WINDOW_SIZES = {"S": (360, 520), "M": (420, 600), "L": (520, 740)}
MIN_WINDOW = (340, 480)
BASE_SIZE = WINDOW_SIZES["M"]   # ui.scale == 1.0 at this size

SETTINGS_FILE = ".games_over_ip.json"   # in the user's home folder
