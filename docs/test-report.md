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
| fuzz | `python tests/fuzz/fuzz_packet.py -max_total_time=300` | 12.9 M execs, no crash | 301 s |

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
| test files | 26 | 29 modules + 4 support + 2 fuzz + `conftest.py` | `tests/support` is not collected |
| test LOC | 8396 | 8759 | `git ls-files 'tests/**/*.py'` (includes `support/`, `conftest.py`, `fuzz/`) |
| tests > 3 s | 1 | 1 | `tests/integration/test_mcp.py::test_log_plan_and_awg_arguments` (60.0 s, `slow`) |

Test LOC grew because Phase 3 added Hypothesis laws and the stateful machine, and because the shared support
package now owns the fakes, the USB simulator, the CLI helpers and the logger fakes (487 lines that used to be
inline in three test modules). The LOC target was "measure first, then decide": the consolidation removed the
duplicated AWG golden file and merged the DSWave loop-mode block into the runner without losing assertions.

## Per directory (fast / full)

| directory | files | LOC | tests | skipped | fast | full |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `tests/unit` | 11 | 2336 | 276 | 0 | 3.79 s | 3.76 s |
| `tests/property` | 4 | 111 | 7 | 0 | 0.33 s | 0.33 s |
| `tests/stateful` | 4 | 1967 | 152 | 0 | 12.92 s | 12.91 s |
| `tests/golden` | 5 | 1788 | 838 | 548 | 0.14 s | 0.13 s |
| `tests/integration` | 5 | 1964 | 113 | 6 | 10.51 s | 70.19 s |
| `tests/support` | 4 | 487 | 0 | 0 | — | — |
| `tests/fuzz` | 2 | 84 | 0 | 0 | — | — |

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

| target | surface | 5 min run | corpus |
| --- | --- | ---: | --- |
| `fuzz_packet.py` | `packet.split_frames`, `packet.decode` with a 2 analog/2 digital repeat layout | 12,863,193 execs, 0 crashes | 4 seeds + 7 generated units |
| `fuzz_adapter.py` | `mso_adapter.decode` / `decode_frames` with a magic-valid head and a fuzzed trigger word | 12,841,387 execs, 0 crashes | 4 seeds + 17 generated units |

The nightly job runs the same targets with `-max_total_time=300` (5 min each, inside the 5-10 min budget).

Only `PacketError`/`ValueError` are accepted; anything else is a crash.

**These runs are crash-robustness checks, not coverage-guided exploration.** The targets import the driver inside
`atheris.instrument_imports()`, but libFuzzer's own counters stay flat for the whole run (`cov: 72 ft: 72
corp: 1/1b` in both five-minute logs), and the generated units in the corpus directory are only the ones the
initial seed sweep produced. Atheris instruments native code, and the Python hooks it installs here do not move
the counters. The targets are still worth running - three million executions per minute against the byte-level
decoders with defined-error assertions - but this report does not claim corpus-driven exploration.

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

- The golden layer is one table-driven runner for the AWG programs, the old repeat snapshot and the DSWave
  loop-mode setup block (`tests/golden/test_golden.py`). The recorded families that read their own traces
  (`test_mso_golden`, `test_packet_golden`, `test_awg_modes_golden`, `test_repeat_golden`) stay as separate
  table-driven files: they are already table-driven over their fixture, and merging them would rewrite hundreds of
  recorded assertions for no coverage gain.
- `tests/support/` owns every shared fake: `fakes.py` (scope fakes, `ScriptEngine`, the selftest scope), `sim.py`
  (the USB transport simulator used by the engine tests), `cli.py` (CLI JSON and watch helpers) and `logging.py`
  (loop-mode logger fakes). No test imports another test module any more.
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
