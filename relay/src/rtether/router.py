"""Dispatch phone packets to per-flow TCP/UDP translators and keep the flow tables."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from collections.abc import Callable
from dataclasses import dataclass

from rtether.packet import (
    ACK,
    PROTO_TCP,
    PROTO_UDP,
    RST,
    SYN,
    FlowKey,
    PacketError,
    TCPSegment,
    build_tcp_packet,
    parse_ipv4,
    parse_tcp,
    parse_udp,
)
from rtether.tcp import Connector, TCPFlow
from rtether.udp import UDPFlow

log = logging.getLogger("rtether.router")

TCP_IDLE = 300
TCP_HALF_CLOSED_IDLE = 30
UDP_IDLE = 60
DNS_IDLE = 10

VIRTUAL_SUBNET = ipaddress.IPv4Network("10.8.0.0/24")  # the tunnel itself; nothing lives here
LOOPBACK = ipaddress.IPv4Network("127.0.0.0/8")


@dataclass
class Stats:
    rx: int = 0
    tx: int = 0
    dropped: int = 0


class Router:
    def __init__(
        self,
        send: Callable[[bytes], None],
        dns_server: str,
        connector: Connector = asyncio.open_connection,
        allow_loopback: bool = False,
    ):
        self._raw_send = send
        self._dns_server = dns_server
        self._connector = connector
        self._allow_loopback = allow_loopback
        self.stats = Stats()
        self.tcp_flows: dict[FlowKey, TCPFlow] = {}
        self.udp_flows: dict[FlowKey, UDPFlow] = {}

    def _send(self, packet: bytes) -> None:
        self.stats.tx += 1
        self._raw_send(packet)

    def _unreachable(self, dst: str) -> bool:
        address = ipaddress.IPv4Address(dst)
        if address in VIRTUAL_SUBNET:
            return True
        return address in LOOPBACK and not self._allow_loopback

    async def handle(self, raw: bytes) -> None:
        self.stats.rx += 1
        try:
            ip = parse_ipv4(raw)
            if ip.protocol == PROTO_TCP:
                await self._handle_tcp(ip.src, ip.dst, parse_tcp(ip))
            elif ip.protocol == PROTO_UDP:
                await self._handle_udp(ip.src, ip.dst, parse_udp(ip))
            else:
                self.stats.dropped += 1
        except PacketError as exc:
            self.stats.dropped += 1
            log.debug("dropped packet: %s", exc)

    async def _handle_tcp(self, src: str, dst: str, seg: TCPSegment) -> None:
        key: FlowKey = (src, seg.src_port, dst, seg.dst_port)
        flow = self.tcp_flows.get(key)
        if flow is not None and flow.state == "CLOSED":
            self.tcp_flows.pop(key, None)
            flow = None
        if flow is None:
            if seg.flags & RST:
                return
            if not seg.flags & SYN:
                self._reset_stray(src, dst, seg)
                return
            if self._unreachable(dst):
                self._refuse_syn(src, dst, seg)
                return
            flow = TCPFlow(key, self._send, self._connector)
            self.tcp_flows[key] = flow
        await flow.handle(seg)
        if flow.state == "CLOSED":
            self.tcp_flows.pop(key, None)

    def _refuse_syn(self, src: str, dst: str, seg: TCPSegment) -> None:
        reply = TCPSegment(seg.dst_port, seg.src_port, 0, seg.seq + 1, RST | ACK, 0, b"")
        self._send(build_tcp_packet(dst, src, reply))

    def _reset_stray(self, src: str, dst: str, seg: TCPSegment) -> None:
        """A segment for a connection we do not know (relay restarted): reset it."""
        ack = (seg.seq + len(seg.data)) % (1 << 32)
        reply = TCPSegment(seg.dst_port, seg.src_port, seg.ack, ack, RST | ACK, 0, b"")
        self._send(build_tcp_packet(dst, src, reply))

    async def _handle_udp(self, src: str, dst: str, dgram) -> None:
        key: FlowKey = (src, dgram.src_port, dst, dgram.dst_port)
        flow = self.udp_flows.get(key)
        if flow is None:
            if self._unreachable(dst) and not (dst == "10.8.0.1" and dgram.dst_port == 53):
                self.stats.dropped += 1
                return
            flow = UDPFlow(key, self._send, self._dns_server)
            self.udp_flows[key] = flow
        await flow.handle(dgram)
        if flow.closed:
            self.udp_flows.pop(key, None)

    def reset(self) -> None:
        for flow in list(self.tcp_flows.values()):
            flow.close(send_rst=True)
        for flow in list(self.udp_flows.values()):
            flow.close()
        self.tcp_flows.clear()
        self.udp_flows.clear()

    def evict_idle(self, now: float) -> int:
        evicted = 0
        for key, flow in list(self.tcp_flows.items()):
            limit = TCP_HALF_CLOSED_IDLE if flow.half_closed else TCP_IDLE
            if flow.state == "CLOSED" or now - flow.last_activity > limit:
                flow.close(send_rst=True)
                del self.tcp_flows[key]
                evicted += 1
        for key, flow in list(self.udp_flows.items()):
            limit = DNS_IDLE if flow.is_dns else UDP_IDLE
            if flow.closed or now - flow.last_activity > limit:
                flow.close()
                del self.udp_flows[key]
                evicted += 1
        return evicted

    def summary(self) -> str:
        s = self.stats
        return (
            f"tcp={len(self.tcp_flows)} udp={len(self.udp_flows)} "
            f"rx={s.rx} tx={s.tx} dropped={s.dropped}"
        )
