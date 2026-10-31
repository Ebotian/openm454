# DSClaude

An open driver and command-line interface for the **DreamSourceLab DSCope M454** USB mixed-signal
oscilloscope (4 analog + 8 digital channels + AWG), built so that an AI coding agent (Claude Code)
can capture and analyze real signals by itself, without the vendor GUI.

**Status: work in progress.** The USB "v3" protocol is being recovered from the vendor application
(DSWave 1.2.1) by static analysis and USB tracing. See `docs/STATUS.md` once it exists.

## What is here

- `re/tools/` — reverse-engineering tools:
  - `usb_probe.py` read-only first contact (descriptors, MCU version registers).
  - `dswave_usbtrace.py` Frida tracer that logs every WinUSB call DSWave makes (no admin rights needed).
  - `dswave_scpi.py` executes HAL SCPI commands inside a running DSWave via Frida.
  - `dswave_ui.py` UI Automation helper for DSWave.
- `re/notes/` — protocol reconnaissance and design notes.
- `dscope/` — the Python driver and CLI (in progress).

## What is NOT here (and never will be)

The vendor application, its embedded FPGA bitstream and firmware images, decompiled vendor code,
raw USB traces (they contain the device's security key) and the vendor datasheet are kept out of
this repository. The driver extracts the FPGA bitstream at run time from a locally installed copy
of DSWave.

## Safety

The driver never sends the firmware-upgrade request (bRequest 0xB3), never writes the device
EEPROM, and only configures the FPGA when the device reports it unconfigured.

## License

GPL-3.0-only for the code in this repository (see `LICENSE`). DSView (GPLv3) by DreamSourceLab was
used as a protocol reference. DSWave and DSCope are trademarks of DreamSourceLab; this project is not
affiliated with DreamSourceLab.
