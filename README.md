# DSClaude

An open driver and command-line interface for the **DreamSourceLab DSCope M454** USB mixed-signal oscilloscope
(4 analog channels up to 400 MSa/s, 8 digital channels, built-in 0-3 V signal generator), written so that an AI
coding agent (Claude Code) can capture and analyze real signals by itself, without the vendor GUI.

The M454 speaks a newer ("v3") USB protocol than the devices supported by the open-source DSView. The protocol was
recovered from DSWave 1.2.1 by static analysis, by driving DSWave's own hardware layer through injected commands and
by USB tracing; see `docs/PROTOCOL.md`.

## Use

Requirements: Windows, Python 3.10+, `pip install libusb1 numpy matplotlib lz4`, the scope bound to WinUSB (it is by
default), and a local install of DSWave 1.2.1 (only to extract the FPGA bitstream once; it is never redistributed).
Close DSWave before using the driver: the scope accepts one USB client.

```
python -m dscope info
python -m dscope capture -c A1,D0 --vdiv 1V --offset 1.5V --timebase 200us --trig A1:rise:1.4V
python -m dscope watch -c A1 --every 0.5s --count 10 --fields A1.freq_hz,A1.vpp_v
python -m dscope awg set --wave sine --freq 10k --low 0V --high 2V
python -m dscope analyze captures/<file>.npz --window -1ms:1ms
python -m dscope selftest
```

Every command prints one JSON document (measurements, applied settings, file paths); captures are saved as `.npz`
(raw codes + scaling) and `.png`. Python API: `dscope.scope.Scope`, `dscope.capture.load_capture`.
The agent-facing guide is `skill/SKILL.md`.

## Layout

- `dscope/` driver: USB transport with a safety whitelist, device bring-up (FPGA, security handshake, calibration),
  register compiler (`mso.py`, reproduces DSWave's register programs exactly), stream decoder (`packet.py`),
  acquisition engine, measurements, plots, AWG, CLI, self-test.
- `re/` reverse-engineering tools (Frida WinUSB tracer, in-process SCPI injector, scenario runner, Ghidra scripts)
  and notes.
- `tests/` offline golden tests (register programs, AWG programs, decoder) and hardware tests (`-m hw`).

## What is NOT here

The vendor application, its embedded FPGA bitstream and firmware images, decompiled vendor code, raw USB traces
(they contain the device's security key) and the vendor datasheet are kept out of this repository.

## Safety

The driver never sends the firmware-upgrade request (bRequest 0xB3), never writes the device EEPROM, and configures
the FPGA only when the device reports it unconfigured.

## License

GPL-3.0 (see `LICENSE`). DSView (GPLv3) by DreamSourceLab was used as a protocol reference. DSWave and DSCope are
trademarks of DreamSourceLab; this project is not affiliated with DreamSourceLab.
