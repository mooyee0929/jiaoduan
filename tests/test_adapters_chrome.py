"""An adapter tool against the contact fixture in a real headless Chrome."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from agentlayer.errors import LayerError
from agentlayer.gate import Policy
from bench.fixture_server import serve
from tests.web_harness import engine_with_chrome, run

pytestmark = pytest.mark.chrome
ADAPTERS = (Path(__file__).parent.parent / "bench" / "adapters",)
ARGS = {"topic": "Support", "message": "The export button does nothing."}


def test_the_tool_is_listed_only_on_its_page() -> None:
    async def scenario() -> tuple[str, str]:
        with serve() as (host, _):
            async with engine_with_chrome(adapters=ADAPTERS) as engine:
                await engine.open("a", f"http://{host}/contact.html")
                here = await engine.tools("a")
                await engine.open("b", f"http://{host}/application.html")
                return here, await engine.tools("b")

    here, elsewhere = run(scenario())
    assert "send_contact_message(topic: string (Sales|Support), message: string)" in here
    assert "[commits: needs approval]" in here
    assert elsewhere == "no adapter tools for this surface"


def test_one_call_sends_the_form_after_approval() -> None:
    async def scenario() -> tuple[str, list[object]]:
        with serve() as (host, recorder):
            async with engine_with_chrome(adapters=ADAPTERS) as engine:
                await engine.open("a", f"http://{host}/contact.html")
                with pytest.raises(LayerError) as caught:
                    await engine.call_tool("a", "send_contact_message", ARGS)
                assert caught.value.code == "COMMIT_GATED"
                assert "1 fill: 2 fields ok" in caught.value.message
                await asyncio.sleep(0.2)
                assert recorder.snapshot()["submissions"] == {}
                gate_id = caught.value.message.split("gate=")[1].split()[0]
                engine.gate_approve(gate_id)
                result = await engine.call_tool(
                    "a", "send_contact_message", ARGS, gate=gate_id
                )
                await asyncio.sleep(0.2)
                return result, recorder.snapshot()["submissions"]["contact"]

    result, submissions = run(scenario())
    assert "3 wait: wait text_visible: met" in result
    assert submissions == [{"topic": "Support", "message": "The export button does nothing."}]


def test_a_redesigned_page_reports_drift_instead_of_guessing() -> None:
    async def scenario() -> LayerError:
        with serve() as (host, recorder):
            async with engine_with_chrome(Policy.ALLOW, adapters=ADAPTERS) as engine:
                await engine.open("a", f"http://{host}/contact.html?v=2")
                with pytest.raises(LayerError) as caught:
                    await engine.call_tool("a", "send_contact_message", ARGS)
                await asyncio.sleep(0.2)
                assert recorder.snapshot()["submissions"] == {}
                return caught.value

    error = run(scenario())
    assert error.code == "ADAPTER_DRIFT"
    assert "2 press: FAILED ADAPTER_DRIFT" in error.message
    assert "matched nothing" in error.message
