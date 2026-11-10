"""Postconditions: what an action was supposed to achieve, checked without a model."""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from jiaoduan.errors import INVALID_ARGS, LayerError
from jiaoduan.model import Evidence, Node, Ref, RequestRecord, Settle, State

KINDS = frozenset({"value", "checked", "text_visible", "appears", "gone", "url", "request"})


@dataclass(frozen=True)
class ExpectResult:
    kind: str
    ok: bool
    detail: str
    evidence: Evidence


@dataclass(frozen=True)
class Scene:
    """Everything a postcondition can look at."""

    nodes: Sequence[Node]
    url: str
    settle: Settle
    target: Ref | None


def _normal(text: str | None) -> str:
    return " ".join((text or "").split())


def _matches(node: Node, spec: object) -> bool:
    if isinstance(spec, str):
        return spec.casefold() in node.name.casefold()
    if isinstance(spec, Mapping):
        if "role" in spec and node.role.value != spec["role"]:
            return False
        if "name" in spec:
            return str(spec["name"]).casefold() in node.name.casefold()
        return "role" in spec
    raise LayerError(INVALID_ARGS, f"cannot match an element against {spec!r}")


def _node_for(scene: Scene, spec: Mapping[str, object]) -> Node | None:
    wanted = spec.get("ref", scene.target)
    return next((n for n in scene.nodes if wanted and n.ref == wanted), None)


def _check_value(scene: Scene, spec: Mapping[str, object]) -> ExpectResult:
    node = _node_for(scene, spec)
    want = _normal(str(spec["value"]))
    if node is None:
        return ExpectResult("value", False, "target element is gone", Evidence.NONE)
    got = _normal(node.value)
    if got == want:
        return ExpectResult("value", True, f'reads "{got}"', Evidence.TREE)
    return ExpectResult("value", False, f'wanted "{want}", reads "{got}"', Evidence.NONE)


def _check_checked(scene: Scene, spec: Mapping[str, object]) -> ExpectResult:
    node = _node_for(scene, spec)
    want = bool(spec["checked"])
    if node is None:
        return ExpectResult("checked", False, "target element is gone", Evidence.NONE)
    got = bool({State.CHECKED, State.PRESSED, State.SELECTED} & node.states)
    return ExpectResult(
        "checked", got == want, f"reads {'on' if got else 'off'}",
        Evidence.TREE if got == want else Evidence.NONE,
    )


def _check_text(scene: Scene, spec: Mapping[str, object]) -> ExpectResult:
    needle = _normal(str(spec["text_visible"])).casefold()
    found = any(needle in _normal(f"{n.name} {n.value or ''}").casefold() for n in scene.nodes)
    return ExpectResult(
        "text_visible", found, "found" if found else "not on the surface",
        Evidence.TREE if found else Evidence.NONE,
    )


def _check_presence(scene: Scene, spec: Mapping[str, object], kind: str) -> ExpectResult:
    present = any(_matches(n, spec[kind]) for n in scene.nodes)
    ok = present if kind == "appears" else not present
    return ExpectResult(
        kind, ok, "present" if present else "absent", Evidence.TREE if ok else Evidence.NONE
    )


def _check_url(scene: Scene, spec: Mapping[str, object]) -> ExpectResult:
    ok = re.search(str(spec["url"]), scene.url) is not None
    return ExpectResult("url", ok, scene.url[:200], Evidence.TREE if ok else Evidence.NONE)


def _request_matches(record: RequestRecord, spec: Mapping[str, object]) -> bool:
    if "method" in spec and record.method != str(spec["method"]).upper():
        return False
    if "url" in spec and re.search(str(spec["url"]), record.path) is None:
        return False
    if "status" in spec:
        return record.status == spec["status"]
    return record.status is not None and 200 <= record.status < 300


def _check_request(scene: Scene, spec: Mapping[str, object]) -> ExpectResult:
    wanted = spec["request"]
    if not isinstance(wanted, Mapping):
        raise LayerError(INVALID_ARGS, "request expects {method?, url?, status?}")
    hit = next((r for r in scene.settle.requests if _request_matches(r, wanted)), None)
    if hit is None:
        seen = ", ".join(f"{r.method} {r.path} {r.status}" for r in scene.settle.requests[:3])
        return ExpectResult("request", False, f"no match; saw: {seen or 'none'}", Evidence.NONE)
    return ExpectResult(
        "request", True, f"{hit.method} {hit.path} {hit.status}", Evidence.NETWORK
    )


def check(scene: Scene, spec: Mapping[str, object]) -> ExpectResult:
    kinds = KINDS & set(spec)
    if len(kinds) != 1:
        raise LayerError(
            INVALID_ARGS, f"expect needs exactly one of {sorted(KINDS)}, got {sorted(spec)}"
        )
    kind = next(iter(kinds))
    if kind == "value":
        return _check_value(scene, spec)
    if kind == "checked":
        return _check_checked(scene, spec)
    if kind == "text_visible":
        return _check_text(scene, spec)
    if kind == "url":
        return _check_url(scene, spec)
    if kind == "request":
        return _check_request(scene, spec)
    return _check_presence(scene, spec, kind)
