# pupil_daq

A pupillometry data acquisition system that measures pupil size and movement using IR and visible-light photodiodes. An ESP32-S3 microcontroller drives LEDs and samples four ADC channels over SPI; a Python/tkinter GUI connects over USB serial to control the device and record data.

## Hardware

| Component | Part | Interface | Address/Pins |
|---|---|---|---|
| Microcontroller | ESP32-S3 | — | — |
| External ADC | TI ADS8588S (8-ch, 18-bit SAR) | SPI (FSPI, 1 MHz, MODE1) | CS=33, SCK=36, MISO=37 |
| PD gain pot | AD524X digital pot | I2C | 0x2E |
| VIS LED DAC | MCP4725 (12-bit) | I2C | 0x61 |
| IR LED | — | GPIO | Pin 16 |
| Indicator LED | — | GPIO | Pin 1 |
| I2C bus | — | — | SDA=3, SCL=4 |

The firmware reads 4 ADC channels: CH1 (IR photodiode), CH2 (visible photodiode), CH3 (VIS LED current sense), CH4 (IR LED current sense). The main loop alternates IR LED on/off and samples both states. CSV recording captures the full `high`/`low`/`high − low` data for all 4 channels. The live GUI plot always shows raw LED-on (`high`) readings for CH1 and CH2 on its top two subplots; the third subplot is selectable (VIS LED current, or CH1–CH4) — see "Running the GUI" below.

## Requirements

- Python 3.12, Windows
- An ESP32-S3 board flashed with `firmware/firmware.ino`, connected over USB
- The AD524X Arduino library (install via Arduino IDE Library Manager — not included in this repo) if you need to rebuild the firmware

## Setup

```
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
```

## Flashing the firmware

1. Open `firmware/firmware.ino` in the Arduino IDE.
2. Install the AD524X library via the Library Manager.
3. Select your ESP32-S3 board and the correct serial port.
4. Upload at 115200 baud.

## Running the GUI

```
python -m daq_gui.app
```

(or `python daq_gui/app.py`)

### Multiple devices

The GUI supports multiple DAQ boards at once. Each board gets its own tab.

1. Enter **Cohort** and **Test** labels in the top toolbar — these are shared across all boards.
2. Click **Add device**, pick the COM port, and enter an **Animal ID**. The tab auto-connects.
3. Repeat for each board.
4. Click **Start all** to choose a save directory; recordings start on all connected boards simultaneously. Files are named `{cohort}_{animal}_{test}_{timestamp}.csv`.
5. Click **Stop all** to stop streaming and close all recordings.

You can also control each board individually from its own tab.

### Per-device controls

- **VIS LED current** — set as a DAC code (0–4095); the label next to the spinbox shows the equivalent mA before you apply it.
- **Live plot** — three stacked subplots vs. elapsed time (seconds): CH1 IR PD (top, blue, volts) and CH2 VIS PD (middle, red, volts) are fixed. The bottom subplot is selectable via the **"Plot 3"** dropdown: VIS LED current (commanded, mA, green step function — the default), or CH1/CH2/CH3/CH4 raw readings in volts. CH3 (VIS LED current sense) and CH4 (IR LED current sense) are otherwise not shown anywhere in the live view, so this is the way to inspect them without hardware. Rolling 30-second window; each subplot auto-scales independently. Dashed yellow vertical lines mark events across all three subplots (device on/off, LED and gain changes, stream state, schedule fires, etc.). Requires `matplotlib` (listed in `requirements.txt`).
- **Disconnect / Connect** — sends a stop-device command before closing the port; the board is left idle rather than running.
- **Stream decimation** — the firmware sends every Nth sample's `DATA` line; default 10. Lower = more data; higher = less serial traffic.
- **Sample rate** — set in the Streaming section (10–250 Hz, default 100 Hz). The firmware gates samples with `micros()` so serial parsing still runs between samples.
- **VIS Schedule** — click **Edit schedule...** to open the schedule editor. Add steps as `(duration_s, DAC_code)` pairs in order (e.g. `2s at 400 DAC, 12s at 200 DAC, 1s at 4000 DAC`). Each step holds its value for its full duration before the next step starts. Click **Upload to device** to push the schedule, then **Start recording** to begin — the schedule clock starts when recording starts, not when the device starts, so the LED changes are synchronized to your recording. The firmware executes the schedule against its own `micros()` clock with no host timing jitter. Schedule fires appear as steps on the **VIS LED current** subplot and are logged as events in the CSV. **Start all** automatically uploads and starts each panel's schedule.

### Recording

- **Duration** — set in the Recording section of each device's controls sidebar (seconds; 0 = unlimited). A countdown shows elapsed / total time and the recording stops automatically when time is up.
- **Event column** — the CSV has an `event` column. Normal data rows have an empty event field. Every significant command sent during recording (device on/off, LED changes, gains, stream state, sample rate) is written as its own timestamped row with empty data columns. Filter with `df[df['event'].notna()]` in pandas.
- **Metadata sidecar** — a `.json` file (same base name as the `.csv`) is written when recording starts, capturing all settings in effect at that moment.

### CSV format

| Column | Type | Notes |
|---|---|---|
| `host_time` | float | `time.time()` on the PC |
| `sample_counter` | int | Firmware counter; empty on event rows |
| `high_ch1`–`high_ch4` | int | Raw ADC counts, IR LED on; empty on event rows |
| `low_ch1`–`low_ch4` | int | Raw ADC counts, IR LED off; empty on event rows |
| `difference_ch1`–`difference_ch4` | int | high − low; empty on event rows |
| `event` | str | Description; empty on data rows |

## Running tests

```
python -m unittest discover -s tests
```

Tests cover `protocol.py` only (parsing and conversion functions). `serial_worker.py` and `app.py` are not tested directly since they require hardware or a tkinter display.

## Serial protocol

Most commands are `CMD,VALUE\n`. Cmd 13 uses three params: `13,TIME_S,DAC_CODE\n`. The firmware responds with prefixed lines (`OK …`, `ERR …`, `DATA,…`, `STATUS …`, `SINGLE_RAW,…`, `SINGLE_VOLTS,…`, `SCHED,…`).

| Cmd | Value | Description |
|---|---|---|
| 0 | 0/1 | Stop / Start device |
| 1 | 0–255 | Set visible PD gain (AD524X ch0) |
| 2 | 0–255 | Set IR PD gain (AD524X ch1) |
| 3 | 0–4095 | Set VIS LED DAC code (MCP4725) |
| 4 | µs | Pulse IR LED for N microseconds |
| 7 | 0 | Print STATUS |
| 8 | 0 | Single ADC read (SINGLE_RAW + SINGLE_VOLTS) |
| 9 | 0/1 | Disable / Enable ADC streaming |
| 10 | N | Set stream decimation (print every N loops); default 10 |
| 11 | N | Set sample rate to N Hz (clamped 10–250); default 100 |
| 12 | 0 | Clear VIS light schedule |
| 13 | T,D | Append schedule step: at T seconds after schedule start, set VIS LED to DAC code D |
| 14 | 0 | Start schedule execution (locks `schedStartUs` to now) |
| 15 | 0 | Stop schedule execution |

There's no pulsing/blinking mode for the VIS LED — cmd 3 just sets it to a steady DAC code (current) until changed. Cmds 12–15 are used by the GUI's schedule editor.

## Measurement math

- ADC: raw 16-bit values are signed two's complement, full scale ±5.0 V → `volts = (int16(raw) / 32768.0) * 5.0`
- VIS LED current: `current_mA = dac_code / 4095 * 33.0` (max ≈ 33 mA, limited by a 100 Ω sense resistor against the 3.3 V DAC reference) — the GUI computes this from the DAC code you set via `protocol.vis_dac_code_to_current_ma`

These constants are duplicated in firmware and `daq_gui/protocol.py` — keep both in sync if hardware changes (e.g. a different sense resistor).
