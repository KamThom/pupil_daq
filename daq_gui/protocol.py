from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping


ADC_FULL_SCALE_VOLTS = 5.0
VIS_LED_DAC_REFERENCE_VOLTS = 3.3
VIS_LED_SENSE_RESISTOR_OHMS = 100.0
VIS_LED_MAX_CURRENT_MA = VIS_LED_DAC_REFERENCE_VOLTS / VIS_LED_SENSE_RESISTOR_OHMS * 1000.0


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


@dataclass(frozen=True)
class SchedEvent:
    time_s: int
    dac_code: int


ParsedLine = DataFrame | SingleRead | Status | Message | SchedEvent


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

    if line.startswith("SCHED,"):
        parts = line.split(",")
        if len(parts) == 3:
            try:
                return SchedEvent(time_s=int(parts[1]), dac_code=int(parts[2]))
            except ValueError:
                pass
        return Message("error", f"Malformed SCHED line: {line}")

    if line.startswith("ERR"):
        return Message("error", line)
    if line.startswith("OK"):
        return Message("ok", line)
    return Message("info", line)


def command(command_id: int, value: int) -> str:
    return f"{command_id},{value}\n"


def command_schedule_clear() -> str:
    return "12,0\n"


def command_schedule_step(time_s: int, dac_code: int) -> str:
    return f"13,{time_s},{dac_code}\n"


def command_schedule_start() -> str:
    return "14,0\n"


def command_schedule_stop() -> str:
    return "15,0\n"


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def vis_dac_code_to_current_ma(dac_code: int) -> float:
    dac_code = clamp(dac_code, 0, 4095)
    return dac_code / 4095 * VIS_LED_MAX_CURRENT_MA
