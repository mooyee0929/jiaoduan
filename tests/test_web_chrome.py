"""End-to-end against a real headless Chrome. Run with: pytest -m chrome"""

from __future__ import annotations

import re

import pytest

from agentlayer.engine import Engine
from agentlayer.errors import LayerError
from agentlayer.gate import Policy
from tests.web_harness import engine_with_chrome, pages_server, run

pytestmark = pytest.mark.chrome


def ref(text: str, needle: str) -> str:
    for line in text.splitlines():
        if needle in line:
            found = re.search(r"\[(e\d+)\]", line)
            if found:
                return found.group(1)
    raise AssertionError(f"{needle!r} not in:\n{text}")


async def open_basic(engine: Engine, host: str) -> str:
    return await engine.open("a", f"http://{host}/basic.html")


def test_observe_shows_form_controls_and_masks_passwords() -> None:
    async def scenario() -> None:
        with pages_server() as host:
            async with engine_with_chrome() as engine:
                text = await open_basic(engine, host)
                assert 'textbox "Name"' in text
                assert 'checkbox "Agree"' in text
                secret = ref(text, 'textbox "Secret"')
                result = await engine.act("a", secret, "set", "hunter2")
                assert "hunter2" not in result
                assert "hunter2" not in await engine.observe("a")

    run(scenario())


def test_set_reaches_the_field_with_tree_evidence() -> None:
    async def scenario() -> None:
        with pages_server() as host:
            async with engine_with_chrome() as engine:
                text = await open_basic(engine, host)
                result = await engine.act("a", ref(text, '"Name"'), "set", "Ada")
                assert "evidence tree" in result and '-> "Ada"' in result
                again = await engine.act("a", ref(text, '"Name"'), "set", "Grace")
                assert '"Ada" -> "Grace"' in again

    run(scenario())


def test_press_reports_the_diff() -> None:
    async def scenario() -> None:
        with pages_server() as host:
            async with engine_with_chrome() as engine:
                text = await open_basic(engine, host)
                result = await engine.act("a", ref(text, '"Add row"'), "press")
                assert 'row 1' in result and "delivered" in result

    run(scenario())


def test_request_expect_passes_on_success_and_fails_on_a_server_error() -> None:
    async def scenario() -> None:
        with pages_server() as host:
            async with engine_with_chrome() as engine:
                text = await open_basic(engine, host)
                good = await engine.act(
                    "a", ref(text, '"Save draft"'), "press",
                    expect={"request": {"method": "POST", "url": "/api/save"}},
                )
                assert "evidence network" in good and "net POST /api/save 200" in good
                bad = await engine.act(
                    "a", ref(text, '"Save failing"'), "press",
                    expect={"request": {"method": "POST", "url": "/api/fail"}},
                )
                assert "expect request: FAILED" in bad and "evidence none" in bad

    run(scenario())


def test_toggle_is_idempotent() -> None:
    async def scenario() -> None:
        with pages_server() as host:
            async with engine_with_chrome() as engine:
                text = await open_basic(engine, host)
                box = ref(text, '"Agree"')
                first = await engine.act("a", box, "toggle", "on")
                assert "+checked" in first
                second = await engine.act("a", box, "toggle", "on")
                assert "nothing was sent" in second

    run(scenario())


def test_overlay_is_detected_instead_of_clicking_something_else() -> None:
    async def scenario() -> None:
        with pages_server() as host:
            async with engine_with_chrome() as engine:
                text = await open_basic(engine, host)
                await engine.act("a", ref(text, '"Show overlay"'), "press")
                with pytest.raises(LayerError) as caught:
                    await engine.act("a", ref(text, '"Covered"'), "press")
                assert caught.value.code == "NOT_ACTIONABLE"
                assert "on top" in caught.value.message

    run(scenario())


def test_clickable_div_with_no_role_gets_a_ref() -> None:
    async def scenario() -> None:
        with pages_server() as host:
            async with engine_with_chrome() as engine:
                text = await open_basic(engine, host)
                target = ref(text, "Plain div target")
                result = await engine.act("a", target, "press")
                assert "div clicked" in result

    run(scenario())


def test_submit_button_is_gated_then_released_by_approval() -> None:
    async def scenario() -> None:
        with pages_server() as host:
            async with engine_with_chrome() as engine:
                text = await open_basic(engine, host)
                submit = ref(text, 'button "Submit"')
                with pytest.raises(LayerError) as caught:
                    await engine.act("a", submit, "press")
                assert caught.value.code == "COMMIT_GATED"
                engine.gate_approve("g1")
                result = await engine.act("a", submit, "press", gate="g1")
                assert "submitted" in result

    run(scenario())


def test_return_in_a_form_field_is_gated_and_the_guard_backs_it_up() -> None:
    async def scenario() -> None:
        with pages_server() as host:
            async with engine_with_chrome() as engine:
                text = await open_basic(engine, host)
                await engine.act("a", ref(text, '"Name"'), "focus")
                with pytest.raises(LayerError) as caught:
                    await engine.key("a", "Enter")
                assert caught.value.code == "COMMIT_GATED"
                assert "submitted" not in await engine.observe("a")

    run(scenario())


def test_guard_stops_a_submit_nobody_asked_for() -> None:
    async def scenario() -> None:
        with pages_server() as host:
            async with engine_with_chrome() as engine:
                backend = engine.backend_for(engine_surface(await open_basic(engine, host)))
                surface = engine_surface(await engine.observe("a"))
                page = backend.page(surface)  # type: ignore[attr-defined]
                await page.session.send("Runtime.evaluate", {
                    "expression": "document.querySelector('form').requestSubmit(); 1",
                })
                text = await engine.observe("a")
                assert "submitted" not in text

    run(scenario())


def engine_surface(text: str):  # type: ignore[no-untyped-def]
    from agentlayer.model import SurfaceId

    return SurfaceId(text.split()[0])


def test_navigation_is_reported_and_refs_of_the_old_page_die() -> None:
    async def scenario() -> None:
        with pages_server() as host:
            async with engine_with_chrome() as engine:
                text = await open_basic(engine, host)
                name = ref(text, '"Name"')
                result = await engine.act("a", ref(text, '"Go to second"'), "press")
                assert "page replaced" in result and "Second page" in result
                with pytest.raises(LayerError) as caught:
                    await engine.act("a", name, "focus")
                assert caught.value.code == "STALE_REF"

    run(scenario())


def test_iframes_and_shadow_dom_are_one_tree() -> None:
    async def scenario() -> None:
        with pages_server() as host:
            async with engine_with_chrome() as engine:
                text = await engine.open("a", f"http://{host}/frames.html")
                assert text.count('"Inner button"') == 2, text
                assert text.count('"Shadow button"') == 2, text
                lines = [line for line in text.splitlines() if '"Inner button"' in line]
                shadow = [line for line in text.splitlines() if '"Shadow button"' in line]
                fields = [line for line in text.splitlines() if '"Inner field"' in line]
                for line_set, needle in ((lines, "inner clicked"), (shadow, "shadow clicked")):
                    for line in line_set:
                        result = await engine.act("a", ref(line, "button"), "press")
                        assert needle in result, result
                for line in fields:
                    result = await engine.act("a", ref(line, "textbox"), "set", "typed")
                    assert "evidence tree" in result, result

    run(scenario())


def test_two_agents_work_in_separate_windows_without_interfering() -> None:
    async def scenario() -> None:
        with pages_server() as host:
            async with engine_with_chrome() as engine:
                a = await engine.open("a", f"http://{host}/basic.html")
                b = await engine.open("b", f"http://{host}/basic.html")
                assert a.split()[0] != b.split()[0]
                first = await engine.act("a", ref(a, '"Name"'), "set", "for-a")
                second = await engine.act("b", ref(b, '"Name"'), "set", "for-b")
                assert '-> "for-a"' in first and '-> "for-b"' in second
                with pytest.raises(LayerError) as caught:
                    await engine.observe("b", a.split()[0])
                assert caught.value.code == "SURFACE_CLAIMED"

    run(scenario())


def test_allow_policy_lets_commits_through() -> None:
    async def scenario() -> None:
        with pages_server() as host:
            async with engine_with_chrome(Policy.ALLOW) as engine:
                text = await open_basic(engine, host)
                result = await engine.act("a", ref(text, 'button "Submit"'), "press")
                assert "submitted" in result

    run(scenario())
