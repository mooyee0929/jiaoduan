from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable
from typing import TypeVar

import pytest

from agentlayer.claims import Claims, SessionLocks
from agentlayer.engine import Engine
from agentlayer.errors import LayerError
from agentlayer.gate import Gate, Policy
from agentlayer.model import Action, ActionKind, CommitFacts, Handle, KeyContext, RawNode
from agentlayer.trace import Trace
from tests.fakes import SURFACE, FakeBackend, button, form, textbox

T = TypeVar("T")


def run(coroutine: Awaitable[T]) -> T:
    async def wrapper() -> T:
        return await coroutine

    return asyncio.run(wrapper())


def make(tree: list[RawNode], policy: Policy = Policy.ASK) -> tuple[Engine, FakeBackend]:
    backend = FakeBackend(tree)
    return Engine({"fake": backend}, gate=Gate(policy)), backend


def ref_of(text: str, name: str) -> str:
    line = next(line for line in text.splitlines() if f'"{name}"' in line)
    return line.split("[e")[1].split("]")[0].join(["e", ""])


def test_observe_lists_elements_with_refs() -> None:
    engine, _ = make([form(1, "Sign up", textbox(2, "Email"), button(3, "Next"))])
    text = run(engine.observe("a", str(SURFACE)))
    assert '- textbox "Email" [e2] [editable]' in text
    assert "3 elements" in text


def test_set_reports_the_diff_and_tree_evidence() -> None:
    field = textbox(2, "Email")
    engine, backend = make([field])

    def apply(_: Handle, action: Action) -> None:
        field.value = action.text

    backend.on_perform = apply

    async def scenario() -> str:
        seen = await engine.observe("a", str(SURFACE))
        return await engine.act("a", ref_of(seen, "Email"), "set", "a@b.c")

    text = run(scenario())
    assert "evidence tree" in text
    assert 'value "" -> "a@b.c"' in text


def test_a_set_that_does_not_stick_warns_and_claims_no_evidence() -> None:
    engine, _ = make([textbox(2, "Email")])

    async def scenario() -> str:
        seen = await engine.observe("a", str(SURFACE))
        return await engine.act("a", ref_of(seen, "Email"), "set", "a@b.c")

    text = run(scenario())
    assert "evidence none" in text and 'wanted "a@b.c"' in text


def test_press_with_a_failed_expect_has_no_evidence() -> None:
    engine, _ = make([button(1, "Go")])

    async def scenario() -> str:
        seen = await engine.observe("a", str(SURFACE))
        return await engine.act("a", ref_of(seen, "Go"), "press", expect={"text_visible": "Done"})

    text = run(scenario())
    assert "expect text_visible: FAILED" in text and "evidence none" in text


def test_commit_buttons_stop_at_the_gate_until_a_person_approves() -> None:
    engine, backend = make([button(1, "Submit")])

    async def scenario() -> str:
        seen = await engine.observe("a", str(SURFACE))
        ref = ref_of(seen, "Submit")
        with pytest.raises(LayerError) as caught:
            await engine.act("a", ref, "press")
        assert caught.value.code == "COMMIT_GATED" and backend.performed == []
        assert "g1" in engine.gate_list()
        engine.gate_approve("g1")
        return await engine.act("a", ref, "press", gate="g1")

    assert "press button" in run(scenario())
    assert len(backend.performed) == 1 and backend.armed


def test_toggle_already_in_state_sends_nothing() -> None:
    from agentlayer.model import Role, State

    box = RawNode(1, Role.CHECKBOX, "checkbox", "Agree", actionable=True,
                  states=frozenset({State.CHECKED}))
    engine, backend = make([box])

    async def scenario() -> str:
        seen = await engine.observe("a", str(SURFACE))
        return await engine.act("a", ref_of(seen, "Agree"), "toggle", "on")

    text = run(scenario())
    assert backend.performed == [] and "nothing was sent" in text


def test_toggle_presses_when_the_state_differs() -> None:
    from agentlayer.model import Role

    box = RawNode(1, Role.CHECKBOX, "checkbox", "Agree", actionable=True)
    engine, backend = make([box])

    async def scenario() -> None:
        seen = await engine.observe("a", str(SURFACE))
        await engine.act("a", ref_of(seen, "Agree"), "toggle", "on")

    run(scenario())
    assert backend.performed[0][1].kind is ActionKind.PRESS


def test_return_that_would_submit_is_gated() -> None:
    engine, backend = make([textbox(1, "Email")])
    backend.key_ctx = KeyContext(implicit_submit=True, detail="single-line field in a form")

    async def scenario() -> None:
        await engine.observe("a", str(SURFACE))
        with pytest.raises(LayerError) as caught:
            await engine.key("a", "Enter")
        assert caught.value.code == "COMMIT_GATED"

    run(scenario())
    assert backend.keys == []


def test_another_agents_surface_is_refused() -> None:
    engine, _ = make([button(1, "Go")])

    async def scenario() -> None:
        await engine.observe("a", str(SURFACE))
        with pytest.raises(LayerError) as caught:
            await engine.observe("b", str(SURFACE))
        assert caught.value.code == "SURFACE_CLAIMED"
        engine.disconnect("a")
        await engine.observe("b", str(SURFACE))

    run(scenario())


def test_a_replaced_element_is_acted_on_with_a_note() -> None:
    field = textbox(2, "Email")
    tree = [form(9, "F", field)]
    engine, backend = make(tree)

    async def scenario() -> str:
        seen = await engine.observe("a", str(SURFACE))
        ref = ref_of(seen, "Email")
        backend.tree = [form(9, "F", textbox(50, "Email"))]
        return await engine.act("a", ref, "focus")

    text = run(scenario())
    assert "re-rendered" in text
    assert backend.performed[0][0] == 50


def test_trace_records_the_locator_but_never_the_typed_text() -> None:
    lines: list[str] = []
    backend = FakeBackend([textbox(2, "Password")])
    engine = Engine({"fake": backend}, gate=Gate(Policy.ASK), trace=Trace(lines.append))

    async def scenario() -> None:
        seen = await engine.observe("a", str(SURFACE))
        await engine.act("a", ref_of(seen, "Password"), "set", "hunter2")

    run(scenario())
    assert "hunter2" not in "".join(lines)
    record = json.loads(lines[0])
    assert record["text_len"] == 7 and record["locator"]["name"] == "Password"
    assert record["op"] == "act" and record["v"] == 1


def test_unknown_mode_is_rejected() -> None:
    engine, _ = make([button(1, "Go")])
    with pytest.raises(LayerError):
        run(engine.observe("a", str(SURFACE), mode="sideways"))


def test_commit_facts_are_consulted_for_submit_buttons() -> None:
    engine, backend = make([button(1, "Update Profile")])
    backend.facts = CommitFacts(submit_button=True)

    async def scenario() -> None:
        seen = await engine.observe("a", str(SURFACE))
        with pytest.raises(LayerError) as caught:
            await engine.act("a", ref_of(seen, "Update Profile"), "press")
        assert caught.value.code == "COMMIT_GATED"

    run(scenario())


def test_claims_refuse_a_second_owner_and_release_all() -> None:
    claims = Claims()
    claims.acquire("a", SURFACE)
    with pytest.raises(LayerError):
        claims.acquire("b", SURFACE)
    assert claims.release_all("a") == [SURFACE]
    claims.acquire("b", SURFACE)


def test_session_locks_limit_concurrency() -> None:
    async def scenario() -> int:
        locks = SessionLocks()
        running = 0
        peak = 0

        async def worker() -> None:
            nonlocal running, peak
            async with locks.hold("site", 1):
                running += 1
                peak = max(peak, running)
                await asyncio.sleep(0)
                running -= 1

        await asyncio.gather(*(worker() for _ in range(5)))
        return peak

    assert asyncio.run(scenario()) == 1
