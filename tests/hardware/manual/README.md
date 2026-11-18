# Manual group: needs hands on the bench, not implemented

These cannot be driven by pytest from here: they need a person (a cable out, a suspend/resume, a power cycle) or
they would break the run they are meant to observe. They are listed in `bench.json` under `manual` and are not
collected as tests, so the suite never reports them as skips.

| case | how | what to check |
|---|---|---|
| unplug / replug while a run is active | pull the USB cable during a stream, then plug it back | the run ends with a device error (not a hang), `openm454 recover` brings the scope back, the next capture is complete |
| suspend / resume in the middle of a stream | `rtcwake -m mem -s 20` on the host while streaming | the stream reports the gap or stops cleanly; no half-written .npz; the next open works |
| usbfs budget exhausted | `echo 2 > /sys/module/usbcore/parameters/usbfs_memory_mb` (root), run a capture, then restore the old value | `NO_MEM` with the usbfs hint, not a silent retry loop |
| power cycle of the scope | unplug the barrel jack, plug it back | the next open reloads the FPGA (`fpga_configured_now` true); no stale lock |
| generator reprogrammed while armed | `openm454 awg set ...` during a capture | refused by the stop handshake, the run is unaffected |

TODO: turn the first three into tests once there is a bench that can be reset between runs (a switchable USB hub and a
way to hand the device back to the suite), and only then mark them `hwsoak` or their own marker.
