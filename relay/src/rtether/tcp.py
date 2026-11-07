"""Userspace TCP: one TCPFlow per phone connection, backed by a real PC socket.

The USB link between phone and relay is itself a reliable byte stream, so this
translator does not retransmit. It acknowledges in-order data immediately, drops
anything else, and lets the phone's own stack retransmit if it ever needs to.
"""

from __future__ import annotations

import asyncio
import logging
import random
import time
from collections.abc import Awaitable, Callable

from rtether.packet import ACK, FIN, PSH, RST, SYN, FlowKey, TCPSegment, build_tcp_packet

log = logging.getLogger("rtether.tcp")

MSS = 1360
WINDOW = 65535
BACKPRESSURE_LIMIT = 262144

_SEQ_MOD = 1 << 32

Connector = Callable[[str, int], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]]


def _seq_lt_eq(a: int, b: int) -> bool:
    """a <= b in modulo-2**32 sequence arithmetic."""
    return ((b - a) % _SEQ_MOD) < (_SEQ_MOD // 2)


class TCPFlow:
    def __init__(
        self,
        key: FlowKey,
        send: Callable[[bytes], None],
        connector: Connector = asyncio.open_connection,
    ):
        self.key = key
        self._send = send
        self._connector = connector
        self.state = "CLOSED"
        self.last_activity = time.monotonic()
        self.isn = random.randrange(_SEQ_MOD)
        self.snd_una = self.isn
        self.snd_nxt = self.isn
        self.rcv_nxt = 0
        self._phone_isn = 0
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._pump: asyncio.Task | None = None
        self._fin_sent = False

    # -- public -------------------------------------------------------------

    @property
    def half_closed(self) -> bool:
        return self.state in ("CLOSE_WAIT", "FIN_WAIT", "LAST_ACK")

    async def handle(self, seg: TCPSegment) -> None:
        self.last_activity = time.monotonic()
        if seg.flags & RST:
            self.close()
            return
        if seg.flags & SYN:
            await self._on_syn(seg)
            return
        if self.state == "CLOSED":
            return
        self._on_ack(seg)
        if self.state == "CLOSED":
            return
        if seg.seq != self.rcv_nxt:
            # Out of order or duplicate: tell the phone what we expect, drop the rest.
            self._emit(ACK)
            return
        if seg.data:
            if not self._accept_data(seg.data):
                return  # backpressure: no ACK, the phone will retransmit later
        if seg.flags & FIN:
            self._on_phone_fin()
        elif seg.data:
            self._emit(ACK)

    def close(self, send_rst: bool = False) -> None:
        if send_rst and self.state != "CLOSED":
            self._emit(RST | ACK)
        self.state = "CLOSED"
        if self._pump is not None and not self._pump.done():
            self._pump.cancel()
        if self._writer is not None and not self._writer.is_closing():
            self._writer.close()

    # -- phone -> PC ----------------------------------------------------------

    async def _on_syn(self, seg: TCPSegment) -> None:
        if self.state != "CLOSED":
            if seg.seq == self._phone_isn:
                self._send_synack()  # our SYN-ACK was lost; repeat it
            return
        self._phone_isn = seg.seq
        self.rcv_nxt = (seg.seq + 1) % _SEQ_MOD
        _, _, dst_ip, dst_port = self.key
        try:
            self._reader, self._writer = await self._connector(dst_ip, dst_port)
        except (OSError, asyncio.TimeoutError) as exc:
            log.debug("connect %s:%d failed: %s", dst_ip, dst_port, exc)
            self._send_rst_for_syn()
            self.state = "CLOSED"
            return
        self.state = "ESTABLISHED"
        self._send_synack()
        self._pump = asyncio.create_task(self._pump_socket())

    def _on_ack(self, seg: TCPSegment) -> None:
        if not seg.flags & ACK:
            return
        if not (_seq_lt_eq(self.snd_una, seg.ack) and _seq_lt_eq(seg.ack, self.snd_nxt)):
            return  # acknowledges bytes we never sent; ignore
        self.snd_una = seg.ack
        if self._fin_sent and seg.ack == self.snd_nxt and self.state == "LAST_ACK":
            self.close()

    def _accept_data(self, data: bytes) -> bool:
        writer = self._writer
        if writer is None or writer.is_closing():
            return True  # nowhere to put it; ACK anyway so the phone moves on
        if writer.transport.get_write_buffer_size() > BACKPRESSURE_LIMIT:
            return False
        writer.write(data)
        self.rcv_nxt = (self.rcv_nxt + len(data)) % _SEQ_MOD
        return True

    def _on_phone_fin(self) -> None:
        self.rcv_nxt = (self.rcv_nxt + 1) % _SEQ_MOD
        self._emit(ACK)
        if self.state == "FIN_WAIT":
            self.close()
            return
        self.state = "CLOSE_WAIT"
        writer = self._writer
        if writer is not None and not writer.is_closing():
            try:
                if writer.can_write_eof():
                    writer.write_eof()
                else:
                    writer.close()
            except OSError:
                writer.close()

    # -- PC -> phone ----------------------------------------------------------

    async def _pump_socket(self) -> None:
        assert self._reader is not None
        try:
            while True:
                chunk = await self._reader.read(MSS)
                if not chunk:
                    self._on_socket_eof()
                    return
                self.last_activity = time.monotonic()
                self._emit(PSH | ACK, chunk)
        except asyncio.CancelledError:
            raise
        except (OSError, asyncio.IncompleteReadError) as exc:
            log.debug("socket error on %s: %s", self.key, exc)
            self.close(send_rst=True)

    def _on_socket_eof(self) -> None:
        if self.state == "CLOSED" or self._fin_sent:
            return
        self._fin_sent = True
        self._emit(FIN | ACK)
        self.snd_nxt = (self.snd_nxt + 1) % _SEQ_MOD
        self.state = "LAST_ACK" if self.state == "CLOSE_WAIT" else "FIN_WAIT"

    # -- packet builders ------------------------------------------------------

    def _emit(self, flags: int, data: bytes = b"") -> None:
        src_ip, src_port, dst_ip, dst_port = self.key
        seg = TCPSegment(dst_port, src_port, self.snd_nxt, self.rcv_nxt, flags, WINDOW, data)
        self._send(build_tcp_packet(dst_ip, src_ip, seg))
        if data:
            self.snd_nxt = (self.snd_nxt + len(data)) % _SEQ_MOD

    def _send_synack(self) -> None:
        src_ip, src_port, dst_ip, dst_port = self.key
        seg = TCPSegment(dst_port, src_port, self.isn, self.rcv_nxt, SYN | ACK, WINDOW, b"")
        self._send(build_tcp_packet(dst_ip, src_ip, seg))
        self.snd_nxt = (self.isn + 1) % _SEQ_MOD

    def _send_rst_for_syn(self) -> None:
        src_ip, src_port, dst_ip, dst_port = self.key
        seg = TCPSegment(dst_port, src_port, 0, self.rcv_nxt, RST | ACK, 0, b"")
        self._send(build_tcp_packet(dst_ip, src_ip, seg))
