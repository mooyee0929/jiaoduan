"""The macOS backend against a real Cocoa fixture app through the Accessibility API.

Needs Accessibility access for the terminal running pytest. The fixture app never activates,
so these tests also check that the person's frontmost app and pointer stay where they were.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest

from agentlayer.adapters.registry import AdapterRegistry
from agentlayer.engine import Engine
from agentlayer.errors import LayerError
from agentlayer.gate import Gate, Policy

pytestmark = pytest.mark.mac
_START_WAIT = 10.0


def frontmost() -> str:
    return subprocess.run(["lsappinfo", "front"], capture_output=True, text=True,
                          check=True).stdout.strip()


def pointer() -> tuple[float, float]:
    import Quartz

    location = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    return (round(location.x, 1), round(location.y, 1))


class Fixture:
    def __init__(self, process: subprocess.Popen[bytes], state: Path) -> None:
        self.process = process
        self.state_path = state

    @property
    def target(self) -> str:
        return f"pid:{self.process.pid}"

    def state(self) -> dict[str, Any]:
        return dict(json.loads(self.state_path.read_text(encoding="utf-8")))


@pytest.fixture
def app(tmp_path: Path) -> Iterator[Fixture]:
    state = tmp_path / "state.json"
    process = subprocess.Popen(
        [sys.executable, "-m", "bench.fixtures.mac.fixture_app", str(state)],
        cwd=Path(__file__).parent.parent,
    )
    try:
        deadline = time.monotonic() + _START_WAIT
        while not state.exists():
            if time.monotonic() > deadline or process.poll() is not None:
                raise RuntimeError("the fixture app did not start")
            time.sleep(0.05)
        yield Fixture(process, state)
    finally:
        process.terminate()
        process.wait(timeout=5)


def run(work: Any) -> Any:
    return asyncio.run(work)


def engine(policy: Policy = Policy.ASK) -> Engine:
    from agentlayer.backends.mac.backend import MacBackend

    return Engine({"mac": MacBackend()}, gate=Gate(policy), adapters=AdapterRegistry(()))


def test_observe_reads_native_controls(app: Fixture) -> None:
    async def scenario() -> str:
        layer = engine()
        try:
            await layer.open("a", app.target)
            return await layer.observe("a")
        finally:
            await layer.stop()

    text = run(scenario())
    assert 'window "Fixture Form"' in text
    assert 'textbox "Name" = ""' in text
    assert 'checkbox "Subscribe to news"' in text
    assert 'combobox "Size" = "Small"' in text and "[haspopup]" in text
    assert 'radio "Courier"' in text


def test_fill_reaches_the_app_model_without_moving_focus_or_pointer(app: Fixture) -> None:
    front, where = frontmost(), pointer()

    async def scenario() -> str:
        layer = engine()
        try:
            await layer.open("a", app.target)
            return await layer.fill("a", {
                "Name": "Ada Lovelace",
                "Subscribe to news": "on",
                "Size": "Large",
                "Delivery method": "Courier",
            })
        finally:
            await layer.stop()

    report = run(scenario())
    assert report.startswith("fill: 4/4 ok"), report
    assert app.state() == {
        "name": "Ada Lovelace", "subscribe": True, "size": "Large", "delivery": "Courier",
        "saved": 0,
    }
    assert frontmost() == front
    assert pointer() == where


def test_an_unknown_menu_choice_is_reported_and_the_menu_closed(app: Fixture) -> None:
    async def scenario() -> tuple[str, str]:
        layer = engine()
        try:
            await layer.open("a", app.target)
            report = await layer.fill("a", {"Size": "Huge"})
            return report, await layer.observe("a")
        finally:
            await layer.stop()

    report, after = run(scenario())
    assert "not_found" in report
    assert "menuitem" not in after
    assert app.state()["size"] == "Small"


def test_the_default_button_is_gated_and_runs_once_approved(app: Fixture) -> None:
    async def scenario() -> None:
        layer = engine()
        try:
            await layer.open("a", app.target)
            text = await layer.observe("a", query="Save")
            ref = text.split("[e")[1].split("]")[0]
            with pytest.raises(LayerError) as caught:
                await layer.act("a", f"e{ref}", "press")
            assert caught.value.code == "COMMIT_GATED"
            assert app.state()["saved"] == 0
            gate_id = caught.value.message.split("gate=")[1].split()[0]
            layer.gate_approve(gate_id)
            await layer.act("a", f"e{ref}", "press", gate=gate_id)
        finally:
            await layer.stop()

    run(scenario())
    assert app.state()["saved"] == 1


def test_return_in_a_text_field_is_gated_when_it_would_press_the_default_button(
    app: Fixture,
) -> None:
    async def scenario() -> None:
        layer = engine()
        try:
            await layer.open("a", app.target)
            await layer.fill("a", {"Name": "Ada"})
            with pytest.raises(LayerError) as caught:
                await layer.key("a", "Enter")
            assert caught.value.code == "COMMIT_GATED"
        finally:
            await layer.stop()

    run(scenario())
    assert app.state()["saved"] == 0
