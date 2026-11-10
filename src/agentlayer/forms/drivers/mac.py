"""Drivers for native macOS controls. The truth source is the control's own AXValue."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence
from typing import TYPE_CHECKING, ClassVar, cast

from agentlayer.forms.drivers.web import RadioDriver
from agentlayer.forms.fill import toggle_word
from agentlayer.forms.schema import (
    DriverContext,
    Field,
    FieldKind,
    Probe,
    WidgetDriver,
    WriteOutcome,
    WriteStatus,
    match_option,
    normal,
    same_value,
)
from agentlayer.model import Action, ActionKind, Evidence, Node, Role

if TYPE_CHECKING:
    from agentlayer.backends.mac.backend import MacBackend

_MENU_WAIT = 2.0


def _mac(ctx: DriverContext) -> MacBackend:
    return cast("MacBackend", ctx.backend)


class MacTextDriver:
    name: ClassVar[str] = "text"
    kind: ClassVar[FieldKind] = FieldKind.TEXT

    def match(self, node: Node, probe: Probe) -> int:
        return 5 if node.role in {Role.TEXTBOX, Role.SEARCHBOX} else 0

    async def options(self, ctx: DriverContext, field: Field) -> tuple[str, ...]:
        return ()

    async def read(self, ctx: DriverContext, field: Field) -> str:
        value = await _mac(ctx).value_of(field.handle)
        return value if isinstance(value, str) else ""

    async def write(self, ctx: DriverContext, field: Field, value: str) -> WriteOutcome:
        if same_value(await self.read(ctx, field), value):
            return WriteOutcome(WriteStatus.UNCHANGED, value, Evidence.TREE)
        await ctx.act(field.handle, Action(ActionKind.SET, text=value), budget=1.0)
        got = await self.read(ctx, field)
        if same_value(got, value):
            return WriteOutcome(WriteStatus.OK, got, Evidence.TREE)
        return WriteOutcome(WriteStatus.MISMATCH, got)


class MacToggleDriver:
    name: ClassVar[str] = "toggle"
    kind: ClassVar[FieldKind] = FieldKind.TOGGLE

    def match(self, node: Node, probe: Probe) -> int:
        return 10 if node.role in {Role.CHECKBOX, Role.SWITCH} else 0

    async def options(self, ctx: DriverContext, field: Field) -> tuple[str, ...]:
        return ()

    async def read(self, ctx: DriverContext, field: Field) -> str:
        return "on" if await _mac(ctx).value_of(field.handle) == 1 else "off"

    async def write(self, ctx: DriverContext, field: Field, value: str) -> WriteOutcome:
        wanted = toggle_word(value)
        if await self.read(ctx, field) == wanted:
            return WriteOutcome(WriteStatus.UNCHANGED, wanted, Evidence.TREE)
        await ctx.act(field.handle, Action(ActionKind.PRESS), budget=1.0)
        got = await self.read(ctx, field)
        status = WriteStatus.OK if got == wanted else WriteStatus.MISMATCH
        return WriteOutcome(status, got, Evidence.TREE if got == wanted else Evidence.NONE)


class MacPopupDriver:
    """A pop-up button: press it, wait for its menu, press the item, read the button back."""

    name: ClassVar[str] = "listbox"
    kind: ClassVar[FieldKind] = FieldKind.LISTBOX

    def match(self, node: Node, probe: Probe) -> int:
        return 10 if node.native_role in {"AXPopUpButton", "AXComboBox"} else 0

    async def options(self, ctx: DriverContext, field: Field) -> tuple[str, ...]:
        return ()

    async def read(self, ctx: DriverContext, field: Field) -> str:
        value = await _mac(ctx).value_of(field.handle)
        return value if isinstance(value, str) else ""

    async def _menu_items(self, ctx: DriverContext) -> list[Node]:
        deadline = asyncio.get_running_loop().time() + _MENU_WAIT
        while True:
            items = [
                n for n in await ctx.nodes()
                if n.role is Role.MENUITEM and n.name and n.handle is not None
                and n.native_role == "AXMenuItem"
            ]
            if items or asyncio.get_running_loop().time() >= deadline:
                return items
            await asyncio.sleep(0.1)

    async def write(self, ctx: DriverContext, field: Field, value: str) -> WriteOutcome:
        current = await self.read(ctx, field)
        if same_value(current, value):
            return WriteOutcome(WriteStatus.UNCHANGED, current, Evidence.TREE)
        await ctx.act(field.handle, Action(ActionKind.PRESS), budget=0.5)
        items = await self._menu_items(ctx)
        chosen, candidates = match_option([n.name for n in items], value)
        item = next((n for n in items if n.name == chosen), None)
        if item is None or item.handle is None:
            await ctx.key("Escape", budget=0.3)
            status = WriteStatus.AMBIGUOUS if candidates else WriteStatus.NOT_FOUND
            return WriteOutcome(status, current, candidates=candidates)
        await ctx.act(item.handle, Action(ActionKind.PRESS), budget=1.0)
        got = await self.read(ctx, field)
        if normal(got).casefold() == normal(item.name).casefold():
            return WriteOutcome(WriteStatus.OK, got, Evidence.TREE)
        return WriteOutcome(WriteStatus.MISMATCH, got, note=f'pressed "{item.name}"')


DRIVERS: tuple[WidgetDriver, ...] = (
    MacTextDriver(), MacToggleDriver(), MacPopupDriver(), RadioDriver(),
)


class MacForms:
    drivers: Sequence[WidgetDriver] = DRIVERS

    def __init__(self, backend: MacBackend) -> None:
        self._backend = backend

    async def probe(self, surface: str, node: Node) -> Probe:
        return Probe(tag=node.native_role)

    async def extra_fields(self, ctx: DriverContext) -> list[Field]:
        return []
