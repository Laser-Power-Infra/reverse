"""Dispatch phone packets to per-flow TCP/UDP translators and keep the flow tables."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass

from rtether.packet import (
    PROTO_TCP,
    PROTO_UDP,
    SYN,
    FlowKey,
    PacketError,
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
    ):
        self._raw_send = send
        self._dns_server = dns_server
        self._connector = connector
        self.stats = Stats()
        self.tcp_flows: dict[FlowKey, TCPFlow] = {}
        self.udp_flows: dict[FlowKey, UDPFlow] = {}

    def _send(self, packet: bytes) -> None:
        self.stats.tx += 1
        self._raw_send(packet)

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

    async def _handle_tcp(self, src: str, dst: str, seg) -> None:
        key: FlowKey = (src, seg.src_port, dst, seg.dst_port)
        flow = self.tcp_flows.get(key)
        if flow is None:
            if not seg.flags & SYN:
                self.stats.dropped += 1
                return
            flow = TCPFlow(key, self._send, self._connector)
            self.tcp_flows[key] = flow
        await flow.handle(seg)
        if flow.state == "CLOSED":
            self.tcp_flows.pop(key, None)

    async def _handle_udp(self, src: str, dst: str, dgram) -> None:
        key: FlowKey = (src, dgram.src_port, dst, dgram.dst_port)
        flow = self.udp_flows.get(key)
        if flow is None:
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
