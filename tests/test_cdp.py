from __future__ import annotations

import asyncio
import json

import pytest

from agentlayer.backends.web.cdp import CdpDisconnected, CdpError, CdpTimeout, Connection
from agentlayer.backends.web.transport import TransportClosed


class ScriptedTransport:
    """Feeds queued messages to the client and records what it sends."""

    def __init__(self) -> None:
        self.sent: list[dict[str, object]] = []
        self.inbox: asyncio.Queue[str | None] = asyncio.Queue()

    async def send(self, message: str) -> None:
        self.sent.append(json.loads(message))

    async def receive(self) -> str:
        item = await self.inbox.get()
        if item is None:
            raise TransportClosed("gone")
        return item

    async def close(self) -> None:
        self.inbox.put_nowait(None)

    def reply(self, message_id: object, result: dict[str, object]) -> None:
        self.inbox.put_nowait(json.dumps({"id": message_id, "result": result}))


async def _settle() -> None:
    for _ in range(5):
        await asyncio.sleep(0)


def test_send_correlates_out_of_order_replies() -> None:
    async def scenario() -> None:
        transport = ScriptedTransport()
        conn = Connection(transport, timeout=1)
        conn.start()
        first = asyncio.create_task(conn.send("A.one"))
        second = asyncio.create_task(conn.send("B.two"))
        await _settle()
        ids = {m["method"]: m["id"] for m in transport.sent}
        transport.reply(ids["B.two"], {"who": "b"})
        transport.reply(ids["A.one"], {"who": "a"})
        assert await first == {"who": "a"}
        assert await second == {"who": "b"}
        await conn.close()

    asyncio.run(scenario())


def test_session_id_is_routed_on_commands_and_events() -> None:
    async def scenario() -> None:
        transport = ScriptedTransport()
        conn = Connection(transport, timeout=1)
        conn.start()
        session = conn.session("s1")
        seen: list[dict[str, object]] = []
        session.on("Page.loaded", seen.append)
        conn.on("Page.loaded", lambda _: seen.append({"wrong": True}))
        task = asyncio.create_task(session.send("Page.enable"))
        await _settle()
        assert transport.sent[0]["sessionId"] == "s1"
        transport.reply(transport.sent[0]["id"], {})
        await task
        transport.inbox.put_nowait(
            json.dumps({"method": "Page.loaded", "sessionId": "s1", "params": {"t": 1}})
        )
        await _settle()
        assert seen == [{"t": 1}]
        await conn.close()

    asyncio.run(scenario())


def test_error_reply_raises_cdp_error() -> None:
    async def scenario() -> None:
        transport = ScriptedTransport()
        conn = Connection(transport, timeout=1)
        conn.start()
        task = asyncio.create_task(conn.send("DOM.nope"))
        await _settle()
        transport.inbox.put_nowait(
            json.dumps(
                {"id": transport.sent[0]["id"], "error": {"code": -32601, "message": "not found"}}
            )
        )
        with pytest.raises(CdpError, match="not found"):
            await task
        await conn.close()

    asyncio.run(scenario())


def test_timeout_is_reported_with_the_method() -> None:
    async def scenario() -> None:
        conn = Connection(ScriptedTransport(), timeout=0.01)
        conn.start()
        with pytest.raises(CdpTimeout, match="Slow.call"):
            await conn.send("Slow.call")
        await conn.close()

    asyncio.run(scenario())


def test_browser_exit_fails_every_pending_command() -> None:
    async def scenario() -> None:
        transport = ScriptedTransport()
        conn = Connection(transport, timeout=5)
        closed: list[bool] = []
        conn.on_close(lambda: closed.append(True))
        conn.start()
        pending = [asyncio.create_task(conn.send(f"M.{i}")) for i in range(3)]
        await _settle()
        transport.inbox.put_nowait(None)
        for task in pending:
            with pytest.raises(CdpDisconnected):
                await task
        assert closed == [True]
        with pytest.raises(CdpDisconnected):
            await conn.send("After.close")

    asyncio.run(scenario())


def test_a_failing_event_handler_does_not_stop_the_others() -> None:
    async def scenario() -> None:
        transport = ScriptedTransport()
        conn = Connection(transport, timeout=1)
        conn.start()
        seen: list[int] = []

        def bad(_: dict[str, object]) -> None:
            raise ValueError("boom")

        conn.on("E.v", bad)
        conn.on("E.v", lambda p: seen.append(int(p["n"])))
        transport.inbox.put_nowait(json.dumps({"method": "E.v", "params": {"n": 7}}))
        await _settle()
        assert seen == [7]
        await conn.close()

    asyncio.run(scenario())
