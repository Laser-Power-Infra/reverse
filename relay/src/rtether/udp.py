"""UDP relay: one PC datagram socket per phone port, with DNS redirected to a real server."""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time
from collections import Counter
from collections.abc import Callable

from rtether.dns import DNS_VIRTUAL
from rtether.packet import UDPDatagram, build_udp_packet

log = logging.getLogger("rtether.udp")


def _make_socket(ipv6: bool = False) -> socket.socket:
    """An unconnected UDP socket that keeps receiving after an ICMP port-unreachable.

    On Windows a UDP socket reports ICMP unreachable as WSAECONNRESET on the next
    receive, and asyncio's proactor stops reading after that error. Turning the
    behaviour off (SIO_UDP_CONNRESET) makes the socket behave like one on Linux.
    """
    sock = socket.socket(socket.AF_INET6 if ipv6 else socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    sock.bind(("::" if ipv6 else "0.0.0.0", 0))
    if hasattr(socket, "SIO_UDP_CONNRESET"):
        sock.ioctl(socket.SIO_UDP_CONNRESET, False)
    return sock


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, on_datagram: Callable[[bytes, tuple], None]):
        self._on_datagram = on_datagram

    def datagram_received(self, data: bytes, addr) -> None:
        self._on_datagram(data, addr)

    def error_received(self, exc: Exception) -> None:
        log.debug("udp socket error ignored: %s", exc)


Endpoint = tuple[str, int]


class UDPFlow:
    """Everything one phone port sends, through one PC socket whatever the destination.

    One socket per destination would give each destination a different public port, which
    is what a "symmetric NAT" does; games that pair players directly refuse to work behind
    one. Sharing the socket keeps the phone's public port the same for every server.
    """

    def __init__(
        self,
        src: Endpoint,
        send: Callable[[bytes], None],
        dns_server: str,
        dns_port: int = 53,
        on_dns: Callable[[bytes], None] = lambda reply: None,
    ):
        self.src = src
        self._send = send
        self._dns = (dns_server, dns_port)
        self._on_dns = on_dns
        self.replies: Counter[Endpoint] = Counter()  # per destination, as the phone named it
        self.destinations: set[Endpoint] = set()
        self.last_activity = time.monotonic()
        self.closed = False
        self._transports: dict[bool, asyncio.DatagramTransport] = {}  # by "is IPv6"
        self._asked: dict[Endpoint, Endpoint] = {}  # real remote -> what the phone asked
        self._peers: set[str] = set()  # addresses contacted; only these may send back

    @property
    def is_dns(self) -> bool:
        return all(port == 53 for _, port in self.destinations)

    def remote_for(self, dst_ip: str, dst_port: int) -> Endpoint:
        """Where a datagram for `dst` really goes.

        DNS for the tunnel's own address, or for a private one the PC cannot reach (a
        teammate's phone asks the hub's hotspot address), goes to the PC's DNS server.
        """
        private = ipaddress.ip_address(dst_ip).is_private and dst_ip != self._dns[0]
        if dst_port == 53 and (dst_ip == DNS_VIRTUAL or private):
            return self._dns
        return (dst_ip, dst_port)

    async def handle(self, dst_ip: str, dgram: UDPDatagram) -> None:
        if self.closed:
            return
        self.last_activity = time.monotonic()
        dst = (dst_ip, dgram.dst_port)
        remote = self.remote_for(*dst)
        self.destinations.add(dst)
        self._asked[remote] = dst
        self._peers.add(remote[0])
        ipv6 = ":" in remote[0]
        transport = self._transports.get(ipv6)
        if transport is None:
            loop = asyncio.get_running_loop()
            try:
                transport, _ = await loop.create_datagram_endpoint(
                    lambda: _Protocol(self._on_reply), sock=_make_socket(ipv6)
                )
            except OSError as exc:
                log.debug("udp endpoint for %s failed: %s", self.src, exc)
                self.close()
                return
            self._transports[ipv6] = transport
        try:
            transport.sendto(dgram.data, remote)
        except OSError as exc:
            log.debug("udp send to %s failed: %s", remote, exc)

    @property
    def local_port(self) -> int:
        for transport in self._transports.values():
            return transport.get_extra_info("sockname")[1]
        return 0

    def close(self) -> None:
        self.closed = True
        for transport in self._transports.values():
            transport.close()
        self._transports.clear()

    def _on_reply(self, data: bytes, addr: tuple) -> None:
        if self.closed:
            return
        remote = (addr[0], addr[1])
        # Label the reply as the address the phone asked (DNS is redirected). A sender the
        # phone never contacted is dropped, so the internet cannot reach into the phone.
        asked = self._asked.get(remote) or (remote if remote[0] in self._peers else None)
        if asked is None:
            return
        self.last_activity = time.monotonic()
        self.replies[asked] += 1
        if asked[1] == 53:
            self._on_dns(data)
        src_ip, src_port = self.src
        self._send(build_udp_packet(asked[0], src_ip, UDPDatagram(asked[1], src_port, data)))
