from __future__ import annotations

import json
from pathlib import Path

import pytest

from agentlayer.adapters.registry import AdapterRegistry, applies
from agentlayer.adapters.runner import list_tools, locate
from agentlayer.adapters.schema import AdapterInvalid, parse, placeholders
from agentlayer.adapters.tools import check_paths, draft_from_trace
from agentlayer.engine import Engine
from agentlayer.errors import LayerError
from agentlayer.gate import Gate, Policy
from agentlayer.model import Locator, Node, Ref, Role, Surface, SurfaceId
from agentlayer.trace import SCHEMA
from tests.fakes import SURFACE, FakeBackend, button, form, text
from tests.test_engine import run

CONTACT = """
[adapter]
id = "example.test/contact"
version = 2
match = { url = "fake://contact*" }
concurrency = { key = "origin", max = 1 }

[[tool]]
name = "send_message"
description = "Send the contact form"
input.topic = { type = "string", enum = ["sales", "support"] }
input.urgent = { type = "boolean", required = false }

[[tool.step]]
goto = "fake://contact?topic={topic}"

[[tool.step]]
press = { role = "button", name = "Send" }
commit = true

[[tool.step]]
expect = { text_visible = "Thanks" }
"""


def write_adapter(directory: Path, name: str, body: str) -> Path:
    path = directory / name
    path.write_text(body, encoding="utf-8")
    return path


def test_a_valid_adapter_parses_into_tools_and_steps() -> None:
    adapter = parse(CONTACT, "contact.toml")
    assert adapter.id == "example.test/contact" and adapter.version == 2
    tool = adapter.tools[0]
    assert [s.kind for s in tool.steps] == ["goto", "press", "expect"]
    assert tool.commits
    assert {p.name: p.required for p in tool.params} == {"topic": True, "urgent": False}


@pytest.mark.parametrize("broken, message", [
    ('[adapter]\nid="x"\nmatch={url="*"}\n', "at least one [[tool]]"),
    ('[adapter]\nid="x"\n[[tool]]\nname="t"\n[[tool.step]]\ngoto="u"\n', "match needs url or app"),
    ('[adapter]\nmatch={url="*"}\n[[tool]]\nname="Bad-Name"\n[[tool.step]]\ngoto="u"\n',
     "lower_snake_case"),
    ('[adapter]\nmatch={url="*"}\n[[tool]]\nname="t"\n[[tool.step]]\ngoto="u"\nrun="rm -rf"\n',
     "unknown keys"),
    ('[adapter]\nmatch={url="*"}\n[[tool]]\nname="t"\n[[tool.step]]\ngoto="{missing}"\n',
     "undeclared inputs"),
    ('[adapter]\nmatch={url="*"}\n[[tool]]\nname="t"\n[[tool.step]]\ngoto="u"\ncommit=true\n',
     "commit = true only applies to press"),
    ('[adapter]\nmatch={url="*"}\n[[tool]]\nname="t"\n[[tool.step]]\njs="alert(1)"\n',
     "exactly one of"),
])
def test_adapters_that_could_run_code_or_are_malformed_are_rejected(
    broken: str, message: str
) -> None:
    with pytest.raises(AdapterInvalid) as caught:
        parse(broken, "broken.toml")
    assert message in caught.value.message


def test_placeholders_are_found_inside_nested_values() -> None:
    assert placeholders({"a": "{x} and {y}", "b": ["{z}"], "c": 3}) == {"x", "y", "z"}


def test_registry_matches_by_url_pattern_and_skips_broken_files(tmp_path: Path) -> None:
    write_adapter(tmp_path, "contact.toml", CONTACT)
    write_adapter(tmp_path, "broken.toml", "not = [valid")
    registry = AdapterRegistry((tmp_path,))
    page = Surface(SurfaceId("web:1"), "web", "Contact", "fake://contact/form")
    other = Surface(SurfaceId("web:2"), "web", "Other", "fake://elsewhere")
    assert [a.id for a in registry.matching(page)] == ["example.test/contact"]
    assert registry.matching(other) == []
    assert tmp_path / "broken.toml" in registry.problems


def test_an_app_adapter_matches_the_app_name_only() -> None:
    body = CONTACT.replace('match = { url = "fake://contact*" }', 'match = { app = "Notes" }')
    adapter = parse(body, "notes.toml")
    assert applies(adapter, Surface(SurfaceId("mac:1"), "mac", "Notes", "notes"))
    assert not applies(adapter, Surface(SurfaceId("web:1"), "web", "Notes", "https://notes"))


def test_list_tools_shows_inputs_and_commit_marker() -> None:
    listing = list_tools([parse(CONTACT, "contact.toml")])
    assert "send_message(topic: string (sales|support), urgent: boolean?)" in listing
    assert "[commits: needs approval]" in listing


def node(name: str, role: Role, ref: str, scope: tuple[tuple[Role, str], ...] = ()) -> Node:
    return Node(
        handle=ref, role=role, native_role=role.value, name=name, value=None,
        states=frozenset(), depth=0, actionable=True, container=False,
        locator=Locator(role, name, scope), ref=Ref(ref),
    )


def test_locate_needs_one_match_unless_an_ordinal_is_given() -> None:
    nodes = [
        node("Save", Role.BUTTON, "e1", ((Role.FORM, "Billing"),)),
        node("Save", Role.BUTTON, "e2", ((Role.FORM, "Shipping"),)),
    ]
    with pytest.raises(LayerError) as caught:
        locate(nodes, {"role": "button", "name": "Save"}, 3)
    assert caught.value.code == "ADAPTER_DRIFT" and "2 elements" in caught.value.message
    assert locate(nodes, {"name": "save", "scope": ["Shipping"]}, 1).ref == "e2"
    assert locate(nodes, {"name": "Save", "ordinal": 1}, 1).ref == "e1"
    with pytest.raises(LayerError) as missing:
        locate(nodes, {"name": "Delete"}, 2)
    assert "matched nothing" in missing.value.message


def contact_engine(tmp_path: Path, policy: Policy = Policy.ASK) -> tuple[Engine, FakeBackend]:
    write_adapter(tmp_path, "contact.toml", CONTACT)
    backend = FakeBackend([form(1, "Contact", button(2, "Send"))])
    backend.location = "fake://contact"
    engine = Engine({"fake": backend}, gate=Gate(policy), adapters=AdapterRegistry((tmp_path,)))
    return engine, backend


def thank_on_send(backend: FakeBackend) -> None:
    def respond(handle: object, action: object) -> None:
        backend.tree = [text("Thanks, we got it")]

    backend.on_perform = respond


def test_a_commit_step_is_gated_then_the_rerun_with_the_gate_id_finishes(
    tmp_path: Path,
) -> None:
    engine, backend = contact_engine(tmp_path)
    thank_on_send(backend)

    async def scenario() -> str:
        await engine.observe("a", str(SURFACE))
        with pytest.raises(LayerError) as caught:
            await engine.call_tool("a", "send_message", {"topic": "sales"})
        assert caught.value.code == "COMMIT_GATED"
        assert "1 goto: loaded fake://contact?topic=sales" in caught.value.message
        assert "gate approve" in (caught.value.hint or "")
        gate_id = caught.value.message.split("gate=")[1].split()[0]
        assert backend.performed == []
        engine.gate_approve(gate_id)
        return await engine.call_tool("a", "send_message", {"topic": "sales"}, gate=gate_id)

    result = run(scenario())
    assert "3 expect: expect text_visible ok" in result
    assert len(backend.performed) == 1


def test_bad_arguments_are_refused_before_any_step_runs(tmp_path: Path) -> None:
    engine, backend = contact_engine(tmp_path)

    async def scenario(arguments: dict[str, object]) -> str:
        await engine.observe("a", str(SURFACE))
        return await engine.call_tool("a", "send_message", arguments)

    for arguments, words in (
        ({}, "needs topic"),
        ({"topic": "billing"}, "one of"),
        ({"topic": "sales", "urgent": "yes"}, "must be a boolean"),
        ({"topic": "sales", "extra": 1}, "has no inputs"),
    ):
        with pytest.raises(LayerError) as caught:
            run(scenario(arguments))
        assert caught.value.code == "INVALID_ARGS" and words in caught.value.message
    assert backend.location == "fake://contact"


def test_a_page_that_changed_reports_drift_with_the_current_view(tmp_path: Path) -> None:
    engine, backend = contact_engine(tmp_path, Policy.ALLOW)

    async def scenario() -> str:
        await engine.observe("a", str(SURFACE))
        return await engine.call_tool("a", "send_message", {"topic": "support"})

    with pytest.raises(LayerError) as caught:
        run(scenario())
    assert caught.value.code == "ADAPTER_DRIFT"
    assert "3 expect: FAILED" in caught.value.message
    assert 'button "Send"' in (caught.value.hint or "")


def test_unknown_tool_lists_what_is_available(tmp_path: Path) -> None:
    engine, _ = contact_engine(tmp_path)

    async def scenario() -> str:
        await engine.observe("a", str(SURFACE))
        return await engine.call_tool("a", "nope", {})

    with pytest.raises(LayerError) as caught:
        run(scenario())
    assert caught.value.code == "NOT_FOUND" and "send_message" in caught.value.message


def test_check_paths_reports_each_file(tmp_path: Path) -> None:
    good = write_adapter(tmp_path, "good.toml", CONTACT)
    bad = write_adapter(tmp_path, "bad.toml", "[adapter]\n")
    assert "ok   " in check_paths([str(good)])
    with pytest.raises(LayerError) as caught:
        check_paths([str(good), str(bad)])
    assert "FAIL" in caught.value.message and "ok   " in caught.value.message


def test_a_trace_drafts_an_adapter_with_typed_values_as_inputs(tmp_path: Path) -> None:
    records = [
        {"v": SCHEMA, "ok": True, "op": "open", "url": "https://shop.test/contact?x=1"},
        {"v": SCHEMA, "ok": True, "op": "act", "action": "set", "text_len": 5,
         "locator": {"role": "textbox", "name": "Your name"}},
        {"v": SCHEMA, "ok": False, "op": "act", "action": "press",
         "locator": {"role": "button", "name": "Wrong"}},
        {"v": SCHEMA, "ok": True, "op": "act", "action": "press",
         "locator": {"role": "button", "name": "Send"}},
    ]
    trace = tmp_path / "trace.jsonl"
    trace.write_text("\n".join(json.dumps(r) for r in records), encoding="utf-8")
    draft = draft_from_trace(str(trace), "shop.test/contact", "send_contact")
    adapter = parse(draft, "draft.toml")
    assert adapter.url == "https://shop.test/contact*"
    assert [p.name for p in adapter.tools[0].params] == ["your_name"]
    assert [s.kind for s in adapter.tools[0].steps] == ["fill", "press"]
    assert "Wrong" not in draft
    assert "set commit = true" in draft
