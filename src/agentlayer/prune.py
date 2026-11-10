"""Turn a backend's raw tree into the short list of lines worth an agent's tokens."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from enum import StrEnum

from agentlayer.errors import INVALID_ARGS, LayerError
from agentlayer.model import SCOPE_DEPTH, SCOPE_ROLES, Locator, Node, RawNode, Ref, Role, State

DEFAULT_MAX_NODES = 300
SKELETON_DEPTH = 3
SEARCH_LIMIT = 30

_ALWAYS_KEPT = frozenset({Role.WINDOW, Role.DIALOG, Role.FORM, Role.TABLE, Role.DOCUMENT})
_INVISIBLE_UNNAMED = frozenset({Role.OTHER, Role.GROUP, Role.IMAGE, Role.TEXT, Role.LIST,
                                Role.LISTITEM, Role.ROW, Role.CELL, Role.FRAME, Role.REGION,
                                Role.MAIN, Role.NAVIGATION})
_SKELETON_ALWAYS = frozenset({Role.HEADING, Role.DIALOG, Role.ALERT, Role.FORM})


class Mode(StrEnum):
    AUTO = "auto"
    FULL = "full"
    INTERACTIVE = "interactive"
    SKELETON = "skeleton"


def _keep(raw: RawNode, parent_name: str) -> bool:
    if raw.actionable or raw.role in _ALWAYS_KEPT:
        return True
    if raw.role in _INVISIBLE_UNNAMED and not raw.name and raw.value is None:
        return False
    # Link and button text is already their name.
    return not (raw.role is Role.TEXT and raw.name and raw.name in parent_name)


def _no_children_shown(raw: RawNode) -> bool:
    """A field's inner editor repeats its value; a closed dropdown's options are not on screen."""
    if raw.role in {Role.TEXTBOX, Role.SEARCHBOX}:
        return True
    return raw.role is Role.COMBOBOX and State.COLLAPSED in raw.states


def _label_key(text: str) -> str:
    return text.strip().rstrip("*:： ").casefold()


def _without_repeated_labels(nodes: list[Node]) -> list[Node]:
    """Drop a text line that only repeats the name of the control next to it."""
    kept: list[Node] = []
    for index, node in enumerate(nodes):
        if node.role is Role.TEXT:
            key = _label_key(node.name)
            neighbours = [nodes[i] for i in (index - 1, index + 1) if 0 <= i < len(nodes)]
            if key and any(n.actionable and _label_key(n.name) == key for n in neighbours):
                continue
        kept.append(node)
    return kept


def flatten(roots: Sequence[RawNode]) -> list[Node]:
    """Depth-first, in document order, with unlabeled inert wrappers collapsed away."""
    out: list[Node] = []
    Frame = tuple[RawNode, int, tuple[tuple[Role, str], ...], str]
    stack: list[Frame] = [(root, 0, (), "") for root in reversed(roots)]
    while stack:
        raw, depth, scope, parent_name = stack.pop()
        child_depth = depth
        child_scope = scope
        child_parent = parent_name
        if _keep(raw, parent_name):
            out.append(Node(
                handle=raw.handle,
                role=raw.role,
                native_role=raw.native_role,
                name=raw.name,
                value=raw.value,
                states=raw.states,
                depth=depth,
                actionable=raw.actionable,
                container=False,
                locator=Locator(raw.role, raw.name, scope),
            ))
            child_depth = depth + 1
            child_parent = raw.name
            if raw.role in SCOPE_ROLES and raw.name:
                child_scope = (*scope, (raw.role, raw.name))[-SCOPE_DEPTH:]
        if _no_children_shown(raw):
            continue
        for child in reversed(raw.children):
            stack.append((child, child_depth, child_scope, child_parent))
    kept = _without_repeated_labels(out)
    for index, node in enumerate(kept[:-1]):
        node.container = kept[index + 1].depth > node.depth
    return kept


def subtree(nodes: Sequence[Node], ref: Ref) -> list[Node]:
    """``ref`` and everything under it, re-based to depth zero."""
    for index, node in enumerate(nodes):
        if node.ref == ref:
            end = index + 1
            while end < len(nodes) and nodes[end].depth > node.depth:
                end += 1
            return [replace(n, depth=n.depth - node.depth) for n in nodes[index:end]]
    raise LayerError(INVALID_ARGS, f"{ref} is not on this surface", hint="call observe")


def _interactive(nodes: Sequence[Node]) -> list[Node]:
    """Controls plus the named containers and headings that give them context."""
    return [
        n for n in nodes
        if n.actionable or n.role is Role.HEADING or (n.container and n.name)
    ]


def _skeleton(nodes: Sequence[Node]) -> list[Node]:
    shown: list[Node] = []
    index = 0
    while index < len(nodes):
        node = nodes[index]
        if node.depth < SKELETON_DEPTH or node.role in _SKELETON_ALWAYS:
            end = index + 1
            while end < len(nodes) and nodes[end].depth > node.depth:
                end += 1
            hidden = end - index - 1
            if node.depth == SKELETON_DEPTH - 1 and hidden:
                shown.append(replace(node, hidden_children=hidden))
                index = end
                continue
            shown.append(node)
        index += 1
    return shown


def select(
    nodes: Sequence[Node], mode: Mode, max_nodes: int = DEFAULT_MAX_NODES
) -> tuple[list[Node], Mode, bool]:
    """The lines to show, the mode actually used, and whether the budget cut anything."""
    effective = mode
    if mode is Mode.AUTO:
        effective = Mode.FULL if len(nodes) <= max_nodes else Mode.SKELETON
    if effective is Mode.INTERACTIVE:
        chosen = _interactive(nodes)
    elif effective is Mode.SKELETON:
        chosen = _skeleton(nodes)
    else:
        chosen = list(nodes)
    return chosen[:max_nodes], effective, len(chosen) > max_nodes


def search(
    nodes: Sequence[Node], query: str, role: str | None = None, limit: int = SEARCH_LIMIT
) -> tuple[list[Node], int]:
    """Matches on name or value, flat, and how many matched in total."""
    needle = query.strip().casefold()
    wanted = role.strip().casefold() if role else None
    hits = [
        replace(n, depth=0) for n in nodes
        if (wanted is None or n.role.value == wanted)
        and needle in f"{n.name} {n.value or ''}".casefold()
    ]
    return hits[:limit], len(hits)


def text_of(nodes: Sequence[Node], limit: int = 8000) -> str:
    """All readable text in document order."""
    pieces: list[str] = []
    size = 0
    for node in nodes:
        piece = node.value if node.value and node.role is not Role.TEXT else node.name
        if not piece or (pieces and pieces[-1] == piece):
            continue
        pieces.append(piece)
        size += len(piece) + 1
        if size > limit:
            pieces.append("[truncated]")
            break
    return "\n".join(pieces)
