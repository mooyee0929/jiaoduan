"""What an action changed, scoped to the elements that actually differ."""

from __future__ import annotations

from collections import defaultdict, deque
from collections.abc import Sequence
from dataclasses import dataclass, field

from agentlayer.model import Node, Ref, Role, Settle, State
from agentlayer.refs import SyncReport

# Above this share of the page changing, a line-by-line diff costs more than a fresh look.
_REPLACED_RATIO = 0.4
_REPLACED_MIN_NODES = 20


@dataclass(frozen=True)
class NodeChange:
    before: Node
    after: Node

    @property
    def only_focus(self) -> bool:
        return (
            self.before.value == self.after.value
            and self.before.name == self.after.name
            and self.before.states ^ self.after.states == frozenset({State.FOCUSED})
        )


@dataclass(frozen=True)
class Effect:
    settle: Settle
    added: tuple[Node, ...] = ()
    removed: tuple[Node, ...] = ()
    changed: tuple[NodeChange, ...] = ()
    rebound: tuple[Ref, ...] = ()
    died: tuple[Ref, ...] = ()
    focus: Node | None = None
    page_replaced: bool = False
    after: tuple[Node, ...] = field(default=(), repr=False)

    @property
    def empty(self) -> bool:
        return not (self.added or self.removed or self.changed or self.page_replaced)


def _key(node: Node) -> object:
    return node.ref if node.ref is not None else (node.role, node.name, node.locator.scope)


def diff(
    before: Sequence[Node], after: Sequence[Node], sync: SyncReport, settle: Settle
) -> Effect:
    remaining: dict[object, deque[Node]] = defaultdict(deque)
    for node in before:
        remaining[_key(node)].append(node)

    added: list[Node] = []
    changed: list[NodeChange] = []
    focus: Node | None = None
    for node in after:
        queue = remaining.get(_key(node))
        if not queue:
            added.append(node)
            continue
        old = queue.popleft()
        if old.value != node.value or old.name != node.name or old.states != node.states:
            change = NodeChange(old, node)
            if change.only_focus:
                if State.FOCUSED in node.states:
                    focus = node
            else:
                changed.append(change)
    removed = [n for queue in remaining.values() for n in queue]
    removed.sort(key=before.index)

    biggest = max(len(before), len(after))
    churn = len(added) + len(removed)
    replaced = settle.navigated_to is not None or (
        biggest >= _REPLACED_MIN_NODES and churn > biggest * _REPLACED_RATIO
    )
    return Effect(
        settle=settle,
        added=tuple(added),
        removed=tuple(_visible_removals(removed, added)),
        changed=tuple(changed),
        rebound=sync.rebound,
        died=sync.died,
        focus=focus,
        page_replaced=replaced,
        after=tuple(after),
    )


def _visible_removals(removed: Sequence[Node], added: Sequence[Node]) -> list[Node]:
    """Drop text that only moved: it reads as noise and says nothing about the result."""
    added_text = {n.name for n in added if n.role is Role.TEXT}
    return [n for n in removed if not (n.role is Role.TEXT and n.name in added_text)]
