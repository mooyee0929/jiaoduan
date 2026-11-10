"""Offline adapter tools: validate files, and draft an adapter from an action trace."""

from __future__ import annotations

import json
import re
from collections.abc import Sequence
from pathlib import Path
from urllib.parse import urlsplit

from jiaoduan.adapters.schema import AdapterInvalid, load
from jiaoduan.errors import INVALID_ARGS, LayerError
from jiaoduan.trace import SCHEMA

_SLUG = re.compile(r"[^a-z0-9]+")


def check_paths(paths: Sequence[str]) -> str:
    if not paths:
        raise LayerError(INVALID_ARGS, "name one or more adapter files")
    lines: list[str] = []
    failed = False
    for raw in paths:
        try:
            adapter = load(Path(raw))
        except AdapterInvalid as exc:
            failed = True
            lines.append(f"FAIL {exc.message}")
            continue
        except OSError as exc:
            failed = True
            lines.append(f"FAIL {raw}: {exc}")
            continue
        tools = ", ".join(t.name for t in adapter.tools)
        lines.append(f"ok   {raw}: {adapter.id} v{adapter.version} ({tools})")
    if failed:
        raise LayerError(INVALID_ARGS, "\n".join(lines))
    return "\n".join(lines)


def _slug(text: str) -> str:
    return _SLUG.sub("_", text.casefold()).strip("_")[:40] or "value"


def _toml(value: str) -> str:
    return json.dumps(value, ensure_ascii=False)


def draft_from_trace(trace: str, adapter_id: str, tool_name: str) -> str:
    """A starting point for an adapter. Typed values were never recorded, so they become inputs."""
    records = [
        json.loads(line) for line in Path(trace).read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    records = [r for r in records if r.get("v") == SCHEMA and r.get("ok")]
    if not records:
        raise LayerError(INVALID_ARGS, f"{trace} has no successful actions")
    first_url = next((r["url"] for r in records if r.get("url")), "")
    parts = urlsplit(first_url)
    match = f"{parts.scheme}://{parts.netloc}{parts.path}*" if parts.netloc else "*"
    inputs: dict[str, str] = {}
    steps: list[str] = []
    for record in records:
        locator = record.get("locator") or {}
        name, role = str(locator.get("name", "")), str(locator.get("role", ""))
        op, action = record.get("op"), record.get("action")
        if op == "fill" or (op == "act" and action in {"set", "toggle", "select", "upload"}):
            param = _slug(name)
            inputs.setdefault(param, name)
            steps.append(f"[[tool.step]]\nfill = {{ {_toml(name)} = \"{{{param}}}\" }}\n")
        elif op == "act" and action == "press":
            steps.append(
                "[[tool.step]]\n"
                f"press = {{ role = {_toml(role)}, name = {_toml(name)} }}\n"
                "# review: set commit = true if this press submits, pays or deletes\n"
            )
    if not steps:
        raise LayerError(INVALID_ARGS, f"{trace} has no replayable actions")
    head = [
        "[adapter]",
        f"id = {_toml(adapter_id)}",
        "version = 1",
        f"match = {{ url = {_toml(match)} }}",
        "",
        "[[tool]]",
        f"name = {_toml(tool_name)}",
        'description = "Drafted from a trace; review before use"',
    ]
    for param, label in inputs.items():
        head.append(f"input.{param} = {{ type = \"string\", description = {_toml(label)} }}")
    return "\n".join(head) + "\n\n" + "\n".join(steps)
