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
    parse_ipv4,
    parse_tcp,
    parse_udp,
)

# IPv4 header from the Wikipedia "IPv4 header checksum" example, plus a zero payload.
WIKI_HEADER = bytes.fromhex("45000073000040004011b861c0a80001c0a800c7")
WIKI_PACKET = WIKI_HEADER + bytes(95)


def test_checksum_rfc1071_example():
    assert checksum(bytes.fromhex("0001f203f4f5f6f7")) == 0x220D


def test_parse_ipv4_wikipedia_header():
    pkt = parse_ipv4(WIKI_PACKET)
    assert pkt.src == "192.168.0.1"
    assert pkt.dst == "192.168.0.199"
    assert pkt.protocol == 17
    assert pkt.ttl == 64
    assert len(pkt.payload) == 95


def test_parse_ipv4_bad_checksum_raises():
    bad = bytearray(WIKI_PACKET)
    bad[12] ^= 0x01
    with pytest.raises(PacketError):
        parse_ipv4(bytes(bad))


def test_parse_ipv4_rejects_ipv6_and_short():
    with pytest.raises(PacketError):
        parse_ipv4(b"\x60" + bytes(39))
    with pytest.raises(PacketError):
        parse_ipv4(b"\x45" * 10)


def test_parse_ipv4_rejects_fragment():
    frag = bytearray(WIKI_PACKET)
    frag[6] |= 0x20  # more-fragments flag
    frag[10:12] = b"\x00\x00"
    frag[10:12] = checksum(bytes(frag[:20])).to_bytes(2, "big")
    with pytest.raises(PacketError):
        parse_ipv4(bytes(frag))


def test_tcp_round_trip():
    seg = TCPSegment(40000, 443, 1, 2, SYN | ACK, 65535, b"hi")
    raw = build_tcp_packet("10.8.0.2", "1.1.1.1", seg)
    ip = parse_ipv4(raw)
    assert ip.src == "10.8.0.2"
    assert ip.dst == "1.1.1.1"
    assert ip.protocol == 6
    assert parse_tcp(ip) == seg


def test_tcp_checksum_verified():
    seg = TCPSegment(40000, 443, 1, 2, ACK, 65535, b"payload")
    raw = bytearray(build_tcp_packet("10.8.0.2", "1.1.1.1", seg))
    raw[-1] ^= 0xFF
    with pytest.raises(PacketError):
        parse_tcp(parse_ipv4(bytes(raw)))


def test_udp_round_trip():
    dgram = UDPDatagram(5353, 53, bytes(12))
    raw = build_udp_packet("10.8.0.2", "10.8.0.1", dgram)
    ip = parse_ipv4(raw)
    assert ip.protocol == 17
    assert parse_udp(ip) == dgram
