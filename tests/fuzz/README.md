# Atheris fuzz targets

Linux-only Atheris scripts, not pytest tests. They are excluded from the normal suite and run in the
nightly fuzz job (`.github/workflows/fuzz.yml`).

    python tests/fuzz/fuzz_packet.py -max_total_time=300 tests/fuzz/corpus/fuzz_packet
    python tests/fuzz/fuzz_adapter.py -max_total_time=300 tests/fuzz/corpus/fuzz_adapter

Needs the fuzz extra (`pip install -e ".[fuzz]"`) and `OPENM454_NO_USB=1`. Python coverage comes from
`atheris.instrument_imports()`, so the target imports the driver inside that block; without it the corpus stays
flat (only the native interpreter paths are instrumented).

- `fuzz_packet.py`: arbitrary bytes through `packet.split_frames` and `packet.decode` (frame layout from a
  2 analog / 2 digital / repeat capture).
- `fuzz_adapter.py`: arbitrary payload through `mso_adapter.decode` and `mso_adapter.decode_frames`; the head
  stays magic-valid (that is what `packet.parse_head` enforces on device bytes) and its trigger word comes from
  the input, so the trigger path is fuzzed too.

Both assert "no crash, only defined errors": `packet.PacketError` and `ValueError` are the accepted outcomes.
The checked-in `corpus/*/*.seed` files are the starting seed; generated units are git-ignored and uploaded as a
job artifact. A crash is promoted to a seed and, where possible, to a fixed Hypothesis example.
