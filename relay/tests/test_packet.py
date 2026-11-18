import pytest

from rtether.packet import (
    ACK,
    SYN,
    PacketError,
    TCPSegment,
    UDPDatagram,
    build_tcp_packet,
    build_udp_packet,
    checksum,
    parse_ip,
    parse_tcp,
    parse_udp,
)

# IPv4 header from the Wikipedia "IPv4 header checksum" example, plus a zero payload.
WIKI_HEADER = bytes.fromhex("45000073000040004011b861c0a80001c0a800c7")
WIKI_PACKET = WIKI_HEADER + bytes(95)


def test_checksum_rfc1071_example():
    assert checksum(bytes.fromhex("0001f203f4f5f6f7")) == 0x220D


def test_parse_ip_wikipedia_header():
    pkt = parse_ip(WIKI_PACKET)
    assert pkt.src == "192.168.0.1"
    assert pkt.dst == "192.168.0.199"
    assert pkt.protocol == 17
    assert pkt.ttl == 64
    assert len(pkt.payload) == 95


def test_parse_ip_bad_checksum_raises():
    bad = bytearray(WIKI_PACKET)
    bad[12] ^= 0x01
    with pytest.raises(PacketError):
        parse_ip(bytes(bad))


def test_parse_ip_rejects_other_versions_and_short():
    with pytest.raises(PacketError):
        parse_ip(b"\x70" + bytes(39))  # version 7
    with pytest.raises(PacketError):
        parse_ip(b"\x60" + bytes(38))  # IPv6, one byte short
    with pytest.raises(PacketError):
        parse_ip(b"\x45" * 10)


def test_parse_ip_rejects_fragment():
    frag = bytearray(WIKI_PACKET)
    frag[6] |= 0x20  # more-fragments flag
    frag[10:12] = b"\x00\x00"
    frag[10:12] = checksum(bytes(frag[:20])).to_bytes(2, "big")
    with pytest.raises(PacketError):
        parse_ip(bytes(frag))


def test_tcp_round_trip():
    seg = TCPSegment(40000, 443, 1, 2, SYN | ACK, 65535, b"hi")
    raw = build_tcp_packet("10.8.0.2", "1.1.1.1", seg)
    ip = parse_ip(raw)
    assert ip.src == "10.8.0.2"
    assert ip.dst == "1.1.1.1"
    assert ip.protocol == 6
    assert parse_tcp(ip) == seg


def test_tcp_checksum_verified():
    seg = TCPSegment(40000, 443, 1, 2, ACK, 65535, b"payload")
    raw = bytearray(build_tcp_packet("10.8.0.2", "1.1.1.1", seg))
    raw[-1] ^= 0xFF
    with pytest.raises(PacketError):
        parse_tcp(parse_ip(bytes(raw)))


def test_udp_round_trip():
    dgram = UDPDatagram(5353, 53, bytes(12))
    raw = build_udp_packet("10.8.0.2", "10.8.0.1", dgram)
    ip = parse_ip(raw)
    assert ip.protocol == 17
    assert parse_udp(ip) == dgram


PHONE6, SITE6 = "fd00:8::2", "2001:db8::1"


def test_ipv6_tcp_and_udp_round_trip():
    raw = build_tcp_packet(PHONE6, SITE6, TCPSegment(40000, 443, 7, 0, SYN, 65535, b"hi"))
    ip = parse_ip(raw)
    assert (ip.src, ip.dst, len(raw)) == (PHONE6, SITE6, 40 + 20 + 2)
    assert parse_tcp(ip).data == b"hi"
    udp = parse_ip(build_udp_packet(PHONE6, SITE6, UDPDatagram(40001, 53, b"q")))
    assert parse_udp(udp).data == b"q"


def test_ipv6_bad_checksum_and_zero_udp_checksum_rejected():
    raw = bytearray(build_tcp_packet(PHONE6, SITE6, TCPSegment(1, 2, 0, 0, ACK, 1, b"x")))
    raw[-1] ^= 0xFF
    with pytest.raises(PacketError):
        parse_tcp(parse_ip(bytes(raw)))
    udp = bytearray(build_udp_packet(PHONE6, SITE6, UDPDatagram(1, 2, b"x")))
    udp[46:48] = b"\x00\x00"
    with pytest.raises(PacketError):
        parse_udp(parse_ip(bytes(udp)))


def test_icmpv6_unreachable_answers_the_sender():
    from rtether.packet import PROTO_ICMPV6, _pseudo_checksum, build_icmpv6_unreachable

    original = build_udp_packet(PHONE6, SITE6, UDPDatagram(40001, 443, b"quic"))
    reply = parse_ip(build_icmpv6_unreachable(original))
    assert (reply.src, reply.dst, reply.protocol) == (SITE6, PHONE6, PROTO_ICMPV6)
    assert reply.payload[:2] == b"\x01\x00" and reply.payload[8:] == original
    assert _pseudo_checksum(reply.src, reply.dst, PROTO_ICMPV6, reply.payload) == 0


def test_unreachable_and_echo_reply_for_ipv4():
    from rtether.packet import build_echo_reply, build_ip, build_unreachable

    original = build_udp_packet("10.8.0.2", "1.2.3.4", UDPDatagram(40001, 443, b"quic"))
    reply = parse_ip(build_unreachable(original))
    assert (reply.src, reply.dst, reply.protocol) == ("1.2.3.4", "10.8.0.2", 1)
    assert reply.payload[:2] == b"\x03\x03" and reply.payload[8:] == original[:28]
    assert checksum(reply.payload) == 0

    ping = build_ip("10.8.0.2", "8.8.8.8", 1, b"\x08\x00\xf7\xff\x00\x01\x00\x02data")
    pong = parse_ip(build_echo_reply(ping))
    assert (pong.src, pong.dst) == ("8.8.8.8", "10.8.0.2")
    assert pong.payload[0] == 0 and pong.payload[4:] == b"\x00\x01\x00\x02data"
    assert checksum(pong.payload) == 0
    assert build_echo_reply(original) is None


def test_echo_reply_for_ipv6():
    from rtether.packet import PROTO_ICMPV6, _pseudo_checksum, build_echo_reply, build_ip

    ping = build_ip(PHONE6, SITE6, PROTO_ICMPV6, b"\x80\x00\x00\x00\x00\x01\x00\x02data")
    pong = parse_ip(build_echo_reply(ping))
    assert (pong.src, pong.dst, pong.payload[0]) == (SITE6, PHONE6, 129)
    assert _pseudo_checksum(pong.src, pong.dst, PROTO_ICMPV6, pong.payload) == 0
