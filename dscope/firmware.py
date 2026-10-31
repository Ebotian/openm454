"""Extract the M454 FPGA bitstream from a locally installed DSWave.exe (never redistributed).

DSWave 1.2.1 embeds the bitstream in a 'DSLZ' container: b'DSLZ' + u32 BE raw size + SHA-256(raw) + LZ4 block.
"""
import hashlib
import os
import struct

from .errors import DscopeError

DSWAVE_EXE_SHA256 = 'A21A1D410593C4981F1D10309E7A819E60BBACA2558563249EE939BB37ECA42C'
DSLZ_OFFSET, DSLZ_LEN = 0xC471E0, 0x3E414
RAW_SIZE = 466408
RAW_SHA256 = 'f45a97ea44ffda110cffc659eaba250d191985f7e60fdfcbd153b57372e9cfa1'
BIN_VERSION = 0x3100

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CANDIDATE_EXES = [os.path.join(ROOT, 'DSWave', 'DSWave.exe')]
CACHE = os.path.join(ROOT, 'firmware', 'm454_fpga_%04x.bin' % BIN_VERSION)


def _lz4_block_decode(src, size):
    try:
        import lz4.block
        return lz4.block.decompress(src, uncompressed_size=size)
    except ImportError:
        pass
    dst = bytearray()
    i, n = 0, len(src)
    while i < n:
        tok = src[i]; i += 1
        lit = tok >> 4
        if lit == 15:
            while True:
                b = src[i]; i += 1; lit += b
                if b != 255:
                    break
        dst += src[i:i + lit]; i += lit
        if i >= n:
            break
        off = src[i] | (src[i + 1] << 8); i += 2
        ml = tok & 15
        if ml == 15:
            while True:
                b = src[i]; i += 1; ml += b
                if b != 255:
                    break
        ml += 4
        start = len(dst) - off
        for k in range(ml):
            dst.append(dst[start + k])
    return bytes(dst)


def extract_from_exe(path):
    data = open(path, 'rb').read()
    if hashlib.sha256(data).hexdigest().upper() != DSWAVE_EXE_SHA256:
        raise DscopeError('FIRMWARE_SOURCE', '%s is not DSWave 1.2.1 (hash mismatch)' % path)
    blob = data[DSLZ_OFFSET:DSLZ_OFFSET + DSLZ_LEN]
    if blob[:4] != b'DSLZ' or struct.unpack('>I', blob[4:8])[0] != RAW_SIZE:
        raise DscopeError('FIRMWARE_SOURCE', 'DSLZ container not found where expected')
    raw = _lz4_block_decode(blob[40:], RAW_SIZE)
    if len(raw) != RAW_SIZE or hashlib.sha256(raw).digest() != blob[8:40] or hashlib.sha256(raw).hexdigest() != RAW_SHA256:
        raise DscopeError('FIRMWARE_SOURCE', 'bitstream checksum mismatch')
    return raw


def bitstream():
    """Return the verified raw FPGA bitstream (cached under firmware/, which is git-ignored)."""
    if os.path.exists(CACHE):
        raw = open(CACHE, 'rb').read()
        if hashlib.sha256(raw).hexdigest() == RAW_SHA256:
            return raw
    for exe in CANDIDATE_EXES:
        if os.path.exists(exe):
            raw = extract_from_exe(exe)
            os.makedirs(os.path.dirname(CACHE), exist_ok=True)
            open(CACHE, 'wb').write(raw)
            return raw
    raise DscopeError('FIRMWARE_SOURCE', 'DSWave 1.2.1 not found; needed once to extract the FPGA bitstream',
                      'install DSWave 1.2.1 or copy its folder to DSWave/ next to the dscope package')


if __name__ == '__main__':
    import sys
    b = bitstream()
    print('bitstream ok: %d bytes, sha256 %s -> %s' % (len(b), RAW_SHA256[:16], CACHE))
    sys.exit(0)
