"""Turn a backend's raw tree into the short list of lines worth an agent's tokens."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from enum import StrEnum

from jiaoduan import render
from jiaoduan.errors import INVALID_ARGS, LayerError
from jiaoduan.model import SCOPE_DEPTH, SCOPE_ROLES, Locator, Node, RawNode, Ref, Role, State

DEFAULT_MAX_NODES = 300
SKELETON_DEPTH = 3
SEARCH_LIMIT = 30

_ALWAYS_KEPT = frozenset({Role.WINDOW, Role.DIALOG, Role.FORM, Role.TABLE, Role.DOCUMENT})
# Unnamed rows stay: without them a table flattens into cells with no row boundaries.
_INVISIBLE_UNNAMED = frozenset({Role.OTHER, Role.GROUP, Role.IMAGE, Role.TEXT, Role.LIST,
                                Role.LISTITEM, Role.CELL, Role.FRAME, Role.REGION,
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


_CONTEXT_ROLES = frozenset({Role.ROW, Role.LISTITEM, Role.TREEITEM})
CONTEXT_CHARS = 240


def context(nodes: Sequence[Node], index: int) -> tuple[Node, str] | None:
    """The row or list item a match sits in, with that item's text, so a cell found by search
    comes with the rest of its row instead of needing another round trip."""
    depth = nodes[index].depth
    for back in range(index - 1, -1, -1):
        candidate = nodes[back]
        if candidate.depth >= depth:
            continue
        depth = candidate.depth
        if candidate.role in _CONTEXT_ROLES:
            end = back + 1
            while end < len(nodes) and nodes[end].depth > candidate.depth:
                end += 1
            if candidate.role is Role.ROW:
                cells = _cells(nodes, back, end)
                headers = _header_row(nodes, back)
                if headers and len(headers) == len(cells):
                    pairs = zip(headers, cells, strict=True)
                    labelled = [f"{h}: {c}" if h else c for h, c in pairs]
                    return candidate, " | ".join(labelled)[:CONTEXT_CHARS]
            pieces: list[str] = []
            for node in nodes[back + 1:end]:
                piece = node.value if node.value else node.name
                if piece and piece not in pieces:
                    pieces.append(piece)
            return candidate, " | ".join(pieces)[:CONTEXT_CHARS]
    return None


def _cells(nodes: Sequence[Node], row: int, end: int) -> list[str]:
    depth = nodes[row].depth + 1
    return [
        n.value or n.name for n in nodes[row + 1:end]
        if n.depth == depth and n.role in {Role.CELL, Role.COLUMNHEADER}
    ]


def _header_row(nodes: Sequence[Node], row: int) -> list[str]:
    """Column names from the nearest earlier row in the same table made of header cells."""
    depth = nodes[row].depth
    for back in range(row - 1, -1, -1):
        node = nodes[back]
        if node.depth < depth:
            return []
        if node.depth == depth and node.role is Role.ROW:
            end = back + 1
            while end < len(nodes) and nodes[end].depth > depth:
                end += 1
            children = [n for n in nodes[back + 1:end] if n.depth == depth + 1]
            if children and all(n.role is Role.COLUMNHEADER for n in children):
                return [n.name for n in children]
    return []


def search_text(
    nodes: Sequence[Node], query: str, role: str | None = None, limit: int = SEARCH_LIMIT
) -> tuple[list[str], int]:
    """Rendered matches; one without a ref is followed by the row it belongs to."""
    needle = query.strip().casefold()
    wanted = role.strip().casefold() if role else None
    lines: list[str] = []
    total = 0
    seen_context: set[int] = set()
    for index, node in enumerate(nodes):
        if wanted is not None and node.role.value != wanted:
            continue
        if needle not in f"{node.name} {node.value or ''}".casefold():
            continue
        total += 1
        if total > limit:
            continue
        lines.append(render.node_line(node, 0))
        found = None if node.ref else context(nodes, index)
        if found is not None and id(found[0]) not in seen_context:
            seen_context.add(id(found[0]))
            owner, text = found
            ref = f" [{owner.ref}]" if owner.ref else ""
            lines.append(f"  in {owner.role.value}{ref}: {render.quote(text, CONTEXT_CHARS)}")
    return lines, total


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
