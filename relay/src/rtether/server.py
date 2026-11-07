"""The relay's listening side: accepts the phone app's framed packet stream."""

from __future__ import annotations

import asyncio
import logging
import time

from rtether.framing import FrameError, encode_frame, read_frame
from rtether.router import Router
from rtether.tcp import Connector

log = logging.getLogger("rtether.server")

KEEPALIVE_INTERVAL = 10.0
EVICT_INTERVAL = 10.0


class RelayServer:
    def __init__(
        self,
        host: str,
        port: int,
        dns_server: str,
        verbose: bool = False,
        connector: Connector = asyncio.open_connection,
        allow_loopback: bool = False,
    ):
        self._host = host
        self._requested_port = port
        self._verbose = verbose
        self.router = Router(self._send, dns_server, connector, allow_loopback=allow_loopback)
        self.port = port
        self._server: asyncio.AbstractServer | None = None
        self._client: asyncio.StreamWriter | None = None
        self._tasks: list[asyncio.Task] = []

    @property
    def client_connected(self) -> bool:
        return self._client is not None and not self._client.is_closing()

    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_client, self._host, self._requested_port
        )
        self.port = self._server.sockets[0].getsockname()[1]
        self._tasks = [
            asyncio.create_task(self._keepalive_loop()),
            asyncio.create_task(self._evict_loop()),
        ]
        log.info("listening on %s:%d", self._host, self.port)

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks = []
        self.router.reset()
        if self._client is not None:
            self._client.close()
            self._client = None
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    def _send(self, packet: bytes) -> None:
        client = self._client
        if client is None or client.is_closing():
            return
        try:
            client.write(encode_frame(packet))
        except (FrameError, OSError) as exc:
            log.debug("send to phone failed: %s", exc)

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
        previous = self._client
        if previous is not None and not previous.is_closing():
            log.info("new phone connection replaces the previous one")
            previous.close()
        self.router.reset()
        self._client = writer
        log.info("phone connected")
        try:
            while True:
                frame = await read_frame(reader)
                if frame is None:
                    break
                if not frame:
                    continue
                await self.router.handle(frame)
                await writer.drain()  # backpressure: stop reading if the phone is not draining
        except FrameError as exc:
            log.warning("phone stream error: %s", exc)
        except (OSError, asyncio.IncompleteReadError) as exc:
            log.debug("phone connection error: %s", exc)
        finally:
            if self._client is writer:
                self._client = None
                self.router.reset()
                log.info("phone disconnected")
            writer.close()

    async def _keepalive_loop(self) -> None:
        while True:
            await asyncio.sleep(KEEPALIVE_INTERVAL)
            client = self._client
            if client is not None and not client.is_closing():
                client.write(b"\x00\x00")

    async def _evict_loop(self) -> None:
        while True:
            await asyncio.sleep(EVICT_INTERVAL)
            self.router.evict_idle(time.monotonic())
            if self._verbose:
                log.info(self.router.summary())
