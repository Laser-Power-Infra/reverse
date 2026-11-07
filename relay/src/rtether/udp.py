"""UDP relay: one PC datagram socket per phone flow, with DNS redirected to a real server."""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from collections.abc import Callable

from rtether.dns import DNS_VIRTUAL
from rtether.packet import FlowKey, UDPDatagram, build_udp_packet

log = logging.getLogger("rtether.udp")


def _make_socket() -> socket.socket:
    """An unconnected UDP socket that keeps receiving after an ICMP port-unreachable.

    On Windows a UDP socket reports ICMP unreachable as WSAECONNRESET on the next
    receive, and asyncio's proactor stops reading after that error. Turning the
    behaviour off (SIO_UDP_CONNRESET) makes the socket behave like one on Linux.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    sock.bind(("0.0.0.0", 0))
    if hasattr(socket, "SIO_UDP_CONNRESET"):
        sock.ioctl(socket.SIO_UDP_CONNRESET, False)
    return sock


class _Protocol(asyncio.DatagramProtocol):
    def __init__(self, on_datagram: Callable[[bytes], None]):
        self._on_datagram = on_datagram

    def datagram_received(self, data: bytes, addr) -> None:
        self._on_datagram(data)

    def error_received(self, exc: Exception) -> None:
        log.debug("udp socket error ignored: %s", exc)


class UDPFlow:
    def __init__(
        self,
        key: FlowKey,
        send: Callable[[bytes], None],
        dns_server: str,
        dns_port: int = 53,
    ):
        self.key = key
        self._send = send
        self.last_activity = time.monotonic()
        self.closed = False
        self._transport: asyncio.DatagramTransport | None = None
        _, _, dst_ip, dst_port = key
        self.is_dns = dst_port == 53
        if dst_ip == DNS_VIRTUAL and dst_port == 53:
            self._remote = (dns_server, dns_port)
        else:
            self._remote = (dst_ip, dst_port)

    async def handle(self, dgram: UDPDatagram) -> None:
        if self.closed:
            return
        self.last_activity = time.monotonic()
        if self._transport is None:
            loop = asyncio.get_running_loop()
            try:
                self._transport, _ = await loop.create_datagram_endpoint(
                    lambda: _Protocol(self._on_reply), sock=_make_socket()
                )
            except OSError as exc:
                log.debug("udp endpoint for %s failed: %s", self.key, exc)
                self.close()
                return
        try:
            self._transport.sendto(dgram.data, self._remote)
        except OSError as exc:
            log.debug("udp send to %s failed: %s", self._remote, exc)

    @property
    def local_port(self) -> int:
        if self._transport is None:
            return 0
        return self._transport.get_extra_info("sockname")[1]

    def close(self) -> None:
        self.closed = True
        if self._transport is not None:
            self._transport.close()
            self._transport = None

    def _on_reply(self, data: bytes) -> None:
        if self.closed:
            return
        self.last_activity = time.monotonic()
        src_ip, src_port, dst_ip, dst_port = self.key
        reply = UDPDatagram(dst_port, src_port, data)
        self._send(build_udp_packet(dst_ip, src_ip, reply))
