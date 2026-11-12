"""What a form is: fields, the drivers that know each widget, and how fields are collected."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar, Protocol

from jiaoduan import prune
from jiaoduan.model import (
    Action,
    Delivery,
    Evidence,
    Handle,
    Locator,
    Node,
    Role,
    Settle,
    State,
    SurfaceId,
)

if TYPE_CHECKING:
    from jiaoduan.engine import Engine
    from jiaoduan.model import Backend

_MAX_GROUP = 6
_MAX_GROUP_LABEL = 24
_YES_NO = frozenset({"yes", "no"})
_GROUP_ROLES = frozenset({Role.GROUP, Role.RADIOGROUP, Role.TOOLBAR})
_DEFAULT_SETTLE = 1.5


class FieldKind(StrEnum):
    TEXT = "text"
    SELECT = "select"
    COMBOBOX = "combobox"
    LISTBOX = "listbox"
    TOGGLE = "toggle"
    RADIO = "radio"
    BUTTONS = "buttons"
    FILE = "file"


@dataclass(frozen=True)
class Probe:
    """Facts about one control that the accessibility tree does not carry."""

    tag: str = ""
    input_type: str = ""
    popup: str = ""
    autocomplete: str = ""
    name_attr: str = ""
    pressable_state: bool = False
    # A label from elements outside the control; a popup button's own name is usually its value.
    label: str = ""


@dataclass(frozen=True)
class Member:
    label: str
    handle: Handle
    checked: bool


@dataclass
class Field:
    label: str
    kind: FieldKind
    driver: WidgetDriver
    handle: Handle
    locator: Locator
    required: bool = False
    disabled: bool = False
    section: str = ""
    # The label of the group the field's own elements sit in, such as an upload's slot.
    group: str = ""
    members: tuple[Member, ...] = ()
    id: str = ""
    value: str = ""
    options: tuple[str, ...] = ()


class WriteStatus(StrEnum):
    OK = "ok"
    UNCHANGED = "unchanged"
    MISMATCH = "mismatch"
    AMBIGUOUS = "ambiguous"
    NOT_FOUND = "not_found"
    ERROR = "error"


@dataclass(frozen=True)
class WriteOutcome:
    status: WriteStatus
    got: str = ""
    evidence: Evidence = Evidence.NONE
    candidates: tuple[str, ...] = ()
    note: str = ""


class WidgetDriver(Protocol):
    name: ClassVar[str]
    kind: ClassVar[FieldKind]

    def match(self, node: Node, probe: Probe) -> int:
        """How well this driver fits; 0 means not at all."""
        ...

    async def read(self, ctx: DriverContext, field: Field) -> str: ...

    async def options(self, ctx: DriverContext, field: Field) -> tuple[str, ...]: ...

    async def write(self, ctx: DriverContext, field: Field, value: str) -> WriteOutcome: ...


class FormSupport(Protocol):
    drivers: Sequence[WidgetDriver]

    async def probe(self, surface: SurfaceId, node: Node) -> Probe: ...

    async def extra_fields(self, ctx: DriverContext) -> list[Field]:
        """Fields with no accessible element, such as a hidden file input."""
        ...


@dataclass
class DriverContext:
    engine: Engine
    agent: str
    surface: SurfaceId
    support: FormSupport

    @property
    def backend(self) -> Backend:
        return self.engine.backend_for(self.surface)

    async def nodes(self) -> list[Node]:
        nodes, _ = await self.engine.read_surface(self.agent, self.surface)
        return nodes

    async def act(
        self, handle: Handle, action: Action, budget: float = _DEFAULT_SETTLE
    ) -> tuple[Delivery, Settle]:
        backend = self.backend
        mark = backend.begin(self.surface)
        delivery = await backend.perform(self.surface, handle, action)
        return delivery, await backend.settle(self.surface, mark, budget)

    async def key(self, keys: str, budget: float = _DEFAULT_SETTLE) -> None:
        backend = self.backend
        mark = backend.begin(self.surface)
        await backend.key(self.surface, keys)
        await backend.settle(self.surface, mark, budget)


def normal(text: str | None) -> str:
    return " ".join((text or "").split())


def _alnum(text: str) -> str:
    return "".join(c for c in text if c.isalnum())


def same_value(got: str, wanted: str) -> bool:
    """Equal, or equal once a field's own formatting such as a phone mask is ignored."""
    left, right = normal(got).casefold(), normal(wanted).casefold()
    if left == right:
        return True
    return bool(_alnum(left)) and _alnum(left) == _alnum(right)


def match_option(
    options: Sequence[str], wanted: str, limit: int = 8
) -> tuple[str | None, tuple[str, ...]]:
    """The option to pick, or the candidates a caller must choose between.

    An exact match always wins, which is what stops "No" from selecting "Not sure".
    """
    target = normal(wanted).casefold()
    cleaned = [(option, normal(option).casefold()) for option in options]
    exact = [option for option, text in cleaned if text == target]
    if exact:
        return exact[0], ()
    starts = [option for option, text in cleaned if text.startswith(target)]
    if len(starts) == 1:
        return starts[0], ()
    contains = [option for option, text in cleaned if target in text]
    if len(contains) == 1 and len(target) >= 3:
        return contains[0], ()
    candidates = starts or contains
    return None, tuple(candidates[:limit])


def _parents(nodes: Sequence[Node]) -> list[Node | None]:
    parents: list[Node | None] = []
    path: list[Node] = []
    for node in nodes:
        while path and path[-1].depth >= node.depth:
            path.pop()
        parents.append(path[-1] if path else None)
        path.append(node)
    return parents


def _preceding_text(nodes: Sequence[Node], index: int) -> str:
    for node in reversed(nodes[max(0, index - 4):index]):
        if node.role in {Role.TEXT, Role.HEADING} and node.name:
            return node.name.rstrip("*: ")
    return ""


def _pick_driver(support: FormSupport, node: Node, probe: Probe) -> WidgetDriver | None:
    best: WidgetDriver | None = None
    score = 0
    for driver in support.drivers:
        fit = driver.match(node, probe)
        if fit > score:
            best, score = driver, fit
    return best


def _driver_of_kind(support: FormSupport, kind: FieldKind) -> WidgetDriver | None:
    return next((d for d in support.drivers if d.kind is kind), None)


@dataclass
class _Collector:
    ctx: DriverContext
    nodes: Sequence[Node]
    parents: list[Node | None]
    consumed: set[int] = field(default_factory=set)
    fields: list[Field] = field(default_factory=list)
    heading: str = ""

    async def run(self) -> list[Field]:
        for index, node in enumerate(self.nodes):
            if node.role is Role.HEADING:
                self.heading = node.name
            if index in self.consumed or not node.actionable or node.handle is None:
                continue
            await self._visit(index, node)
        return self.fields

    async def _visit(self, index: int, node: Node) -> None:
        if node.role is Role.RADIO:
            self._group(index, node, FieldKind.RADIO, Role.RADIO)
            return
        if node.role is Role.BUTTON:
            if await self._is_button_group(index, node):
                self._group(index, node, FieldKind.BUTTONS, Role.BUTTON)
                return
            if State.HASPOPUP not in node.states:
                return
        elif node.role not in {
            Role.TEXTBOX, Role.SEARCHBOX, Role.COMBOBOX, Role.CHECKBOX, Role.SWITCH,
        }:
            return
        probe = await self.ctx.support.probe(self.ctx.surface, node)
        driver = _pick_driver(self.ctx.support, node, probe)
        if driver is None or node.handle is None:
            return
        locator = node.locator
        if node.role is Role.BUTTON:
            # A popup button is named by what it shows, which changes once a choice is made;
            # the field is known by its outside label so it keeps its id across that change.
            label = probe.label or _preceding_text(self.nodes, index) or node.name
            locator = Locator(node.role, label, node.locator.scope)
        else:
            label = node.name or _preceding_text(self.nodes, index)
        self.fields.append(Field(
            label=label.rstrip("*: "),
            kind=driver.kind,
            driver=driver,
            handle=node.handle,
            locator=locator,
            required=State.REQUIRED in node.states,
            disabled=State.DISABLED in node.states,
            section=self.heading,
        ))

    def _siblings(self, index: int, role: Role) -> list[int]:
        parent = self.parents[index]
        depth = self.nodes[index].depth
        found = [index]
        cursor = index + 1
        while cursor < len(self.nodes) and len(found) < _MAX_GROUP:
            node = self.nodes[cursor]
            if node.depth < depth:
                break
            if node.depth == depth and self.parents[cursor] is parent:
                if node.role is role:
                    found.append(cursor)
                elif node.role is not Role.TEXT:
                    break
            cursor += 1
        return found

    async def _is_button_group(self, index: int, node: Node) -> bool:
        members = self._siblings(index, Role.BUTTON)
        if len(members) < 2:
            return False
        names = [self.nodes[i].name.strip().casefold() for i in members]
        if any(len(name) > _MAX_GROUP_LABEL for name in names):
            return False
        if set(names) <= _YES_NO:
            return True
        probe = await self.ctx.support.probe(self.ctx.surface, node)
        return probe.pressable_state

    def _group(self, index: int, node: Node, kind: FieldKind, role: Role) -> None:
        indices = self._siblings(index, role)
        driver = _driver_of_kind(self.ctx.support, kind)
        if driver is None or node.handle is None:
            return
        self.consumed.update(indices)
        parent = self.parents[index]
        # Only a group names its members; a form or region around them is too far away.
        grouped = parent is not None and parent.role in _GROUP_ROLES and parent.name
        label = (parent.name if grouped and parent else "") or _preceding_text(self.nodes, index)
        members = tuple(
            Member(
                self.nodes[i].name, self.nodes[i].handle,
                bool({State.CHECKED, State.PRESSED} & self.nodes[i].states),
            )
            for i in indices
        )
        self.fields.append(Field(
            label=label.rstrip("*: "),
            kind=kind,
            driver=driver,
            handle=node.handle,
            locator=Locator(Role.GROUP, label, node.locator.scope),
            required=any(State.REQUIRED in self.nodes[i].states for i in indices),
            section=self.heading,
            members=members,
        ))


async def collect(
    ctx: DriverContext, root: str | None, *, with_values: bool = True
) -> list[Field]:
    """Every field on the surface, or under ``root``, in document order."""
    nodes = await ctx.nodes()
    scoped: Sequence[Node] = nodes
    if root is not None:
        scoped = prune.subtree(nodes, ctx.engine.state(ctx.agent).refs.lookup(root).ref)
    fields = await _Collector(ctx, scoped, _parents(scoped)).run()
    fields += await ctx.support.extra_fields(ctx)
    ctx.engine.form_ids(ctx.agent, ctx.surface).assign(fields)
    if with_values:
        for item in fields:
            item.value = await item.driver.read(ctx, item)
            item.options = await item.driver.options(ctx, item)
    return fields
