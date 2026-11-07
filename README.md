# openm454

An open driver and command-line interface for the **DreamSourceLab DSCope M454** USB mixed-signal oscilloscope
(4 analog channels up to 400 MSa/s, 8 digital channels, built-in 0-3 V signal generator), written so that an AI
coding agent (Claude Code) can capture and analyze real signals by itself, without the vendor GUI.

The M454 speaks a newer ("v3") USB protocol than the devices supported by the open-source DSView. The protocol was
recovered from DSWave 1.2.1 by static analysis, by driving DSWave's own hardware layer through injected commands and
by USB tracing; see `docs/PROTOCOL.md`.

## Use

Requirements: Windows, Python 3.10+, `pip install libusb1 numpy matplotlib lz4`, the scope bound to WinUSB (it is by
default), and DSWave 1.2.1, needed once to extract the FPGA bitstream (it is never redistributed): point
`OPENM454_DSWAVE` at its `DSWave.exe` (or folder), or copy the DSWave folder to `DSWave/` next to `openm454/`. The
bitstream is then cached in `firmware/`. Close DSWave before using the driver: the scope accepts one USB client.

```
python -m openm454 info
python -m openm454 capture -c A1,D0 --vdiv 1V --offset 1.5V --timebase 200us --trig A1:rise:1.4V
python -m openm454 watch -c A1 --every 0.5s --count 10 --fields A1.freq_hz,A1.vpp_v
python -m openm454 awg set --wave sine --freq 10k --low 0V --high 2V
python -m openm454 analyze captures/<file>.npz --window -1ms:1ms
python -m openm454 capture -c D0,D1 --rate 20M --duration 2ms --trig D0:pulse>:200us --decode "i2c:scl=D0,sda=D1"
python -m openm454 selftest
```

## Recording modes and measured speeds

| Command | How it records | Length | Rates |
|---|---|---|---|
| `capture` | into the 512 MiB sample memory, read out afterwards (250-265 MB/s) | one record: 400M samples per channel / 512 MiB | up to 400 MSa/s (1 channel) |
| `stream` | every record is sent while it is acquired, at any data rate (`--record-first`: above 102.4 MB/s acquire it first and read it out afterwards, as DSWave does); longer than one record or `--duration forever`: record after record, each refreshing the previous one | unlimited; between records ~0.2 s while the readout keeps up, plus the rest of the record above it (0.7 s at 250 MB/s; 2.4 s with `--record-first`); the last record is kept (`--save-each`: all) | as `capture`: analog and digital at their own rates |
| `log` | loop mode: one endless gap-free stream, kept in RAM (default; written once at the end; a RAM ring keeps the newest part of longer runs) or, with `--storage disk`, dumped in chunk files while running | until `openm454 stop` or the duration; RAM budget / disk allowance | one rate for all channels, 100 MHz / N for any integer N (1 kSa/s..100 MSa/s, e.g. 33.333 MSa/s); interleaved 200 MSa/s (<= 2 analog, <= 4 digital channels) and 400 MSa/s (<= 1 analog, <= 2 digital) |

Measured on the M454 over USB 3 with every input on a 1 kHz square, every period checked for gaps (2026-11-01/02):

- The data rate is rate x (analog channels + digital channels / 8) bytes/s.
- `stream`: real time at DSWave's 100 MB/s ceiling and at 200 MB/s. At 400 MB/s the readout ran at 270-290 MB/s and
  the rest waited in the sample memory; the record arrived complete. 12 channels at 50 MSa/s (250 MB/s, ~2 s
  records): 0.74 / 0.66 s between records when sent while acquired (the default), 2.35 / 2.45 s with
  `--record-first`; records complete and continuous.
- `log`: 150 MB/s (2 analog + 8 digital at 50 MSa/s) for 3 min (27 GB) and for 30 s to disk with a constant host
  lag; 200 MB/s (2 analog at 100 MSa/s) for 20 s without backlog; 5 MB/s for 10 min. At 250 MB/s (4 analog + 8
  digital at 50 MSa/s) the readout reached ~200 MB/s and the backlog grew ~55 MB/s. A 300 MB/s burst of 3 s (2
  analog + 8 digital at 100 MSa/s) read out at 266 MB/s and stayed complete.
- `log` above 100 MSa/s interleaves the converters exactly as `capture` does (DSWave never does this in loop
  mode). Verified on the scope: 1 analog channel at 200 and 400 MSa/s, 2 analog at 200, 2 digital at 400, 4 digital
  at 200 and mixed sets. 2 digital channels at 400 MSa/s or 4 at 200 MSa/s are 100 MB/s and run without a time
  limit. 1 analog channel at 200 MSa/s (200 MB/s) ran 10 s without backlog.
- The sample memory is a 512 MiB ring in loop mode: the host may fall up to ~512 MiB behind without losing data (host
  stalls of 10 s at 50 MB/s were absorbed). Beyond that the scope overwrites unread data without any flag and keeps
  streaming: from then on the data are garbage (one phase step on analog, the digital channels misaligned) until
  the run is restarted. Measured with the driver's backlog estimate, the break came at 508-514 MiB (three runs), so
  `log` stops at 480 MiB.
- `log --plan` (no device needed) prints the verdict for a rate, the gap-free limit and recommendation (next
  section), how long the run can last (`max_duration_s`) and where the data goes: RAM by default (refused when it
  does not fit the budget unless `--keep-last`), `--storage disk` for ~256 MB chunk files (it warns about SSD wear:
  200 MB/s is 720 GB per hour), `--storage none` for statistics only.
- `bench` measures this PC (free RAM, decode speed, disk write speed, USB readout) and records how much RAM and disk
  the owner allows for recordings and the data folder (`bench --ram 8GB --disk 50GB --dir <folder>`,
  `state/host.json`); `log` takes its defaults from there. Measured here: decode 468 MB/s, USB readout 206 MB/s
  (4 analog + 8 digital) / 267 MB/s (2 analog + 8 digital).

### Above the readout limit: how `log` decides

D = the data rate; R = how fast this PC reads the scope while it acquires. R depends on the channel set (measured
here: 202 MB/s for 4 analog + 8 digital at 50 MSa/s, 264 MB/s for the same at 100 MSa/s and for 2 analog + 8
digital at 100 MSa/s, 275 MB/s for 4 analog at 100 MSa/s).

1. R is measured, not assumed: the first `log` above 150 MB/s with a channel set runs a probe of up to 2 s (a
   `probe` line) and remembers R in `state/host.json`: the measured rate, or "kept up" (R >= D) when it reads at
   >= 99 % of D. Later runs above 150 MB/s refresh it when the result is clear (still falling behind at a steady
   rate at the stop, or caught up at the end), and so do `bench` and `--remeasure`. Until then `--plan` assumes
   190 MB/s and marks the plan `provisional`.
2. D <= R: the host keeps up (`sustained` / `near_limit`); the run lasts as long as the storage allows.
3. D > R: the scope gets D - R ahead per second (`backlog_growth_mb_s`), and it sends nothing after a stop, so the
   unread part is lost. A log is gap-free for at most `gapfree_max_s` = 480 MiB x R / (D x (D - R)), the signal the
   host has read when the scope is 480 MiB ahead. A shorter log runs normally.
4. A longer log (or `forever`) is compared with what `stream` would do with the same channels and settings
   (`stream_alternative`, from stream's own planning): records of up to 400M samples per channel / 512 MiB, sent
   while acquired, with gaps of record x (D - R) / R + 0.18 s, at stream's rates (1-2-5 steps x interleave; it may
   run a domain slower, e.g. 2 analog + 8 digital at 100 MSa/s samples the digital channels at 50 MSa/s):
   - `STREAM_BETTER` (exit 2): stream samples every channel at least as fast, and its records are at least as long
     as the log's one gap-free piece (or one record holds the whole duration). The error carries the stream
     command, with the same vertical settings.
   - `NEEDS_FORCE` (exit 2): the log's piece is the longest gap-free recording possible but shorter than asked.
     `--force` records it and then stops (`stop_reason` `ring_full`, ok, with a warning); the rest of the requested
     time is not recorded. Both errors carry the numbers in `error.plan`.
5. While running, every statistics line has `backlog_mb`; while the backlog grows, also `backlog_growth_mb_s`,
   `ring_full_in_s` and `gapfree_projected_s`. The result has `unread_at_stop_mb` (acquired, never read).

Examples here: 12 channels at 50 MSa/s (250 MB/s, R = 204): gap-free 8.88 s vs stream records of 2.03 s with 0.64 s
gaps, so `NEEDS_FORCE`; forced, the run stopped after 8.84 s (8.30 s with R = 202 in an earlier run), every period
present up to the last sample. 12 channels at 100 MSa/s (500 MB/s, R = 264): 1.13 s vs 1.02 s records with 1.09 s
gaps, still `NEEDS_FORCE`; with a readout below ~250 MB/s `stream` would win. 200 MB/s read at 190 MB/s: 10 MB/s
backlog growth, the guard after ~50 s, 47.8 s of gap-free signal.

The backlog is estimated as D x (time since the head) - bytes received, checked on every USB transfer, against a
baseline that follows the scope / PC clock difference: it may rise by at most 200 ppm x D per second, and only
while the host is caught up (estimate below 16 MiB), so the estimate stays exact over hours and a real backlog is
never absorbed. A guard stop while nothing arrives at all is reported as a stalled scope (`TIMEOUT_DATA`); a
forced log that was planned to end at the guard reports `ring_full`, any other guard stop `HOST_TOO_SLOW`.

### `log` or `stream`?

Both record untriggered, but they are not the same mode of the scope. `log` is one endless run (loop mode);
`stream` is a series of records, each programmed with a fixed length that the scope stops at.

| | `log` | `stream` |
|---|---|---|
| gaps | none inside a run | one per record (refresh) |
| gap between two runs, 12 channels at 20 MSa/s (100 MB/s) | 0.18 s | 0.19 s |
| gap between two runs, 12 channels at 50 MSa/s (250 MB/s, above the readout limit) | 0.51 s after a 1.5 s run: 0.33 s to read the run's backlog (data the scope acquires meanwhile are dropped), then 0.18 s restart | 0.66-0.74 s after a ~2 s record: the rest of the record is read (sent while acquired), then the restart; 2.33-2.45 s with `--record-first` (the whole record read out after it was acquired) |
| rates | one rate for every channel: 100 MHz / N for any integer N, interleaved 200 / 400 MSa/s on 1-2 channels | analog and digital at their own rates (e.g. 1 analog at 400 MSa/s with 8 digital at 100 MSa/s), from DSWave's 1-2-5 x interleave table (e.g. 80 MSa/s) |
| above the readout limit (~200-275 MB/s) | one gap-free piece of at most 480 MiB x R / (D x (D - R)) (the scope sends nothing after a stop; see above) | records of up to 512 MiB / D (400M samples per channel), each sent while acquired, with gaps |
| below ~51 kB/s (e.g. 1 channel at 1-20 kSa/s) | supported | the rate is raised |
| protocol decoders | offline on the saved file (`analyze --decode`) | `--decode` on the kept record |
| register program | DSWave's Data Acquisition mode (interleaving: this driver's extension, verified) | DSWave's own program for this window (golden-tested) |

The restart (~0.18 s) is the same for both; the difference is what has to be read out before it. Use `log` for
continuous recording; `stream` for different analog and digital rates, decoding the record, or above the readout
limit when `log` says `STREAM_BETTER` or the whole time matters more than one long gap-free piece.

```
python -m openm454 bench --ram 8GB --disk 50GB --dir logs
python -m openm454 log --plan -c A0,D0-D7 --rate 1M --duration 10min
python -m openm454 log -c A0,D0-D7 --rate 1M --duration 10min                   # kept in RAM, one .npz at the end
python -m openm454 log -c A0,D0-D7 --rate 1M --duration forever                 # RAM ring; end it with: python -m openm454 stop
python -m openm454 stream -c D0-D7 --rate 100M --duration 60s --every 1s
```

Every command prints one JSON document (measurements, applied settings, file paths); captures are saved as `.npz`
(raw codes + scaling) and `.png`. `python -m openm454 <command> --help` lists every option. Python API:
`openm454.scope.Scope`, `openm454.capture.load_capture`, `Capture.decode()`.

Protocol decoders (`--decode SPEC`, repeatable, on `capture`, `watch`, `stream` and `analyze`):
`uart:rx=D4,baud=115200|auto,bits=8,parity=none,stop=1,invert=0`, `spi:clk=..,mosi=..,miso=..,cs=..,mode=0..3`,
`i2c:scl=..,sda=..`, `can:rx=..,bitrate=500k|auto,invert=0`. They report every frame with trigger-relative times and
its errors (parity/framing, partial words, NACKs, CRC/stuff/form errors), and remove the comparator chatter of slow
edges. Output format: `docs/schemas/decode.json`. Decoding needs about 10 samples per bit or clock (I2C: 9 per SCL
period, no errors; 5-6 per period still decode with well under 1 % errors). They have been checked against real buses (SPI up to 18 MHz, RS232, CAN up to 1 Mbit/s, I2C up to 400 kHz)
generated by a microcontroller, including deep captures and decoded streams.

## Layout

- `openm454/` driver: USB transport with a safety whitelist, device bring-up (FPGA, security handshake, calibration),
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
