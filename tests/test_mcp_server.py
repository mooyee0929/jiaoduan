"""The tool definitions a model actually sees."""

from __future__ import annotations

import asyncio
from typing import Any

from agentlayer.client import Client
from agentlayer.mcp_server import build_server


def tools() -> dict[str, Any]:
    server = build_server(Client("test", spawn=False))
    return {tool.name: tool for tool in asyncio.run(server.list_tools())}


def test_every_documented_tool_is_offered_and_nothing_approves_gates() -> None:
    names = set(tools())
    assert names == {
        "surfaces", "open", "observe", "act", "key", "read", "wait", "form", "fill", "tools",
        "call", "report",
    }
    assert not any("gate" in name or "approve" in name for name in names)


def test_act_offers_every_engine_action() -> None:
    from agentlayer.model import ActionKind

    schema = tools()["act"].input_schema["properties"]["action"]
    offered = set(schema.get("enum") or [option["const"] for option in schema["anyOf"]])
    assert offered == {kind.value for kind in ActionKind}
