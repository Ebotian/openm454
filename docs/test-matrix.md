# Behavior matrix

What the suite is supposed to cover, and which layer covers it. A critical row has at least two independent
layers: an example/golden oracle (left) and a generated one (right). Update this table when a behavior is added;
`docs/test-report.md` records the measured numbers.

| behavior | unit | property | stateful | golden | integration | fuzz | spec |
| --- | :--: | :--: | :--: | :--: | :--: | :--: | :--: |
| channel counts (1/2/4 analog, 1/2/4/8 digital, permutations) | ✅ | ✅ | ✅ | ✅ |  |  |  |
| timing: default profile, floor profile, fixed keys refused, untriggered 100 ms stop | ✅ | ✅ | ✅ |  |  |  |  |
| trigger: none, rise/fall, any, normal/auto/force, holdoff | ✅ |  | ✅ | ✅ |  |  |  |
| trigger: misfire detection, re-arm, retries share the wait, no-trigger is `TIMEOUT_NO_TRIGGER` |  |  | ✅ |  | ✅ |  | ✅ |
| repeat: 0/1/N frames, forced vs triggered, mode-3 refusal, plan limits | ✅ |  | ✅ | ✅ |  |  | ✅ |
| repeat: misfire retry keeps the retry frames, stop holds 100 ms |  |  | ✅ |  |  |  |  |
| stream/log: live vs record-first, chunk sizes, repeat refusal, save/load | ✅ |  | ✅ |  | ✅ |  | ✅ |
| packet: head/tail, first vs later frame, block sizes, endianness/permutation, malformed input | ✅ | ✅ |  | ✅ |  | ✅ |  |
| setup/stop: setup program, start, stop, cross-table caches, repeated runs |  |  | ✅ | ✅ |  |  | ✅ |
| interfaces: CLI, MCP, API, save/load, info/watch/log | ✅ | ✅ |  |  | ✅ |  |  |
| hardware: real M454 golden traces (`hw` marker; no pytest hardware test exists yet) |  |  |  |  |  |  |  |

## Notes on the marks

- **timing and spec:** the Quint model has a single `quiet` flag for the stop, not the capture-spacing profile, so
  the profile floors are covered by unit, property and stateful tests only.
- **channel counts and fuzz:** both fuzz targets use one fixed 2 analog / 2 digital layout, so they say nothing about
  other channel counts; the fuzz mark on the packet row is about the byte surface, not the layout.
- **hardware:** `test_selftest` is an offline fake-scope test and lives in `tests/unit/`; there is no pytest test that
  needs the scope today, so the hardware row is documentation of recorded bench runs, not a CI layer.
- **selftest** appears in the interfaces row because it is a CLI/MCP-facing command with an offline oracle.

## Layers

| layer | files | how it runs |
| --- | --- | --- |
| unit | `tests/unit/` | fast suite |
| property | `tests/property/` (Hypothesis) | fast suite |
| stateful | `tests/stateful/` (fake device + acquisition engine) | fast suite |
| golden | `tests/golden/` (register programs, AWG, repeat snapshots) | fast suite |
| integration | `tests/integration/` (CLI, MCP, logger, replay) | fast suite; `slow` marked tests only in the full suite |
| fuzz | `tests/fuzz/` (Atheris) | nightly job, not pytest |
| spec | `specs/quint/` (Quint + Apalache) | nightly/dispatch job, not pytest |
| hardware | `tests/hardware/` (`hw` marker) | never in CI |

## Independent oracles

- Repeat: the old DSWave snapshot (`tests/fixtures/dswave/triggered_repeat_setup.json`, pinned in
  `tests/golden/test_golden.py`) and the new remote's recorded repeat trace (`tests/golden/test_repeat_golden.py`)
  are two different recordings; the stateful machine and the Quint model check the decisions, not the bytes.
- Timing: `tests/unit/test_timing.py` fixes the profile rules, `tests/property/test_timing_laws.py` checks the
  floor law for every key, and `tests/stateful/test_acquire_sim.py` checks the traced 100 ms holds.
- Packet: `tests/golden/test_packet_golden.py` pins recorded layouts, `tests/property/test_packet.py` checks the
  layout algebra, `tests/fuzz/fuzz_packet.py` and `fuzz_adapter.py` search the byte surface for crashes.

## Not covered by design

- Windows timing/interaction behavior: measured earlier on the bench, not reproducible in CI.
- The USB transport itself (`libusb`): the offline suite runs a fake device; `hw` tests own the real path.
- Benchmark/soak durations: the Linux 1000/1000 evidence is a recorded run, not a test.
