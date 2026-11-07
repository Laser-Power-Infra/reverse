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


def unused_tcp_port() -> int:
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    return port


class Harness:
    def __init__(self, port: int, connector=asyncio.open_connection):
        self.sent: list[bytes] = []
        self.key = (PHONE, PHONE_PORT, "127.0.0.1", port)
        self.flow = TCPFlow(self.key, self.sent.append, connector)

    def segs(self) -> list[TCPSegment]:
        return [parse_tcp(parse_ipv4(raw)) for raw in self.sent]

    def last(self) -> TCPSegment:
        return self.segs()[-1]

    def data_segs(self) -> list[TCPSegment]:
        return [s for s in self.segs() if s.data]

    def phone(
        self, flags: int, seq: int, ack: int = 0, data: bytes = b"", window: int = 65535
    ) -> TCPSegment:
        return TCPSegment(PHONE_PORT, self.key[3], seq, ack, flags, window, data)

    async def syn(self, seq: int = 100, window: int = 65535) -> None:
        """Send a SYN and wait for the relay's connect attempt to finish."""
        await self.flow.handle(self.phone(SYN, seq, window=window))
        if self.flow.connect_task is not None:
            await self.flow.connect_task

    async def handshake(self, window: int = 65535) -> int:
        """Complete the handshake; return the relay's ISN."""
        await self.syn(window=window)
        isn = self.last().seq
        await self.flow.handle(self.phone(ACK, 101, isn + 1, window=window))
        return isn


async def test_syn_opens_socket_and_replies_synack(echo):
    h = Harness(echo.port)
    await h.syn()
    assert h.last().flags == SYN | ACK
    assert h.last().ack == 101
    assert h.flow.state == "ESTABLISHED"
    h.flow.close()


async def test_syn_to_closed_port_replies_rst():
    h = Harness(unused_tcp_port())
    await h.syn()
    assert h.last().flags & RST
    assert h.flow.state == "CLOSED"


async def test_data_is_relayed_and_echoed_back(echo):
    h = Harness(echo.port)
    isn = await h.handshake()
    await h.flow.handle(h.phone(PSH | ACK, 101, isn + 1, b"ping"))
    assert h.last().flags & ACK
    assert h.last().ack == 105
    await asyncio.sleep(0.05)
    assert h.data_segs() and h.data_segs()[-1].data == b"ping"
    assert h.data_segs()[-1].flags & PSH
    h.flow.close()


async def test_duplicate_segment_is_acked_and_dropped(echo):
    h = Harness(echo.port)
    isn = await h.handshake()
    seg = h.phone(PSH | ACK, 101, isn + 1, b"ping")
    await h.flow.handle(seg)
    await h.flow.handle(seg)
    await asyncio.sleep(0.05)
    assert echo.received == b"ping"
    assert len(h.data_segs()) == 1
    assert h.last().ack == 105
    h.flow.close()


async def test_large_response_is_split_at_mss(echo):
    h = Harness(echo.port)
    isn = await h.handshake()
    await h.flow.handle(h.phone(PSH | ACK, 101, isn + 1, b"x" * 3000))
    await asyncio.sleep(0.1)
    assert b"".join(s.data for s in h.data_segs()) == b"x" * 3000
    assert all(len(s.data) <= MSS for s in h.data_segs())
    assert len(h.data_segs()) >= 3
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
    await h.syn()
    first = h.last()
    await h.syn()
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


# --- findings from the whole-branch review -------------------------------------


async def test_connect_does_not_block_handle(echo):
    """C1: a slow connect must not stall handling of other packets."""

    async def slow_connector(host, port):
        await asyncio.sleep(0.3)
        return await asyncio.open_connection(host, port)

    h = Harness(echo.port, slow_connector)
    await asyncio.wait_for(h.flow.handle(h.phone(SYN, 100)), 0.05)
    assert h.flow.state == "SYN_RECEIVED"
    assert h.sent == []
    await h.flow.connect_task
    assert h.last().flags == SYN | ACK
    h.flow.close()


async def test_connect_timeout_sends_rst(echo):
    """C1: a destination that never answers gets a RST after our own timeout."""

    async def hanging_connector(host, port):
        await asyncio.sleep(10)
        return await asyncio.open_connection(host, port)

    h = Harness(echo.port, hanging_connector)
    h.flow.connect_timeout = 0.05
    await h.syn()
    assert h.last().flags & RST
    assert h.flow.state == "CLOSED"


async def test_close_during_connect_closes_socket(echo):
    """C3: a flow evicted or reset while connecting must not leak the socket."""
    release = asyncio.Event()

    async def gated_connector(host, port):
        await release.wait()
        return await asyncio.open_connection(host, port)

    h = Harness(echo.port, gated_connector)
    await h.flow.handle(h.phone(SYN, 100))
    assert h.flow.state == "SYN_RECEIVED"
    h.flow.close()
    release.set()
    await h.flow.connect_task
    await asyncio.sleep(0.05)
    assert h.flow.state == "CLOSED"
    assert not any(s.flags & SYN for s in h.segs())
    assert echo.disconnects == 1


async def test_pump_honours_phone_window(echo):
    """C2: never send more unacknowledged bytes than the phone's receive window."""
    h = Harness(echo.port)
    isn = await h.handshake(window=1000)
    await h.flow.handle(h.phone(PSH | ACK, 101, isn + 1, b"x" * 3000, window=1000))
    await asyncio.sleep(0.1)
    sent_bytes = sum(len(s.data) for s in h.data_segs())
    assert 0 < sent_bytes <= 1000
    await h.flow.handle(h.phone(ACK, 3101, isn + 1 + sent_bytes, window=1000))
    await asyncio.sleep(0.1)
    assert sum(len(s.data) for s in h.data_segs()) > sent_bytes
    h.flow.close()


async def test_duplicate_syn_after_data_keeps_snd_nxt(echo):
    """I1: a retransmitted SYN must not rewind our sequence number."""
    h = Harness(echo.port)
    isn = await h.handshake()
    await h.flow.handle(h.phone(PSH | ACK, 101, isn + 1, b"ping"))
    await asyncio.sleep(0.05)
    snd_nxt = h.flow.snd_nxt
    assert snd_nxt == isn + 1 + 4
    await h.syn()
    assert h.last().flags == SYN | ACK
    assert h.last().seq == isn
    assert h.flow.snd_nxt == snd_nxt
    h.flow.close()


async def test_new_syn_on_existing_key_gets_challenge_ack(echo):
    """I2: a SYN with a new ISN on a live flow gets a plain ACK, not silence."""
    calls = 0

    async def counting_connector(host, port):
        nonlocal calls
        calls += 1
        return await asyncio.open_connection(host, port)

    h = Harness(echo.port, counting_connector)
    await h.handshake()
    await h.syn(seq=5000)
    assert h.last().flags == ACK
    assert calls == 1
    assert h.flow.state == "ESTABLISHED"
    h.flow.close()
