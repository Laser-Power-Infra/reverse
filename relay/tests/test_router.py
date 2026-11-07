import asyncio
import time

import pytest

from rtether.packet import (
    ACK,
    PSH,
    RST,
    SYN,
    TCPSegment,
    UDPDatagram,
    build_ipv4,
    build_tcp_packet,
    build_udp_packet,
    parse_ipv4,
    parse_tcp,
)
from rtether.router import DNS_IDLE, TCP_HALF_CLOSED_IDLE, TCP_IDLE, UDP_IDLE, Router

PHONE = "10.8.0.2"


@pytest.fixture
async def tcp_port():
    async def hold(reader, writer):
        await reader.read()
        writer.close()

    server = await asyncio.start_server(hold, "127.0.0.1", 0)
    yield server.sockets[0].getsockname()[1]
    server.close()
    await server.wait_closed()


def syn_packet(port: int, dst: str = "127.0.0.1") -> bytes:
    return build_tcp_packet(PHONE, dst, TCPSegment(40000, port, 100, 0, SYN, 65535, b""))


def udp_packet(port: int, dst: str = "127.0.0.1") -> bytes:
    return build_udp_packet(PHONE, dst, UDPDatagram(40001, port, b"x"))


def make_router(sent: list[bytes], allow_loopback: bool = True) -> Router:
    return Router(sent.append, "1.1.1.1", allow_loopback=allow_loopback)


async def settle(router: Router) -> None:
    for flow in list(router.tcp_flows.values()):
        if flow.connect_task is not None:
            await flow.connect_task


def test_constants():
    assert (TCP_IDLE, TCP_HALF_CLOSED_IDLE, UDP_IDLE, DNS_IDLE) == (300, 30, 60, 10)


async def test_tcp_syn_creates_flow(tcp_port):
    sent: list[bytes] = []
    router = make_router(sent)
    await router.handle(syn_packet(tcp_port))
    assert len(router.tcp_flows) == 1
    await settle(router)
    assert router.stats.rx == 1
    assert router.stats.tx == 1
    router.reset()


async def test_udp_creates_flow(tcp_port):
    sent: list[bytes] = []
    router = make_router(sent)
    await router.handle(udp_packet(tcp_port))
    assert len(router.udp_flows) == 1
    router.reset()


async def test_other_protocol_dropped():
    sent: list[bytes] = []
    router = make_router(sent)
    await router.handle(build_ipv4(PHONE, "127.0.0.1", 1, b"\x08\x00\x00\x00"))
    assert router.stats.dropped == 1
    assert not router.tcp_flows and not router.udp_flows


async def test_garbage_frame_is_dropped_and_stream_continues(tcp_port):
    sent: list[bytes] = []
    router = make_router(sent)
    await router.handle(b"\xff" * 30)
    await router.handle(syn_packet(tcp_port))
    assert router.stats.dropped == 1
    assert len(router.tcp_flows) == 1
    await settle(router)
    router.reset()


async def test_evict_idle_removes_old_flows(tcp_port):
    sent: list[bytes] = []
    router = make_router(sent)
    await router.handle(syn_packet(tcp_port))
    await router.handle(udp_packet(tcp_port))
    await settle(router)
    now = time.monotonic()
    assert router.evict_idle(now) == 0
    next(iter(router.tcp_flows.values())).last_activity = now - 301
    assert router.evict_idle(now) == 1
    assert not router.tcp_flows and len(router.udp_flows) == 1
    next(iter(router.udp_flows.values())).last_activity = now - 61
    assert router.evict_idle(now) == 1
    assert not router.udp_flows


async def test_reset_closes_everything(tcp_port):
    sent: list[bytes] = []
    router = make_router(sent)
    await router.handle(syn_packet(tcp_port))
    await router.handle(udp_packet(tcp_port))
    await settle(router)
    router.reset()
    assert not router.tcp_flows and not router.udp_flows
    assert parse_tcp(parse_ipv4(sent[-1])).flags & RST


async def test_summary_format(tcp_port):
    sent: list[bytes] = []
    router = make_router(sent)
    await router.handle(syn_packet(tcp_port))
    await settle(router)
    assert router.summary() == "tcp=1 udp=0 rx=1 tx=1 dropped=0"
    router.reset()


# --- findings from the whole-branch review -------------------------------------


async def test_syn_to_virtual_subnet_gets_immediate_rst():
    """C1b: nothing lives in 10.8.0.0/24; answer at once instead of a 21 s connect."""
    sent: list[bytes] = []
    router = make_router(sent)
    await router.handle(syn_packet(853, dst="10.8.0.1"))
    assert not router.tcp_flows
    seg = parse_tcp(parse_ipv4(sent[-1]))
    assert seg.flags & RST
    assert seg.ack == 101


async def test_stray_segment_gets_rst():
    """I3: after a relay restart the phone's old connections must be reset, not blackholed."""
    sent: list[bytes] = []
    router = make_router(sent)
    stray = build_tcp_packet(
        PHONE, "127.0.0.1", TCPSegment(40000, 80, 500, 9000, PSH | ACK, 65535, b"GET /")
    )
    await router.handle(stray)
    assert not router.tcp_flows
    seg = parse_tcp(parse_ipv4(sent[-1]))
    assert seg.flags & RST
    assert seg.seq == 9000
    assert seg.ack == 505


async def test_loopback_blocked_by_default(tcp_port):
    """I5: the phone must not reach services bound to the PC's own loopback."""
    sent: list[bytes] = []
    router = make_router(sent, allow_loopback=False)
    await router.handle(syn_packet(tcp_port))
    assert not router.tcp_flows
    assert parse_tcp(parse_ipv4(sent[-1])).flags & RST
    await router.handle(udp_packet(tcp_port))
    assert not router.udp_flows
    assert router.stats.dropped == 1
