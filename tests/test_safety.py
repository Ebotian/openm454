"""The transport only ever emits bRequest 0xB0/0xB1/0xB2 and refuses unsafe register writes."""
import pytest
from dscope.transport import Transport
from dscope.errors import DscopeError


class FakeHandle:
    def __init__(self):
        self.ops = []

    def controlWrite(self, rt, req, v, i, data, timeout=0):
        self.ops.append(('W', rt, req, bytes(data))); return len(data)

    def controlRead(self, rt, req, v, i, n, timeout=0):
        self.ops.append(('R', rt, req, n)); return bytes(n)

    def bulkWrite(self, ep, data, timeout=0):
        self.ops.append(('B', ep, len(data))); return len(data)


@pytest.fixture
def t():
    tr = Transport(); tr.h = FakeHandle(); return tr


def test_only_allowed_requests(t):
    t.write(0x01, 1); t.read(0xF0); t.nvm_read(0x0000, 64)
    assert {op[2] for op in t.h.ops if op[0] in 'WR'} <= {0xB0, 0xB1, 0xB2}


@pytest.mark.parametrize('addr,val', [(0xFE, 0xFFFE), (0xFC, 1), (0xFD, 1), (0xFB, 0x1000), (0xF0, 1), (0x0E, 1), (0x0F, 1), (0xF4, 1)])
def test_refused_writes(t, addr, val):
    with pytest.raises(DscopeError):
        t.write(addr, val)
    assert not t.h.ops


def test_ep2_only_in_fpga_context(t):
    with pytest.raises(DscopeError):
        t.fpga_bulk_write(b'x' * 10)
    with t.guarded('fpga'):
        t.write(0xFC, 10); t.fpga_bulk_write(b'x' * 10)
    t.nvm_read(0, 4)                               # sets fb_written
    with t.guarded('fpga'):
        with pytest.raises(DscopeError):
            t.fpga_bulk_write(b'x' * 10)


def test_forbidden_request_assert(t):
    with pytest.raises(AssertionError):
        t._ctrl_out(0xB3, b'\x00')
