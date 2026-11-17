# Quint capture model

capture.qnt is the abstract host/device state machine: modes, trigger/repeat,
stream/log, retries, no-trigger and stop. It is a design model, not the USB packet
encoding.

Invariants: type_ok, no_repeat_in_stream_or_log, accepted_frames_only,
repeats_are_capture_only, no_trigger_is_no_trigger, no_silent_fallback,
quiet_after_untriggered_stop.

Run:

    quint test specs/quint/capture.qnt
    quint verify specs/quint/capture.qnt --init=init --step=step \
      --invariant=type_ok,no_repeat_in_stream_or_log,accepted_frames_only,repeats_are_capture_only,no_trigger_is_no_trigger,no_silent_fallback,quiet_after_untriggered_stop \
      --max-steps=12
