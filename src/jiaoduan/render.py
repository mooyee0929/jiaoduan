"""Plain text for agents: compact on purpose, because tokens are the point.

Page and app text is untrusted, so every label goes through ``quote``: control characters are
removed, it stays on one line, and it stays inside its quotes.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Sequence

from jiaoduan.diff import Effect, NodeChange
from jiaoduan.expect import ExpectResult
from jiaoduan.model import Delivery, Evidence, Node, Settle, State

NAME_CHARS = 80
VALUE_CHARS = 120
EFFECT_LINES = 60


def one_line(text: str, limit: int) -> str:
    cleaned = "".join(
        " " if ch in "\t\r\n" or unicodedata.category(ch) in {"Zl", "Zp"} else ch
        for ch in text
        if ch in "\t\r\n" or unicodedata.category(ch)[0] != "C"
    )
    flat = " ".join(cleaned.split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def quote(text: str, limit: int) -> str:
    flat = one_line(text, limit)
    return '"' + flat.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _states(node: Node) -> str:
    return ", ".join(s.value for s in State if s in node.states)


def node_line(node: Node, indent: int | None = None) -> str:
    depth = node.depth if indent is None else indent
    parts = [f"{'  ' * depth}- {node.role.value}"]
    if node.name:
        parts.append(quote(node.name, NAME_CHARS))
    if node.value is not None:
        parts.append(f"= {quote(node.value, VALUE_CHARS)}")
    if node.ref:
        parts.append(f"[{node.ref}]")
    states = _states(node)
    if states:
        parts.append(f"[{states}]")
    if node.hidden_children:
        parts.append(f"(+{node.hidden_children} more, drill with root={node.ref})")
    return " ".join(parts)


def nodes_text(nodes: Sequence[Node]) -> str:
    return "\n".join(node_line(n) for n in nodes)


def describe_node(node: Node) -> str:
    name = f" {quote(node.name, NAME_CHARS)}" if node.name else ""
    ref = f" [{node.ref}]" if node.ref else ""
    return f"{node.role.value}{name}{ref}"


def _status(delivery: Delivery, evidence: Evidence, settle: Settle) -> str:
    parts = [delivery.value, f"evidence {evidence.label}"]
    if settle.settled:
        parts.append(f"settled {settle.elapsed_ms}ms")
    else:
        parts.append(f"NOT settled after {settle.elapsed_ms}ms")
    if settle.navigated_to:
        parts.append(f"nav {settle.navigated_to[:120]}")
    for record in settle.requests[:3]:
        parts.append(f"net {record.method} {record.path[:80]} {record.status or 'pending'}")
    return " | ".join(parts)


def _change_line(change: NodeChange, indent: int = 0) -> str:
    old, new = change.before, change.after
    head = f"{'  ' * indent}~ {describe_node(new)}"
    deltas: list[str] = []
    if old.name != new.name:
        deltas.append(f"name {quote(old.name, 40)} -> {quote(new.name, 40)}")
    if old.value != new.value:
        before = quote(old.value or "", VALUE_CHARS)
        deltas.append(f"value {before} -> {quote(new.value or '', VALUE_CHARS)}")
    gained = [s.value for s in State if s in new.states - old.states and s is not State.FOCUSED]
    lost = [s.value for s in State if s in old.states - new.states and s is not State.FOCUSED]
    if gained:
        deltas.append("+" + ",".join(gained))
    if lost:
        deltas.append("-" + ",".join(lost))
    return f"{head} {'; '.join(deltas)}"


def effect_lines(effect: Effect) -> list[str]:
    lines: list[str] = []
    if effect.added:
        base = min(n.depth for n in effect.added)
        lines += [
            f"{'  ' * (n.depth - base)}+ {_body(n)}" for n in effect.added
        ]
    lines += [f"- {describe_node(n)}" for n in effect.removed]
    lines += [_change_line(c) for c in effect.changed]
    if effect.rebound:
        lines.append(f"rebound (re-rendered, same element): {', '.join(effect.rebound)}")
    if effect.died:
        lines.append(f"gone: {', '.join(effect.died)}")
    if effect.focus is not None:
        lines.append(f"focus: {describe_node(effect.focus)}")
    if len(lines) > EFFECT_LINES:
        hidden = len(lines) - EFFECT_LINES
        lines = [*lines[:EFFECT_LINES], f"(+{hidden} more changes, call observe)"]
    return lines


def _body(node: Node) -> str:
    return node_line(node, indent=0).removeprefix("- ")


def action_text(
    verb: str,
    target: Node | None,
    delivery: Delivery,
    evidence: Evidence,
    effect: Effect,
    expects: Sequence[ExpectResult],
    warnings: Sequence[str],
    replaced_view: str | None = None,
) -> str:
    subject = f"{verb} {describe_node(target)}" if target is not None else verb
    lines = [f"{subject}: {_status(delivery, evidence, effect.settle)}"]
    for result in expects:
        lines.append(f"expect {result.kind}: {'ok' if result.ok else 'FAILED'} ({result.detail})")
    lines += [f"note: {one_line(w, 200)}" for w in (*warnings, *effect.settle.notes)]
    if effect.page_replaced and replaced_view is not None:
        lines.append("page replaced:")
        lines.append(replaced_view)
    elif effect.empty:
        lines.append("no visible change")
    else:
        lines += effect_lines(effect)
    return "\n".join(lines)
