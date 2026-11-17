"""Atheris target: arbitrary device bytes through the MSO adapter decoders.

The head packet is kept magic-valid (the device head is checked by parse_head); its trigger word comes from the
input, so the trigger path is fuzzed too. The payload and the frame bytes are arbitrary.

Not collected by pytest. Run on Linux:
    python tests/fuzz/fuzz_adapter.py -max_total_time=300 tests/fuzz/corpus/fuzz_adapter
"""
import struct
import sys
import types

import atheris

with atheris.instrument_imports():
    from openm454 import mso_adapter, packet, scope


def _config():
    s = scope.Settings(analog=['A0', 'A1'], digital=['D0', 'D1'], duration=1e-3, samples=10_000, repeat=2)
    hal, _, _, _ = scope.hal_config(s, repeat=scope.repeat_plan(s))
    prog, geom = scope._compile(hal, scope._cached_zero())
    return geom, hal


GEOM, HAL = _config()


def TestOneInput(data):
    data = bytes(data)
    trig_word = int.from_bytes(data[:4].ljust(4, b'\x00'), 'little')
    head = packet.parse_head(struct.pack('<4I', packet.HEAD_MAGIC, 0, trig_word & 0xFFFFFFFF, 0))
    raw = types.SimpleNamespace(head=head, data=data, frames=[data])
    for decode in (mso_adapter.decode, mso_adapter.decode_frames):
        try:
            decode(raw, GEOM, HAL)
        except (packet.PacketError, ValueError):
            pass


atheris.Setup(sys.argv, TestOneInput)
atheris.Fuzz()
