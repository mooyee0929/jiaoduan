"""Run an adapter tool: each step goes through the same engine calls an agent would make.

A tool is safe to run again from the start. Fills that are already done come back unchanged,
which is what lets a gated commit be approved and the whole tool re-run with the gate id.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING
from urllib.parse import urlsplit

from jiaoduan import prune, render
from jiaoduan.adapters.schema import PARAM_TYPES, Adapter, Step, Tool
from jiaoduan.errors import ADAPTER_DRIFT, COMMIT_GATED, INVALID_ARGS, NOT_FOUND, LayerError
from jiaoduan.expect import Scene, check
from jiaoduan.forms.schema import WriteStatus
from jiaoduan.model import Node, Ref, Settle, SurfaceId

if TYPE_CHECKING:
    from jiaoduan.engine import Engine

_GOOD = frozenset({WriteStatus.OK, WriteStatus.UNCHANGED})
_PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


def list_tools(adapters: Sequence[Adapter]) -> str:
    lines: list[str] = []
    for adapter in adapters:
        for tool in adapter.tools:
            params = ", ".join(
                f"{p.name}: {p.type}{'' if p.required else '?'}"
                + (f" ({'|'.join(p.enum)})" if p.enum else "")
                for p in tool.params
            )
            commits = " [commits: needs approval]" if tool.commits else ""
            lines.append(f"{tool.name}({params}) {tool.description}{commits}  <{adapter.id}>")
    return "\n".join(lines) or "no adapter tools for this surface"


def _bind(tool: Tool, arguments: Mapping[str, object]) -> dict[str, str]:
    unknown = set(arguments) - {p.name for p in tool.params}
    if unknown:
        raise LayerError(INVALID_ARGS, f"{tool.name} has no inputs {sorted(unknown)}")
    bound: dict[str, str] = {}
    for param in tool.params:
        if param.name not in arguments:
            if param.required:
                raise LayerError(INVALID_ARGS, f"{tool.name} needs {param.name}")
            bound[param.name] = ""
            continue
        value = arguments[param.name]
        if not isinstance(value, PARAM_TYPES[param.type]) or (
            param.type == "number" and isinstance(value, bool)
        ):
            raise LayerError(INVALID_ARGS, f"{param.name} must be a {param.type}")
        text = str(value).lower() if isinstance(value, bool) else str(value)
        if param.enum and text not in param.enum:
            raise LayerError(INVALID_ARGS, f"{param.name} is one of {list(param.enum)}")
        bound[param.name] = text
    return bound


def _render_value(value: object, bound: Mapping[str, str]) -> object:
    if isinstance(value, str):
        return _PLACEHOLDER.sub(lambda m: bound[m.group(1)], value)
    if isinstance(value, Mapping):
        return {k: _render_value(v, bound) for k, v in value.items()}
    if isinstance(value, list):
        return [_render_value(v, bound) for v in value]
    return value


def _matches(node: Node, spec: Mapping[str, object]) -> bool:
    if "role" in spec and node.role.value != spec["role"]:
        return False
    if "name" in spec and node.name.casefold() != str(spec["name"]).casefold():
        return False
    if "name_contains" in spec and str(spec["name_contains"]).casefold() not in (
        node.name.casefold()
    ):
        return False
    scope = spec.get("scope")
    if isinstance(scope, list):
        names = [name.casefold() for _, name in node.locator.scope]
        return all(str(part).casefold() in names for part in scope)
    return True


def locate(nodes: Sequence[Node], spec: Mapping[str, object], step_no: int) -> Node:
    hits = [n for n in nodes if n.ref is not None and _matches(n, spec)]
    ordinal = spec.get("ordinal")
    if isinstance(ordinal, int) and 1 <= ordinal <= len(hits):
        return hits[ordinal - 1]
    if len(hits) == 1:
        return hits[0]
    found = "nothing" if not hits else f"{len(hits)} elements"
    raise LayerError(
        ADAPTER_DRIFT, f"step {step_no}: {dict(spec)} matched {found}",
        hint="the page changed since the adapter was written; continue by hand with observe",
    )


def _concurrency_key(adapter: Adapter, location: str) -> str:
    if adapter.concurrency_key == "origin":
        parts = urlsplit(location)
        return f"{adapter.id}|{parts.scheme}://{parts.netloc}"
    if adapter.concurrency_key == "app":
        return f"{adapter.id}|{location}"
    return f"{adapter.id}|{adapter.concurrency_key}"


def _find_tool(adapters: Sequence[Adapter], name: str) -> tuple[Adapter, Tool]:
    hits = [(a, t) for a in adapters for t in a.tools if t.name == name]
    if not hits:
        available = ", ".join(t.name for a in adapters for t in a.tools) or "none"
        raise LayerError(NOT_FOUND, f"no tool {name!r} here; available: {available}")
    if len(hits) > 1:
        raise LayerError(INVALID_ARGS, f"{name} is defined by {[a.id for a, _ in hits]}")
    return hits[0]


async def run_tool(
    engine: Engine,
    agent: str,
    surface: SurfaceId,
    name: str,
    arguments: Mapping[str, object],
    gate: str | None,
) -> str:
    backend = engine.backend_for(surface)
    info = await backend.describe(surface)
    adapter, tool = _find_tool(engine.adapters.matching(info), name)
    bound = _bind(tool, arguments)
    lines = [f"{tool.name} <{adapter.id} v{adapter.version}>"]
    outputs: dict[str, str] = {}
    async with engine.session_locks.hold(
        _concurrency_key(adapter, info.location), adapter.concurrency_max
    ):
        for number, step in enumerate(tool.steps, start=1):
            try:
                summary = await _run_step(engine, agent, surface, step, bound, number, gate,
                                          outputs)
            except LayerError as exc:
                if exc.code == COMMIT_GATED:
                    exc.hint = (
                        "a person approves with `jiaoduan gate approve <id>`; then call this "
                        "tool again with the same arguments and gate=<id>"
                    )
                lines.append(f"{number} {step.kind}: FAILED {exc.code}: {exc.message}")
                raise LayerError(exc.code, "\n".join(lines), delivery=exc.delivery,
                                 hint=exc.hint) from exc
            lines.append(f"{number} {step.kind}: {summary}")
    lines += [f"{key} = {render.quote(value, 400)}" for key, value in outputs.items()]
    return "\n".join(lines)


async def _run_step(
    engine: Engine,
    agent: str,
    surface: SurfaceId,
    step: Step,
    bound: Mapping[str, str],
    number: int,
    gate: str | None,
    outputs: dict[str, str],
) -> str:
    args = _render_value(step.args, bound)
    if step.kind == "goto":
        await engine.backend_for(surface).navigate(surface, str(args))
        return f"loaded {str(args)[:120]}"
    if step.kind == "fill":
        assert isinstance(args, Mapping)
        values = {str(k): str(v) for k, v in args.items() if str(v) != ""}
        lines, _, _ = await engine.fill_lines(agent, values, surface)
        bad = [line for line in lines if line.outcome.status not in _GOOD]
        if bad:
            detail = "; ".join(
                f"{line.label}: {line.outcome.status.value} {line.outcome.note}".strip()
                for line in bad
            )
            raise LayerError(ADAPTER_DRIFT, f"step {number}: {detail}")
        return f"{len(lines)} fields ok"
    nodes, _ = await engine.read_surface(agent, surface)
    if step.kind in {"press", "read"}:
        assert isinstance(args, Mapping)
        target = locate(nodes, args, number)
        assert target.ref is not None
        if step.kind == "read":
            text = await engine.read(agent, target.ref)
            outputs[str(args.get("name") or args.get("name_contains") or target.name)] = text
            return f"read {len(text)} chars"
        result = await engine.act(
            agent, target.ref, "press", gate=gate if step.commit else None,
            force_commit=step.commit,
        )
        return result.splitlines()[0]
    assert isinstance(args, Mapping)
    if step.kind == "wait":
        ok, text = await engine.wait_result(agent, args, surface, step.timeout)
        if not ok:
            raise LayerError(ADAPTER_DRIFT, f"step {number}: {text}")
        return text
    info = await engine.backend_for(surface).describe(surface)
    verdict = check(Scene(nodes, info.location, Settle(True, 0), Ref("")), args)
    if not verdict.ok:
        view, _, _ = prune.select(nodes, prune.Mode.SKELETON, 60)
        raise LayerError(
            ADAPTER_DRIFT, f"step {number}: expect {verdict.kind} failed ({verdict.detail})",
            hint="current page:\n" + render.nodes_text(view),
        )
    return f"expect {verdict.kind} ok"
