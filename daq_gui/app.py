from __future__ import annotations

import csv
import json
import queue
import time
import tkinter as tk
from collections import deque
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg
from matplotlib.figure import Figure

from daq_gui.protocol import (
    DataFrame,
    Message,
    SchedEvent,
    SingleRead,
    Status,
    command,
    command_schedule_clear,
    command_schedule_start,
    command_schedule_step,
    command_schedule_stop,
    parse_line,
    raw_to_volts,
    vis_dac_code_to_current_ma,
)
from daq_gui.serial_worker import ConnectionEvent, SerialWorker
from daq_gui import theme


PLOT_WINDOW_S = 30.0   # seconds of history shown in the rolling window

# Commands whose values are worth logging as events in the CSV.
_EVENT_COMMANDS: dict[int, object] = {
    0:  lambda v: "Device ON" if v else "Device OFF",
    1:  lambda v: f"VIS gain={v}",
    2:  lambda v: f"IR gain={v}",
    3:  lambda v: f"VIS LED DAC={v}",
    4:  lambda v: f"IR pulse {v} us",
    9:  lambda v: "Stream ON" if v else "Stream OFF",
    11: lambda v: f"Sample rate={v} Hz",
}


def accent_button(parent: tk.Misc, text: str, command: object, accent: str, **kwargs: object) -> tuple[tk.Frame, ttk.Button]:
    """A ttk.Button with a colored strip on its left edge, for buttons that signal a specific
    action category (go/stop/primary/schedule). ttk can't color a single border side on its own,
    so this wraps the button in a small colored frame and insets the button 3px from its left
    edge to let that color show through. Buttons with no category should just use ttk.Button
    directly — the default style already reads as a button (raised face + outline) on its own."""
    wrap = tk.Frame(parent, background=accent)
    btn = ttk.Button(wrap, text=text, command=command, **kwargs)
    btn.pack(fill="both", expand=True, padx=(3, 0))
    return wrap, btn


def collapsible_section(
    parent: tk.Misc, title: str, accent: str, start_open: bool = True
) -> tuple[tk.Frame, tk.Frame]:
    """A collapsible panel with a colored left edge and a clickable header (replaces ttk.LabelFrame,
    which has no collapse support). Returns (outer, body): pack/grid `outer` into the layout, then
    pack/grid content into `body`."""
    outer = tk.Frame(parent, background=accent)
    inner = tk.Frame(outer, background=theme.PANEL, padx=10, pady=8)
    inner.pack(fill="both", expand=True, padx=(3, 0))

    is_open = {"value": start_open}
    header = tk.Frame(inner, background=theme.PANEL, cursor="hand2")
    header.pack(fill="x")
    arrow = tk.Label(
        header, text="▾" if start_open else "▸",
        background=theme.PANEL, foreground=theme.FG_MUTED, cursor="hand2",
    )
    arrow.pack(side="left", padx=(0, 6))
    title_label = tk.Label(
        header, text=title, background=theme.PANEL, foreground=theme.FG_MUTED,
        font=("TkDefaultFont", 9, "bold"), cursor="hand2", anchor="w",
    )
    title_label.pack(side="left", fill="x", expand=True)

    body = tk.Frame(inner, background=theme.PANEL)
    if start_open:
        body.pack(fill="both", expand=True, pady=(8, 0))

    def toggle(_event: object = None) -> None:
        if is_open["value"]:
            body.pack_forget()
            arrow.configure(text="▸")
        else:
            body.pack(fill="both", expand=True, pady=(8, 0))
            arrow.configure(text="▾")
        is_open["value"] = not is_open["value"]

    for widget in (header, arrow, title_label):
        widget.bind("<Button-1>", toggle)

    return outer, body


class PlotCanvas(tk.Frame):
    """Live dual-channel plot: CH1 IR PD and CH2 VIS PD in volts vs. elapsed time."""

    _BG = theme.BG
    _PANEL = theme.PANEL
    _GRID = theme.BORDER
    _FG = theme.FG

    # Selectable data sources for the third (bottom) subplot.
    _THIRD_CHANNEL_INFO: dict[str, dict[str, str]] = {
        "sched": {"label": "VIS LED current (commanded)", "title": "VIS LED current", "ylabel": "VIS (mA)", "color": theme.GREEN},
        "ch1": {"label": "CH1 IR Photodiode", "title": "CH1  IR Photodiode", "ylabel": "IR PD (V)", "color": theme.BLUE},
        "ch2": {"label": "CH2 VIS Photodiode", "title": "CH2  VIS Photodiode", "ylabel": "VIS PD (V)", "color": theme.RED},
        "ch3": {"label": "CH3 VIS LED current sense", "title": "CH3  VIS LED current sense", "ylabel": "CH3 (V)", "color": theme.PURPLE},
        "ch4": {"label": "CH4 IR LED current sense", "title": "CH4  IR LED current sense", "ylabel": "CH4 (V)", "color": theme.ORANGE},
    }

    def __init__(self, master: tk.Misc, **kwargs: object) -> None:
        super().__init__(master, background=self._BG, **kwargs)

        self._fig = Figure(facecolor=self._BG)
        self._ax_ir = self._fig.add_subplot(3, 1, 1)
        self._ax_vis = self._fig.add_subplot(3, 1, 2, sharex=self._ax_ir)
        self._ax_sched = self._fig.add_subplot(3, 1, 3, sharex=self._ax_ir)
        self._fig.subplots_adjust(hspace=0.50, left=0.12, right=0.97, top=0.95, bottom=0.09)

        self._mpl_canvas = FigureCanvasTkAgg(self._fig, master=self)
        self._mpl_canvas.get_tk_widget().pack(fill="both", expand=True)

        self._times: deque[float] = deque()
        self._ch1_v: deque[float] = deque()
        self._ch2_v: deque[float] = deque()
        self._ch3_v: deque[float] = deque()
        self._ch4_v: deque[float] = deque()
        self._sched_times: deque[float] = deque()
        self._sched_levels: deque[float] = deque()   # mA
        self._event_markers: list[tuple[float, str]] = []
        self._event_lines: list = []
        self._t0: float | None = None
        self._last_draw: float = 0.0
        self._third_channel: str = "sched"
        self._window_s: float = PLOT_WINDOW_S

        self._style_axes()
        (self._line_ir,) = self._ax_ir.plot([], [], color=theme.BLUE, lw=1.2)
        (self._line_vis,) = self._ax_vis.plot([], [], color=theme.RED, lw=1.2)
        (self._line_sched,) = self._ax_sched.plot([], [], color=theme.GREEN, lw=1.5, drawstyle="steps-post")

    # ── public API ───────────────────────────────────────────────────────────

    def add_frame(self, frame: DataFrame, host_time: float) -> None:
        if self._t0 is None:
            self._t0 = host_time
        t = host_time - self._t0
        self._times.append(t)
        self._ch1_v.append(raw_to_volts(frame.high[0]))
        self._ch2_v.append(raw_to_volts(frame.high[1]))
        self._ch3_v.append(raw_to_volts(frame.high[2]))
        self._ch4_v.append(raw_to_volts(frame.high[3]))
        cutoff = t - self._window_s * 2
        while self._times and self._times[0] < cutoff:
            self._times.popleft()
            self._ch1_v.popleft()
            self._ch2_v.popleft()
            self._ch3_v.popleft()
            self._ch4_v.popleft()
        self._throttled_redraw()

    @classmethod
    def third_channel_choices(cls) -> list[str]:
        return [info["label"] for info in cls._THIRD_CHANNEL_INFO.values()]

    def set_third_channel_by_label(self, label: str) -> None:
        for key, info in self._THIRD_CHANNEL_INFO.items():
            if info["label"] == label:
                self.set_third_channel(key)
                return

    def set_window_s(self, seconds: float) -> None:
        seconds = max(1.0, float(seconds))
        if seconds == self._window_s:
            return
        self._window_s = seconds
        for ax in (self._ax_ir, self._ax_vis, self._ax_sched):
            ax.set_xlim(0, seconds)
        self._redraw()
        self._mpl_canvas.draw_idle()

    def set_third_channel(self, key: str) -> None:
        if key not in self._THIRD_CHANNEL_INFO or key == self._third_channel:
            return
        self._third_channel = key
        info = self._THIRD_CHANNEL_INFO[key]
        self._ax_sched.set_title(info["title"], color=self._FG, fontsize=9, pad=3)
        self._ax_sched.set_ylabel(info["ylabel"], color=self._FG, fontsize=9)
        self._line_sched.set_color(info["color"])
        self._line_sched.set_drawstyle("steps-post" if key == "sched" else "default")
        self._ax_sched.set_ylim(-0.5, 35) if key == "sched" else self._ax_sched.set_ylim(-5.5, 5.5)
        self._redraw()
        self._mpl_canvas.draw_idle()

    def add_sched_event(self, dac_code: int, host_time: float) -> None:
        if self._t0 is None:
            return
        t = host_time - self._t0
        ma = vis_dac_code_to_current_ma(dac_code)
        self._sched_times.append(t)
        self._sched_levels.append(ma)
        self._throttled_redraw()

    def add_event(self, description: str, host_time: float) -> None:
        if self._t0 is None:
            return
        self._event_markers.append((host_time - self._t0, description))
        if len(self._event_markers) > 200:
            self._event_markers = self._event_markers[-200:]

    def clear(self) -> None:
        self._times.clear()
        self._ch1_v.clear()
        self._ch2_v.clear()
        self._ch3_v.clear()
        self._ch4_v.clear()
        self._sched_times.clear()
        self._sched_levels.clear()
        self._event_markers.clear()
        for ln in self._event_lines:
            try:
                ln.remove()
            except ValueError:
                pass
        self._event_lines.clear()
        self._t0 = None
        self._last_draw = 0.0
        self._line_ir.set_data([], [])
        self._line_vis.set_data([], [])
        self._line_sched.set_data([], [])
        for ax in (self._ax_ir, self._ax_vis):
            ax.set_xlim(0, self._window_s)
            ax.set_ylim(-5.5, 5.5)
        self._ax_sched.set_xlim(0, self._window_s)
        self._ax_sched.set_ylim(-0.5, 35) if self._third_channel == "sched" else self._ax_sched.set_ylim(-5.5, 5.5)
        self._mpl_canvas.draw_idle()

    # ── internal ─────────────────────────────────────────────────────────────

    def _style_axes(self) -> None:
        for ax in (self._ax_ir, self._ax_vis, self._ax_sched):
            ax.set_facecolor(self._PANEL)
            ax.tick_params(colors=self._FG, labelsize=8)
            for spine in ax.spines.values():
                spine.set_edgecolor(self._GRID)
            ax.grid(True, color=self._GRID, lw=0.5, alpha=0.7)
        self._ax_ir.set_ylabel("IR PD (V)", color=self._FG, fontsize=9)
        self._ax_vis.set_ylabel("VIS PD (V)", color=self._FG, fontsize=9)
        self._ax_sched.set_ylabel("VIS (mA)", color=self._FG, fontsize=9)
        self._ax_sched.set_xlabel("Time (s)", color=self._FG, fontsize=9)
        self._ax_ir.tick_params(labelbottom=False)
        self._ax_vis.tick_params(labelbottom=False)
        self._ax_ir.set_title("CH1  IR Photodiode", color=self._FG, fontsize=9, pad=3)
        self._ax_vis.set_title("CH2  VIS Photodiode", color=self._FG, fontsize=9, pad=3)
        self._ax_sched.set_title("VIS LED current", color=self._FG, fontsize=9, pad=3)
        for ax in (self._ax_ir, self._ax_vis):
            ax.set_xlim(0, self._window_s)
            ax.set_ylim(-5.5, 5.5)
        self._ax_sched.set_xlim(0, self._window_s)
        self._ax_sched.set_ylim(-0.5, 35)

    def _throttled_redraw(self) -> None:
        now = time.time()
        if now - self._last_draw < 0.05:   # cap at ~20 fps
            return
        self._last_draw = now
        self._redraw()

    def _redraw(self) -> None:
        if not self._times:
            return
        t_now = self._times[-1]
        t_min = max(0.0, t_now - self._window_s)

        times = list(self._times)
        ch1 = list(self._ch1_v)
        ch2 = list(self._ch2_v)
        mask = [t >= t_min for t in times]
        t_vis = [t for t, m in zip(times, mask) if m]
        ch1_vis = [v for v, m in zip(ch1, mask) if m]
        ch2_vis = [v for v, m in zip(ch2, mask) if m]

        self._line_ir.set_data(t_vis, ch1_vis)
        self._line_vis.set_data(t_vis, ch2_vis)

        for ax, vals in ((self._ax_ir, ch1_vis), (self._ax_vis, ch2_vis)):
            if vals:
                lo, hi = min(vals), max(vals)
                pad = max((hi - lo) * 0.1, 0.05)
                ax.set_ylim(lo - pad, hi + pad)

        self._ax_ir.set_xlim(t_min, t_now + 0.5)

        if self._third_channel == "sched":
            # VIS schedule step plot — extend last level to right edge of window
            sched_t = list(self._sched_times)
            sched_y = list(self._sched_levels)
            if sched_t:
                sched_t.append(t_now + 0.5)
                sched_y.append(sched_y[-1])
                self._line_sched.set_data(sched_t, sched_y)
                hi = max(sched_y[:-1])   # exclude phantom point
                self._ax_sched.set_ylim(-0.5, max(hi * 1.2, 2.0))
            else:
                self._line_sched.set_data([], [])
        else:
            channel_data = {"ch1": ch1, "ch2": ch2, "ch3": list(self._ch3_v), "ch4": list(self._ch4_v)}[self._third_channel]
            channel_vis = [v for v, m in zip(channel_data, mask) if m]
            self._line_sched.set_data(t_vis, channel_vis)
            if channel_vis:
                lo, hi = min(channel_vis), max(channel_vis)
                pad = max((hi - lo) * 0.1, 0.05)
                self._ax_sched.set_ylim(lo - pad, hi + pad)

        # Rebuild event marker lines across all three axes
        for ln in self._event_lines:
            try:
                ln.remove()
            except ValueError:
                pass
        self._event_lines.clear()
        for t_ev, _ in self._event_markers:
            if t_min <= t_ev <= t_now + 0.5:
                l1 = self._ax_ir.axvline(t_ev, color=theme.AMBER, lw=1.0, ls="--", alpha=0.7)
                l2 = self._ax_vis.axvline(t_ev, color=theme.AMBER, lw=1.0, ls="--", alpha=0.7)
                l3 = self._ax_sched.axvline(t_ev, color=theme.AMBER, lw=1.0, ls="--", alpha=0.7)
                self._event_lines.extend([l1, l2, l3])

        self._mpl_canvas.draw_idle()


class _ScrollFrame(ttk.Frame):
    """Vertically scrollable container. Pack/grid children into .inner."""

    def __init__(self, master: tk.Misc, **kwargs: object) -> None:
        super().__init__(master, **kwargs)
        self.rowconfigure(0, weight=1)
        self.columnconfigure(0, weight=1)

        self._canvas = tk.Canvas(self, borderwidth=0, highlightthickness=0, background=theme.BG)
        self._sb = ttk.Scrollbar(self, orient="vertical", command=self._canvas.yview)
        self.inner = ttk.Frame(self._canvas)
        self._max_inner_width = 0

        self._win = self._canvas.create_window((0, 0), window=self.inner, anchor="nw")
        self._canvas.configure(yscrollcommand=self._sb.set)

        self.inner.bind("<Configure>", self._on_inner_configure)
        self._canvas.bind("<Configure>", self._on_canvas_configure)

        self._canvas.grid(row=0, column=0, sticky="nsew")
        self._sb.grid(row=0, column=1, sticky="ns")

        # Bind mouse-wheel only while the pointer is over the scroll area.
        self._canvas.bind("<Enter>", lambda _: self._canvas.bind_all("<MouseWheel>", self._on_wheel))
        self._canvas.bind("<Leave>", lambda _: self._canvas.unbind_all("<MouseWheel>"))

    def _on_inner_configure(self, _event: tk.Event) -> None:
        self._canvas.update_idletasks()
        # Width only ever grows -- collapsing a section shouldn't make the sidebar narrower,
        # just shorter. Height (scrollregion) tracks content exactly in both directions.
        self._max_inner_width = max(self._max_inner_width, self.inner.winfo_reqwidth())
        self._canvas.configure(width=self._max_inner_width, scrollregion=self._canvas.bbox("all"))

    def _on_canvas_configure(self, event: tk.Event) -> None:
        self._canvas.itemconfig(self._win, width=event.width)

    def _on_wheel(self, event: tk.Event) -> None:
        self._canvas.yview_scroll(-int(event.delta / 120), "units")


class SharedConfig:
    """Experiment-level settings shared across all device panels."""

    def __init__(self) -> None:
        self.cohort_var = tk.StringVar(value="")
        self.test_var = tk.StringVar(value="")


class AddDeviceDialog(tk.Toplevel):
    """Modal dialog to choose a COM port and animal ID when adding a device."""

    def __init__(self, master: tk.Misc, available_ports: list[str]) -> None:
        super().__init__(master)
        self.configure(background=theme.BG)
        self.title("Add Device")
        self.resizable(False, False)
        self.grab_set()

        self.result_port: str | None = None
        self.result_animal_id: str = ""

        frame = ttk.Frame(self, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)

        ttk.Label(frame, text="Serial port").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=(0, 6))
        self.port_var = tk.StringVar()
        combo = ttk.Combobox(
            frame, textvariable=self.port_var, values=available_ports, state="readonly", width=20
        )
        combo.grid(row=0, column=1, sticky="ew", pady=(0, 6))
        if available_ports:
            combo.set(available_ports[0])

        ttk.Label(frame, text="Animal ID").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(0, 12))
        self.animal_var = tk.StringVar()
        ttk.Entry(frame, textvariable=self.animal_var, width=20).grid(row=1, column=1, sticky="ew", pady=(0, 12))

        buttons = ttk.Frame(frame)
        buttons.grid(row=2, column=0, columnspan=2, sticky="e")
        ttk.Button(buttons, text="Cancel", command=self.destroy).pack(side="right", padx=(4, 0))
        wrap, _btn = accent_button(buttons, "Add", self._confirm, theme.GREEN)
        wrap.pack(side="right")

        self.bind("<Return>", lambda _: self._confirm())
        self.bind("<Escape>", lambda _: self.destroy())
        self.transient(master)
        self.wait_window(self)

    def _confirm(self) -> None:
        port = self.port_var.get()
        if not port:
            messagebox.showerror("No port", "Select a serial port.", parent=self)
            return
        self.result_port = port
        self.result_animal_id = self.animal_var.get().strip()
        self.destroy()


class DevicePanel(ttk.Frame):
    """Per-device panel: plot, log, controls, and recording. One instance per notebook tab."""

    def __init__(
        self,
        master: tk.Misc,
        port: str,
        animal_id: str,
        shared: SharedConfig,
    ) -> None:
        super().__init__(master)
        self.port = port
        self.animal_id_var = tk.StringVar(value=animal_id)
        self.shared = shared
        self._alive = True

        self.worker = SerialWorker()
        self.csv_file = None
        self.csv_writer = None
        self.frame_count = 0

        self.connection_var = tk.StringVar(value="Connecting...")
        self.recording_var = tk.StringVar(value="Not recording")
        self.latest_var = tk.StringVar(value="No samples received")
        self.vis_gain_var = tk.IntVar(value=0)
        self.ir_gain_var = tk.IntVar(value=0)
        self.vis_dac_var = tk.IntVar(value=0)
        self.vis_current_label_var = tk.StringVar(value="")
        self.vis_dac_var.trace_add("write", self._update_vis_current_label)
        self.pulse_var = tk.IntVar(value=500)
        self.decimation_var = tk.IntVar(value=10)
        self.sample_rate_var = tk.IntVar(value=100)
        self.duration_var = tk.IntVar(value=0)

        self._recording_start: float | None = None
        self._recording_duration: int = 0
        self._schedule: list[tuple[int, int]] = []
        self._sched_status_var = tk.StringVar(value="No steps")
        self._sched_dialog: ScheduleEditorDialog | None = None

        self._build_ui()
        self._update_vis_current_label()
        self.worker.connect(port)
        self.after(30, self._poll_serial)

    def _build_ui(self) -> None:
        self.columnconfigure(1, weight=1)
        self.rowconfigure(0, weight=1)

        # --- Left controls column (scrollable) ---
        scroll = _ScrollFrame(self)
        scroll.grid(row=0, column=0, sticky="ns", padx=(8, 0), pady=8)
        controls = scroll.inner

        ttk.Label(controls, text="Animal ID").pack(anchor="w", pady=(0, 2))
        ttk.Entry(controls, textvariable=self.animal_id_var, width=18).pack(fill="x", pady=(0, 8))

        outer, body = collapsible_section(controls, "CONNECTION", theme.BLUE, start_open=True)
        outer.pack(fill="x", pady=(0, 8))
        tk.Label(body, text=f"Port: {self.port}", background=theme.PANEL, foreground=theme.FG, font=("Consolas", 9)).pack(anchor="w")
        tk.Label(body, textvariable=self.connection_var, background=theme.PANEL, foreground=theme.FG_MUTED).pack(anchor="w", pady=(4, 0))
        self._connect_wrap, self.connect_button = accent_button(body, "Disconnect", self.toggle_connection, theme.RED)
        self._connect_wrap.pack(fill="x", pady=(6, 0))

        outer, body = collapsible_section(controls, "OPERATION", theme.BLUE, start_open=True)
        outer.pack(fill="x", pady=(0, 8))
        wrap, _btn = accent_button(body, "Start device", lambda: self.send(0, 1), theme.GREEN)
        wrap.pack(fill="x", pady=(0, 4))
        wrap, _btn = accent_button(body, "Stop device", lambda: self.send(0, 0), theme.RED)
        wrap.pack(fill="x", pady=(0, 4))
        ttk.Button(body, text="Read status", command=lambda: self.send(7, 0)).pack(fill="x", pady=(0, 4))
        ttk.Button(body, text="Single ADC read", command=lambda: self.send(8, 0)).pack(fill="x")

        outer, body = collapsible_section(controls, "RECORDING", theme.BLUE, start_open=True)
        outer.pack(fill="x", pady=(0, 8))
        self._compact_row(body, "Duration (s, 0=unlim.)", self.duration_var, 0, 86400, 10).pack(fill="x")

        outer, body = collapsible_section(controls, "VIS SCHEDULE", theme.PURPLE, start_open=True)
        outer.pack(fill="x", pady=(0, 8))
        tk.Label(body, textvariable=self._sched_status_var, background=theme.PANEL, foreground=theme.FG_MUTED).pack(anchor="w", pady=(0, 6))
        wrap, _btn = accent_button(body, "Edit schedule...", self._open_schedule_editor, theme.PURPLE)
        wrap.pack(fill="x")

        outer, body = collapsible_section(controls, "OUTPUTS AND GAINS", theme.BLUE, start_open=False)
        outer.pack(fill="x", pady=(0, 8))
        self._compact_row(
            body, "Visible PD gain (0-255)", self.vis_gain_var, 0, 255, 1,
            lambda: self.send(1, self.vis_gain_var.get()), theme.BLUE,
        ).pack(fill="x", pady=(0, 4))
        self._compact_row(
            body, "IR PD gain (0-255)", self.ir_gain_var, 0, 255, 1,
            lambda: self.send(2, self.ir_gain_var.get()), theme.BLUE,
        ).pack(fill="x", pady=(0, 4))
        self._compact_row(
            body, "VIS LED DAC (0-4095)", self.vis_dac_var, 0, 4095, 1,
            lambda: self.send(3, self.vis_dac_var.get()), theme.BLUE, extra_var=self.vis_current_label_var,
        ).pack(fill="x", pady=(0, 4))
        self._compact_row(
            body, "IR pulse (us)", self.pulse_var, 0, 1_000_000, 100,
            lambda: self.send(4, self.pulse_var.get()), theme.BLUE,
        ).pack(fill="x")

        outer, body = collapsible_section(controls, "STREAMING", theme.BLUE, start_open=False)
        outer.pack(fill="x", pady=(0, 8))
        self._compact_row(
            body, "Sample rate (Hz, 10-250)", self.sample_rate_var, 10, 250, 1,
            lambda: self.send(11, self.sample_rate_var.get()), theme.BLUE,
        ).pack(fill="x", pady=(0, 4))
        self._compact_row(
            body, "Decimation", self.decimation_var, 1, 65535, 1,
            lambda: self.send(10, self.decimation_var.get()), theme.BLUE,
        ).pack(fill="x", pady=(0, 6))
        stream_btns = tk.Frame(body, background=theme.PANEL)
        stream_btns.pack(fill="x")
        stream_btns.columnconfigure((0, 1), weight=1)
        wrap, _btn = accent_button(stream_btns, "Enable stream", lambda: self.send(9, 1), theme.GREEN)
        wrap.grid(row=0, column=0, sticky="ew", padx=(0, 3))
        wrap, _btn = accent_button(stream_btns, "Disable stream", lambda: self.send(9, 0), theme.RED)
        wrap.grid(row=0, column=1, sticky="ew", padx=(3, 0))

        # --- Right: plot + log ---
        main = ttk.Frame(self, padding=(8, 8, 8, 8))
        main.grid(row=0, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(0, weight=3)
        main.rowconfigure(2, weight=2)

        self.plot = PlotCanvas(main)
        self.plot.grid(row=0, column=0, sticky="nsew")

        plot_actions = ttk.Frame(main, padding=(0, 6))
        plot_actions.grid(row=1, column=0, sticky="ew")
        ttk.Label(plot_actions, textvariable=self.latest_var).pack(side="left")
        ttk.Label(plot_actions, text="Plot 3:").pack(side="left", padx=(12, 4))
        self.third_channel_var = tk.StringVar(value=PlotCanvas.third_channel_choices()[0])
        third_channel_combo = ttk.Combobox(
            plot_actions,
            textvariable=self.third_channel_var,
            values=PlotCanvas.third_channel_choices(),
            state="readonly",
            width=26,
        )
        third_channel_combo.pack(side="left")
        third_channel_combo.bind(
            "<<ComboboxSelected>>",
            lambda _event: self.plot.set_third_channel_by_label(self.third_channel_var.get()),
        )
        ttk.Label(plot_actions, text="Window (s):").pack(side="left", padx=(12, 4))
        self.plot_window_var = tk.IntVar(value=int(PLOT_WINDOW_S))
        ttk.Spinbox(
            plot_actions, textvariable=self.plot_window_var, from_=2, to=300, increment=1, width=5
        ).pack(side="left")
        self.plot_window_var.trace_add("write", self._on_plot_window_change)
        ttk.Button(plot_actions, text="Clear plot", command=self.plot.clear).pack(side="right")
        self._record_wrap, self.record_button = accent_button(
            plot_actions, "Start recording", self.toggle_recording, theme.GREEN
        )
        self._record_wrap.pack(side="right", padx=6)
        self.recording_label = ttk.Label(plot_actions, textvariable=self.recording_var, style="Muted.TLabel")
        self.recording_label.pack(side="right")

        self.log = ScrolledText(
            main,
            height=12,
            state="disabled",
            font=("Consolas", 9),
            background=theme.PANEL,
            foreground=theme.FG,
            insertbackground=theme.FG,
            selectbackground=theme.BLUE,
            selectforeground="#f9fafb",
            relief="flat",
            borderwidth=0,
            highlightthickness=1,
            highlightbackground=theme.BORDER,
            highlightcolor=theme.BLUE,
        )
        self.log.vbar.configure(
            background=theme.PANEL,
            troughcolor=theme.BG,
            activebackground=theme.BORDER,
            highlightthickness=0,
            borderwidth=0,
        )
        self.log.grid(row=2, column=0, sticky="nsew")

    @staticmethod
    def _compact_row(
        parent: tk.Misc,
        label: str,
        variable: tk.Variable,
        minimum: int | float,
        maximum: int | float,
        increment: int | float,
        command: object = None,
        accent: str | None = None,
        extra_var: tk.StringVar | None = None,
    ) -> tk.Frame:
        """One row: label, spinbox, optional live readout, optional Apply button — replaces the old
        stacked label/spinbox/button layout to keep the sidebar from running on so long."""
        row = tk.Frame(parent, background=theme.PANEL)
        tk.Label(row, text=label, background=theme.PANEL, foreground=theme.FG, anchor="w", width=22).grid(
            row=0, column=0, sticky="w"
        )
        spin = ttk.Spinbox(row, textvariable=variable, from_=minimum, to=maximum, increment=increment, width=8)
        spin.grid(row=0, column=1, sticky="w", padx=(6, 6))
        spin.set(str(variable.get()))
        col = 2
        if extra_var is not None:
            tk.Label(row, textvariable=extra_var, background=theme.PANEL, foreground=theme.FG_MUTED, width=10, anchor="w").grid(
                row=0, column=col, sticky="w", padx=(0, 6)
            )
            col += 1
        if command is not None:
            wrap, _btn = accent_button(row, "Apply", command, accent)
            wrap.grid(row=0, column=col, sticky="w")
        return row

    def toggle_connection(self) -> None:
        if self.worker.connected:
            self._disconnect_sequence()
        else:
            self.connection_var.set("Connecting...")
            self.connect_button.configure(text="Disconnect")
            self._connect_wrap.configure(background=theme.RED)
            self.worker.connect(self.port)

    def _disconnect_sequence(self) -> None:
        """Reset GUI to defaults, send those defaults to hardware, stop device, then disconnect."""
        self._reset_controls()
        # Send defaults then stop; break on first failure (port already gone).
        for cmd_id, val in [(1, 0), (2, 0), (3, 0), (11, 100), (0, 0)]:
            try:
                self.worker.send(command(cmd_id, val))
            except RuntimeError:
                break
        self.worker.disconnect()

    def send_raw(self, text: str) -> bool:
        try:
            self.worker.send(text)
            self._append_log(f"> {text.strip()}")
            return True
        except RuntimeError as exc:
            messagebox.showerror("Not connected", str(exc))
            return False

    def send(self, command_id: int, value: int) -> bool:
        try:
            self.worker.send(command(command_id, value))
            self._append_log(f"> {command_id},{value}")
            event_fn = _EVENT_COMMANDS.get(command_id)
            if event_fn is not None:
                self._record_event(event_fn(value))
            return True
        except RuntimeError as exc:
            messagebox.showerror("Not connected", str(exc))
            return False

    def _open_schedule_editor(self) -> None:
        if self._sched_dialog is not None and self._sched_dialog.winfo_exists():
            self._sched_dialog.lift()
            return
        self._sched_dialog = ScheduleEditorDialog(self.winfo_toplevel(), self)

    def _upload_schedule(self) -> bool:
        """Send CMD 12 (clear) + CMD 13 for each step in insertion order. Durations become cumulative absolute times."""
        if not self.worker.connected:
            messagebox.showerror("Not connected", "Connect to a device first.")
            return False
        self.send_raw(command_schedule_clear())
        cumulative_t = 0
        for duration_s, dac_code in self._schedule:
            self.send_raw(command_schedule_step(cumulative_t, dac_code))
            cumulative_t += duration_s
        n = len(self._schedule)
        total_s = sum(d for d, _ in self._schedule)
        self._sched_status_var.set(
            f"{n} step{'s' if n != 1 else ''} ({total_s}s total) uploaded" if n else "No steps"
        )
        if n:
            self._record_event(f"VIS schedule uploaded: {n} step(s), {total_s}s total")
        return True

    def _update_vis_current_label(self, *_args: object) -> None:
        try:
            dac_code = self.vis_dac_var.get()
        except tk.TclError:
            return
        self.vis_current_label_var.set(f"= {vis_dac_code_to_current_ma(dac_code):.2f} mA")

    def _on_plot_window_change(self, *_args: object) -> None:
        try:
            seconds = self.plot_window_var.get()
        except tk.TclError:
            return
        if seconds <= 0:
            return
        self.plot.set_window_s(seconds)

    def _poll_serial(self) -> None:
        if not self._alive:
            return
        try:
            while True:
                item = self.worker.incoming.get_nowait()
                if isinstance(item, ConnectionEvent):
                    self.connection_var.set(item.detail)
                    self.connect_button.configure(text="Disconnect" if item.connected else "Connect")
                    self._connect_wrap.configure(background=theme.RED if item.connected else theme.GREEN)
                    if not item.connected:
                        self.stop_recording()
                        self._reset_controls()
                else:
                    self._handle_line(item)
        except queue.Empty:
            pass
        self._update_recording_status()
        self.after(30, self._poll_serial)

    def _update_recording_status(self) -> None:
        if self.csv_file is None or self._recording_start is None:
            return
        elapsed = time.time() - self._recording_start
        if self._recording_duration > 0:
            if elapsed >= self._recording_duration:
                self._record_event("Recording auto-stopped")
                self.stop_recording()
            else:
                self.recording_var.set(
                    f"Recording — {int(elapsed)}s / {self._recording_duration}s"
                )
        else:
            self.recording_var.set(f"Recording — {int(elapsed)}s elapsed")

    def _handle_line(self, line: str) -> None:
        parsed = parse_line(line)
        if isinstance(parsed, DataFrame):
            host_time = time.time()
            self.frame_count += 1
            self.plot.add_frame(parsed, host_time)
            ir_v = raw_to_volts(parsed.high[0])
            vis_v = raw_to_volts(parsed.high[1])
            self.latest_var.set(
                f"Sample {parsed.sample_counter} | IR: {ir_v:.4f} V   VIS: {vis_v:.4f} V"
            )
            self._record_frame(parsed)
        elif isinstance(parsed, Status):
            self._sync_status(parsed)
            summary = ", ".join(f"{k}={v}" for k, v in parsed.values.items())
            self._append_log(f"STATUS: {summary}")
        elif isinstance(parsed, SingleRead):
            values = ", ".join(f"{v:g}" for v in parsed.values)
            self._append_log(f"{parsed.kind}: {values}")
        elif isinstance(parsed, SchedEvent):
            host_time = time.time()
            desc = (
                f"VIS schedule: t={parsed.time_s}s → DAC={parsed.dac_code}"
                f" ({vis_dac_code_to_current_ma(parsed.dac_code):.2f} mA)"
            )
            self._append_log(desc)
            self._record_event(desc)
            self.plot.add_sched_event(parsed.dac_code, host_time)
        elif isinstance(parsed, Message) and parsed.text:
            self._append_log(parsed.text)

    def _sync_status(self, status: Status) -> None:
        mapping = {
            "streamDecimation": self.decimation_var,
            "sampleRateHz": self.sample_rate_var,
            "visDac": self.vis_dac_var,
            "visibleGain": self.vis_gain_var,
            "irGain": self.ir_gain_var,
        }
        for key, var in mapping.items():
            try:
                var.set(int(status.values[key]))
            except (KeyError, ValueError):
                continue

    def _append_log(self, text: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", f"[{timestamp}] {text}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _reset_controls(self) -> None:
        """Reset all control variables to power-on defaults after disconnect."""
        self.vis_gain_var.set(0)
        self.ir_gain_var.set(0)
        self.vis_dac_var.set(0)
        self.pulse_var.set(500)
        self.decimation_var.set(10)
        self.sample_rate_var.set(100)
        self.frame_count = 0
        self.latest_var.set("No samples received")
        self.plot.clear()

    def toggle_recording(self) -> None:
        if self.csv_file is not None:
            self.stop_recording()
        else:
            self.start_recording()

    def start_recording(self, path: str | None = None) -> bool:
        """Start a CSV recording. Prompts for a file if path is not given. Returns True on success."""
        if self.csv_file is not None:
            return True
        if path is None:
            cohort = self.shared.cohort_var.get() or "cohort"
            test = self.shared.test_var.get() or "test"
            animal = self.animal_id_var.get() or "animal"
            suggested = f"{cohort}_{animal}_{test}_{time.strftime('%Y%m%d_%H%M%S')}.csv"
            path = filedialog.asksaveasfilename(
                title="Save recording",
                defaultextension=".csv",
                filetypes=[("CSV files", "*.csv")],
                initialfile=suggested,
            )
        if not path:
            return False

        self._recording_start = time.time()
        self._recording_duration = self.duration_var.get()

        self.csv_file = Path(path).open("w", newline="", encoding="utf-8")
        self.csv_writer = csv.writer(self.csv_file)
        self.csv_writer.writerow(
            ["host_time", "sample_counter"]
            + [f"high_ch{i}" for i in range(1, 5)]
            + [f"low_ch{i}" for i in range(1, 5)]
            + [f"difference_ch{i}" for i in range(1, 5)]
            + ["event"]
        )
        self._write_metadata_sidecar(path)
        self._record_event("Recording started")

        if self._schedule and self.worker.connected:
            self.send_raw(command_schedule_start())

        dur = self._recording_duration
        dur_label = f"{dur}s" if dur else "unlimited"
        self.recording_var.set(f"Recording — 0s / {dur_label}" if dur else "Recording — 0s elapsed")
        self.recording_label.configure(style="Recording.TLabel")
        self.record_button.configure(text="Stop recording")
        self._record_wrap.configure(background=theme.RED)
        return True

    def stop_recording(self) -> None:
        if self._schedule and self.worker.connected:
            try:
                self.worker.send(command_schedule_stop())
            except RuntimeError:
                pass
        if self.csv_file is not None:
            self._record_event("Recording stopped")
            self.csv_file.close()
        self.csv_file = None
        self.csv_writer = None
        self._recording_start = None
        self._recording_duration = 0
        self.recording_var.set("Not recording")
        self.recording_label.configure(style="Muted.TLabel")
        self.record_button.configure(text="Start recording")
        self._record_wrap.configure(background=theme.GREEN)

    def _record_frame(self, frame: DataFrame) -> None:
        if self.csv_writer is None:
            return
        self.csv_writer.writerow(
            [time.time(), frame.sample_counter, *frame.high, *frame.low, *frame.difference, ""]
        )
        self.csv_file.flush()

    def _record_event(self, description: str) -> None:
        t = time.time()
        self.plot.add_event(description, t)
        if self.csv_writer is None:
            return
        self.csv_writer.writerow([t] + [""] * 13 + [description])
        self.csv_file.flush()

    def _write_metadata_sidecar(self, csv_path: str) -> None:
        meta = {
            "recording_start": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "cohort": self.shared.cohort_var.get(),
            "test": self.shared.test_var.get(),
            "animal_id": self.animal_id_var.get(),
            "port": self.port,
            "sample_rate_hz": self.sample_rate_var.get(),
            "stream_decimation": self.decimation_var.get(),
            "vis_gain": self.vis_gain_var.get(),
            "ir_gain": self.ir_gain_var.get(),
            "vis_dac_code": self.vis_dac_var.get(),
            "vis_led_ma": round(vis_dac_code_to_current_ma(self.vis_dac_var.get()), 3),
            "recording_duration_s": self._recording_duration or None,
            "vis_schedule": [
                {"time_s": t, "dac_code": d}
                for t, d in sorted(self._schedule, key=lambda s: s[0])
            ],
        }
        sidecar = Path(csv_path).with_suffix(".json")
        with sidecar.open("w", encoding="utf-8") as f:
            json.dump(meta, f, indent=2)

    def teardown(self) -> None:
        """Stop polling, recording, and serial connection cleanly."""
        self._alive = False
        self.stop_recording()
        if self.worker.connected:
            self._disconnect_sequence()
        else:
            self.worker.disconnect()


class ScheduleEditorDialog(tk.Toplevel):
    """Non-modal dialog for editing the VIS light schedule for one DevicePanel."""

    def __init__(self, master: tk.Misc, panel: DevicePanel) -> None:
        super().__init__(master)
        self.configure(background=theme.BG)
        self.panel = panel
        self.title(f"VIS Light Schedule — {panel.port}")
        self.resizable(True, True)
        self.geometry("490x500")
        self.minsize(420, 380)
        self.transient(master)

        self._build_ui()
        self._refresh_tree()

    def _build_ui(self) -> None:
        tree_frame = ttk.Frame(self, padding=(8, 8, 8, 4))
        tree_frame.pack(fill="both", expand=True)
        tree_frame.columnconfigure(0, weight=1)
        tree_frame.rowconfigure(0, weight=1)

        cols = ("time_s", "dac", "ma")
        self._tree = ttk.Treeview(
            tree_frame, columns=cols, show="headings", height=10, selectmode="browse"
        )
        self._tree.heading("time_s", text="Duration (s)")
        self._tree.heading("dac", text="VIS DAC code")
        self._tree.heading("ma", text="Current (mA)")
        self._tree.column("time_s", width=90, anchor="center")
        self._tree.column("dac", width=110, anchor="center")
        self._tree.column("ma", width=110, anchor="center")

        sb = ttk.Scrollbar(tree_frame, orient="vertical", command=self._tree.yview)
        self._tree.configure(yscrollcommand=sb.set)
        self._tree.grid(row=0, column=0, sticky="nsew")
        sb.grid(row=0, column=1, sticky="ns")
        self._tree.bind("<<TreeviewSelect>>", self._on_select)

        entry_frame = ttk.LabelFrame(self, text="Add / edit step", padding=8)
        entry_frame.pack(fill="x", padx=8, pady=4)
        entry_frame.columnconfigure(1, weight=1)
        entry_frame.columnconfigure(3, weight=1)

        ttk.Label(entry_frame, text="Duration (s):").grid(row=0, column=0, sticky="w", padx=(0, 4))
        self._time_var = tk.StringVar(value="1")
        ttk.Spinbox(
            entry_frame, textvariable=self._time_var, from_=1, to=86400, increment=1, width=8
        ).grid(row=0, column=1, sticky="ew", padx=(0, 12))

        ttk.Label(entry_frame, text="DAC code (0-4095):").grid(row=0, column=2, sticky="w", padx=(0, 4))
        self._dac_var = tk.StringVar(value="0")
        ttk.Spinbox(
            entry_frame, textvariable=self._dac_var, from_=0, to=4095, increment=1, width=8
        ).grid(row=0, column=3, sticky="ew", padx=(0, 8))
        self._ma_label_var = tk.StringVar(value="= 0.00 mA")
        ttk.Label(entry_frame, textvariable=self._ma_label_var).grid(row=0, column=4, sticky="w")
        self._dac_var.trace_add("write", self._update_ma_label)

        btn_frame = ttk.Frame(entry_frame)
        btn_frame.grid(row=1, column=0, columnspan=5, pady=(6, 0), sticky="w")
        ttk.Button(btn_frame, text="Add", command=self._add_step, width=9).pack(side="left", padx=(0, 4))
        ttk.Button(btn_frame, text="Update", command=self._update_step, width=9).pack(side="left", padx=(0, 4))
        ttk.Button(btn_frame, text="Remove", command=self._remove_step, width=9).pack(side="left", padx=(0, 4))
        ttk.Button(btn_frame, text="Clear all", command=self._clear_all, width=9).pack(side="left")

        ttk.Label(
            self,
            text="Each step holds for its duration, then the next step starts. Schedule begins when recording starts. Upload before recording.",
            style="Muted.TLabel",
            wraplength=450,
        ).pack(padx=8, pady=(0, 4))

        bottom = ttk.Frame(self, padding=(8, 4, 8, 8))
        bottom.pack(fill="x")
        wrap, _btn = accent_button(bottom, "Upload to device", self._upload, theme.PURPLE)
        wrap.pack(side="left")
        ttk.Button(bottom, text="Close", command=self.destroy).pack(side="right")

    def _refresh_tree(self) -> None:
        for row in self._tree.get_children():
            self._tree.delete(row)
        for duration_s, dac_code in self.panel._schedule:
            ma = vis_dac_code_to_current_ma(dac_code)
            self._tree.insert("", "end", values=(duration_s, dac_code, f"{ma:.2f}"))
        n = len(self.panel._schedule)
        total_s = sum(d for d, _ in self.panel._schedule)
        self.panel._sched_status_var.set(
            f"{n} step{'s' if n != 1 else ''} ({total_s}s total)" if n else "No steps"
        )

    def _on_select(self, _event: tk.Event) -> None:
        sel = self._tree.selection()
        if not sel:
            return
        vals = self._tree.item(sel[0], "values")
        self._time_var.set(vals[0])
        self._dac_var.set(vals[1])

    def _update_ma_label(self, *_args: object) -> None:
        try:
            dac = int(self._dac_var.get())
        except (ValueError, tk.TclError):
            self._ma_label_var.set("= — mA")
            return
        self._ma_label_var.set(f"= {vis_dac_code_to_current_ma(dac):.2f} mA")

    def _parse_fields(self) -> tuple[int, int] | None:
        try:
            time_s = int(self._time_var.get())
            dac = int(self._dac_var.get())
        except (ValueError, tk.TclError):
            messagebox.showerror("Invalid input", "Time and DAC code must be whole numbers.", parent=self)
            return None
        if time_s <= 0:
            messagebox.showerror("Invalid input", "Duration must be > 0 seconds.", parent=self)
            return None
        if not 0 <= dac <= 4095:
            messagebox.showerror("Invalid input", "DAC code must be 0–4095.", parent=self)
            return None
        return time_s, dac

    def _add_step(self) -> None:
        parsed = self._parse_fields()
        if parsed is None:
            return
        self.panel._schedule.append(parsed)
        self._refresh_tree()

    def _update_step(self) -> None:
        sel = self._tree.selection()
        if not sel:
            messagebox.showinfo("No selection", "Select a step to update.", parent=self)
            return
        parsed = self._parse_fields()
        if parsed is None:
            return
        vals = self._tree.item(sel[0], "values")
        old = (int(vals[0]), int(vals[1]))
        try:
            self.panel._schedule.remove(old)
        except ValueError:
            pass
        self.panel._schedule.append(parsed)
        self._refresh_tree()

    def _remove_step(self) -> None:
        sel = self._tree.selection()
        if not sel:
            return
        vals = self._tree.item(sel[0], "values")
        try:
            self.panel._schedule.remove((int(vals[0]), int(vals[1])))
        except ValueError:
            pass
        self._refresh_tree()

    def _clear_all(self) -> None:
        if self.panel._schedule and not messagebox.askyesno(
            "Clear schedule", "Remove all steps?", parent=self
        ):
            return
        self.panel._schedule.clear()
        self._refresh_tree()

    def _upload(self) -> None:
        self.panel._upload_schedule()


class DaqApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Pupil DAQ Control")
        self.geometry("1280x820")
        self.minsize(960, 720)

        theme.apply(self)

        self.shared = SharedConfig()
        self.panels: list[DevicePanel] = []
        self._placeholder_tab: ttk.Frame | None = None

        self._build_ui()
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        toolbar = ttk.Frame(self, padding=(8, 6, 8, 6))
        toolbar.pack(fill="x", side="top")

        ttk.Label(toolbar, text="Cohort:").pack(side="left")
        ttk.Entry(toolbar, textvariable=self.shared.cohort_var, width=14).pack(side="left", padx=(2, 10))
        ttk.Label(toolbar, text="Test:").pack(side="left")
        ttk.Entry(toolbar, textvariable=self.shared.test_var, width=14).pack(side="left", padx=(2, 10))

        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=8)

        wrap, _btn = accent_button(toolbar, "Add device", self._add_device, theme.BLUE)
        wrap.pack(side="left", padx=(0, 4))
        wrap, _btn = accent_button(toolbar, "Remove device", self._remove_device, theme.RED)
        wrap.pack(side="left", padx=(0, 8))

        ttk.Separator(toolbar, orient="vertical").pack(side="left", fill="y", padx=8)

        wrap, _btn = accent_button(toolbar, "Start all", self._start_all, theme.GREEN)
        wrap.pack(side="left", padx=(0, 4))
        wrap, _btn = accent_button(toolbar, "Stop all", self._stop_all, theme.RED)
        wrap.pack(side="left")

        ttk.Separator(self, orient="horizontal").pack(fill="x")

        self.notebook = ttk.Notebook(self)
        self.notebook.pack(fill="both", expand=True, padx=4, pady=4)

        placeholder = ttk.Frame(self.notebook)
        ttk.Label(
            placeholder,
            text='Click "Add device" to connect a DAQ board.',
            style="Muted.TLabel",
        ).pack(expand=True)
        self.notebook.add(placeholder, text="No devices")
        self._placeholder_tab = placeholder

    def _add_device(self) -> None:
        all_ports = SerialWorker.available_ports()
        used = {panel.port for panel in self.panels}
        available = [p for p in all_ports if p not in used] or all_ports

        dialog = AddDeviceDialog(self, available)
        if dialog.result_port is None:
            return

        if self._placeholder_tab is not None:
            self.notebook.forget(self._placeholder_tab)
            self._placeholder_tab = None

        port = dialog.result_port
        animal_id = dialog.result_animal_id

        panel = DevicePanel(self.notebook, port=port, animal_id=animal_id, shared=self.shared)
        self.notebook.add(panel, text=animal_id if animal_id else port)
        self.notebook.select(panel)
        self.panels.append(panel)

        def _on_animal_change(*_: object) -> None:
            try:
                self.notebook.tab(panel, text=panel.animal_id_var.get() or panel.port)
            except tk.TclError:
                pass

        panel.animal_id_var.trace_add("write", _on_animal_change)

    def _remove_device(self) -> None:
        selected = self.notebook.select()
        if not selected:
            return

        panel = next((p for p in self.panels if str(p) == selected), None)
        if panel is None:
            return

        label = panel.animal_id_var.get() or panel.port
        if panel.csv_file is not None:
            ok = messagebox.askyesno(
                "Remove device",
                f"A recording is active on {label}.\nStop recording and remove this device?",
            )
        else:
            ok = messagebox.askyesno("Remove device", f"Remove {label} from the session?")
        if not ok:
            return

        panel.teardown()
        self.notebook.forget(panel)
        self.panels.remove(panel)

        if not self.panels:
            placeholder = ttk.Frame(self.notebook)
            ttk.Label(
                placeholder,
                text='Click "Add device" to connect a DAQ board.',
                style="Muted.TLabel",
            ).pack(expand=True)
            self.notebook.add(placeholder, text="No devices")
            self._placeholder_tab = placeholder

    def _start_all(self) -> None:
        """Ask for a save directory, auto-generate filenames, then start device + stream on all connected panels."""
        connected = [p for p in self.panels if p.worker.connected]
        if not connected:
            messagebox.showinfo("No devices", "No devices are connected.")
            return
        save_dir = filedialog.askdirectory(title="Choose save directory for all recordings")
        if not save_dir:
            return
        cohort = self.shared.cohort_var.get() or "cohort"
        test = self.shared.test_var.get() or "test"
        stamp = time.strftime("%Y%m%d_%H%M%S")
        for panel in connected:
            animal = panel.animal_id_var.get() or panel.port
            path = str(Path(save_dir) / f"{cohort}_{animal}_{test}_{stamp}.csv")
            panel._upload_schedule()   # clear + upload steps
            panel.send(0, 1)           # start device
            panel.send(9, 1)           # enable stream
            panel.start_recording(path=path)   # opens CSV, then sends cmd 14 to start schedule

    def _stop_all(self) -> None:
        for panel in self.panels:
            if panel.worker.connected:
                panel.send(0, 0)
            panel.stop_recording()

    def _on_close(self) -> None:
        for panel in self.panels:
            panel.teardown()
        self.destroy()


def main() -> None:
    app = DaqApp()
    app.mainloop()


if __name__ == "__main__":
    main()
