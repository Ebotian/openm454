# Behavior matrix

What the suite is supposed to cover, and which layer covers it. A critical row has at least two independent
layers: an example/golden oracle (left) and a generated one (right). Update this table when a behavior is added;
`docs/test-report.md` records the measured numbers.

| behavior | unit | property | stateful | golden | integration | fuzz | spec |
| --- | :--: | :--: | :--: | :--: | :--: | :--: | :--: |
| channel counts (1/2/4 analog, 1/2/4/8 digital, permutations) | ✅ | ✅ | ✅ | ✅ |  |  |  |
| timing: default profile, floor profile, fixed keys refused, untriggered 100 ms stop | ✅ | ✅ | ✅ |  |  |  |  |
| trigger: none, rise/fall, any, normal/auto/force, holdoff | ✅ |  | ✅ | ✅ |  |  |  |
| trigger: a misfire is re-armed and the retry is returned; a triggered run without a head is `TIMEOUT_NO_TRIGGER` |  |  | ✅ |  | ✅ |  | ✅ |
| repeat: 0/1/N frames, forced vs triggered, plan limits, mode-3 refusal | ✅ |  | ✅ | ✅ |  |  |  |
| repeat: one start, the frames are neither checked nor re-armed |  |  | ✅ |  |  |  |  |
| stream/log: repeat is refused | ✅ |  | ✅ |  | ✅ |  | ✅ |
| stream/log: live vs record-first, chunk sizes, save/load | ✅ |  |  |  | ✅ |  |  |
| packet: head/tail, first vs later frame, block sizes, endianness/permutation, malformed input | ✅ | ✅ |  | ✅ |  | ✅ |  |
| setup/stop: setup program, start, stop, cross-table caches, repeated runs |  |  | ✅ | ✅ |  |  |  |
| stop: an untriggered stop leaves the run quiet (the 100 ms hold is in `acquire`) |  |  | ✅ |  |  |  | ✅ |
| interfaces: CLI, MCP, API, save/load, info/watch/log | ✅ | ✅ |  |  | ✅ |  |  |
| hardware: real M454 golden traces (`hw` marker; no pytest hardware test exists yet) |  |  |  |  |  |  |  |

## Notes on the marks

- **What the Quint model encodes:** modes 0/1/2, the repeat flag, running/streaming/logging, frame counters and a
  `quiet` flag. It does not encode payloads, the capture-spacing profile or its floors, plan limits, chunk sizes,
  save/load, cross-table caches or the 100 ms hold itself; the `spec` marks are only on the rows it encodes.
- **timing:** the profile floors are covered by unit, property and stateful tests; the model only knows that a stop
  returns to quiet.
- **repeat:** repeat mode is one start; this baseline neither checks the frames for misfires nor re-arms them
  (`trigger_check_reason` says so), so there is no retry frame to keep. The misfire retry row is the normal
  (non-repeat) path.
- **channel counts and fuzz:** both fuzz targets use one fixed 2 analog / 2 digital layout, so they say nothing about
  other channel counts; the fuzz mark on the packet row is about the byte surface, not the layout.
- **hardware:** `test_selftest` is an offline fake-scope test and lives in `tests/unit/`; there is no pytest test
  that needs the scope today, so the hardware row is documentation of recorded bench runs, not a CI layer.
- **selftest** appears in the interfaces row because it is a CLI/MCP-facing command with an offline oracle.
