# Recorded DSWave material (`traces` tests)

Some tests compare the driver against what DSWave 1.2.1 itself sent to the scope. The recordings are not in the
repository: every recorded session contains the device's security key (written to registers 0x0E/0x0F at open), and
the bulk data is ~900 MB. Those tests carry the `traces` marker and skip without the material:

```
python -m pytest tests -m "not traces"     # everything that runs from the repository alone
python -m pytest tests -m traces           # the recorded-material group (needs the files below)
```

| test | reads |
| --- | --- |
| `tests/golden/test_mso_golden.py` | `re/traces/S01_warm/scen/*.json`, `state/cache/zero_info_from_trace.bin` |
| `tests/golden/test_packet_golden.py` | `re/traces/S01_warm/scen/*.json` + `bulk_in.bin` |
| `tests/golden/test_repeat_golden.py` | the same session (`repeat_mode`) + the zero info |
| `tests/integration/test_integration_replay.py` | the same session + the zero info |
| `tests/golden/test_awg_modes_golden.py` | a session that recorded `corpus5` (`--extract`, see the file's docstring) |

## What a session is

One DSWave 1.2.1 run, driving one M454, with every USB call DSWave makes logged:

```
re/traces/<SESSION>/
  trace.jsonl       every control and bulk transfer, one JSON object per event
  bulk_in.bin       the bulk IN payloads (EP 0x86), referenced by offset from the scenario files
  scen/<name>.json  one per scenario: {name, scpi [[cmd, ret]...], run_ret, stopped, log [HAL log lines],
                    config_writes, run_writes [[kind, register, addr, value]...],
                    bulk_in [{seq, pipe, len, actual, err, bin_off, bin_len, head}...]}
```

`state/cache/zero_info_from_trace.bin` is the 512-byte zero-info block (EEPROM 0x000-0x1FF, `docs/PROTOCOL.md`
section 10) of the unit that was recorded. It is per unit: the golden setup programs (VPWM words) only match it on
the unit the session was recorded from. The driver reads the same block at every open (`Scope().dev.zero_info`).

The scenarios are the corpora in `tests/fixtures/scenarios/` (`corpus1.json` .. `corpus5.json`): each is a list of
`{name, scpi: [SCPI commands], run, timeout}`, replayed in file order, corpus1 to corpus4 as one session
(`S01_warm`: the SCPI state accumulates across scenarios exactly as in DSWave's HAL, which the replay in
`tests/golden/test_mso_golden.py` models), corpus5 as a session of its own.

## How the existing sessions were made

1. A copy of DSWave 1.2.1 (never the installed one), the scope on USB 3, nothing else holding it.
2. DSWave started under a USB call tracer that logs every WinUSB call with its payload (the `trace.jsonl` /
   `bulk_in.bin` above).
3. Each scenario's SCPI commands injected into DSWave's HAL in corpus order, one acquisition per scenario where the
   scenario has `run`, the register writes and bulk transfers of each run cut out into `scen/<name>.json`.

The tracer and the SCPI injector are not part of this repository: they instrument the vendor application.
Recording needs the hardware and the vendor software, and nothing in the material may be published as is: strip
every write to 0x0E/0x0F before anything leaves the machine.

A recording made without instrumenting DSWave is possible from the USB side alone (USBPcap on Windows, usbmon on
Linux) while DSWave is driven by hand: that gives `trace.jsonl`-equivalent control and bulk transfers, but only
for settings the UI can produce, and the conversion into `scen/*.json` has to be written. The DSWave loop-mode
recording in `tests/fixtures/dswave/` was made that way and reduced to its setup block (no key writes), which is
why it is in the repository and runs in CI.

## Pending

A small sanitized fixture extracted from `S01_warm` (setup blocks without 0x0E/0x0F, a few short bulk records) would
let most of the `traces` group run in CI. Not done yet.
