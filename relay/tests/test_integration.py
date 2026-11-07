import asyncio
import http.server
import threading
from pathlib import Path

import pytest

from rtether.server import RelayServer
from tests.fake_phone import FakePhone


@pytest.fixture
def http_port(tmp_path: Path):
    (tmp_path / "hello.txt").write_text("reverse tether ok")
    handler = lambda *a, **k: http.server.SimpleHTTPRequestHandler(
        *a, directory=str(tmp_path), **k
    )
    httpd = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield httpd.server_address[1]
    httpd.shutdown()


class _Echo(asyncio.DatagramProtocol):
    def connection_made(self, transport):
        self.transport = transport

    def datagram_received(self, data, addr):
        self.transport.sendto(data, addr)


@pytest.fixture
async def udp_port():
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(_Echo, local_addr=("127.0.0.1", 0))
    yield transport.get_extra_info("sockname")[1]
    transport.close()


@pytest.fixture
async def relay():
    server = RelayServer("127.0.0.1", 0, dns_server="127.0.0.1", allow_loopback=True)
    await server.start()
    yield server
    await server.stop()


async def test_http_get_through_relay(relay, http_port):
    phone = FakePhone()
    await phone.connect(relay.port)
    body = await phone.http_get("127.0.0.1", http_port, "/hello.txt")
    assert body.endswith(b"reverse tether ok")
    assert body.startswith(b"HTTP/1.0 200")
    await phone.close()


async def test_udp_echo_through_relay(relay, udp_port):
    phone = FakePhone()
    await phone.connect(relay.port)
    assert await phone.udp_echo("127.0.0.1", udp_port, b"dns-like") == b"dns-like"
    await phone.close()
