"""A scriptable Backend, so the Engine is tested without any real UI."""

from __future__ import annotations

from collections.abc import Callable

from agentlayer.model import (
    Action,
    CommitFacts,
    Delivery,
    Handle,
    KeyContext,
    Mark,
    RawNode,
    Role,
    Settle,
    State,
    Surface,
    SurfaceId,
)

SURFACE = SurfaceId("fake:1")


def button(handle: Handle, name: str, **kwargs: object) -> RawNode:
    return RawNode(handle, Role.BUTTON, "button", name, actionable=True, **kwargs)  # type: ignore[arg-type]


def textbox(handle: Handle, name: str, value: str | None = None) -> RawNode:
    return RawNode(handle, Role.TEXTBOX, "textbox", name, value, actionable=True,
                   states=frozenset({State.EDITABLE}))


def text(name: str) -> RawNode:
    return RawNode(None, Role.TEXT, "StaticText", name)


def form(handle: Handle, name: str, *children: RawNode) -> RawNode:
    return RawNode(handle, Role.FORM, "form", name, children=list(children))


class FakeBackend:
    name = "fake"

    def __init__(self, tree: list[RawNode]) -> None:
        self.tree = tree
        self.performed: list[tuple[Handle, Action]] = []
        self.keys: list[str] = []
        self.armed: list[int] = []
        self.on_perform: Callable[[Handle, Action], None] | None = None
        self.facts = CommitFacts()
        self.key_ctx = KeyContext()
        self.settle_result = Settle(True, 10)
        self.delivery = Delivery.DELIVERED
        self.location = "fake://page"
        self.closed: list[SurfaceId] = []
        self._sequence = 0

    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None

    async def surfaces(self) -> list[Surface]:
        return [await self.describe(SURFACE)]

    async def open(self, target: str, *, guard: bool = True) -> Surface:
        self.location = target
        return await self.describe(SURFACE)

    async def describe(self, surface: SurfaceId) -> Surface:
        return Surface(surface, "fake", "Fake", self.location)

    async def read_tree(self, surface: SurfaceId) -> list[RawNode]:
        return self.tree

    async def perform(self, surface: SurfaceId, handle: Handle, action: Action) -> Delivery:
        self.performed.append((handle, action))
        if self.on_perform is not None:
            self.on_perform(handle, action)
        return self.delivery

    async def key(self, surface: SurfaceId, keys: str) -> Delivery:
        self.keys.append(keys)
        return self.delivery

    async def key_context(self, surface: SurfaceId, keys: str) -> KeyContext:
        return self.key_ctx

    async def commit_facts(self, surface: SurfaceId, handle: Handle) -> CommitFacts:
        return self.facts

    async def arm_commit(self, surface: SurfaceId, milliseconds: int) -> None:
        self.armed.append(milliseconds)

    def begin(self, surface: SurfaceId) -> Mark:
        self._sequence += 1
        return Mark(self._sequence, 0.0)

    async def settle(self, surface: SurfaceId, mark: Mark, budget: float) -> Settle:
        return self.settle_result

    async def close_surface(self, surface: SurfaceId) -> None:
        self.closed.append(surface)
