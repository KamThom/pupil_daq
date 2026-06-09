from __future__ import annotations

import csv
import queue
import time
import tkinter as tk
from collections import deque
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText

from daq_gui.protocol import DataFrame, Message, SingleRead, Status, command, parse_line
from daq_gui.serial_worker import ConnectionEvent, SerialWorker


CHANNEL_COLORS = ("#3b82f6", "#ef4444", "#22c55e", "#a855f7")
PLOT_POINTS = 500


class PlotCanvas(tk.Canvas):
    def __init__(self, master: tk.Misc, **kwargs: object) -> None:
        super().__init__(master, background="#111827", highlightthickness=0, **kwargs)
        self.series = [deque(maxlen=PLOT_POINTS) for _ in range(4)]
        self.bind("<Configure>", lambda _event: self.redraw())

    def add_frame(self, frame: DataFrame) -> None:
        for series, value in zip(self.series, frame.difference):
            series.append(value)
        self.redraw()

    def clear(self) -> None:
        for series in self.series:
            series.clear()
        self.redraw()

    def redraw(self) -> None:
        self.delete("all")
        width = max(self.winfo_width(), 2)
        height = max(self.winfo_height(), 2)
        margin = 34
        self.create_line(margin, height / 2, width, height / 2, fill="#374151")
        self.create_text(5, 8, text="High - Low (raw ADC counts)", anchor="nw", fill="#9ca3af")

        all_values = [value for series in self.series for value in series]
        if not all_values:
            self.create_text(width / 2, height / 2, text="No stream data", fill="#6b7280")
            return

        limit = max(max(abs(value) for value in all_values), 1)
        usable_width = max(width - margin, 1)
        usable_height = max(height - 30, 1)

        for index, (series, color) in enumerate(zip(self.series, CHANNEL_COLORS), start=1):
            self.create_text(
                margin + (index - 1) * 62,
                8,
                text=f"CH{index}",
                anchor="nw",
                fill=color,
            )
            if len(series) < 2:
                continue
            points: list[float] = []
            denominator = max(len(series) - 1, 1)
            for sample_index, value in enumerate(series):
                x = margin + sample_index / denominator * usable_width
                y = height / 2 - value / limit * usable_height * 0.45
                points.extend((x, y))
            self.create_line(*points, fill=color, width=2)


class DaqApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Pupil DAQ Control")
        self.geometry("1180x760")
        self.minsize(960, 640)

        self.worker = SerialWorker()
        self.csv_file = None
        self.csv_writer = None
        self.frame_count = 0

        self.port_var = tk.StringVar()
        self.connection_var = tk.StringVar(value="Disconnected")
        self.recording_var = tk.StringVar(value="Not recording")
        self.latest_var = tk.StringVar(value="No samples received")
        self.vis_gain_var = tk.IntVar(value=0)
        self.ir_gain_var = tk.IntVar(value=0)
        self.vis_dac_var = tk.IntVar(value=0)
        self.pulse_var = tk.IntVar(value=500)
        self.decimation_var = tk.IntVar(value=100)

        self._build_ui()
        self.refresh_ports()
        self.after(30, self._poll_serial)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)
        root.columnconfigure(1, weight=1)
        root.rowconfigure(1, weight=1)

        connection = ttk.LabelFrame(root, text="Connection", padding=8)
        connection.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        connection.columnconfigure(1, weight=1)
        ttk.Label(connection, text="Serial port").grid(row=0, column=0, padx=(0, 6))
        self.port_combo = ttk.Combobox(connection, textvariable=self.port_var, state="readonly")
        self.port_combo.grid(row=0, column=1, sticky="ew")
        ttk.Button(connection, text="Refresh", command=self.refresh_ports).grid(row=0, column=2, padx=6)
        self.connect_button = ttk.Button(connection, text="Connect", command=self.toggle_connection)
        self.connect_button.grid(row=0, column=3)
        ttk.Label(connection, textvariable=self.connection_var).grid(row=0, column=4, padx=(12, 0))

        controls = ttk.Frame(root)
        controls.grid(row=1, column=0, sticky="nsw", padx=(0, 8))

        operation = ttk.LabelFrame(controls, text="Operation", padding=8)
        operation.pack(fill="x", pady=(0, 8))
        ttk.Button(operation, text="Start device", command=lambda: self.send(0, 1)).pack(fill="x")
        ttk.Button(operation, text="Stop device", command=lambda: self.send(0, 0)).pack(fill="x", pady=4)
        ttk.Button(operation, text="Read status", command=lambda: self.send(7, 0)).pack(fill="x")
        ttk.Button(operation, text="Single ADC read", command=lambda: self.send(8, 0)).pack(fill="x", pady=(4, 0))

        settings = ttk.LabelFrame(controls, text="Outputs and gains", padding=8)
        settings.pack(fill="x", pady=(0, 8))
        self._number_control(settings, "Visible PD gain", self.vis_gain_var, 0, 255, 1)
        ttk.Button(settings, text="Apply visible gain", command=lambda: self.send(1, self.vis_gain_var.get())).pack(fill="x")
        self._number_control(settings, "IR PD gain", self.ir_gain_var, 0, 255, 1)
        ttk.Button(settings, text="Apply IR gain", command=lambda: self.send(2, self.ir_gain_var.get())).pack(fill="x")
        self._number_control(settings, "Visible LED DAC", self.vis_dac_var, 0, 4095, 10)
        ttk.Button(settings, text="Apply visible LED", command=lambda: self.send(3, self.vis_dac_var.get())).pack(fill="x")
        self._number_control(settings, "IR pulse (us)", self.pulse_var, 0, 1_000_000, 100)
        ttk.Button(settings, text="Pulse IR LED", command=lambda: self.send(4, self.pulse_var.get())).pack(fill="x")

        stream = ttk.LabelFrame(controls, text="Streaming", padding=8)
        stream.pack(fill="x")
        self._number_control(stream, "Decimation", self.decimation_var, 1, 65535, 1)
        ttk.Button(stream, text="Apply decimation", command=lambda: self.send(10, self.decimation_var.get())).pack(fill="x")
        ttk.Button(stream, text="Enable stream", command=lambda: self.send(9, 1)).pack(fill="x", pady=4)
        ttk.Button(stream, text="Disable stream", command=lambda: self.send(9, 0)).pack(fill="x")

        main = ttk.Frame(root)
        main.grid(row=1, column=1, sticky="nsew")
        main.columnconfigure(0, weight=1)
        main.rowconfigure(0, weight=3)
        main.rowconfigure(2, weight=2)

        self.plot = PlotCanvas(main)
        self.plot.grid(row=0, column=0, sticky="nsew")

        plot_actions = ttk.Frame(main, padding=(0, 6))
        plot_actions.grid(row=1, column=0, sticky="ew")
        ttk.Label(plot_actions, textvariable=self.latest_var).pack(side="left")
        ttk.Button(plot_actions, text="Clear plot", command=self.plot.clear).pack(side="right")
        self.record_button = ttk.Button(plot_actions, text="Start CSV recording", command=self.toggle_recording)
        self.record_button.pack(side="right", padx=6)
        ttk.Label(plot_actions, textvariable=self.recording_var).pack(side="right")

        self.log = ScrolledText(main, height=12, state="disabled", font=("Consolas", 9))
        self.log.grid(row=2, column=0, sticky="nsew")

    @staticmethod
    def _number_control(
        parent: ttk.LabelFrame,
        label: str,
        variable: tk.IntVar,
        minimum: int,
        maximum: int,
        increment: int,
    ) -> None:
        ttk.Label(parent, text=label).pack(anchor="w", pady=(5, 0))
        ttk.Spinbox(
            parent,
            textvariable=variable,
            from_=minimum,
            to=maximum,
            increment=increment,
            width=20,
        ).pack(fill="x")

    def refresh_ports(self) -> None:
        ports = self.worker.available_ports()
        self.port_combo["values"] = ports
        if ports and self.port_var.get() not in ports:
            self.port_var.set(ports[0])

    def toggle_connection(self) -> None:
        if self.worker.connected:
            self.worker.disconnect()
            return
        port = self.port_var.get()
        if not port:
            messagebox.showerror("No serial port", "Select a serial port first.")
            return
        self.connection_var.set(f"Connecting to {port}...")
        self.worker.connect(port)

    def send(self, command_id: int, value: int) -> None:
        try:
            self.worker.send(command(command_id, value))
            self._append_log(f"> {command_id},{value}")
        except RuntimeError as exc:
            messagebox.showerror("Not connected", str(exc))

    def _poll_serial(self) -> None:
        try:
            while True:
                item = self.worker.incoming.get_nowait()
                if isinstance(item, ConnectionEvent):
                    self.connection_var.set(item.detail)
                    self.connect_button.configure(text="Disconnect" if item.connected else "Connect")
                else:
                    self._handle_line(item)
        except queue.Empty:
            pass
        self.after(30, self._poll_serial)

    def _handle_line(self, line: str) -> None:
        parsed = parse_line(line)
        if isinstance(parsed, DataFrame):
            self.frame_count += 1
            self.plot.add_frame(parsed)
            difference = ", ".join(str(value) for value in parsed.difference)
            self.latest_var.set(f"Sample {parsed.sample_counter} | H-L: {difference}")
            self._record_frame(parsed)
        elif isinstance(parsed, Status):
            self._sync_status(parsed)
            summary = ", ".join(f"{key}={value}" for key, value in parsed.values.items())
            self._append_log(f"STATUS: {summary}")
        elif isinstance(parsed, SingleRead):
            values = ", ".join(f"{value:g}" for value in parsed.values)
            self._append_log(f"{parsed.kind}: {values}")
        elif isinstance(parsed, Message) and parsed.text:
            self._append_log(parsed.text)

    def _sync_status(self, status: Status) -> None:
        controls = {
            "streamDecimation": self.decimation_var,
            "visDac": self.vis_dac_var,
            "visibleGain": self.vis_gain_var,
            "irGain": self.ir_gain_var,
        }
        for key, variable in controls.items():
            try:
                variable.set(int(status.values[key]))
            except (KeyError, ValueError):
                continue

    def _append_log(self, text: str) -> None:
        timestamp = time.strftime("%H:%M:%S")
        self.log.configure(state="normal")
        self.log.insert("end", f"[{timestamp}] {text}\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def toggle_recording(self) -> None:
        if self.csv_file is not None:
            self._stop_recording()
            return
        path = filedialog.asksaveasfilename(
            title="Record DAQ stream",
            defaultextension=".csv",
            filetypes=[("CSV files", "*.csv")],
            initialfile=time.strftime("daq_%Y%m%d_%H%M%S.csv"),
        )
        if not path:
            return
        self.csv_file = Path(path).open("w", newline="", encoding="utf-8")
        self.csv_writer = csv.writer(self.csv_file)
        self.csv_writer.writerow(
            ["host_time", "sample_counter"]
            + [f"high_ch{i}" for i in range(1, 5)]
            + [f"low_ch{i}" for i in range(1, 5)]
            + [f"difference_ch{i}" for i in range(1, 5)]
        )
        self.recording_var.set(f"Recording: {Path(path).name}")
        self.record_button.configure(text="Stop CSV recording")

    def _record_frame(self, frame: DataFrame) -> None:
        if self.csv_writer is None or self.csv_file is None:
            return
        self.csv_writer.writerow(
            [time.time(), frame.sample_counter, *frame.high, *frame.low, *frame.difference]
        )
        self.csv_file.flush()

    def _stop_recording(self) -> None:
        if self.csv_file is not None:
            self.csv_file.close()
        self.csv_file = None
        self.csv_writer = None
        self.recording_var.set("Not recording")
        self.record_button.configure(text="Start CSV recording")

    def _on_close(self) -> None:
        self._stop_recording()
        self.worker.disconnect()
        self.destroy()


def main() -> None:
    app = DaqApp()
    app.mainloop()


if __name__ == "__main__":
    main()
