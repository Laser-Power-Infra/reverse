"""Dispatch phone packets to per-flow TCP/UDP translators and keep the flow tables."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass

from rtether.dns import answers
from rtether.packet import (
    ACK,
    PROTO_TCP,
    PROTO_UDP,
    RST,
    SYN,
    FlowKey,
    PacketError,
    TCPSegment,
    build_echo_reply,
    build_tcp_packet,
    build_unreachable,
    parse_ip,
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

# The tunnel itself; nothing lives here. A phone at 10.8.0.X is also fd00:8::X.
VIRTUAL_SUBNETS = (ipaddress.ip_network("10.8.0.0/24"), ipaddress.ip_network("fd00:8::/64"))
NO_IPV6 = "ipv6: not available on this PC"
QUIC_REFUSED = "quic (udp 443): refused so apps use TCP"


def _is_quic_initial(data: bytes) -> bool:
    """True for the packet that opens a QUIC connection (RFC 9000 long header, v1 or v2).

    Games also use UDP port 443 for their own protocols; those must pass.
    """
    return (
        len(data) >= 1200
        and data[0] & 0xC0 == 0xC0
        and data[1:5] in (b"\x00\x00\x00\x01", b"\x6b\x33\x43\xcf")
    )


_PROTOCOL_NAMES = {1: "icmp", 58: "icmpv6"}


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
        has_ipv6: bool = True,
        allow_quic: bool = False,
    ):
        self._raw_send = send
        self._dns_server = dns_server
        self._connector = connector
        self._allow_loopback = allow_loopback
        self._has_ipv6 = has_ipv6
        self._allow_quic = allow_quic
        self.stats = Stats()
        self.blocked: Counter[str] = Counter()
        self._names: dict[str, str] = {}  # address -> name, learned from DNS replies
        self.tcp_flows: dict[FlowKey, TCPFlow] = {}
        self.udp_flows: dict[tuple[str, int], UDPFlow] = {}  # by phone address and port

    def _send(self, packet: bytes) -> None:
        self.stats.tx += 1
        self._raw_send(packet)

    def _unreachable(self, dst: str) -> str | None:
        """Why the relay will not connect to `dst`, or None when it will."""
        address = ipaddress.ip_address(dst)
        if any(address in subnet for subnet in VIRTUAL_SUBNETS):
            return "inside the tunnel"
        if address.is_loopback and not self._allow_loopback:
            return "the PC's own loopback"
        if address.version == 6 and not self._has_ipv6:
            return NO_IPV6
        return None

    def _block(self, kind: str, dst: str, port: int | None, reason: str) -> None:
        """Count something the phone tried that did not get through; log it the first time."""
        if reason in (NO_IPV6, QUIC_REFUSED):
            line = reason  # one line for all of it, or every site would be listed twice
        else:
            if ipaddress.ip_address(dst).is_multicast:
                return  # local discovery chatter, never meant to leave the phone
            where = dst if port is None else f"[{dst}]:{port}" if ":" in dst else f"{dst}:{port}"
            name = self._names.get(dst)
            line = f"{kind} {where}{f' ({name})' if name else ''}: {reason}"
        if not self.blocked[line]:
            log.info("blocked: %s", line)
        self.blocked[line] += 1

    def _trace(self, what: str, src: str, sport: int, dst: str, port: int, how: str = "") -> None:
        """One line per conversation start/end, shown by `--verbose`, to see what an app does."""
        if log.isEnabledFor(logging.DEBUG):
            name = self._names.get(dst)
            log.debug(
                "%s %s:%d -> %s:%d%s%s",
                what, src, sport, dst, port, f" ({name})" if name else "", f": {how}" if how else "",
            )

    def blocked_report(self) -> list[str]:
        return [f"{count:6d}x  {line}" for line, count in self.blocked.most_common()]

    def _learn_names(self, reply: bytes) -> None:
        for name, address in answers(reply):
            self._names[address] = name

    async def handle(self, raw: bytes) -> None:
        self.stats.rx += 1
        try:
            ip = parse_ip(raw)
            if ip.protocol == PROTO_TCP:
                await self._handle_tcp(ip.src, ip.dst, parse_tcp(ip))
            elif ip.protocol == PROTO_UDP:
                await self._handle_udp(ip.src, ip.dst, parse_udp(ip), raw)
            elif reply := build_echo_reply(raw):
                # shortcut: the relay answers a ping itself without contacting the host, so
                # a ping cannot show that a host is down; send a real one (Windows
                # IcmpSendEcho) if ping is ever needed as a diagnostic.
                self._send(reply)
            else:
                self.stats.dropped += 1
                name = _PROTOCOL_NAMES.get(ip.protocol, f"protocol {ip.protocol}")
                self._block(name, ip.dst, None, "only TCP, UDP and ping are carried")
        except PacketError as exc:
            self.stats.dropped += 1
            line = f"bad packet: {exc}"
            if not self.blocked[line]:
                log.info("blocked: %s", line)
            self.blocked[line] += 1

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
            reason = self._unreachable(dst)
            if reason:
                self._block("tcp", dst, seg.dst_port, reason)
                self._trace("tcp refused", src, seg.src_port, dst, seg.dst_port, reason)
                self._refuse_syn(src, dst, seg)
                return
            flow = TCPFlow(
                key,
                self._send,
                self._connector,
                lambda why: self._block("tcp", dst, seg.dst_port, why),
            )
            self.tcp_flows[key] = flow
            self._trace("tcp start", src, seg.src_port, dst, seg.dst_port)
        await flow.handle(seg)
        if flow.state == "CLOSED":
            self.tcp_flows.pop(key, None)
            self._trace("tcp end", src, seg.src_port, dst, seg.dst_port, "closed")

    def _refuse_syn(self, src: str, dst: str, seg: TCPSegment) -> None:
        reply = TCPSegment(seg.dst_port, seg.src_port, 0, seg.seq + 1, RST | ACK, 0, b"")
        self._send(build_tcp_packet(dst, src, reply))

    def _reset_stray(self, src: str, dst: str, seg: TCPSegment) -> None:
        """A segment for a connection we do not know (relay restarted): reset it."""
        ack = (seg.seq + len(seg.data)) % (1 << 32)
        reply = TCPSegment(seg.dst_port, seg.src_port, seg.ack, ack, RST | ACK, 0, b"")
        self._send(build_tcp_packet(dst, src, reply))

    async def _handle_udp(self, src: str, dst: str, dgram, raw: bytes) -> None:
        # Checked per datagram: one flow (one phone port) serves every destination.
        reason = self._unreachable(dst)
        quic = dgram.dst_port == 443 and _is_quic_initial(dgram.data)
        if not reason and quic and not self._allow_quic:
            reason = QUIC_REFUSED  # many networks drop QUIC; apps then wait for a timeout
        if reason and not (dst == "10.8.0.1" and dgram.dst_port == 53):
            self.stats.dropped += 1
            self._block("udp", dst, dgram.dst_port, reason)
            self._trace("udp refused", src, dgram.src_port, dst, dgram.dst_port, reason)
            # Tell the app at once so it falls back instead of timing out.
            self._send(build_unreachable(raw))
            return
        key = (src, dgram.src_port)
        flow = self.udp_flows.get(key)
        if flow is None:
            flow = UDPFlow(key, self._send, self._dns_server, on_dns=self._learn_names)
            self.udp_flows[key] = flow
        if (dst, dgram.dst_port) not in flow.destinations:
            self._trace("udp start", src, dgram.src_port, dst, dgram.dst_port)
        await flow.handle(dst, dgram)
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
                how = "closed" if flow.state == "CLOSED" else f"reset after {limit} s idle"
                self._trace("tcp end", *key, how)
                flow.close(send_rst=True)
                del self.tcp_flows[key]
                evicted += 1
        for key, flow in list(self.udp_flows.items()):
            limit = DNS_IDLE if flow.is_dns else UDP_IDLE
            if flow.closed or now - flow.last_activity > limit:
                for dst, port in sorted(flow.destinations):
                    replies = flow.replies[(dst, port)]
                    self._trace("udp end", *key, dst, port, f"{replies} replies")
                    if not replies:
                        self._block("udp", dst, port, "no reply")
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
