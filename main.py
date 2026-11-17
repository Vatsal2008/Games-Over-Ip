#!/usr/bin/env python3
"""Games over IP - run me:  python main.py   (or double-click play.bat on Windows)"""
import sys

if sys.version_info < (3, 8):
    sys.exit("Games over IP needs Python 3.8 or newer (you have %d.%d)." % sys.version_info[:2])

try:
    import tkinter  # noqa: F401
except ImportError:
    sys.exit("tkinter is missing. Windows/macOS: reinstall Python from python.org. "
             "Linux: sudo apt install python3-tk")

from goip.app import main

if __name__ == "__main__":
    main()
