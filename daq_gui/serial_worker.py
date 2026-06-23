from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass

import serial
from serial.tools import list_ports


@dataclass(frozen=True)
class ConnectionEvent:
    connected: bool
    detail: str


class SerialWorker:
    def __init__(self) -> None:
        self.incoming: queue.Queue[str | ConnectionEvent] = queue.Queue()
        self._outgoing: queue.Queue[str] = queue.Queue()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._port: serial.Serial | None = None

    @staticmethod
    def available_ports() -> list[str]:
        return [port.device for port in list_ports.comports()]

    @property
    def connected(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def connect(self, port_name: str, baud_rate: int = 115200) -> None:
        if self.connected:
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run,
            args=(port_name, baud_rate),
            name="daq-serial",
            daemon=True,
        )
        self._thread.start()

    def disconnect(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
        self._thread = None

    def send(self, text: str) -> None:
        if not self.connected:
            raise RuntimeError("DAQ is not connected")
        self._outgoing.put(text)

    def _run(self, port_name: str, baud_rate: int) -> None:
        try:
            self._port = serial.Serial(port_name, baud_rate, timeout=0.05)
            time.sleep(1.2)
            self._port.reset_input_buffer()
            self.incoming.put(ConnectionEvent(True, f"Connected to {port_name}"))

            while True:
                self._write_pending()
                if self._stop_event.is_set():
                    break
                raw = self._port.readline()
                if raw:
                    self.incoming.put(raw.decode("utf-8", errors="replace").strip())
        except serial.SerialException as exc:
            self.incoming.put(ConnectionEvent(False, f"Serial error: {exc}"))
        finally:
            if self._port is not None and self._port.is_open:
                self._port.close()
            self._port = None
            self.incoming.put(ConnectionEvent(False, "Disconnected"))

    def _write_pending(self) -> None:
        if self._port is None:
            return
        while True:
            try:
                text = self._outgoing.get_nowait()
            except queue.Empty:
                return
            self._port.write(text.encode("ascii"))
