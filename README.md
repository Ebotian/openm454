# DSClaude

An open driver and command-line interface for the **DreamSourceLab DSCope M454** USB mixed-signal oscilloscope
(4 analog channels up to 400 MSa/s, 8 digital channels, built-in 0-3 V signal generator), written so that an AI
coding agent (Claude Code) can capture and analyze real signals by itself, without the vendor GUI.

The M454 speaks a newer ("v3") USB protocol than the devices supported by the open-source DSView. The protocol was
recovered from DSWave 1.2.1 by static analysis, by driving DSWave's own hardware layer through injected commands and
by USB tracing; see `docs/PROTOCOL.md`.

## Use

Requirements: Windows, Python 3.10+, `pip install libusb1 numpy matplotlib lz4`, the scope bound to WinUSB (it is by
default), and DSWave 1.2.1, needed once to extract the FPGA bitstream (it is never redistributed): point
`DSCOPE_DSWAVE` at its `DSWave.exe` (or folder), or copy the DSWave folder to `DSWave/` next to `dscope/`. The
bitstream is then cached in `firmware/`. Close DSWave before using the driver: the scope accepts one USB client.

```
python -m dscope info
python -m dscope capture -c A1,D0 --vdiv 1V --offset 1.5V --timebase 200us --trig A1:rise:1.4V
python -m dscope watch -c A1 --every 0.5s --count 10 --fields A1.freq_hz,A1.vpp_v
python -m dscope awg set --wave sine --freq 10k --low 0V --high 2V
python -m dscope analyze captures/<file>.npz --window -1ms:1ms
python -m dscope capture -c D0,D1 --rate 20M --duration 2ms --trig D0:pulse>:200us --decode "i2c:scl=D0,sda=D1"
python -m dscope selftest
```

## Recording modes and measured speeds

| Command | How it records | Length | Rates |
|---|---|---|---|
| `capture` | into the 512 MiB sample memory, read out afterwards (250-265 MB/s) | one record: 400M samples per channel / 512 MiB | up to 400 MSa/s (1 channel) |
| `stream` | streamed while acquired (below 102.4 MB/s, DSWave's switch); longer than one record or `--duration forever`: record after record, each refreshing the previous one | unlimited, ~0.3 s gap between records; the last record is kept (`--save-each`: all) | as `capture` |
| `log` | loop mode: one endless gap-free stream, kept in RAM (default; written once at the end; a RAM ring keeps the newest part of longer runs) or, with `--storage disk`, dumped in chunk files while running | until `dscope stop` or the duration; RAM budget / disk allowance | one rate for all channels, 1 kSa/s..100 MSa/s |

Measured on the M454 over USB 3 with every input on a 1 kHz square, every period checked for gaps (2026-11-01/02):

- The data rate is rate x (analog channels + digital channels / 8) bytes/s.
- `stream`: real time at DSWave's 100 MB/s ceiling, and also when forced to 200 MB/s. At 400 MB/s the readout ran at
  270-290 MB/s and the rest waited in the sample memory; the record arrived complete.
- `log`: 150 MB/s (2 analog + 8 digital at 50 MSa/s) for 3 min (27 GB) and for 30 s to disk with a constant host
  lag; 200 MB/s (2 analog at 100 MSa/s) for 20 s without backlog; 5 MB/s for 10 min. At 250 MB/s (4 analog + 8
  digital at 50 MSa/s) the readout reached ~200 MB/s and the backlog grew ~55 MB/s. A 300 MB/s burst of 3 s (2
  analog + 8 digital at 100 MSa/s) read out at 266 MB/s and stayed complete.
- The sample memory is a 512 MiB ring in loop mode: the host may fall up to ~512 MiB behind without losing data (host
  stalls of 10 s at 50 MB/s were absorbed). Beyond that the scope overwrites unread data without any flag, so `log`
  stops with `HOST_TOO_SLOW` at 384 MiB of backlog.
- `log --plan` (no device needed) prints the verdict for a rate (sustained up to 150 MB/s, near the limit up to
  190 MB/s, above that a burst of at most `burst_max_s`; `--force` relies on the backlog guard instead), how long
  the run can last (`max_duration_s`) and where the data goes: RAM by default (refused when it does not fit the
  budget unless `--keep-last`), `--storage disk` for ~256 MB chunk files (it warns about SSD wear: 200 MB/s is
  720 GB per hour), `--storage none` for statistics only.
- `bench` measures this PC (free RAM, decode speed, disk write speed, USB readout) and records how much RAM and disk
  the owner allows for recordings and the data folder (`bench --ram 8GB --disk 50GB --dir <folder>`,
  `state/host.json`); `log` takes its defaults from there. Measured here: decode 468 MB/s, USB readout 206 MB/s
  (4 analog + 8 digital) / 267 MB/s (2 analog + 8 digital).

```
python -m dscope bench --ram 8GB --disk 50GB --dir logs
python -m dscope log --plan -c A0,D0-D7 --rate 1M --duration 10min
python -m dscope log -c A0,D0-D7 --rate 1M --duration 10min                   # kept in RAM, one .npz at the end
python -m dscope log -c A0,D0-D7 --rate 1M --duration forever                 # RAM ring; end it with: python -m dscope stop
python -m dscope stream -c D0-D7 --rate 100M --duration 60s --every 1s
```

Every command prints one JSON document (measurements, applied settings, file paths); captures are saved as `.npz`
(raw codes + scaling) and `.png`. `python -m dscope <command> --help` lists every option. Python API:
`dscope.scope.Scope`, `dscope.capture.load_capture`, `Capture.decode()`.

Protocol decoders (`--decode SPEC`, repeatable, on `capture`, `watch`, `stream` and `analyze`):
`uart:rx=D4,baud=115200|auto,bits=8,parity=none,stop=1,invert=0`, `spi:clk=..,mosi=..,miso=..,cs=..,mode=0..3`,
`i2c:scl=..,sda=..`, `can:rx=..,bitrate=500k|auto,invert=0`. They report every frame with trigger-relative times and
its errors (parity/framing, partial words, NACKs, CRC/stuff/form errors), and remove the comparator chatter of slow
edges. Output format: `docs/schemas/decode.json`. Decoding needs about 10 samples per bit or clock (I2C: 9 per SCL
period, no errors; 5-6 per period still decode with well under 1 % errors). They have been checked against real buses (SPI up to 18 MHz, RS232, CAN up to 1 Mbit/s, I2C up to 400 kHz)
generated by a microcontroller, including deep captures and decoded streams.

## Layout

- `dscope/` driver: USB transport with a safety whitelist, device bring-up (FPGA, security handshake, calibration),
  register compiler (`mso.py`, reproduces DSWave's register programs exactly), stream decoder (`packet.py`),
  acquisition engine, loop-mode logger (`logger.py`), measurements, plots, AWG, CLI, self-test, protocol decoders
  (`decode.py`) and synthetic bus traffic for tests (`protosim.py`).
- `tests/` offline tests: golden register and AWG programs, the stream decoder, the acquisition engine against a
  simulated device, measurements, protocol decoders on synthetic traffic, safety rules (`python -m pytest tests`).
- `docs/PROTOCOL.md` the recovered USB protocol.

## What is NOT here

The vendor application, its embedded FPGA bitstream and firmware images, decompiled vendor code, raw USB traces
(they contain the device's security key), the reverse-engineering workspace and the vendor datasheet are kept out of
this repository.

## Safety

The driver never sends the firmware-upgrade request (bRequest 0xB3), never writes the device EEPROM, and configures
the FPGA only when the device reports it unconfigured.

## License

GPL-3.0 (see `LICENSE`). DSView (GPLv3) by DreamSourceLab was used as a protocol reference. DSWave and DSCope are
trademarks of DreamSourceLab; this project is not affiliated with DreamSourceLab.
