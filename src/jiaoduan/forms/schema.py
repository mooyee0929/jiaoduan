"""What a form is: fields, the drivers that know each widget, and how fields are collected."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import TYPE_CHECKING, ClassVar, Protocol

from jiaoduan import prune
from jiaoduan.errors import STALE_REF, LayerError
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
_MAX_CHOICES = 40
_MAX_GROUP_LABEL = 24
_YES_NO = frozenset({"yes", "no"})
_GROUP_ROLES = frozenset({Role.GROUP, Role.RADIOGROUP, Role.TOOLBAR})
_DEFAULT_SETTLE = 1.5


class FieldKind(StrEnum):
    TEXT = "text"
    NUMBER = "number"
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
    # For a radio or choice button: which question it answers, and that question's text.
    group: str = ""
    question: str = ""


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
    input_type: str = ""
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


_MIN_PHONE_DIGITS = 7
_MAX_COUNTRY_CODE_DIGITS = 3


def same_phone(got: str, wanted: str) -> bool:
    """The same number once a page's formatting, or a country code it adds, is ignored.

    "+1 (626) 423-4933" is "6264234933" reformatted; "62642349336264234933" is a doubled
    autofill and is not.
    """
    if any(c.isalpha() for c in got + wanted):
        return False
    have = "".join(c for c in got if c.isdigit())
    want = "".join(c for c in wanted if c.isdigit())
    if min(len(have), len(want)) < _MIN_PHONE_DIGITS:
        return False
    if have == want:
        return True
    (short, _), (long, long_text) = sorted(((have, got), (want, wanted)), key=lambda p: len(p[0]))
    extra = len(long) - len(short)
    return (
        extra <= _MAX_COUNTRY_CODE_DIGITS and long.endswith(short)
        and long_text.lstrip().startswith("+")
    )


def same_text(got: str, wanted: str, input_type: str = "") -> bool:
    """``same_value``, also accepting a phone field's own reformatting of the same digits."""
    return same_value(got, wanted) or input_type == "tel" and same_phone(got, wanted)


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
    probes: dict[int, Probe] = field(default_factory=dict)

    async def run(self) -> list[Field]:
        for index, node in enumerate(self.nodes):
            if node.role is Role.HEADING:
                self.heading = node.name
            if index in self.consumed or not node.actionable or node.handle is None:
                continue
            await self._visit(index, node)
        return self.fields

    async def _probe(self, index: int) -> Probe:
        if index not in self.probes:
            self.probes[index] = await self.ctx.support.probe(self.ctx.surface, self.nodes[index])
        return self.probes[index]

    async def _visit(self, index: int, node: Node) -> None:
        if node.role is Role.RADIO:
            await self._group(index, node, FieldKind.RADIO, await self._run(index, Role.RADIO))
            return
        if node.role is Role.BUTTON:
            members = await self._button_group(index)
            if members:
                await self._group(index, node, FieldKind.BUTTONS, members)
                return
            if State.HASPOPUP not in node.states:
                return
        elif node.role not in {
            Role.TEXTBOX, Role.SEARCHBOX, Role.SPINBUTTON, Role.COMBOBOX, Role.CHECKBOX,
            Role.SWITCH,
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
            input_type=probe.input_type.lower(),
        ))

    def _siblings(self, index: int, role: Role, limit: int) -> list[int]:
        parent = self.parents[index]
        depth = self.nodes[index].depth
        found = [index]
        cursor = index + 1
        while cursor < len(self.nodes) and len(found) < limit:
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

    async def _run(self, index: int, role: Role) -> list[int]:
        """The options of one question: siblings in the tree that answer the same question.

        Neighbouring questions in unnamed wrappers look like one run of siblings in the tree,
        with only their question text between them; the page says where each one ends.
        """
        first = await self._probe(index)
        limit = _MAX_CHOICES if first.group and role is Role.RADIO else _MAX_GROUP
        siblings = self._siblings(index, role, limit)
        if not first.group:
            return siblings
        run = [index]
        for cursor in siblings[1:]:
            if (await self._probe(cursor)).group != first.group:
                break
            run.append(cursor)
        return run

    async def _button_group(self, index: int) -> list[int]:
        members = await self._run(index, Role.BUTTON)
        if len(members) < 2:
            return []
        names = [self.nodes[i].name.strip().casefold() for i in members]
        if any(len(name) > _MAX_GROUP_LABEL for name in names):
            return []
        if set(names) <= _YES_NO or (await self._probe(index)).pressable_state:
            return members
        return []

    async def _group(self, index: int, node: Node, kind: FieldKind, indices: list[int]) -> None:
        driver = _driver_of_kind(self.ctx.support, kind)
        if driver is None or node.handle is None:
            return
        self.consumed.update(indices)
        parent = self.parents[index]
        question = (await self._probe(index)).question
        # Only a group names its members; a form or region around them is too far away.
        grouped = parent is not None and parent.role in _GROUP_ROLES and parent.name
        label = (
            question or (parent.name if grouped and parent else "")
            or _preceding_text(self.nodes, index)
        )
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
            group=question,
            members=members,
        ))


async def collect(
    ctx: DriverContext, root: str | None, *, with_values: bool = True
) -> list[Field]:
    """Every field on the surface, or under ``root``, in document order.

    An upload or a conditional question can re-render the form while it is being read; the
    read then starts over once from a fresh tree instead of failing the whole fill.
    """
    try:
        return await _collect_once(ctx, root, with_values=with_values)
    except LayerError as exc:
        if exc.code != STALE_REF:
            raise
        return await _collect_once(ctx, root, with_values=with_values)


async def _collect_once(
    ctx: DriverContext, root: str | None, *, with_values: bool
) -> list[Field]:
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
