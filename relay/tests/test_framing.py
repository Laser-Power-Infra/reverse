import asyncio

import pytest

from rtether.framing import FrameError, encode_frame, read_frame


def make_reader(*chunks: bytes, eof: bool = True) -> asyncio.StreamReader:
    reader = asyncio.StreamReader()
    for chunk in chunks:
        reader.feed_data(chunk)
    if eof:
        reader.feed_eof()
    return reader


def test_encode_prefixes_big_endian_length():
    assert encode_frame(b"abc") == b"\x00\x03abc"


def test_encode_rejects_oversized():
    with pytest.raises(FrameError):
        encode_frame(b"x" * 1501)


async def test_read_frame_handles_split_reads():
    reader = make_reader(b"\x00\x05hel", b"lo")
    assert await read_frame(reader) == b"hello"


async def test_read_keepalive_returns_empty():
    reader = make_reader(b"\x00\x00")
    assert await read_frame(reader) == b""


async def test_read_eof_returns_none():
    reader = make_reader()
    assert await read_frame(reader) is None


async def test_read_truncated_raises():
    reader = make_reader(b"\x00\x05hi")
    with pytest.raises(FrameError):
        await read_frame(reader)


async def test_read_oversized_raises():
    reader = make_reader(b"\x05\xdd", eof=False)
    with pytest.raises(FrameError):
        await read_frame(reader)
