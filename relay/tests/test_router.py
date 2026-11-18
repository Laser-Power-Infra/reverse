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
    build_ip,
    build_tcp_packet,
    build_udp_packet,
    checksum,
    parse_ip,
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


QUIC_INITIAL = b"\xc0\x00\x00\x00\x01" + bytes(1195)


def udp_packet(port: int, dst: str = "127.0.0.1", data: bytes = b"x") -> bytes:
    return build_udp_packet(PHONE, dst, UDPDatagram(40001, port, data))


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
    await router.handle(build_ip(PHONE, "127.0.0.1", 1, b"\x08\x00\x00\x00"))
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
    assert parse_tcp(parse_ip(sent[-1])).flags & RST


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
    seg = parse_tcp(parse_ip(sent[-1]))
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
    seg = parse_tcp(parse_ip(sent[-1]))
    assert seg.flags & RST
    assert seg.seq == 9000
    assert seg.ack == 505


async def test_two_sources_get_independent_flows(tcp_port):
    """Hub mode relies on this: packets from 10.8.0.2 and 10.8.0.10 are separate flows."""
    sent: list[bytes] = []
    router = make_router(sent)
    same_ports = TCPSegment(40000, tcp_port, 100, 0, SYN, 65535, b"")
    await router.handle(build_tcp_packet("10.8.0.2", "127.0.0.1", same_ports))
    await router.handle(build_tcp_packet("10.8.0.10", "127.0.0.1", same_ports))
    await settle(router)
    assert len(router.tcp_flows) == 2
    destinations = sorted(parse_ip(raw).dst for raw in sent)
    assert destinations == ["10.8.0.10", "10.8.0.2"]
    router.reset()


async def test_loopback_blocked_by_default(tcp_port):
    """I5: the phone must not reach services bound to the PC's own loopback."""
    sent: list[bytes] = []
    router = make_router(sent, allow_loopback=False)
    await router.handle(syn_packet(tcp_port))
    assert not router.tcp_flows
    assert parse_tcp(parse_ip(sent[-1])).flags & RST
    await router.handle(udp_packet(tcp_port))
    assert not router.udp_flows
    assert router.stats.dropped == 1


PHONE6 = "fd00:8::2"


async def test_ipv6_tcp_flow_connects_over_loopback():
    async def hold(reader, writer):
        await reader.read()
        writer.close()

    server = await asyncio.start_server(hold, "::1", 0)
    port = server.sockets[0].getsockname()[1]
    sent: list[bytes] = []
    router = make_router(sent)
    await router.handle(
        build_tcp_packet(PHONE6, "::1", TCPSegment(40000, port, 100, 0, SYN, 65535, b""))
    )
    await settle(router)
    reply = parse_ip(sent[-1])
    assert (reply.src, reply.dst) == ("::1", PHONE6)
    assert parse_tcp(reply).flags == SYN | ACK
    router.reset()
    server.close()
    await server.wait_closed()


async def test_without_pc_ipv6_the_phone_is_refused_at_once():
    sent: list[bytes] = []
    router = Router(sent.append, "1.1.1.1", has_ipv6=False)
    site = "2001:db8::1"
    await router.handle(
        build_tcp_packet(PHONE6, site, TCPSegment(40000, 443, 100, 0, SYN, 65535, b""))
    )
    assert parse_tcp(parse_ip(sent[-1])).flags & RST
    await router.handle(build_udp_packet(PHONE6, site, UDPDatagram(40001, 443, b"quic")))
    assert parse_ip(sent[-1]).protocol == 58  # ICMPv6 unreachable
    assert not router.tcp_flows and not router.udp_flows
    assert router.blocked == {"ipv6: not available on this PC": 2}


async def test_blocked_report_names_what_did_not_get_through():
    async def refuse(host, port):
        raise ConnectionRefusedError

    sent: list[bytes] = []
    router = Router(sent.append, "1.1.1.1", connector=refuse)
    router._learn_names(
        b"\x12\x34\x81\x80\x00\x01\x00\x01\x00\x00\x00\x00\x07example\x03com\x00\x00\x01\x00\x01"
        b"\xc0\x0c\x00\x01\x00\x01\x00\x00\x00\x3c\x00\x04\x5d\xb8\xd8\x22"
    )
    await router.handle(syn_packet(443, "93.184.216.34"))
    await settle(router)
    await router.handle(build_ip(PHONE, "8.8.8.8", 1, b"\x08\x00\x00\x00"))  # ping
    await router.handle(build_ip(PHONE, "224.0.0.251", 2, b"\x00"))  # multicast: not reported
    await router.handle(udp_packet(9, "192.0.2.1"))  # nobody answers
    router.evict_idle(time.monotonic() + UDP_IDLE + 1)
    assert router.blocked == {
        "tcp 93.184.216.34:443 (example.com): refused": 1,
        "icmp 8.8.8.8: only TCP, UDP and ping are carried": 1,
        "udp 192.0.2.1:9: no reply": 1,
    }
    assert router.blocked_report()[0].endswith("refused")


async def test_quic_is_refused_at_once_unless_allowed():
    sent: list[bytes] = []
    router = make_router(sent)
    await router.handle(udp_packet(443, "1.2.3.4", QUIC_INITIAL))
    assert parse_ip(sent[-1]).protocol == 1  # ICMP unreachable, straight back
    assert not router.udp_flows
    assert list(router.blocked) == ["quic (udp 443): refused so apps use TCP"]

    allowed = Router(sent.append, "1.1.1.1", allow_quic=True)
    await allowed.handle(udp_packet(443, "127.0.0.1"))  # loopback is refused, not as QUIC
    assert "quic" not in "".join(allowed.blocked)
    allowed.reset()


async def test_ping_is_answered():
    sent: list[bytes] = []
    router = make_router(sent)
    await router.handle(build_ip(PHONE, "8.8.8.8", 1, b"\x08\x00\xf7\xff\x00\x01\x00\x02"))
    reply = parse_ip(sent[-1])
    assert (reply.src, reply.dst, reply.payload[0]) == ("8.8.8.8", PHONE, 0)
    assert not router.blocked


def test_dns_to_a_private_address_goes_to_the_pc_dns_server():
    from rtether.udp import UDPFlow

    def remote(dst, port, dns="8.8.8.8"):
        return UDPFlow((PHONE, 40001), lambda packet: None, dns).remote_for(dst, port)

    assert remote("10.79.54.103", 53) == ("8.8.8.8", 53)  # the hub's hotspot address
    assert remote("10.8.0.1", 53) == ("8.8.8.8", 53)
    assert remote("9.9.9.9", 53) == ("9.9.9.9", 53)  # a public resolver is left alone
    assert remote("10.0.0.5", 53, dns="10.0.0.5") == ("10.0.0.5", 53)  # the office's own
    assert remote("10.79.54.103", 5000) == ("10.79.54.103", 5000)


async def test_quic_is_refused_even_when_that_phone_port_already_has_a_flow(tcp_port):
    sent: list[bytes] = []
    router = make_router(sent)
    await router.handle(udp_packet(tcp_port))  # same phone port, another destination
    await router.handle(udp_packet(443, "1.2.3.4", QUIC_INITIAL))
    assert parse_ip(sent[-1]).protocol == 1
    assert len(router.udp_flows) == 1
    assert next(iter(router.udp_flows.values())).destinations == {("127.0.0.1", tcp_port)}
    router.reset()


async def test_a_fragmented_packet_is_reported(caplog):
    router = make_router([])
    packet = bytearray(udp_packet(9, "192.0.2.1"))
    packet[6] |= 0x20  # "more fragments"
    packet[10:12] = bytes(2)
    packet[10:12] = checksum(bytes(packet[:20])).to_bytes(2, "big")
    with caplog.at_level("INFO", logger="rtether.router"):
        await router.handle(bytes(packet))
        await router.handle(bytes(packet))
    assert router.blocked == {"bad packet: fragmented packets are not supported": 2}
    assert caplog.text.count("blocked: bad packet") == 1


async def test_a_game_on_udp_443_is_not_mistaken_for_quic():
    sent: list[bytes] = []
    router = make_router(sent)
    await router.handle(udp_packet(443, "1.2.3.4", b"\xff\xff game hello"))
    assert not router.blocked and not sent
    assert len(router.udp_flows) == 1
    router.reset()
