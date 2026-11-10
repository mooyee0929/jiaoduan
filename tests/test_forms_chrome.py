"""Form conformance against the application fixture in a real headless Chrome.

No model is involved: these check that each driver writes through the widget's real input
path and reads back from the widget's real source of truth.
"""

from __future__ import annotations

import asyncio
import re
import tempfile
from pathlib import Path
from typing import Any

import pytest

from agentlayer.engine import Engine
from agentlayer.errors import LayerError
from agentlayer.model import SurfaceId
from bench.fixture_server import serve
from tests.web_harness import engine_with_chrome, run

pytestmark = pytest.mark.chrome


async def store(engine: Engine, surface: str) -> dict[str, Any]:
    backend = engine.backend_for(SurfaceId(surface))
    page = backend.page(SurfaceId(surface))  # type: ignore[attr-defined]
    result = await page.session.send(
        "Runtime.evaluate", {"expression": "JSON.stringify(window.__store)", "returnByValue": True}
    )
    import json

    return dict(json.loads(result["result"]["value"]))


def field_id(schema: str, label: str) -> str:
    for line in schema.splitlines():
        if f'"{label}' in line:
            return line.split()[0]
    raise AssertionError(f"{label} not in\n{schema}")


def kinds(schema: str) -> dict[str, str]:
    found: dict[str, str] = {}
    for line in schema.splitlines()[1:]:
        match = re.match(r'(f\d+) (\w+) "([^"]*)"', line)
        if match:
            found[match.group(3)] = match.group(2)
    return found


def test_form_finds_every_widget_with_the_right_driver() -> None:
    async def scenario() -> None:
        with serve() as (host, _):
            async with engine_with_chrome() as engine:
                await engine.open("a", f"http://{host}/application.html")
                schema = await engine.form("a")
                found = kinds(schema)
                assert found["First name"] == "text"
                assert found["Country"] == "select"
                assert found["Degree"] == "combobox"
                assert found["School"] == "listbox"
                assert found["Will you require visa sponsorship?"] == "radio"
                assert found["Are you at least 18 years old?"] == "buttons"
                assert found["I agree to the privacy notice"] == "toggle"
                assert found["Resume/CV"] == "file"
                assert "Visa type" not in found
                assert "4 required empty" in schema.splitlines()[0] or "required empty" in schema

    run(scenario())


def test_fill_writes_every_widget_and_the_store_agrees() -> None:
    async def scenario() -> None:
        with tempfile.TemporaryDirectory() as tmp, serve() as (host, recorder):
            resume = Path(tmp) / "resume.pdf"
            resume.write_bytes(b"%PDF-1.4 test")
            async with engine_with_chrome() as engine:
                opened = await engine.open("a", f"http://{host}/application.html")
                surface = opened.split()[0]
                values = {
                    "Resume/CV": str(resume),
                    "First name": "Ada",
                    "Last name": "Lovelace",
                    "Email": "ada@example.com",
                    "Phone": "5551234567",
                    "Country": "Taiwan",
                    "Degree": "Master of Science",
                    "School": "Riverbend University",
                    "Will you require visa sponsorship?": "No",
                    "Are you at least 18 years old?": "Yes",
                    "I agree to the privacy notice": "on",
                }
                report = await engine.fill("a", values)
                # The late autofill doubles the first name after it was written; the final
                # read-back must catch that instead of reporting success.
                assert re.search(r'"First name" mismatch "AdaAda".*changed after', report), report
                again = await engine.fill("a", {"First name": "Ada"})
                assert "1/1 ok" in again, again
                state = await store(engine, surface)
                assert state["first"] == "Ada"
                assert state["last"] == "Lovelace"
                assert state["email"] == "ada@example.com"
                assert state["phone"] == "(555) 123-4567"
                assert state["country"] == "Taiwan"
                assert state["degree"] == "Master of Science"
                assert state["school"] == "Riverbend University"
                assert state["sponsor"] == "No"
                assert state["adult"] == "Yes"
                assert state["agree"] is True
                assert state["resume"] == "resume.pdf"
                await asyncio.sleep(0.2)
                assert recorder.snapshot()["submissions"] == {}

    run(scenario())


def test_an_ambiguous_option_is_reported_and_nothing_is_chosen() -> None:
    async def scenario() -> None:
        with serve() as (host, _):
            async with engine_with_chrome() as engine:
                opened = await engine.open("a", f"http://{host}/application.html")
                report = await engine.fill("a", {"Degree": "Master"})
                assert "ambiguous" in report
                assert "Master of Science" in report and "Master of Business" in report
                assert "degree" not in await store(engine, opened.split()[0])

    run(scenario())


def test_a_conditional_field_is_announced() -> None:
    async def scenario() -> None:
        with serve() as (host, _):
            async with engine_with_chrome() as engine:
                await engine.open("a", f"http://{host}/application.html")
                report = await engine.fill("a", {"Will you require visa sponsorship?": "Yes"})
                assert 'new fields:' in report and '"Visa type"' in report

    run(scenario())


def test_pressing_a_chosen_yes_no_button_again_is_avoided() -> None:
    async def scenario() -> None:
        with serve() as (host, _):
            async with engine_with_chrome() as engine:
                opened = await engine.open("a", f"http://{host}/application.html")
                await engine.fill("a", {"Are you at least 18 years old?": "Yes"})
                second = await engine.fill("a", {"Are you at least 18 years old?": "Yes"})
                assert "unchanged" in second
                assert (await store(engine, opened.split()[0]))["adult"] == "Yes"

    run(scenario())


def test_return_in_a_text_field_cannot_submit_the_form() -> None:
    async def scenario() -> None:
        with serve() as (host, recorder):
            async with engine_with_chrome() as engine:
                await engine.open("a", f"http://{host}/application.html")
                await engine.fill("a", {"First name": "Ada"})
                text = await engine.observe("a", query="Last name")
                ref = re.search(r"\[(e\d+)\]", text)
                assert ref is not None
                await engine.act("a", ref.group(1), "focus")
                with pytest.raises(LayerError) as caught:
                    await engine.key("a", "Enter")
                assert caught.value.code == "COMMIT_GATED"
                await asyncio.sleep(0.3)
                assert recorder.snapshot()["submissions"] == {}

    run(scenario())


def test_a_script_written_value_is_not_mistaken_for_a_filled_field() -> None:
    """The fixture's store ignores untrusted input, like real framework state."""

    async def scenario() -> None:
        with serve() as (host, _):
            async with engine_with_chrome() as engine:
                opened = await engine.open("a", f"http://{host}/application.html")
                surface = SurfaceId(opened.split()[0])
                page = engine.backend_for(surface).page(surface)  # type: ignore[attr-defined]
                await page.session.send("Runtime.evaluate", {"expression": (
                    "const f = document.getElementById('last'); f.value = 'Typed by JS';"
                    "f.dispatchEvent(new Event('input', {bubbles: true})); 1"
                )})
                assert "last" not in await store(engine, surface)
                report = await engine.fill("a", {"Last name": "Lovelace"})
                assert "1/1 ok" in report
                assert (await store(engine, surface))["last"] == "Lovelace"

    run(scenario())


def ref_named(text: str, role: str, name: str) -> str:
    for line in text.splitlines():
        if line.lstrip().startswith(f'- {role} "{name}') and "[e" in line:
            return line.split("[e", 1)[1].split("]", 1)[0].join(["e", ""])
    raise AssertionError(f'{role} "{name}" not in\n{text}')


def test_element_level_select_uses_the_widget_driver() -> None:
    async def scenario() -> tuple[dict[str, Any], str]:
        with serve() as (host, _):
            async with engine_with_chrome() as engine:
                opened = await engine.open("a", f"http://{host}/application.html")
                seen = await engine.observe("a", mode="full")
                await engine.act("a", ref_named(seen, "combobox", "Country"), "select", "Taiwan")
                await engine.act("a", ref_named(seen, "combobox", "Degree"), "select",
                                 "Master of Science")
                await engine.act("a", ref_named(seen, "button", "School"), "select",
                                 "Riverbend University")
                with pytest.raises(LayerError) as caught:
                    await engine.act("a", ref_named(seen, "combobox", "Degree"), "select",
                                     "Master")
                assert caught.value.code == "AMBIGUOUS"
                assert "Master of Business Administration" in caught.value.message
                return await store(engine, opened.split()[0]), caught.value.message

    state, _ = run(scenario())
    assert state["country"] == "Taiwan"
    assert state["degree"] == "Master of Science"
    assert state["school"] == "Riverbend University"


def test_upload_through_the_button_that_opens_the_file_chooser() -> None:
    async def scenario() -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as tmp, serve() as (host, _):
            resume = Path(tmp) / "resume.pdf"
            resume.write_bytes(b"%PDF-1.4 test")
            async with engine_with_chrome() as engine:
                opened = await engine.open("a", f"http://{host}/application.html")
                seen = await engine.observe("a", mode="full")
                result = await engine.act(
                    "a", ref_named(seen, "button", "Attach"), "upload", str(resume)
                )
                assert "delivered" in result.splitlines()[0]
                return await store(engine, opened.split()[0])

    assert run(scenario())["resume"] == "resume.pdf"


def test_set_on_a_dropdown_that_takes_no_text_chooses_the_option() -> None:
    async def scenario() -> tuple[str, dict[str, Any]]:
        with serve() as (host, _):
            async with engine_with_chrome() as engine:
                opened = await engine.open("a", f"http://{host}/application.html")
                seen = await engine.observe("a", mode="full")
                result = await engine.act(
                    "a", ref_named(seen, "combobox", "Country"), "set", "Japan"
                )
                return result, await store(engine, opened.split()[0])

    result, state = run(scenario())
    assert result.startswith('select combobox "Country"'), result
    assert state["country"] == "Japan"
