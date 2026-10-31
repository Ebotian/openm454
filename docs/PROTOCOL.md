# DSCope M454 USB protocol ("v3", DSWave 1.2.1)

Recovered on 2026-10-31 from DSWave 1.2.1 (closed source) by static analysis (Ghidra, `re/decomp/`),
in-process SCPI injection (`re/tools/dswave_scpi.py`, `re/tools/hal_run.py`) and WinUSB tracing with Frida
(`re/tools/dswave_usbtrace.py`). Evidence: `re/traces/S00_cold` (cold open), `re/traces/S01_warm` (warm open
plus ~125 scripted runs, git-ignored because the traces contain the device security key), `re/scenarios/*.json`
(what each run configured), `re/notes/*.md` (reconnaissance). This file is the authority for the driver in `dscope/`.

## 1. Identification

| Item | Value |
|---|---|
| VID / PID | 0x2A0E / 0x0080 |
| Strings | manufacturer `DreamSourceLab`, product `USB-based DSL Instrument v3`, no serial |
| Speed | SuperSpeed (DSWave also has a High-Speed profile for the same PID) |
| Driver | WinUSB (MS OS descriptors, no INF needed); one opener at a time |
| Interface | config 1, interface 0, alt 0, class 0xFF |
| Endpoints | 0x02 bulk OUT, 0x84 bulk IN, 0x86 bulk IN (all 1024-byte max packet) |
| MCU / FPGA | WCH CH569 (firmware 0x3110), panel MCU (0x3100), Xilinx Spartan-6 XC6SLX16 (bitstream 0x3100) |

## 2. Transport

* **Register write**: control OUT, bmRequestType 0x40, bRequest **0xB0**, wValue 0, wIndex 0, data `[addr, value & 0xFF, value >> 8]`.
* **Register read**: control OUT 0x40/**0xB1** data `[addr, len, ext]`, wait 10 ms, control IN 0xC0/**0xB2** wLength = len.
  `len` < 128; MCU registers (addr >= 0xF0) are read with len 2. `ext` selects a sub-index (probe number for 0xFA).
* **0xB3 = ISP firmware upgrade. Never send it.**
* **EP 0x02 OUT**: FPGA bitstream (after `BULK_SIZE_LOW/HIGH`), and EEPROM writes (after `EEPROM_ADDR`, `BULK_SIZE`).
  Both use the same mechanism, so the driver only writes EP2 inside FPGA configuration, as the first write of a session.
* **EP 0x84 IN**: one-byte handshake used to change registers while a capture streams (see §7).
* **EP 0x86 IN**: capture data (see §8).

## 3. Registers (8-bit address, 16-bit value)

### HW / base (0x00-0x0F)
| Addr | Name | Notes |
|---|---|---|
| 0x00 | HW_BASE_MODE | 0 = single block, 1 = repeat (real-time frames), 2 = stream (slow time bases, roll) |
| 0x01 | HW_BASE_CTRL | write: 1 = run, 0 = stop. read: bit1 SEC_READY, bit2 SEC_PASS (bit5 seen set after DSWave sessions) |
| 0x02 | HW_BASE_ACTION | 0 at start, 0x08 flow pause/stop, 0x10 resume, 0x04 abort/flush (after stop). read = progress |
| 0x03 | HW_BASE_SYNC | sync word written at start (DSWave uses 0) |
| 0x07 / 0x08 | AWG data / AWG address | indirect AWG access: W 0x08 = awg_addr, then W 0x07 = value (DSWave's log labels these swapped) |
| 0x09 / 0x0A / 0x0B | TRIG_ADDR / TRIG_DATA / TRIG_SHFT | indirect trigger memory |
| 0x0E / 0x0F | SEC_CTRL / SEC_DATA | security handshake |

### MSO (0x10-0x74)
HORI 0x10-0x1B (BASE, DEPTH0/1, RWORDS0/1, ADIVL/H, DDIVL/H, AMTXS, DMTXS, SLOWCNT), VERT 0x20-0x2B (DFOLD, DTHR,
DUP, DEN0-3, AFOLD, ACROSS, ABRH_INV, AWR), TRIG 0x30-0x3A (BASE, PRES0/1, PREW0/1, LEVEL, INDEX, MARGIN, HOLD0/1,
PULSE), per analog channel n at 0x40 + 0x10·n: BASE, VERT, VOFF, VPWM, CROSS. Formulas: §6 and `re/notes/mso_formulas.md`.

### MCU (0xF0-0xFF)
| Addr | Name | Access |
|---|---|---|
| 0xF0 | FW_VERSION | R (0x3110) |
| 0xF1 | BIN_VERSION | R (FPGA, 0x3100; valid after configuration) |
| 0xF2 | PANEL_VERSION | R (0x3100) |
| 0xF4 | HARDWARE_INDEX | W 0 (DSWave `:Device:Index 0`) |
| 0xF9 | FLUSH_DATA | W 0 at the end of every setup |
| 0xFA | RATIO_INFO | R, ext = probe index (reads 0x3100 on this unit: no probe detection) |
| 0xFB | EEPROM_ADDR | W address, then R n bytes = EEPROM contents |
| 0xFC / 0xFD | BULK_SIZE_LOW / HIGH | byte count of the next EP2 OUT |
| 0xFE | PROG_B | W 0xFFFE = FPGA PROG_B low, 0x0001 = high |
| 0xFF | HW_STATUS | R: bit1 INIT_B, bit7 FPGA DONE |

## 4. Open sequence (cold and warm; `dscope/device.py`)

1. R 0xF0 (>= 0x3110), R 0xF2 (>= 0x3100). Older firmware would make DSWave run an ISP upgrade: the driver refuses.
2. Claim interface 0. R 0xFF.
3. If bit7 (DONE) = 0: FPGA configuration: W 0xFE=0xFFFE, ~10 ms, W 0xFE=1, poll R 0xFF bit1 (<= 11x),
   W 0xFC = len & 0xFFFF, W 0xFD = len >> 16, EP2 OUT the raw bitstream (466,408 bytes) in one transfer,
   poll R 0xFF bit7 (<= 11 x 50 ms). DSWave takes ~110 ms for this.
4. Wait 100 ms, R 0xF1 (>= 0x3100).
5. Security handshake (§5).
6. R 0xFA ext 0..3 (probe ratio), read calibration: 8 x (W 0xFB = 0x40·i, R 0xFB 64 bytes) = EEPROM 0x000-0x1FF.
7. Idle: W 0x01 = 0, W 0x02 = 8.

Warm open takes ~0.6 s (dominated by the 10 ms read delay).

### FPGA bitstream
Embedded in DSWave.exe 1.2.1 (SHA-256 `A21A1D41...ECA42C`) at file offset 0xC471E0, length 0x3E414, as a `DSLZ`
container: `b'DSLZ'`, u32 BE raw size (466,408), 32-byte SHA-256 of the raw data
(`f45a97ea44ffda110cffc659eaba250d191985f7e60fdfcbd153b57372e9cfa1`), then one LZ4 block.
The raw image starts with 16 x 0xFF and the byte-swapped Spartan-6 sync word `55 99 AA 66`.
`dscope/firmware.py` extracts and verifies it from a local DSWave install; it is never redistributed.

## 5. Security handshake

1. W 0xFB = 0x1000, R 0xFB 16 bytes = key, 8 little-endian u16 words (blank = all 0xFFFF: DSWave would then
   write a new key; the driver refuses instead).
2. W 0x0E = 0, wait 10 ms, W 0x0E = 1. R 0x01: if bit2 (PASS) is already set, done.
3. W 0x0F = 0, W 0x0E = 0x0513.
4. For each key word **in reverse order (word 7 first)**: poll R 0x01 until bit1 (READY), R 0x0F (challenge, unused
   when the key is not blank), W 0x0F = word, W 0x0E = 0x0219.
5. R 0x01: bit2 (PASS) must be set. Without PASS the FPGA still answers, but DSWave treats it as a failure.

## 6. Capture configuration

DSWave's HAL is configured with SCPI-like commands (`:ANALOG:CHANNEL:Vdiv ch,uV`, `:SAMPLEtime ps`,
`:STORAGEdepth S`, `:TRIGger:...`; full vocabulary in `re/notes/recon_dswave_binary.md`) and turns them into a
register program at `:Work:RUN 0,0`. The driver reproduces that program (`dscope/mso.py`, golden-tested
against the recorded runs). Key facts:

* **Channel enable**: ANAn_BASE bit0 = enable, 0x4 = DC (AC clears it), 0x8 = 20 MHz limit.
  VERT_AFOLD = (n_enabled << 8) | log2(fold), analog fold x4 / x2 / x1 for 1 / 2 / 3-4 channels.
  VERT_DEN0 = digital enable mask, VERT_DFOLD likewise with digital fold x4 / x2 / x1 for 1-2 / 3-4 / 5-8 channels.
* **Rates and depth**: S = STORAGEdepth. Base rate = min(100 MHz, S/4 / window) snapped down to 1-2-5
  (1 kHz .. 100 MHz); analog rate = base x afold (max 400 MSa/s), analog depth = S/4 x afold;
  digital likewise with dfold. HORI_ADIV/DDIV = rate dividers (100 MHz / base).
* **Vertical**: ANAn_VERT = calibrated gain code for the V/div (bit15 = attenuation relay, set from 200 mV/div up;
  the only relay click across the range), ANAn_VOFF = offset code (ADC code of 0 V, 128 = centre),
  ANAn_VPWM = offset DAC value from calibration and offset. 235 ADC codes = 8 divisions.
  VERT_ABRH_INV = per-channel invert mask XOR the hardware's own inversion (0x0F).
  ANAn_CROSS = 0xFFFF followed by a 256-entry table per channel (interleave correction); **must be rewritten
  whenever the enabled-channel set changes** (stale tables corrupt levels).
* **Digital threshold**: VERT_DTHR ~ volts x 155 (0..5 V).
* **Trigger**: TRIG_BASE bit0 = enable, bit1 = auto (free-run; normal mode clears it), bit2 = pulse trigger.
  Source and edge go into the trigger memory: TRIG_INDEX = 0x8000 | probe; then indirect words
  0x000 (level mask = ~(1 << bit)), 0x010 = 0xFFFF, 0x100 (polarity: bit set for rising), 0x200 (edge mask = 1 << bit),
  0x400/0x401 (pulse width), then TRIG_SHFT = 0. Analog channels use bits 0-3, digital channels bits 4-11.
  Types: 0 rise, 1 fall, 2 any, 3..6 pulse variants (TRIG_PULSE = 3, 1, 2, 0). TRIG_LEVEL = 8-bit ADC code.
  TRIG_PRES/PREW = pre-trigger size; TRIG_MARGIN = noise margin 0..15; TRIG_HOLD = holdoff in 10 ns units.

## 7. Run / stop

1. Setup program (starts with W 0x01 = 0, ends with W 0x00 = MODE, W 0xF9 = 0).
2. Queue bulk reads on EP 0x86 **before** starting: one 4096-byte head, then 16 data transfers of `pkt_bytes`
   (1,048,320 = 1 MiB - 256 for most single captures; other sizes for slow/stream/repeat modes).
3. Start: W 0x03 = sync (0), W 0x02 = 0, W 0x01 = 1.
4. The head arrives when the capture triggers (normal mode waits forever if nothing triggers), followed by data.
5. Stop (also used to abort a capture that never triggered): read 1 byte from EP 0x84 twice (values 1, 2),
   W 0x01 = 0, W 0x02 = 8, read 1 byte from EP 0x84 (3); cancel the outstanding reads; W 0x01 = 0, W 0x02 = 4.

## 8. Data stream

* **Head** (4096 bytes): u32[0] = 0x55555555; u32[2]: bit31 set = not triggered (free-run), otherwise the trigger
  sample = (u32[2] & 0x3FFFFFFF) + pre-trigger offset (block-rounded `SAMPLEindex`).
* **Single mode, 4 analog + 8 digital**: 320-byte blocks of 64 samples: 256 analog bytes = 8 groups of
  [A0 x8][A1 x8][A2 x8][A3 x8] (8-bit codes), then 64 digital bytes = 8 channels x u64 LE, bit i = sample i.
  Other channel sets / folds: see `re/notes/packet_format.md` and `dscope/packet.py`.
* Only the first N samples (`MSO: send samples: N`) are valid; the device keeps streaming until stopped.
* **Volts** = (code - VOFF) x V/div / 29.375 x probe factor; higher code = higher voltage.

## 9. AWG (MCX output 0..3 V)

Indirect: W 0x08 = awg register, W 0x07 = value. `dscope/awg.py` matches all 13 recorded DSWave programs.

| AWG reg | Meaning |
|---|---|
| 0 WAVE | 0 sine, 1 triangle, 2 square, 3 noise |
| 1 MODE | 0 basic (others: modulation, sweep, burst; not implemented) |
| 2 SWITCH | 1 ... 0 brackets mode / wave / frequency updates |
| 3 ENABLE | 2 = armed, 3 = output running (start = SWITCH 1, ENABLE 3, SWITCH 0; stop = ENABLE 2) |
| 4/5 FCW | floor(f_Hz x 40) |
| 6/7 PHASE | degrees / 360 x 4e9 |
| 8/9 HIGH/LOW | volts x 156 |
| 10/11 DUTY | fraction x 4e9 |
| 12/13 RSLOPE/FSLOPE | floor(68.75 / duty), floor(68.75 / (1 - duty)) |
| 14-17 RPAR/FPAR | int(3846153.2 / f_Hz + 0.5) |

Register names 0x00-0x66 (modulation, sweep, burst, trigger) are listed in `re/notes/recon_dswave_binary.md`.

## 10. Calibration ("zero info")

EEPROM 0x000-0x1FF, 4 channel records of 128 bytes: u16 0x0CAB tag, a per-channel offset-DAC span at byte 5,
12 per-V/div entries `{u16 pwm_base, u16 gain_code, u16 relay}` from byte 10 (gain codes equal DSWave's VGA table:
1 mV 0x31, 2 mV 0x11, 5 mV 0x10, 10 mV 0x00, 20 mV 0x18, 50 mV 0x02, 100 mV 0x1A, 200 mV 0x10001, 500 mV 0x10000,
1 V 0x10018, 2 V 0x10008, 5 V 0x1001A), then 8 signed trims, 0xFF padding. The driver only reads it.
