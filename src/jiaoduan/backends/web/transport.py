"""Byte transport for the Chrome DevTools Protocol.

Chrome's ``--remote-debugging-pipe`` reads commands from fd 3 and writes responses to fd 4,
each message terminated by a NUL byte. A pipe needs no network port, so no other local process
can connect to the browser.
"""

from __future__ import annotations

import asyncio
import os
from typing import Protocol

# One Accessibility.getFullAXTree reply on a long page runs to tens of megabytes.
_MAX_MESSAGE_BYTES = 1 << 30
_DELIMITER = b"\0"


class TransportClosed(ConnectionError):
    """The browser end of the transport went away."""


class Transport(Protocol):
    async def send(self, message: str) -> None: ...

    async def receive(self) -> str:
        """The next whole message. Raises TransportClosed when there are no more."""
        ...

    async def close(self) -> None: ...


class PipeTransport:
    """NUL-delimited CDP messages over a pair of file descriptors owned by this object."""

    def __init__(self, read_fd: int, write_fd: int) -> None:
        self._read_fd = read_fd
        self._write_fd = write_fd
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._closed = False

    async def open(self) -> None:
        loop = asyncio.get_running_loop()
        reader = asyncio.StreamReader(limit=_MAX_MESSAGE_BYTES)
        await loop.connect_read_pipe(
            lambda: asyncio.StreamReaderProtocol(reader), os.fdopen(self._read_fd, "rb", 0)
        )
        write_transport, write_protocol = await loop.connect_write_pipe(
            asyncio.streams.FlowControlMixin, os.fdopen(self._write_fd, "wb", 0)
        )
        self._reader = reader
        self._writer = asyncio.StreamWriter(write_transport, write_protocol, None, loop)

    async def send(self, message: str) -> None:
        if self._writer is None or self._closed:
            raise TransportClosed("transport is not open")
        try:
            self._writer.write(message.encode("utf-8") + _DELIMITER)
            await self._writer.drain()
        except (BrokenPipeError, ConnectionResetError) as exc:
            raise TransportClosed(f"browser pipe closed while writing: {exc}") from exc

    async def receive(self) -> str:
        if self._reader is None:
            raise TransportClosed("transport is not open")
        try:
            data = await self._reader.readuntil(_DELIMITER)
        except asyncio.IncompleteReadError as exc:
            raise TransportClosed("browser pipe reached end of file") from exc
        except ConnectionResetError as exc:
            raise TransportClosed(f"browser pipe reset: {exc}") from exc
        return data[:-1].decode("utf-8")

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._writer is not None:
            self._writer.close()
