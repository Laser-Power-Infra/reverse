"""Length-prefixed framing of IPv4 packets on the phone <-> relay stream.

Each frame is a big-endian uint16 length followed by that many bytes, which are one
complete IPv4 packet. A length of zero is a keepalive and carries no packet.
"""

from __future__ import annotations

import asyncio
import struct

MAX_FRAME = 1500
_HEADER = struct.Struct("!H")


class FrameError(Exception):
    """Raised when the stream violates the framing rules."""


def encode_frame(packet: bytes) -> bytes:
    if len(packet) > MAX_FRAME:
        raise FrameError(f"frame of {len(packet)} bytes exceeds {MAX_FRAME}")
    return _HEADER.pack(len(packet)) + packet


async def read_frame(reader: asyncio.StreamReader) -> bytes | None:
    """Return the next packet, b"" for a keepalive, or None on a clean EOF."""
    try:
        header = await reader.readexactly(_HEADER.size)
    except asyncio.IncompleteReadError as exc:
        if not exc.partial:
            return None
        raise FrameError("stream ended inside a frame header") from exc
    (length,) = _HEADER.unpack(header)
    if length > MAX_FRAME:
        raise FrameError(f"frame of {length} bytes exceeds {MAX_FRAME}")
    if length == 0:
        return b""
    try:
        return await reader.readexactly(length)
    except asyncio.IncompleteReadError as exc:
        raise FrameError("stream ended inside a frame body") from exc
