"""The transport only ever emits bRequest 0xB0/0xB1/0xB2 and refuses unsafe register writes."""
import pytest
from openm454.transport import Transport
from openm454.errors import DscopeError


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
    t.write(0x10, 1); t.read(0xF0); t.nvm_read(0x0000, 64)
    assert {op[2] for op in t.h.ops if op[0] in 'WR'} <= {0xB0, 0xB1, 0xB2}


def test_ctrl_run_bit_only_from_the_engine(t):
    with pytest.raises(DscopeError) as ei:
        t.write(0x01, 1)
    assert ei.value.code == 'UNSAFE_OPERATION' and not t.h.ops and not t.running
    t.write(0x01, 0)                               # stopping is always allowed when idle
    with t.guarded('run'):
        t.write(0x01, 1)
    assert t.running                               # set as the start write is sent
    with pytest.raises(DscopeError):               # plain access while running is refused
        t.write(0x02, 8)


def test_debug_regs_write_refuses_run_registers():
    from types import SimpleNamespace
    from openm454.scope import Scope
    s = Scope()
    s.dev = SimpleNamespace(t=Transport())
    s.dev.t.h = FakeHandle()
    for addr in ('0x00', '0x01', '0x02', '0x03'):
        with pytest.raises(DscopeError) as ei:
            s.debug(SimpleNamespace(debug_cmd='regs-write', addr=addr, value='0'))
        assert ei.value.code == 'UNSAFE_OPERATION'
    assert not s.dev.t.h.ops


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
