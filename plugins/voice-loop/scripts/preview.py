#!/usr/bin/env python3
"""voice-loop — live dictation preview overlay (windowsill#115).

A small always-on-top window that renders the streaming interims as they arrive while you speak:
assembled finals in solid text, the current interim guess in dim italics. When the state file is
deleted by ``dictate.py``'s stop path, the window clears and exits.

Stdlib only, Python 3.10+. The renderer is tkinter — the one GUI toolkit the stdlib ships. When
tkinter is absent or cannot open a display the script exits silently (exit 0), which is the
documented degrade: no display surface → feature off, dictation unchanged.

Usage: preview.py <preview-state-file>
"""

from __future__ import annotations

import json
import os
import sys
import time


def main(argv: list[str]) -> int:
    preview_path = argv[1] if len(argv) > 1 else ""
    if not preview_path:
        return 1

    try:
        import tkinter as tk
    except ImportError:
        # no tkinter at all: silently off, as designed
        return 0

    try:
        root = tk.Tk()
    except tk.TclError:
        # no display: silently off
        return 0

    root.title("voice-loop preview")
    root.overrideredirect(True)
    root.attributes("-topmost", True)
    # Semi-transparent dark background — the preview is a live signal, not a window to interact with
    root.configure(bg="#1e1e1e")
    try:
        root.attributes("-alpha", 0.88)
    except tk.TclError:
        pass  # transparency is best-effort; some WMs / macOS versions may not support it

    # Bottom-centre of the screen, roughly — the same region a desktop notification lands in.
    # x and y are nudged off the absolute edge so the window is visible even on a maximised screen.
    sw = root.winfo_screenwidth()
    sh = root.winfo_screenheight()
    width = min(620, sw - 40)
    root.geometry(f"{width}x80+{max(0, (sw - width) // 2)}+{sh - 130}")

    assembled_label = tk.Label(
        root,
        text="",
        font=("Sans", 14),
        fg="#e0e0e0",
        bg="#1e1e1e",
        wraplength=width - 20,
        justify="left",
    )
    assembled_label.pack(padx=10, pady=(10, 0))

    interim_label = tk.Label(
        root,
        text="",
        font=("Sans", 14, "italic"),
        fg="#808080",
        bg="#1e1e1e",
        wraplength=width - 20,
        justify="left",
    )
    interim_label.pack(padx=10, pady=(0, 10))

    def poll() -> None:
        try:
            with open(preview_path, encoding="utf-8") as fh:
                data = json.load(fh)
        except (FileNotFoundError, OSError, ValueError):
            # The state file is gone (cleared by stop_and_transcribe) or unreadable — clear and exit.
            root.destroy()
            return
        assembled = str(data.get("assembled", ""))
        interim = str(data.get("interim", ""))
        # Only update the labels when the text changed — avoids flicker from writes that carry the
        # same values (the worker writes after every message, and most of them are not transcripts).
        if assembled_label.cget("text") != assembled:
            assembled_label.config(text=assembled)
        if interim_label.cget("text") != interim:
            interim_label.config(text=interim)
        root.after(100, poll)

    poll()
    root.mainloop()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
