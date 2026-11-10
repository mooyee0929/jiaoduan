"""A small Chrome DevTools Protocol client: correlates commands, routes sessions, fans out events.

Uses flat mode: one transport carries every target, and each message names its ``sessionId``.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from jiaoduan.backends.web.transport import Transport, TransportClosed

log = logging.getLogger(__name__)

JsonObject = dict[str, Any]
EventHandler = Callable[[JsonObject], None]
Unsubscribe = Callable[[], None]

DEFAULT_TIMEOUT = 30.0


class CdpError(RuntimeError):
    """Chrome rejected a command."""

    def __init__(self, method: str, code: int, message: str) -> None:
        super().__init__(f"{method}: {message} (code {code})")
        self.method = method
        self.code = code
        self.detail = message


class CdpTimeout(TimeoutError):
    """Chrome did not answer a command in time. The command may still have taken effect."""

    def __init__(self, method: str, timeout: float) -> None:
        super().__init__(f"{method}: no answer within {timeout:g}s")
        self.method = method


class CdpDisconnected(ConnectionError):
    """The browser went away while the command was outstanding."""


class Connection:
    """One browser connection. Call ``start`` once before sending."""

    def __init__(self, transport: Transport, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._transport = transport
        self._timeout = timeout
        self._next_id = 1
        self._pending: dict[int, tuple[str, asyncio.Future[JsonObject]]] = {}
        self._handlers: dict[tuple[str | None, str], list[EventHandler]] = defaultdict(list)
        self._reader: asyncio.Task[None] | None = None
        self._closed_error: Exception | None = None
        self._on_close: list[Callable[[], None]] = []

    @property
    def closed(self) -> bool:
        return self._closed_error is not None

    def start(self) -> None:
        self._reader = asyncio.create_task(self._read_loop(), name="cdp-reader")

    def on_close(self, callback: Callable[[], None]) -> None:
        self._on_close.append(callback)

    async def send(
        self,
        method: str,
        params: JsonObject | None = None,
        *,
        session_id: str | None = None,
        timeout: float | None = None,
    ) -> JsonObject:
        if self._closed_error is not None:
            raise CdpDisconnected(f"{method}: {self._closed_error}")
        message_id = self._next_id
        self._next_id += 1
        payload: JsonObject = {"id": message_id, "method": method, "params": params or {}}
        if session_id is not None:
            payload["sessionId"] = session_id
        future: asyncio.Future[JsonObject] = asyncio.get_running_loop().create_future()
        self._pending[message_id] = (method, future)
        limit = self._timeout if timeout is None else timeout
        try:
            await self._transport.send(json.dumps(payload))
            return await asyncio.wait_for(future, limit)
        except TimeoutError as exc:
            raise CdpTimeout(method, limit) from exc
        except TransportClosed as exc:
            raise CdpDisconnected(f"{method}: {exc}") from exc
        finally:
            self._pending.pop(message_id, None)

    def on(self, method: str, handler: EventHandler, session_id: str | None = None) -> Unsubscribe:
        """Call ``handler(params)`` for each ``method`` event from that session."""
        key = (session_id, method)
        self._handlers[key].append(handler)

        def unsubscribe() -> None:
            handlers = self._handlers.get(key)
            if handlers is not None and handler in handlers:
                handlers.remove(handler)

        return unsubscribe

    def session(self, session_id: str) -> Session:
        return Session(self, session_id)

    async def close(self) -> None:
        await self._transport.close()
        if self._reader is not None:
            self._reader.cancel()
            try:
                await self._reader
            except asyncio.CancelledError:
                log.debug("cdp reader cancelled during close")
        self._fail_pending(CdpDisconnected("connection closed by this client"))

    async def _read_loop(self) -> None:
        try:
            while True:
                self._handle(await self._transport.receive())
        except TransportClosed as exc:
            self._fail_pending(CdpDisconnected(str(exc)))

    def _handle(self, raw: str) -> None:
        try:
            message: JsonObject = json.loads(raw)
        except json.JSONDecodeError:
            log.exception("undecodable CDP message: %.200s", raw)
            return
        if "id" in message:
            self._resolve(message)
        elif "method" in message:
            self._dispatch(message)
        else:
            log.warning("CDP message with neither id nor method: %.200s", raw)

    def _resolve(self, message: JsonObject) -> None:
        entry = self._pending.get(message["id"])
        if entry is None:
            return  # the caller already timed out
        method, future = entry
        if future.done():
            return
        error = message.get("error")
        if error is not None:
            future.set_exception(
                CdpError(method, int(error.get("code", 0)), str(error.get("message", "")))
            )
        else:
            future.set_result(message.get("result", {}))

    def _dispatch(self, message: JsonObject) -> None:
        key = (message.get("sessionId"), message["method"])
        for handler in tuple(self._handlers.get(key, ())):
            try:
                handler(message.get("params", {}))
            except Exception:
                log.exception("handler for %s failed", message["method"])

    def _fail_pending(self, error: Exception) -> None:
        if self._closed_error is not None:
            return
        self._closed_error = error
        for _, future in self._pending.values():
            if not future.done():
                future.set_exception(error)
        for callback in self._on_close:
            try:
                callback()
            except Exception:
                log.exception("close callback failed")


class Session:
    """Commands and events scoped to one attached target."""

    def __init__(self, connection: Connection, session_id: str) -> None:
        self.connection = connection
        self.session_id = session_id

    async def send(
        self, method: str, params: JsonObject | None = None, *, timeout: float | None = None
    ) -> JsonObject:
        return await self.connection.send(
            method, params, session_id=self.session_id, timeout=timeout
        )

    def on(self, method: str, handler: EventHandler) -> Unsubscribe:
        return self.connection.on(method, handler, session_id=self.session_id)
