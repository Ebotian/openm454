# Test suite report

Measured 2026-11-17 on Linux, Python 3.14.6, `origin/main` `47fcb6c` + `test/consolidate`.
Baseline numbers are the pre-phase measurement of the same commit (`docs/test-inventory.md`).

## Suite

| suite | command | result | wall time |
| --- | --- | --- | ---: |
| fast (PR) | `python -m pytest tests` | 832 passed, 554 skipped | **14.5 s** |
| full offline | `python -m pytest tests -m "not hw"` | 833 passed, 554 skipped | **65.3 s** |
| hardware | `python -m pytest tests -m hw` | needs the M454, never in CI | — |
| model | `quint test specs/quint/capture.qnt --match=...` | 4 runs pass | 0.02 s |
| model invariants | `quint verify ... --max-steps=12` | 7 invariants, no violation | 0.64 s |
| fuzz | `python tests/fuzz/fuzz_packet.py -max_total_time=45` | ~2.0 M execs, no crash | 46 s |

The fast suite is the default marker set (`not hw and not slow`) under `pytest-xdist -n auto --dist loadscope`;
the full suite adds the `slow` test. The 554 skips are the recorded-trace and firmware tests that skip when the
git-ignored fixtures are absent.

## Baseline vs. now

| metric | baseline (`47fcb6c`) | now | note |
| --- | ---: | ---: | --- |
| collected | 1377 | 1386 | +9: properties and the capture machine |
| passed (fast) | 823 | 832 | |
| passed (full) | 823 | 833 | |
| skipped | 554 | 554 | unchanged fixture-driven skips |
| fast wall time | not selectable (86.4 s for everything) | **14.5 s** | excludes the 60 s MCP timeout + the 2.2 s logger test |
| full wall time | 86.40 s | 65.3 s | xdist + one golden runner |
| test files | 26 | 30 modules + 4 support + 2 fuzz | `tests/support` is not collected |
| test LOC | 8396 | 8537 | `tests/**/*.py` including `support/`, `conftest.py`, `fuzz/` |
| tests > 3 s | 1 | 1 | `tests/integration/test_mcp.py::test_log_plan_and_awg_arguments` (60.0 s, `slow`) |

Test LOC grew by 141 lines because Phase 3 added Hypothesis laws and the stateful machine; the LOC target was
"measure first, then decide", and the Phase 2 consolidation removed the duplicated AWG golden file
(`test_awg_golden.py`, 30 lines) into the shared runner without losing assertions.

## Per directory (fast / full)

| directory | files | LOC | tests | skipped | fast | full |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `tests/unit` | 11 | 2392 | 276 | 0 | 3.74 s | 3.87 s |
| `tests/property` | 4 | 116 | 7 | 0 | 0.17 s | 0.18 s |
| `tests/stateful` | 4 | 2185 | 152 | 0 | 13.06 s | 13.00 s |
| `tests/golden` | 6 | 1811 | 838 | 548 | 0.14 s | 0.17 s |
| `tests/integration` | 5 | 2033 | 113 | 6 | 10.34 s | 70.09 s |
| `tests/support` | 4 | — | 0 | 0 | — | — |
| `tests/fuzz` | 2 (+2 seeds) | — | 0 | 0 | — | — |

Per-directory times are the junit sum of test durations, so they exceed the wall time under xdist.

## Slow tests

| test | time | handling |
| --- | ---: | --- |
| `tests/integration/test_mcp.py::test_log_plan_and_awg_arguments` | 60.02 s | `slow`, full suite only |
| `tests/integration/test_logger.py::test_bench_without_device_records_the_allowance` | 2.18 s | stays in the fast suite |
| `tests/integration/test_mcp.py::test_a_hung_command_never_hangs_the_call` | 1.83 s | stays in the fast suite |

## Formal model (Quint + Apalache)

- Model: `specs/quint/capture.qnt` — modes 0/1/2/3, repeat, stream/log, trigger/misfire/retry, no-trigger, stop.
- Invariants: `type_ok`, `no_repeat_in_stream_or_log`, `accepted_frames_only`, `repeats_are_capture_only`,
  `no_trigger_is_no_trigger`, `no_silent_fallback`, `quiet_after_untriggered_stop`; bounds `--max-steps=12`.
- Quint 0.33.1, Apalache 0.63.0 (server on `localhost:8822`; without a server `quint verify` downloads its own
  pinned 0.62.4). `quint test` runs the four named executions in 20 ms; `quint verify` returns `[ok] No violation
  found` in 0.64 s.
- The first verification attempt found a real modeling bug (`log` set `mode = 3`); it is fixed in the model.
- Seeded-defect check: `specs/quint/check-seeded.sh` injects "a misfire counts as accepted" and requires a
  counterexample; the trace is kept in `specs/quint/counterexamples/accepted_frames_only.itf.json`.

## Fuzzing (Atheris)

| target | surface | 45 s run | corpus |
| --- | --- | ---: | --- |
| `fuzz_packet.py` | `packet.split_frames`, `packet.decode` with a 2 analog/2 digital repeat layout | 1,974,611 execs, 0 crashes | 4 seeds → 11 units |
| `fuzz_adapter.py` | `mso_adapter.decode` / `decode_frames` with a magic-valid head and a fuzzed trigger word | 1,969,906 execs, 0 crashes | 4 seeds → 19 units |

Only `PacketError`/`ValueError` are accepted; anything else is a crash. Python coverage requires
`atheris.instrument_imports()`: without it the corpus stays at one unit even at 1.5 M execs/s (observed), because
only native interpreter paths are instrumented.

## Seeded-defect acceptance (Python layers)

Each defect is injected into the driver, the whole fast suite is run, and the source is restored. All four are
caught by existing tests (no new test was needed):

| injected defect | caught by | failing tests |
| --- | --- | ---: |
| a misfire retry drops the retry's frames | `tests/stateful/test_trigger_check.py` | 5 |
| a triggered repeat without a head is reported as "the device did not start" | `tests/stateful/test_repeat_plumbing.py` | 3 |
| repeat mode allows `stream()` | `tests/stateful/test_capture_machine.py`, `tests/stateful/test_repeat_plumbing.py` | 2 |
| the untriggered-stop hold drops from 100 ms to the profile quiet time | `tests/stateful/test_acquire_sim.py` | 6 |

Representative tests: `test_misfires_are_discarded_and_rearmed`,
`test_a_triggered_repeat_without_a_head_is_no_trigger`, `test_stream_refuses_repeat_before_touching_the_device`,
`test_an_untriggered_stop_holds_the_traced_100ms_whatever_the_profile`.

## Deviations from the plan

- The golden layer is one table-driven runner for the AWG programs and the old repeat snapshot
  (`tests/golden/test_golden.py`); the large recorded-family suites (`test_mso_golden`, `test_packet_golden`,
  `test_awg_modes_golden`, `test_repeat_golden`, `test_dswave_program`) stay as separate table-driven files
  rather than being merged into one runner — merging them would have rewritten hundreds of recorded assertions for
  no coverage gain.
- `tests/property/test_packet.py` checks the layout algebra only; the round-trip and split-frame laws were dropped
  after they disagreed with the recorded layouts (they encoded an assumption, not the protocol).
- The spec job uses an Apalache server so `quint verify` does not download a second distribution inside CI.
- The old DSWave repeat snapshot and the new remote repeat trace coexist, as required; both are pinned.
- No hardware was used for this phase; the Linux 1000/1000 floor evidence and the Win11 question are documented in
  `docs/formal-testing-plan.md` §9.

## Reproduce

    python -m pytest tests                     # fast, 14.5 s, xdist
    python -m pytest tests -m "not hw"         # full offline, 65.3 s
    quint test specs/quint/capture.qnt --main=capture --match='repeat_ok|no_trigger_ok|stream_ok|log_ok'
    apalache-mc server &                       # then:
    quint verify specs/quint/capture.qnt --main=capture --init=init --step=step --max-steps=12 \
      --server-endpoint localhost:8822 --invariant=type_ok,no_repeat_in_stream_or_log,accepted_frames_only,repeats_are_capture_only,no_trigger_is_no_trigger,no_silent_fallback,quiet_after_untriggered_stop
    QUINT=quint APALACHE_SERVER=localhost:8822 specs/quint/check-seeded.sh
    python tests/fuzz/fuzz_packet.py -max_total_time=300 tests/fuzz/corpus/fuzz_packet
