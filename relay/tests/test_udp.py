import asyncio
import socket

import pytest

from rtether.dns import DNS_VIRTUAL
from rtether.packet import UDPDatagram, parse_ipv4, parse_udp
from rtether.udp import UDPFlow

PHONE = "10.8.0.2"
PHONE_PORT = 40000


class _Echo(asyncio.DatagramProtocol):
    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.transport.sendto(data, addr)


@pytest.fixture
async def echo_port():
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(_Echo, local_addr=("127.0.0.1", 0))
    yield transport.get_extra_info("sockname")[1]
    transport.close()


def _reply(sent: list[bytes]):
    ip = parse_ipv4(sent[-1])
    return ip, parse_udp(ip)


async def _wait_for(sent: list[bytes], count: int, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while len(sent) < count and asyncio.get_running_loop().time() < deadline:
        await asyncio.sleep(0.01)


async def test_datagram_relayed_and_reply_returned(echo_port):
    sent: list[bytes] = []
    flow = UDPFlow((PHONE, PHONE_PORT, "127.0.0.1", echo_port), sent.append, "1.1.1.1")
    await flow.handle(UDPDatagram(PHONE_PORT, echo_port, b"hello"))
    await _wait_for(sent, 1)
    ip, dgram = _reply(sent)
    assert (ip.src, dgram.src_port) == ("127.0.0.1", echo_port)
    assert (ip.dst, dgram.dst_port) == (PHONE, PHONE_PORT)
    assert dgram.data == b"hello"
    flow.close()


async def test_dns_virtual_is_rewritten(echo_port):
    sent: list[bytes] = []
    flow = UDPFlow(
        (PHONE, PHONE_PORT, DNS_VIRTUAL, 53), sent.append, "127.0.0.1", dns_port=echo_port
    )
    assert flow.is_dns
    await flow.handle(UDPDatagram(PHONE_PORT, 53, b"query"))
    await _wait_for(sent, 1)
    ip, dgram = _reply(sent)
    assert (ip.src, dgram.src_port) == (DNS_VIRTUAL, 53)
    assert dgram.data == b"query"
    flow.close()


async def test_unreachable_destination_does_not_crash():
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    probe.bind(("127.0.0.1", 0))
    closed_port = probe.getsockname()[1]
    probe.close()
    sent: list[bytes] = []
    flow = UDPFlow((PHONE, PHONE_PORT, "127.0.0.1", closed_port), sent.append, "1.1.1.1")
    await flow.handle(UDPDatagram(PHONE_PORT, closed_port, b"one"))
    await asyncio.sleep(0.05)
    await flow.handle(UDPDatagram(PHONE_PORT, closed_port, b"two"))
    await asyncio.sleep(0.05)
    assert not flow.closed
    assert sent == []
    flow.close()


async def test_close_releases_socket(echo_port):
    sent: list[bytes] = []
    flow = UDPFlow((PHONE, PHONE_PORT, "127.0.0.1", echo_port), sent.append, "1.1.1.1")
    await flow.handle(UDPDatagram(PHONE_PORT, echo_port, b"hello"))
    await _wait_for(sent, 1)
    flow.close()
    assert flow.closed
    await flow.handle(UDPDatagram(PHONE_PORT, echo_port, b"again"))
    await asyncio.sleep(0.05)
    assert len(sent) == 1
