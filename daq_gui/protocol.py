from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


ADC_FULL_SCALE_VOLTS = 5.0


@dataclass(frozen=True)
class DataFrame:
    sample_counter: int
    high: tuple[int, int, int, int]
    low: tuple[int, int, int, int]

    @property
    def difference(self) -> tuple[int, int, int, int]:
        return tuple(h - l for h, l in zip(self.high, self.low))


@dataclass(frozen=True)
class SingleRead:
    kind: str
    values: tuple[float, float, float, float]


@dataclass(frozen=True)
class Status:
    values: Mapping[str, str]


@dataclass(frozen=True)
class Message:
    level: str
    text: str


ParsedLine = DataFrame | SingleRead | Status | Message


def raw_to_signed(raw: int) -> int:
    raw &= 0xFFFF
    return raw - 0x10000 if raw & 0x8000 else raw


def raw_to_volts(raw: int) -> float:
    return raw_to_signed(raw) / 32768.0 * ADC_FULL_SCALE_VOLTS


def parse_line(line: str) -> ParsedLine:
    line = line.strip()
    if not line:
        return Message("info", "")

    if line.startswith("DATA,"):
        parts = line.split(",")
        if len(parts) != 12 or parts[2] != "H" or parts[7] != "L":
            return Message("error", f"Malformed DATA line: {line}")
        try:
            return DataFrame(
                sample_counter=int(parts[1]),
                high=tuple(int(value) for value in parts[3:7]),
                low=tuple(int(value) for value in parts[8:12]),
            )
        except ValueError:
            return Message("error", f"Malformed DATA values: {line}")

    if line.startswith("SINGLE_RAW,") or line.startswith("SINGLE_VOLTS,"):
        parts = line.split(",")
        if len(parts) != 5:
            return Message("error", f"Malformed single-read line: {line}")
        kind = parts[0]
        try:
            values = tuple(float(value) for value in parts[1:5])
        except ValueError:
            return Message("error", f"Malformed single-read values: {line}")
        return SingleRead(kind, values)

    if line.startswith("STATUS "):
        values: dict[str, str] = {}
        for item in line.removeprefix("STATUS ").split(","):
            key, separator, value = item.partition("=")
            if separator:
                values[key] = value
        return Status(values)

    if line.startswith("ERR"):
        return Message("error", line)
    if line.startswith("OK"):
        return Message("ok", line)
    return Message("info", line)


def command(command_id: int, value: int) -> str:
    return f"{command_id},{value}\n"

