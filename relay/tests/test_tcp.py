import asyncio
import socket

import pytest

from rtether.packet import ACK, FIN, PSH, RST, SYN, TCPSegment, parse_ipv4, parse_tcp
from rtether.tcp import MSS, TCPFlow

PHONE = "10.8.0.2"
PHONE_PORT = 40000


class EchoServer:
    """Echoes bytes back; closes its side when the client half-closes."""

    def __init__(self, close_immediately: bool = False):
        self.close_immediately = close_immediately
        self.disconnects = 0
        self.received = b""

    async def _handle(self, reader, writer):
        if self.close_immediately:
            writer.close()
            await writer.wait_closed()
            self.disconnects += 1
            return
        while True:
            chunk = await reader.read(65536)
            if not chunk:
                break
            self.received += chunk
            writer.write(chunk)
            await writer.drain()
        writer.close()
        self.disconnects += 1

    async def start(self):
        self.server = await asyncio.start_server(self._handle, "127.0.0.1", 0)
        self.port = self.server.sockets[0].getsockname()[1]

    async def stop(self):
        self.server.close()
        await self.server.wait_closed()


@pytest.fixture
async def echo():
    server = EchoServer()
    await server.start()
    yield server
    await server.stop()


class Harness:
    def __init__(self, port: int, connector=asyncio.open_connection):
        self.sent: list[bytes] = []
        self.key = (PHONE, PHONE_PORT, "127.0.0.1", port)
        self.flow = TCPFlow(self.key, self.sent.append, connector)

    def segs(self) -> list[TCPSegment]:
        return [parse_tcp(parse_ipv4(raw)) for raw in self.sent]

    def last(self) -> TCPSegment:
        return self.segs()[-1]

    def phone(self, flags: int, seq: int, ack: int = 0, data: bytes = b"") -> TCPSegment:
        return TCPSegment(PHONE_PORT, self.key[3], seq, ack, flags, 65535, data)

    async def handshake(self) -> int:
        """Complete the handshake; return the relay's ISN."""
        await self.flow.handle(self.phone(SYN, 100))
        isn = self.last().seq
        await self.flow.handle(self.phone(ACK, 101, isn + 1))
        return isn


async def test_syn_opens_socket_and_replies_synack(echo):
    h = Harness(echo.port)
    await h.flow.handle(h.phone(SYN, 100))
    assert h.last().flags == SYN | ACK
    assert h.last().ack == 101
    assert h.flow.state == "ESTABLISHED"
    h.flow.close()


async def test_syn_to_closed_port_replies_rst():
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    unused_port = probe.getsockname()[1]
    probe.close()
    h = Harness(unused_port)
    await h.flow.handle(h.phone(SYN, 100))
    assert h.last().flags & RST
    assert h.flow.state == "CLOSED"


async def test_data_is_relayed_and_echoed_back(echo):
    h = Harness(echo.port)
    await h.handshake()
    await h.flow.handle(h.phone(PSH | ACK, 101, h.last().seq + 1, b"ping"))
    assert h.last().flags & ACK
    assert h.last().ack == 105
    await asyncio.sleep(0.05)
    data_segs = [s for s in h.segs() if s.data]
    assert data_segs and data_segs[-1].data == b"ping"
    assert data_segs[-1].flags & PSH
    h.flow.close()


async def test_duplicate_segment_is_acked_and_dropped(echo):
    h = Harness(echo.port)
    isn = await h.handshake()
    seg = h.phone(PSH | ACK, 101, isn + 1, b"ping")
    await h.flow.handle(seg)
    await h.flow.handle(seg)
    await asyncio.sleep(0.05)
    assert echo.received == b"ping"
    assert len([s for s in h.segs() if s.data]) == 1
    assert h.last().ack == 105
    h.flow.close()


async def test_large_response_is_split_at_mss(echo):
    h = Harness(echo.port)
    isn = await h.handshake()
    await h.flow.handle(h.phone(PSH | ACK, 101, isn + 1, b"x" * 3000))
    await asyncio.sleep(0.1)
    data_segs = [s for s in h.segs() if s.data]
    assert b"".join(s.data for s in data_segs) == b"x" * 3000
    assert all(len(s.data) <= MSS for s in data_segs)
    assert len(data_segs) >= 3
    h.flow.close()


async def test_phone_fin_closes_write_side_then_fins(echo):
    h = Harness(echo.port)
    isn = await h.handshake()
    await h.flow.handle(h.phone(FIN | ACK, 101, isn + 1))
    assert h.last().flags & ACK
    assert h.last().ack == 102
    await asyncio.sleep(0.05)
    assert h.last().flags & FIN
    assert h.flow.state == "LAST_ACK"
    await h.flow.handle(h.phone(ACK, 102, h.last().seq + 1))
    assert h.flow.state == "CLOSED"


async def test_server_eof_sends_fin():
    server = EchoServer(close_immediately=True)
    await server.start()
    try:
        h = Harness(server.port)
        await h.handshake()
        await asyncio.sleep(0.05)
        assert h.last().flags & FIN
        assert h.flow.state == "FIN_WAIT"
        h.flow.close()
    finally:
        await server.stop()


async def test_phone_rst_closes_flow(echo):
    h = Harness(echo.port)
    isn = await h.handshake()
    await h.flow.handle(h.phone(RST, 101, isn + 1))
    assert h.flow.state == "CLOSED"
    await asyncio.sleep(0.05)
    assert echo.disconnects == 1


async def test_ack_beyond_sent_is_ignored(echo):
    h = Harness(echo.port)
    isn = await h.handshake()
    before = (h.flow.state, h.flow.snd_nxt, h.flow.snd_una, len(h.sent))
    await h.flow.handle(h.phone(ACK, 101, isn + 1 + 5000))
    assert (h.flow.state, h.flow.snd_nxt, h.flow.snd_una, len(h.sent)) == before
    h.flow.close()


async def test_duplicate_syn_resends_synack(echo):
    calls = 0

    async def counting_connector(host, port):
        nonlocal calls
        calls += 1
        return await asyncio.open_connection(host, port)

    h = Harness(echo.port, counting_connector)
    await h.flow.handle(h.phone(SYN, 100))
    first = h.last()
    await h.flow.handle(h.phone(SYN, 100))
    assert h.last().flags == SYN | ACK
    assert h.last().seq == first.seq
    assert calls == 1
    h.flow.close()


async def test_backpressure_stops_acking(echo):
    async def slow_connector(host, port):
        reader, writer = await asyncio.open_connection(host, port)
        writer.transport.get_write_buffer_size = lambda: 300000
        return reader, writer

    h = Harness(echo.port, slow_connector)
    isn = await h.handshake()
    sent_before = len(h.sent)
    await h.flow.handle(h.phone(PSH | ACK, 101, isn + 1, b"ping"))
    assert len(h.sent) == sent_before
    h.flow.close()
