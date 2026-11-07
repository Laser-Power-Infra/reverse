"""A stand-in for the Android app: speaks the framed packet protocol to the relay."""

from __future__ import annotations

import asyncio

from rtether.framing import encode_frame, read_frame
from rtether.packet import (
    ACK,
    FIN,
    PSH,
    RST,
    SYN,
    IPv4Packet,
    TCPSegment,
    UDPDatagram,
    build_tcp_packet,
    build_udp_packet,
    parse_ipv4,
    parse_tcp,
    parse_udp,
)

PHONE_IP = "10.8.0.2"


class FakePhone:
    def __init__(self, phone_ip: str = PHONE_IP):
        self.ip = phone_ip
        self._next_port = 40000

    async def connect(self, port: int) -> None:
        self.reader, self.writer = await asyncio.open_connection("127.0.0.1", port)

    async def close(self) -> None:
        self.writer.close()
        await self.writer.wait_closed()

    async def _send(self, packet: bytes) -> None:
        self.writer.write(encode_frame(packet))
        await self.writer.drain()

    async def _recv(self, timeout: float = 2.0) -> IPv4Packet:
        while True:
            frame = await asyncio.wait_for(read_frame(self.reader), timeout)
            if frame is None:
                raise ConnectionError("relay closed the stream")
            if frame:
                return parse_ipv4(frame)

    async def _recv_tcp(self, port: int) -> TCPSegment:
        while True:
            ip = await self._recv()
            if ip.protocol == 6:
                seg = parse_tcp(ip)
                if seg.dst_port == port:
                    return seg

    async def http_get(self, dst_ip: str, dst_port: int, path: str) -> bytes:
        port = self._next_port
        self._next_port += 1

        def seg(flags: int, seq: int, ack: int, data: bytes = b"") -> bytes:
            return build_tcp_packet(
                self.ip, dst_ip, TCPSegment(port, dst_port, seq, ack, flags, 65535, data)
            )

        seq = 1000
        await self._send(seg(SYN, seq, 0))
        synack = await self._recv_tcp(port)
        assert synack.flags == SYN | ACK, synack
        seq += 1
        ack = synack.seq + 1
        request = f"GET {path} HTTP/1.0\r\nHost: {dst_ip}\r\n\r\n".encode()
        await self._send(seg(PSH | ACK, seq, ack, request))
        seq += len(request)
        body = b""
        fin_seen = False
        while not fin_seen:
            s = await self._recv_tcp(port)
            if s.flags & RST:
                raise ConnectionError("relay reset the connection")
            if s.seq != ack:
                continue  # pure ACK or something we already have
            if s.data:
                body += s.data
                ack += len(s.data)
            if s.flags & FIN:
                ack += 1
                fin_seen = True
            if s.data or s.flags & FIN:
                await self._send(seg(ACK, seq, ack))
        await self._send(seg(FIN | ACK, seq, ack))
        last = await self._recv_tcp(port)
        assert last.flags & ACK
        return body

    async def udp_echo(self, dst_ip: str, dst_port: int, payload: bytes) -> bytes:
        port = self._next_port
        self._next_port += 1
        await self._send(
            build_udp_packet(self.ip, dst_ip, UDPDatagram(port, dst_port, payload))
        )
        while True:
            ip = await self._recv()
            if ip.protocol == 17:
                dgram = parse_udp(ip)
                if dgram.dst_port == port:
                    return dgram.data
