import asyncio

import pytest

from rtether.framing import encode_frame, read_frame
from rtether.packet import UDPDatagram, build_udp_packet, parse_ipv4, parse_udp
from rtether.server import EVICT_INTERVAL, KEEPALIVE_INTERVAL, RelayServer

PHONE = "10.8.0.2"


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


@pytest.fixture
async def server():
    srv = RelayServer("127.0.0.1", 0, dns_server="1.1.1.1", allow_loopback=True)
    await srv.start()
    yield srv
    await srv.stop()


async def connect(server):
    return await asyncio.open_connection("127.0.0.1", server.port)


def udp_packet(port: int) -> bytes:
    return build_udp_packet(PHONE, "127.0.0.1", UDPDatagram(40001, port, b"ping"))


def test_intervals():
    assert KEEPALIVE_INTERVAL == 10.0
    assert EVICT_INTERVAL == 10.0


async def test_client_frames_reach_router_and_replies_are_framed(server, echo_port):
    reader, writer = await connect(server)
    writer.write(encode_frame(udp_packet(echo_port)))
    await writer.drain()
    reply = await asyncio.wait_for(read_frame(reader), 2)
    ip = parse_ipv4(reply)
    assert ip.dst == PHONE
    assert parse_udp(ip).data == b"ping"
    writer.close()


async def test_keepalive_frames_ignored(server):
    _, writer = await connect(server)
    writer.write(b"\x00\x00")
    await writer.drain()
    await asyncio.sleep(0.05)
    assert server.router.stats.rx == 0
    assert server.client_connected
    writer.close()


async def test_second_client_resets_router(server, echo_port):
    reader1, writer1 = await connect(server)
    writer1.write(encode_frame(udp_packet(echo_port)))
    await writer1.drain()
    await asyncio.sleep(0.05)
    assert len(server.router.udp_flows) == 1
    _, writer2 = await connect(server)
    await asyncio.sleep(0.05)
    assert server.router.udp_flows == {}
    await asyncio.wait_for(reader1.read(), 2)  # drains anything buffered, returns at EOF
    assert reader1.at_eof()
    writer2.close()


async def test_disconnect_resets_router(server, echo_port):
    _, writer = await connect(server)
    writer.write(encode_frame(udp_packet(echo_port)))
    await writer.drain()
    await asyncio.sleep(0.05)
    assert len(server.router.udp_flows) == 1
    writer.close()
    await asyncio.sleep(0.05)
    assert server.router.udp_flows == {}
    assert not server.client_connected


async def test_oversized_frame_disconnects_client(server):
    reader, writer = await connect(server)
    writer.write(b"\x05\xdd")
    await writer.drain()
    assert await asyncio.wait_for(reader.read(), 2) == b""
