"""Boss-key notes page: a plain, real notes editor drawn over everything."""
from __future__ import annotations

import time
import tkinter as tk
from typing import Any

from ..settings import MAX_NOTES
from ..theme import PAPER, text_family

TEMPLATE = "Lecture notes\n\n• "


class NotesOverlay:
    def __init__(self, app: Any) -> None:
        self.app = app
        root = app.root
        pal = PAPER
        fam = text_family(root)
        self.visible = False
        self.frame = tk.Frame(root, bg=pal.bg_top, bd=0, highlightthickness=0)
        bar = tk.Frame(self.frame, bg=pal.bg_top, bd=0, highlightthickness=0)
        bar.pack(fill="x")
        tk.Label(bar, text="Notes", bg=pal.bg_top, fg=pal.text, font=(fam, 13, "bold"), anchor="w",
                 padx=14, pady=8).pack(side="left")
        tk.Label(bar, text=time.strftime("%d %b %Y"), bg=pal.bg_top, fg=pal.text_faint, font=(fam, 10),
                 padx=14).pack(side="right")
        tk.Frame(self.frame, bg=pal.edge, height=1, bd=0).pack(fill="x")
        self.text = tk.Text(self.frame, wrap="word", bd=0, highlightthickness=0, padx=16, pady=12,
                            bg="#ffffff", fg=pal.text, insertbackground=pal.text, selectbackground="#c8d7ee",
                            font=(fam, 11), undo=True, spacing1=2, spacing3=2)
        self.text.pack(fill="both", expand=True)
        self.text.insert("1.0", app.settings["notes"] or TEMPLATE)

    def show(self) -> None:
        self.visible = True
        self.frame.place(x=0, y=0, relwidth=1, relheight=1)
        self.frame.lift()
        self.app.root.title("Notes")
        self.text.focus_set()
        self.text.mark_set("insert", "end-1c")

    def hide(self) -> None:
        self.save()
        self.visible = False
        self.frame.place_forget()
        self.app.root.title(self.app.ui.theme.title)

    def save(self) -> None:
        content = self.text.get("1.0", "end-1c")[:MAX_NOTES]
        self.app.settings["notes"] = content if content.strip() not in ("", TEMPLATE.strip()) else ""
        self.app.settings.save()

    def destroy(self) -> None:
        try:
            self.frame.destroy()
        except tk.TclError:
            pass
