"""Talks to the daemon, starting it when it is not running."""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
from typing import Any, Self

from agentlayer import paths
from agentlayer.errors import BACKEND_UNAVAILABLE, LayerError
from agentlayer.model import Delivery

_MAX_LINE = 1 << 24
_SPAWN_WAIT = 10.0
_SPAWN_POLL = 0.05


class Client:
    def __init__(self, agent: str, *, role: str = "agent", spawn: bool = True) -> None:
        self._agent = agent
        self._role = role
        self._spawn = spawn
        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._next = 1
        self._lock = asyncio.Lock()

    async def __aenter__(self) -> Self:
        await self.connect()
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()

    async def connect(self) -> None:
        socket = paths.socket_path()
        try:
            await self._attach(str(socket))
            return
        except (FileNotFoundError, ConnectionRefusedError) as exc:
            if not self._spawn:
                raise LayerError(BACKEND_UNAVAILABLE, "the daemon is not running") from exc
        _start_daemon()
        deadline = asyncio.get_running_loop().time() + _SPAWN_WAIT
        while True:
            try:
                await self._attach(str(socket))
                return
            except (FileNotFoundError, ConnectionRefusedError) as exc:
                if asyncio.get_running_loop().time() >= deadline:
                    raise LayerError(
                        BACKEND_UNAVAILABLE,
                        f"the daemon did not start; see {paths.log_path()}",
                    ) from exc
                await asyncio.sleep(_SPAWN_POLL)

    async def _attach(self, socket: str) -> None:
        self._reader, self._writer = await asyncio.open_unix_connection(socket, limit=_MAX_LINE)
        hello = {"agent": self._agent, "role": self._role}
        self._writer.write(json.dumps(hello).encode("utf-8") + b"\n")
        await self._writer.drain()

    async def close(self) -> None:
        if self._writer is not None:
            self._writer.close()
            self._writer = None
            self._reader = None

    async def call(self, op: str, **arguments: Any) -> str:
        args = {k: v for k, v in arguments.items() if v is not None}
        async with self._lock:
            if self._writer is None:
                await self.connect()
            try:
                return await self._exchange(op, args)
            except (ConnectionError, asyncio.IncompleteReadError) as exc:
                await self.close()
                raise LayerError(
                    BACKEND_UNAVAILABLE, f"lost the daemon during {op}: {exc}",
                    delivery=Delivery.UNCERTAIN,
                ) from exc

    async def _exchange(self, op: str, args: dict[str, Any]) -> str:
        assert self._writer is not None and self._reader is not None
        request_id = self._next
        self._next += 1
        payload = {"id": request_id, "op": op, "args": args}
        self._writer.write(json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n")
        await self._writer.drain()
        line = await self._reader.readline()
        if not line:
            raise ConnectionError("the daemon closed the connection")
        response = json.loads(line)
        if response.get("ok"):
            return str(response["text"])
        error = response["error"]
        raise LayerError(
            str(error["code"]), str(error["message"]),
            delivery=Delivery(error["delivery"]), hint=error.get("hint"),
        )


def _start_daemon() -> None:
    log = paths.log_path().open("ab")
    subprocess.Popen(
        [sys.executable, "-m", "agentlayer.daemon"],
        stdin=subprocess.DEVNULL, stdout=log, stderr=log,
        start_new_session=True, env=dict(os.environ),
    )
    log.close()
