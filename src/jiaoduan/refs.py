"""Refs: short names for live elements that stay valid across observations.

The same element keeps its ref. When a framework replaces the underlying node, a ref moves to
the new node only if exactly one candidate has the same role, name and scope, and that move is
always reported to the agent. Otherwise the ref dies and using it fails: a ref is never matched
to a look-alike.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from jiaoduan.errors import INVALID_ARGS, STALE_REF, LayerError
from jiaoduan.model import Handle, Locator, Node, Ref, Role, SurfaceId

_LOOKALIKE_HINTS = 3


@dataclass
class RefEntry:
    ref: Ref
    surface: SurfaceId
    handle: Handle
    role: Role
    locator: Locator
    alive: bool = True


@dataclass(frozen=True)
class SyncReport:
    rebound: tuple[Ref, ...] = ()
    died: tuple[Ref, ...] = ()


def normalize_ref(text: str) -> Ref:
    """Accept "e12", "ref=e12", "[ref=e12]" and "@e12"; reject anything else."""
    token = text.strip().strip("[]").removeprefix("ref=").removeprefix("@").strip()
    if len(token) < 2 or token[0] != "e" or not token[1:].isdigit():
        raise LayerError(INVALID_ARGS, f"{text!r} is not a ref; expected e<N> from observe")
    return Ref(token)


class RefTable:
    """Refs for one agent. Every full read of a surface goes through ``sync``."""

    def __init__(self, limit: int = 20000) -> None:
        self._limit = limit
        self._next = 1
        self._entries: dict[Ref, RefEntry] = {}
        self._by_handle: dict[tuple[SurfaceId, Handle], Ref] = {}

    def sync(self, surface: SurfaceId, nodes: Sequence[Node]) -> SyncReport:
        """Give every node that wants one a ref, and settle what happened to the others."""
        seen: set[Ref] = set()
        fresh: list[Node] = []
        for node in nodes:
            if not node.wants_ref or node.handle is None:
                continue
            ref = self._by_handle.get((surface, node.handle))
            if ref is None:
                fresh.append(node)
                continue
            entry = self._entries[ref]
            if entry.role != node.role:
                self._kill(entry)
                fresh.append(node)
                continue
            entry.locator = node.locator
            node.ref = ref
            seen.add(ref)

        missing = [
            e for e in self._entries.values()
            if e.surface == surface and e.alive and e.ref not in seen
        ]
        rebound, died = self._rebind(surface, missing, fresh)
        for node in fresh:
            if node.ref is None and node.handle is not None:
                node.ref = self._create(surface, node)
        self._evict()
        return SyncReport(rebound=tuple(rebound), died=tuple(died))

    def lookup(self, text: str) -> RefEntry:
        ref = normalize_ref(text)
        entry = self._entries.get(ref)
        if entry is None:
            raise LayerError(
                STALE_REF, f"no element is recorded for {ref}",
                hint="call observe and use a ref from it",
            )
        if not entry.alive:
            raise LayerError(
                STALE_REF, f"the element behind {ref} no longer exists",
                hint=self._lookalike_hint(entry),
            )
        return entry

    def entries(self, surface: SurfaceId) -> list[RefEntry]:
        return [e for e in self._entries.values() if e.surface == surface and e.alive]

    def forget_surface(self, surface: SurfaceId) -> None:
        for ref in [r for r, e in self._entries.items() if e.surface == surface]:
            self._forget(ref)

    def _rebind(
        self, surface: SurfaceId, missing: Sequence[RefEntry], fresh: Sequence[Node]
    ) -> tuple[list[Ref], list[Ref]]:
        candidates: dict[object, list[Node]] = defaultdict(list)
        for node in fresh:
            candidates[node.locator.key()].append(node)
        lost: dict[object, list[RefEntry]] = defaultdict(list)
        for entry in missing:
            lost[entry.locator.key()].append(entry)

        rebound: list[Ref] = []
        died: list[Ref] = []
        for key, entries in lost.items():
            matches = candidates.get(key, [])
            named = entries[0].locator.name != ""
            if named and len(entries) == 1 and len(matches) == 1:
                self._move(entries[0], matches[0], surface)
                rebound.append(entries[0].ref)
            else:
                for entry in entries:
                    self._kill(entry)
                    died.append(entry.ref)
        return rebound, died

    def _move(self, entry: RefEntry, node: Node, surface: SurfaceId) -> None:
        self._by_handle.pop((surface, entry.handle), None)
        entry.handle = node.handle
        entry.locator = node.locator
        self._by_handle[(surface, node.handle)] = entry.ref
        node.ref = entry.ref

    def _create(self, surface: SurfaceId, node: Node) -> Ref:
        ref = Ref(f"e{self._next}")
        self._next += 1
        self._entries[ref] = RefEntry(ref, surface, node.handle, node.role, node.locator)
        self._by_handle[(surface, node.handle)] = ref
        return ref

    def _kill(self, entry: RefEntry) -> None:
        entry.alive = False
        self._by_handle.pop((entry.surface, entry.handle), None)

    def _forget(self, ref: Ref) -> None:
        entry = self._entries.pop(ref)
        self._by_handle.pop((entry.surface, entry.handle), None)

    def _evict(self) -> None:
        excess = len(self._entries) - self._limit
        if excess <= 0:
            return
        dead_first = sorted(self._entries.values(), key=lambda e: e.alive)
        for entry in dead_first[:excess]:
            self._forget(entry.ref)

    def _lookalike_hint(self, gone: RefEntry) -> str:
        similar = [
            e.ref for e in self.entries(gone.surface)
            if e.role == gone.role and e.locator.name == gone.locator.name
        ][:_LOOKALIKE_HINTS]
        if similar:
            return (
                f"same role and name now at: {', '.join(similar)}; "
                "confirm with observe before acting"
            )
        return "call observe and use a ref from it"
