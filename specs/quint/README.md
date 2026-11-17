# Quint capture model

`capture.qnt` is the abstract host/device state machine: modes, trigger/repeat, stream/log, retries,
no-trigger and stop. It is a design model, not the USB packet encoding. `invariants.qnt` names the properties
that Apalache checks; the definitions live in `capture.qnt`.

Invariants: `type_ok`, `no_repeat_in_stream_or_log`, `accepted_frames_only`, `repeats_are_capture_only`,
`no_trigger_is_no_trigger`, `no_silent_fallback`, `quiet_after_untriggered_stop`.

Bounds: `--max-steps=12`, small mode/flag sets, no data payloads — the whole check runs in about a second.

## Run

    quint test specs/quint/capture.qnt --match='repeat_ok|no_trigger_ok|stream_ok|log_ok'
    quint verify specs/quint/capture.qnt --main=capture --init=init --step=step --max-steps=12 \
      --invariant=type_ok,no_repeat_in_stream_or_log,accepted_frames_only,repeats_are_capture_only,no_trigger_is_no_trigger,no_silent_fallback,quiet_after_untriggered_stop

`quint verify` needs Apalache (0.63.0 tested). Without a server it downloads its own pinned 0.62.4; with an
`apalache-mc server` running, point it at the server instead:

    apalache-mc server &
    quint verify ... --server-endpoint localhost:8822

The `spec` job in `.github/workflows/spec.yml` installs Quint and Apalache, starts the server, and runs the
model tests, the invariants, and the seeded-defect check.

## Model vs. driver

The model abstracts away the USB encoding and the timing profile. It encodes the *decisions* the audit found:
a repeat capture is capture-only and refuses stream/log; a misfire is retried, not accepted; an untriggered run
reports `TIMEOUT_NO_TRIGGER` (never "the device did not start"); a stop returns to quiet; and start/stop never
silently fall back to another mode.

## Seeded defect

`check-seeded.sh` copies the model, injects one known bug (`misfire` counts an accepted frame), and requires
Apalache to report the violation. The trace is kept in `counterexamples/accepted_frames_only.itf.json`; the model
itself is never modified. This is the acceptance check that the spec actually constrains the behavior and is not
vacuously true.

    QUINT=quint APALACHE_SERVER=localhost:8822 specs/quint/check-seeded.sh
