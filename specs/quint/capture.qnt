/// Abstract capture/trigger/repeat/stream state machine for the openm454 driver.
/// This is a design model, not the USB packet encoding.
module capture {
  var mode: int
  var repeat: bool
  var running: bool
  var streaming: bool
  var logging: bool
  var frames: int
  var accepted: int
  var discarded: int
  var error: str
  var quiet: bool

  action init = all {
    mode' = 0,
    repeat' = false,
    running' = false,
    streaming' = false,
    logging' = false,
    frames' = 0,
    accepted' = 0,
    discarded' = 0,
    error' = "",
    quiet' = true,
  }

  action configure(r) = all {
    not(running),
    not(streaming),
    not(logging),
    mode' = if (r) 1 else 0,
    repeat' = r,
    running' = false,
    streaming' = false,
    logging' = false,
    frames' = 0,
    accepted' = 0,
    discarded' = 0,
    error' = "",
    quiet' = true,
  }

  action start = all {
    not(running),
    not(streaming),
    not(logging),
    running' = true,
    mode' = mode,
    repeat' = repeat,
    streaming' = false,
    logging' = false,
    frames' = 0,
    accepted' = 0,
    discarded' = 0,
    error' = "",
    quiet' = false,
  }

  action trigger = all {
    running,
    frames' = frames + 1,
    accepted' = accepted + 1,
    discarded' = discarded,
    running' = running,
    mode' = mode,
    repeat' = repeat,
    streaming' = false,
    logging' = false,
    error' = "",
    quiet' = false,
  }

  action misfire = all {
    running,
    frames' = frames,
    accepted' = accepted,     // SEEDED-DEFECT marker: a misfire never counts as accepted
    discarded' = discarded + 1,
    running' = running,
    mode' = mode,
    repeat' = repeat,
    streaming' = false,
    logging' = false,
    error' = "MISFIRE_RETRY",
    quiet' = false,
  }

  action done = all {
    running,
    running' = false,
    mode' = mode,
    repeat' = repeat,
    streaming' = false,
    logging' = false,
    frames' = frames,
    accepted' = accepted,
    discarded' = discarded,
    error' = "",
    quiet' = true,
  }

  action no_trigger = all {
    running,
    running' = false,
    mode' = mode,
    repeat' = repeat,
    streaming' = false,
    logging' = false,
    frames' = frames,
    accepted' = accepted,
    discarded' = discarded,
    error' = "TIMEOUT_NO_TRIGGER",
    quiet' = true,
  }

  action stream = all {
    not(running),
    not(streaming),
    not(logging),
    not(repeat),
    streaming' = true,
    logging' = false,
    running' = false,
    mode' = 2,
    repeat' = false,
    frames' = 0,
    accepted' = 0,
    discarded' = 0,
    error' = "",
    quiet' = false,
  }

  action log = all {
    not(running),
    not(streaming),
    not(logging),
    not(repeat),
    logging' = true,
    streaming' = false,
    running' = false,
    mode' = 0,                 // logging is a host path; it does not put the device in repeat mode 3
    repeat' = false,
    frames' = 0,
    accepted' = 0,
    discarded' = 0,
    error' = "",
    quiet' = false,
  }

  action stop = all {
    streaming or logging,
    streaming' = false,
    logging' = false,
    running' = false,
    mode' = 0,
    repeat' = false,
    frames' = frames,
    accepted' = accepted,
    discarded' = discarded,
    error' = "",
    quiet' = true,
  }

  action step = any {
    configure(false),
    configure(true),
    start,
    trigger,
    misfire,
    done,
    no_trigger,
    stream,
    log,
    stop,
  }

  val type_ok =
    (mode >= 0 and mode <= 3) and frames >= 0 and accepted >= 0 and discarded >= 0

  val no_repeat_in_stream_or_log =
    not((streaming or logging) and repeat)

  val accepted_frames_only =
    accepted <= frames and discarded >= 0

  val misfire_discards_only =
    (error == "MISFIRE_RETRY") implies (discarded >= 1)

  val no_trigger_is_no_trigger =
    (error == "TIMEOUT_NO_TRIGGER") implies (not(running) and not(streaming) and not(logging))

  // repeat compiles to HW_BASE_MODE 1; running a repeat capture in any other mode would be a silent fallback
  val no_silent_fallback =
    not(running and repeat and mode != 1)

  val quiet_after_untriggered_stop =
    (not(streaming) and not(logging) and not(running)) implies quiet

  run repeat_ok =
    init
      .then(configure(true))
      .then(start)
      .then(trigger)
      .then(trigger)
      .then(done)

  run no_trigger_ok =
    init
      .then(configure(true))
      .then(start)
      .then(no_trigger)

  run stream_ok =
    init
      .then(configure(false))
      .then(stream)
      .then(stop)

  run log_ok =
    init
      .then(configure(false))
      .then(log)
      .then(stop)
}
