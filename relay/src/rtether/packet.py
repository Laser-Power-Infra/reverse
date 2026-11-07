"""IPv4 / TCP / UDP parsing and building. Pure functions, no I/O."""

from __future__ import annotations

import ipaddress
import struct
from dataclasses import dataclass

FlowKey = tuple[str, int, str, int]  # (src_ip, src_port, dst_ip, dst_port)

FIN = 0x01
SYN = 0x02
RST = 0x04
PSH = 0x08
ACK = 0x10

PROTO_TCP = 6
PROTO_UDP = 17

_IPV4_HDR = struct.Struct("!BBHHHBBH4s4s")
_TCP_HDR = struct.Struct("!HHIIBBHHH")
_UDP_HDR = struct.Struct("!HHHH")


class PacketError(ValueError):
    """Raised for packets the relay refuses to handle."""


@dataclass(frozen=True)
class IPv4Packet:
    src: str
    dst: str
    protocol: int
    ttl: int
    payload: bytes


@dataclass(frozen=True)
class TCPSegment:
    src_port: int
    dst_port: int
    seq: int
    ack: int
    flags: int
    window: int
    data: bytes


@dataclass(frozen=True)
class UDPDatagram:
    src_port: int
    dst_port: int
    data: bytes


def checksum(data: bytes) -> int:
    """RFC 1071 ones-complement checksum; odd-length input is zero padded."""
    if len(data) % 2:
        data += b"\x00"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total) & 0xFFFF


def _pseudo_checksum(src: str, dst: str, protocol: int, segment: bytes) -> int:
    pseudo = (
        ipaddress.IPv4Address(src).packed
        + ipaddress.IPv4Address(dst).packed
        + struct.pack("!BBH", 0, protocol, len(segment))
    )
    return checksum(pseudo + segment)


def parse_ipv4(raw: bytes) -> IPv4Packet:
    if len(raw) < _IPV4_HDR.size:
        raise PacketError("packet shorter than an IPv4 header")
    (
        ver_ihl,
        _tos,
        total_len,
        _ident,
        flags_frag,
        ttl,
        protocol,
        _hdr_csum,
        src,
        dst,
    ) = _IPV4_HDR.unpack_from(raw)
    if ver_ihl >> 4 != 4:
        raise PacketError("not IPv4")
    header_len = (ver_ihl & 0x0F) * 4
    if header_len < _IPV4_HDR.size or header_len > len(raw):
        raise PacketError("bad IPv4 header length")
    if checksum(raw[:header_len]) != 0:
        raise PacketError("bad IPv4 header checksum")
    if flags_frag & 0x2000 or flags_frag & 0x1FFF:
        raise PacketError("fragmented packets are not supported")
    if total_len < header_len or total_len > len(raw):
        raise PacketError("bad IPv4 total length")
    return IPv4Packet(
        src=str(ipaddress.IPv4Address(src)),
        dst=str(ipaddress.IPv4Address(dst)),
        protocol=protocol,
        ttl=ttl,
        payload=raw[header_len:total_len],
    )


def parse_tcp(ip: IPv4Packet) -> TCPSegment:
    seg = ip.payload
    if len(seg) < _TCP_HDR.size:
        raise PacketError("segment shorter than a TCP header")
    src_port, dst_port, seq, ack, offset_byte, flags, window, _csum, _urg = _TCP_HDR.unpack_from(
        seg
    )
    data_offset = (offset_byte >> 4) * 4
    if data_offset < _TCP_HDR.size or data_offset > len(seg):
        raise PacketError("bad TCP data offset")
    if _pseudo_checksum(ip.src, ip.dst, PROTO_TCP, seg) != 0:
        raise PacketError("bad TCP checksum")
    return TCPSegment(src_port, dst_port, seq, ack, flags & 0x3F, window, seg[data_offset:])


def parse_udp(ip: IPv4Packet) -> UDPDatagram:
    dgram = ip.payload
    if len(dgram) < _UDP_HDR.size:
        raise PacketError("datagram shorter than a UDP header")
    src_port, dst_port, length, csum = _UDP_HDR.unpack_from(dgram)
    if length < _UDP_HDR.size or length > len(dgram):
        raise PacketError("bad UDP length")
    if csum != 0 and _pseudo_checksum(ip.src, ip.dst, PROTO_UDP, dgram[:length]) != 0:
        raise PacketError("bad UDP checksum")
    return UDPDatagram(src_port, dst_port, dgram[_UDP_HDR.size : length])


def build_ipv4(src: str, dst: str, protocol: int, payload: bytes, ttl: int = 64) -> bytes:
    total_len = _IPV4_HDR.size + len(payload)
    header = _IPV4_HDR.pack(
        0x45,
        0,
        total_len,
        0,
        0,
        ttl,
        protocol,
        0,
        ipaddress.IPv4Address(src).packed,
        ipaddress.IPv4Address(dst).packed,
    )
    csum = checksum(header)
    return header[:10] + csum.to_bytes(2, "big") + header[12:] + payload


def build_tcp_packet(src: str, dst: str, seg: TCPSegment, ttl: int = 64) -> bytes:
    header = _TCP_HDR.pack(
        seg.src_port,
        seg.dst_port,
        seg.seq & 0xFFFFFFFF,
        seg.ack & 0xFFFFFFFF,
        (_TCP_HDR.size // 4) << 4,
        seg.flags,
        seg.window,
        0,
        0,
    )
    csum = _pseudo_checksum(src, dst, PROTO_TCP, header + seg.data)
    segment = header[:16] + csum.to_bytes(2, "big") + header[18:] + seg.data
    return build_ipv4(src, dst, PROTO_TCP, segment, ttl)


def build_udp_packet(src: str, dst: str, dgram: UDPDatagram, ttl: int = 64) -> bytes:
    length = _UDP_HDR.size + len(dgram.data)
    header = _UDP_HDR.pack(dgram.src_port, dgram.dst_port, length, 0)
    csum = _pseudo_checksum(src, dst, PROTO_UDP, header + dgram.data) or 0xFFFF
    datagram = header[:6] + csum.to_bytes(2, "big") + dgram.data
    return build_ipv4(src, dst, PROTO_UDP, datagram, ttl)
