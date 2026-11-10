"""The adapter file format: named, typed tools made of steps over semantic locators.

Adapters are data only. There is no step that runs code, so an adapter can be read, reviewed
and shared without trusting its author with the page.
"""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agentlayer.errors import INVALID_ARGS, LayerError

STEP_KINDS = ("goto", "fill", "press", "wait", "read", "expect")
PARAM_TYPES: dict[str, type | tuple[type, ...]] = {
    "string": str, "number": (int, float), "boolean": bool,
}
_TOOL_NAME = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
_STEP_OPTIONS = {"commit", "timeout"}
_LOCATOR_KEYS = {"role", "name", "name_contains", "scope", "ordinal"}
_TOP_LEVEL = {"adapter", "tool"}
_ADAPTER_KEYS = {"id", "version", "match", "concurrency", "description"}
_TOOL_KEYS = {"name", "description", "input", "step"}


class AdapterInvalid(LayerError):
    def __init__(self, source: str, message: str) -> None:
        super().__init__(INVALID_ARGS, f"{source}: {message}")


@dataclass(frozen=True)
class Param:
    name: str
    type: str
    required: bool
    description: str
    enum: tuple[str, ...] = ()


@dataclass(frozen=True)
class Step:
    kind: str
    args: Any
    commit: bool = False
    timeout: float = 10.0


@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    params: tuple[Param, ...]
    steps: tuple[Step, ...]

    @property
    def commits(self) -> bool:
        return any(step.commit for step in self.steps)


@dataclass(frozen=True)
class Adapter:
    id: str
    version: int
    url: str | None
    app: str | None
    concurrency_key: str
    concurrency_max: int
    tools: tuple[Tool, ...]
    source: str


def placeholders(value: object) -> set[str]:
    if isinstance(value, str):
        return set(_PLACEHOLDER.findall(value))
    if isinstance(value, Mapping):
        return set().union(*(placeholders(v) for v in value.values())) if value else set()
    if isinstance(value, list):
        return set().union(*(placeholders(v) for v in value)) if value else set()
    return set()


def _unknown(where: str, given: Mapping[str, object], allowed: set[str], source: str) -> None:
    extra = set(given) - allowed
    if extra:
        raise AdapterInvalid(source, f"{where} has unknown keys {sorted(extra)}")


def _locator(spec: object, where: str, source: str) -> Mapping[str, object]:
    if isinstance(spec, str):
        return {"name": spec}
    if not isinstance(spec, Mapping) or not ({"role", "name", "name_contains"} & set(spec)):
        raise AdapterInvalid(source, f"{where} needs role, name or name_contains")
    _unknown(where, spec, _LOCATOR_KEYS, source)
    return spec


def _step(raw: Mapping[str, object], where: str, source: str) -> Step:
    kinds = [k for k in raw if k in STEP_KINDS]
    if len(kinds) != 1:
        raise AdapterInvalid(source, f"{where} needs exactly one of {list(STEP_KINDS)}")
    kind = kinds[0]
    _unknown(where, raw, {kind, *_STEP_OPTIONS}, source)
    args = raw[kind]
    if kind == "goto" and not isinstance(args, str):
        raise AdapterInvalid(source, f"{where} goto takes a URL string")
    if kind == "fill" and not (isinstance(args, Mapping) and args):
        raise AdapterInvalid(source, f"{where} fill takes a table of field = value")
    if kind in {"press", "read"}:
        args = _locator(args, where, source)
    if kind in {"wait", "expect"} and not isinstance(args, Mapping):
        raise AdapterInvalid(source, f"{where} {kind} takes a postcondition table")
    commit = raw.get("commit", False)
    if not isinstance(commit, bool) or (commit and kind != "press"):
        raise AdapterInvalid(source, f"{where} commit = true only applies to press")
    timeout = raw.get("timeout", 10.0)
    if not isinstance(timeout, int | float) or not 0 < timeout <= 120:
        raise AdapterInvalid(source, f"{where} timeout is 0-120 seconds")
    return Step(kind, args, commit, float(timeout))


def _params(raw: object, where: str, source: str) -> tuple[Param, ...]:
    if raw is None:
        return ()
    if not isinstance(raw, Mapping):
        raise AdapterInvalid(source, f"{where} input is a table of parameters")
    params: list[Param] = []
    for name, spec in raw.items():
        if not isinstance(spec, Mapping):
            raise AdapterInvalid(source, f"{where} input.{name} is a table")
        kind = spec.get("type", "string")
        if kind not in PARAM_TYPES:
            raise AdapterInvalid(source, f"{where} input.{name} type is one of {list(PARAM_TYPES)}")
        enum = tuple(str(v) for v in spec.get("enum", ()))
        params.append(Param(
            name=str(name), type=str(kind), required=bool(spec.get("required", True)),
            description=str(spec.get("description", "")), enum=enum,
        ))
    return tuple(params)


def _tool(raw: Mapping[str, Any], index: int, source: str) -> Tool:
    where = f"tool[{index}]"
    _unknown(where, raw, _TOOL_KEYS, source)
    name = raw.get("name")
    if not isinstance(name, str) or not _TOOL_NAME.match(name):
        raise AdapterInvalid(source, f"{where} name must be lower_snake_case")
    params = _params(raw.get("input"), where, source)
    raw_steps = raw.get("step")
    if not isinstance(raw_steps, list) or not raw_steps:
        raise AdapterInvalid(source, f"{where} needs at least one [[tool.step]]")
    steps = tuple(_step(s, f"{where}.step[{i}]", source) for i, s in enumerate(raw_steps))
    declared = {p.name for p in params}
    used = set().union(*(placeholders(s.args) for s in steps))
    if used - declared:
        raise AdapterInvalid(source, f"{where} uses undeclared inputs {sorted(used - declared)}")
    return Tool(name, str(raw.get("description", "")), params, steps)


def parse(text: str, source: str) -> Adapter:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise AdapterInvalid(source, f"not valid TOML: {exc}") from exc
    _unknown("file", data, _TOP_LEVEL, source)
    head = data.get("adapter")
    if not isinstance(head, Mapping):
        raise AdapterInvalid(source, "missing [adapter]")
    _unknown("[adapter]", head, _ADAPTER_KEYS, source)
    match = head.get("match", {})
    url, app = match.get("url"), match.get("app")
    if not (isinstance(url, str) or isinstance(app, str)):
        raise AdapterInvalid(source, "[adapter] match needs url or app")
    concurrency = head.get("concurrency", {})
    limit = concurrency.get("max", 1)
    if not isinstance(limit, int) or limit < 1:
        raise AdapterInvalid(source, "concurrency.max is a positive integer")
    tools = tuple(_tool(t, i, source) for i, t in enumerate(data.get("tool", [])))
    if not tools:
        raise AdapterInvalid(source, "an adapter needs at least one [[tool]]")
    names = [t.name for t in tools]
    if len(set(names)) != len(names):
        raise AdapterInvalid(source, "tool names must be unique")
    return Adapter(
        id=str(head.get("id", source)), version=int(head.get("version", 1)),
        url=url if isinstance(url, str) else None, app=app if isinstance(app, str) else None,
        concurrency_key=str(concurrency.get("key", "origin")), concurrency_max=limit,
        tools=tools, source=source,
    )


def load(path: Path) -> Adapter:
    return parse(path.read_text(encoding="utf-8"), str(path))
