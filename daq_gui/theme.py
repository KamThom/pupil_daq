from __future__ import annotations

import tkinter as tk
from tkinter import ttk

# Base palette (Slate dark) — shared with PlotCanvas so the whole app reads as one system.
# Elevation scale, each step one shade lighter: BG < PANEL < BORDER (button face) < BORDER_LIGHT (outline/hover).
BG = "#111827"
PANEL = "#1f2937"
BORDER = "#374151"
BORDER_LIGHT = "#475569"
FG = "#e5e7eb"
FG_MUTED = "#9ca3af"

# Accent colors — match the live-plot channel colors (CH1 blue, CH2 red, sched green, CH3 purple, CH4 orange).
# Used as the colored left-edge strip on action buttons and collapsible sections, not as full button fills.
BLUE = "#3b82f6"
GREEN = "#22c55e"
RED = "#ef4444"
ORANGE = "#f97316"
PURPLE = "#a855f7"
AMBER = "#fbbf24"   # event markers on the plot


def apply(root: tk.Tk) -> None:
    """Apply a consistent dark theme, independent of OS/native ttk theme."""
    root.configure(background=BG)

    style = ttk.Style(root)
    style.theme_use("clam")

    style.configure(".", background=BG, foreground=FG, bordercolor=BORDER, lightcolor=BORDER, darkcolor=BORDER)

    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground=FG)
    style.configure("Muted.TLabel", background=BG, foreground=FG_MUTED)
    style.configure("Recording.TLabel", background=BG, foreground=ORANGE)

    style.configure("TLabelframe", background=BG, foreground=FG, bordercolor=BORDER)
    style.configure("TLabelframe.Label", background=BG, foreground=FG)

    style.configure("TSeparator", background=BORDER)

    # Neutral "raised" button: face one shade lighter than the surrounding panel, with a thin
    # outline so it reads as a button at rest, not just on hover. Buttons that need to signal a
    # specific action category get a colored left-edge strip via the accent_button() wrapper in
    # app.py rather than a separate named style — see that helper for why.
    style.configure(
        "TButton",
        background=BORDER,
        foreground=FG,
        bordercolor=BORDER_LIGHT,
        relief="flat",
        borderwidth=1,
        focuscolor=BLUE,
        padding=6,
    )
    style.map(
        "TButton",
        background=[("active", BORDER_LIGHT), ("pressed", BORDER_LIGHT)],
        foreground=[("disabled", FG_MUTED)],
    )

    style.configure("TEntry", fieldbackground=PANEL, foreground=FG, bordercolor=BORDER, insertcolor=FG, padding=4)
    style.map("TEntry", bordercolor=[("focus", BLUE)])

    style.configure(
        "TSpinbox",
        fieldbackground=PANEL,
        foreground=FG,
        background=PANEL,
        bordercolor=BORDER,
        arrowcolor=FG_MUTED,
        padding=4,
    )
    style.map("TSpinbox", bordercolor=[("focus", BLUE)], arrowcolor=[("active", FG)])

    style.configure(
        "TCombobox",
        fieldbackground=PANEL,
        foreground=FG,
        background=PANEL,
        bordercolor=BORDER,
        arrowcolor=FG_MUTED,
        padding=4,
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", PANEL)],
        foreground=[("readonly", FG)],
        bordercolor=[("focus", BLUE)],
        arrowcolor=[("active", FG)],
    )
    root.option_add("*TCombobox*Listbox.background", PANEL)
    root.option_add("*TCombobox*Listbox.foreground", FG)
    root.option_add("*TCombobox*Listbox.selectBackground", BLUE)
    root.option_add("*TCombobox*Listbox.selectForeground", "#f9fafb")

    style.configure("TNotebook", background=BG, bordercolor=BORDER)
    style.configure("TNotebook.Tab", background=PANEL, foreground=FG_MUTED, bordercolor=BORDER, padding=[14, 6])
    style.map(
        "TNotebook.Tab",
        background=[("selected", BG)],
        foreground=[("selected", FG)],
        bordercolor=[("selected", BLUE)],
    )

    style.configure("TScrollbar", background=PANEL, troughcolor=BG, bordercolor=BORDER, arrowcolor=FG_MUTED)
    style.map("TScrollbar", background=[("active", BORDER)])

    style.configure("Treeview", background=PANEL, fieldbackground=PANEL, foreground=FG, bordercolor=BORDER, rowheight=24)
    style.map("Treeview", background=[("selected", BLUE)], foreground=[("selected", "#f9fafb")])
    style.configure("Treeview.Heading", background=BORDER, foreground=FG, bordercolor=BORDER, padding=4)
    style.map("Treeview.Heading", background=[("active", BORDER)])
