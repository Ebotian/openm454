"""docs/schemas/decode.json describes what the decoders return: every output validates, every key is documented."""
import json
import os

import numpy as np

from dscope import protosim as sim
from dscope.capture import Capture
from dscope.decode import decode

SCHEMA = json.load(open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), 'docs', 'schemas',
                                     'decode.json'), encoding='utf-8'))
TYPES = {'object': dict, 'array': list, 'string': str, 'boolean': bool, 'null': type(None)}


def _is(value, t):
    if t == 'integer':
        return isinstance(value, (int, np.integer)) and not isinstance(value, bool)
    if t == 'number':
        return isinstance(value, (int, float, np.integer, np.floating)) and not isinstance(value, bool)
    return isinstance(value, TYPES[t])


def validate(value, schema, path='$'):
    """The JSON Schema subset decode.json uses: type, enum, const, minimum/maximum, required, properties, items,
    $ref (#/$defs/...), allOf, if/then. Returns a list of problems."""
    if '$ref' in schema:
        schema = dict(SCHEMA['$defs'][schema['$ref'].split('/')[-1]], **{k: v for k, v in schema.items() if k != '$ref'})
    bad = []
    if 'type' in schema:
        types = schema['type'] if isinstance(schema['type'], list) else [schema['type']]
        if not any(_is(value, t) for t in types):
            return ['%s: %r is not %s' % (path, value, types)]
    if 'enum' in schema and value not in schema['enum']:
        bad.append('%s: %r not in %s' % (path, value, schema['enum']))
    if 'const' in schema and value != schema['const']:
        bad.append('%s: %r != %r' % (path, value, schema['const']))
    if 'minimum' in schema and value < schema['minimum']:
        bad.append('%s: %r < %r' % (path, value, schema['minimum']))
    if 'maximum' in schema and value > schema['maximum']:
        bad.append('%s: %r > %r' % (path, value, schema['maximum']))
    if isinstance(value, dict):
        bad += ['%s: missing %s' % (path, k) for k in schema.get('required', []) if k not in value]
        for k, sub in schema.get('properties', {}).items():
            if k in value:
                bad += validate(value[k], sub, '%s.%s' % (path, k))
    if isinstance(value, list) and 'items' in schema:
        for i, v in enumerate(value):
            bad += validate(v, schema['items'], '%s[%d]' % (path, i))
    for sub in schema.get('allOf', []):
        bad += validate(value, sub, path)
    if 'if' in schema and not validate(value, schema['if'], path):
        bad += validate(value, schema['then'], path)
    return bad


def documented(result):
    """Keys the decoder emitted that the schema does not mention."""
    proto = result['protocol']
    branch = next(b['then'] for b in SCHEMA['allOf'] if b['if']['properties']['protocol']['const'] == proto)
    top = set(SCHEMA['properties']) | set(branch['properties'])
    frame = SCHEMA['$defs']['%s_frame' % proto]['properties']
    seg = SCHEMA['$defs']['i2c_segment']['properties']
    missing = {'top.' + k for k in result if k not in top}
    for f in result['frames'] + result.get('error_frames', []):
        missing |= {'frame.' + k for k in f if k not in frame}
        for s in f.get('segments', []):
            missing |= {'segment.' + k for k in s if k not in seg}
    return missing


def cap_of(rate, **lines):
    n = max(len(v) for v in lines.values())
    dig = {k: np.pad(np.asarray(v, np.uint8), (0, n - len(v)), mode='edge') for k, v in lines.items()}
    return Capture({'digital_rate_hz': rate, 't0_digital_s': 0.0, 'analog_rate_hz': None, 't0_analog_s': 0.0,
                    'channels': {}}, digital=dig)


def results():
    out = []
    line = sim.uart(list(b'OK') + [0x55], 9600, 1e6, parity='even', stop_errors=(2,), gap_bits=1, invert=True)
    out.append(decode(cap_of(1e6, D4=line), 'uart:rx=D4,baud=auto,parity=even,invert=1'))
    cs, clk, mosi, miso = sim.spi([0x12, 0x34, 0xAB], [1, 2, 3], half=4)
    out.append(decode(cap_of(10e6, D0=cs[:-30], D1=clk[:-30], D2=miso[:-30], D3=mosi[:-30]),
                      'spi:clk=D1,mosi=D3,miso=D2,cs=D0'))
    cs, clk, mosi, miso = sim.spi([0x1234, 0xBEEF], bits=16, half=4)
    out.append(decode(cap_of(10e6, D1=clk, D3=mosi), 'spi:clk=D1,mosi=D3,bits=16'))
    tr = [{'addr': 0x53, 'data': [0x32], 'restart': {'addr': 0x53, 'read': True, 'data': [1, 2]}},
          {'addr': 0x2A, 'nack_addr': True}, {'addr': 0x2A5, 'addr10': True, 'data': [9]},
          {'addr': 0x7A, 'read': True, 'data': [3]}]
    scl, sda = sim.i2c(tr, quarter=5)
    rise = np.flatnonzero(np.diff(scl) == 1) + 1
    out.append(decode(cap_of(10e6, D0=scl[rise[3]:], D1=sda[rise[3]:]), 'i2c:scl=D0,sda=D1', limit=2))
    frames = [dict(ident=0x123, data=b'\x01\x02'), dict(ident=0x1ABCDE12, data=b'', extended=True),
              dict(ident=0x55, remote=True, dlc=1, ack=False), dict(ident=0x10, data=b'\xff', crc_xor=4)]
    bits = sim.can_frame_bits(0x0F0, b'\xff')[:20] + [0] * 6 + [1] * 8
    out.append(decode(cap_of(10e6, D6=sim.can(frames + [bits], 125e3, 10e6)), 'can:rx=D6,bitrate=auto'))
    out.append(decode(cap_of(1e6, D4=np.ones(1000, np.uint8)), 'uart:rx=D4,baud=auto'))   # a quiet line
    return out


def test_every_decoder_output_matches_the_schema():
    for r in results():
        assert validate(r, SCHEMA) == [], r['protocol']


def test_every_emitted_key_is_documented():
    for r in results():
        assert documented(r) == set(), r['protocol']


def test_the_validator_catches_mistakes():
    r = results()[3]
    bad = json.loads(json.dumps(r, default=int))
    bad['count'] = -1
    bad['frames'][0]['segments'][0]['acks'] = ['yes']
    del bad['frames'][0]['stop']
    problems = validate(bad, SCHEMA)
    assert len(problems) == 3, problems
