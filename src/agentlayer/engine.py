"""The operations agents call. Works on any Backend; returns text ready to show a model."""

from __future__ import annotations

import asyncio
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from agentlayer import prune, render
from agentlayer.claims import Claims
from agentlayer.diff import Effect, diff
from agentlayer.errors import (
    BACKEND_UNAVAILABLE,
    INVALID_ARGS,
    NO_ACCESSIBLE_TREE,
    NO_SURFACE,
    NOT_ACTIONABLE,
    LayerError,
)
from agentlayer.expect import ExpectResult, Scene, check
from agentlayer.gate import Gate, Policy, commit_reason
from agentlayer.model import (
    Action,
    ActionKind,
    Backend,
    Delivery,
    Evidence,
    Node,
    Ref,
    Settle,
    State,
    Surface,
    SurfaceId,
)
from agentlayer.refs import RefEntry, RefTable, SyncReport
from agentlayer.trace import Trace, TraceRecord

DEFAULT_SETTLE = 2.0
ARM_MS = 2500
WAIT_POLL = 0.2
MAX_WAIT = 60.0
_URL_LIKE = re.compile(r"^[a-z][a-z0-9+.-]*:|^[\w-]+(\.[\w-]+)+(/|$)", re.IGNORECASE)
_ACTIONS = {kind.value: kind for kind in ActionKind}


@dataclass
class AgentState:
    refs: RefTable = field(default_factory=RefTable)
    views: dict[SurfaceId, list[Node]] = field(default_factory=dict)
    current: SurfaceId | None = None


class Engine:
    def __init__(
        self,
        backends: Mapping[str, Backend],
        *,
        gate: Gate | None = None,
        trace: Trace | None = None,
        claims: Claims | None = None,
    ) -> None:
        self._backends = dict(backends)
        self.gate = gate or Gate()
        self._trace = trace or Trace(None)
        self.claims = claims or Claims()
        self._agents: dict[str, AgentState] = {}
        self._locks: dict[SurfaceId, asyncio.Lock] = {}
        self._started: set[str] = set()
        self._start_lock = asyncio.Lock()

    async def _ensure_started(self, key: str) -> Backend:
        """Backends start on first use, so an unused browser is never launched."""
        backend = self._backends.get(key)
        if backend is None:
            raise LayerError(
                BACKEND_UNAVAILABLE, f"no {key} backend here",
                hint="the macOS backend needs the `mac` extra on macOS",
            )
        async with self._start_lock:
            if key not in self._started:
                await backend.start()
                self._started.add(key)
        return backend

    async def stop(self) -> None:
        for key in tuple(self._started):
            await self._backends[key].stop()
        self._started.clear()

    def backend_for(self, surface: SurfaceId) -> Backend:
        kind = surface.split(":", 1)[0]
        backend = self._backends.get(kind)
        if backend is None:
            raise LayerError(BACKEND_UNAVAILABLE, f"no {kind} backend is running")
        return backend

    def state(self, agent: str) -> AgentState:
        return self._agents.setdefault(agent, AgentState())

    def _lock(self, surface: SurfaceId) -> asyncio.Lock:
        return self._locks.setdefault(surface, asyncio.Lock())

    def _surface(self, agent: str, surface: str | None) -> SurfaceId:
        if surface:
            chosen = SurfaceId(surface)
        else:
            state = self.state(agent)
            if state.current is None:
                raise LayerError(
                    NO_SURFACE, "no surface yet", hint="call open(target) or surfaces()"
                )
            chosen = state.current
        self.claims.acquire(agent, chosen)
        self.state(agent).current = chosen
        return chosen

    async def read_surface(
        self, agent: str, surface: SurfaceId
    ) -> tuple[list[Node], SyncReport]:
        """A full, fresh reading. Every ref decision is based on one of these."""
        backend = self.backend_for(surface)
        nodes = prune.flatten(await backend.read_tree(surface))
        state = self.state(agent)
        report = state.refs.sync(surface, nodes)
        state.views[surface] = nodes
        return nodes, report

    # Reads

    async def list_surfaces(self, agent: str) -> str:
        lines: list[str] = []
        for key in tuple(self._started):
            backend = self._backends[key]
            for surface in await backend.surfaces():
                owner = self.claims.owner(surface.id)
                tag = "" if owner is None else (" (yours)" if owner == agent else f" ({owner})")
                title = render.quote(surface.title, 60)
                lines.append(f"{surface.id}{tag} {title} {surface.location[:120]}")
        return "\n".join(lines) or "no surfaces open"

    async def open(self, agent: str, target: str, kind: str | None = None) -> str:
        backend = await self._ensure_started(kind or ("web" if _URL_LIKE.match(target) else "mac"))
        surface = await backend.open(target)
        self.claims.acquire(agent, surface.id)
        self.state(agent).current = surface.id
        return await self.observe(agent, surface.id)

    async def observe(
        self,
        agent: str,
        surface: str | None = None,
        root: str | None = None,
        mode: str = "auto",
        query: str | None = None,
        role: str | None = None,
        max_nodes: int = prune.DEFAULT_MAX_NODES,
    ) -> str:
        chosen = self._surface(agent, surface)
        async with self._lock(chosen):
            nodes, _ = await self.read_surface(agent, chosen)
            info = await self.backend_for(chosen).describe(chosen)
        if not nodes:
            raise LayerError(
                NO_ACCESSIBLE_TREE, f"{chosen} exposes no accessible elements",
                hint="canvas-drawn or empty UI cannot be driven through this layer",
            )
        header = f'{chosen} {render.quote(info.title, 80)} {info.location[:160]}'
        if query:
            hits, total = prune.search(nodes, query, role)
            shown = f"{len(hits)} of {total} matches" if total > len(hits) else f"{total} matches"
            return "\n".join(filter(None, [f"{header} | {shown}", render.nodes_text(hits)]))
        scoped = prune.subtree(nodes, self.state(agent).refs.lookup(root).ref) if root else nodes
        try:
            wanted = prune.Mode(mode)
        except ValueError as exc:
            raise LayerError(INVALID_ARGS, f"unknown mode {mode!r}") from exc
        shown_nodes, used, cut = prune.select(scoped, wanted, max_nodes)
        refs = sum(1 for n in scoped if n.ref)
        summary = f"{header} | {len(scoped)} elements, {refs} refs, {used.value}"
        if cut:
            summary += f" | INCOMPLETE: first {max_nodes}, narrow with root= or query="
        return f"{summary}\n{render.nodes_text(shown_nodes)}"

    async def read(
        self, agent: str, ref: str | None = None, surface: str | None = None,
        max_chars: int = 8000,
    ) -> str:
        if ref is not None:
            chosen = self.state(agent).refs.lookup(ref).surface
            self.claims.acquire(agent, chosen)
        else:
            chosen = self._surface(agent, surface)
        async with self._lock(chosen):
            nodes, _ = await self.read_surface(agent, chosen)
        if ref is not None:
            nodes = prune.subtree(nodes, self.state(agent).refs.lookup(ref).ref)
        return prune.text_of(nodes, max_chars)

    async def wait(
        self,
        agent: str,
        expect: Mapping[str, object],
        surface: str | None = None,
        timeout: float = 10.0,
    ) -> str:
        chosen = self._surface(agent, surface)
        backend = self.backend_for(chosen)
        limit = min(max(timeout, 0.0), MAX_WAIT)
        started = time.monotonic()
        settle = Settle(True, 0)
        while True:
            async with self._lock(chosen):
                nodes, _ = await self.read_surface(agent, chosen)
                info = await backend.describe(chosen)
            result = check(Scene(nodes, info.location, settle, None), expect)
            elapsed = time.monotonic() - started
            if result.ok or elapsed >= limit:
                outcome = "met" if result.ok else "NOT met"
                ms = int(elapsed * 1000)
                return f"wait {result.kind}: {outcome} after {ms}ms ({result.detail})"
            await asyncio.sleep(WAIT_POLL)

    # Actions

    async def act(
        self,
        agent: str,
        ref: str,
        action: str,
        value: str | None = None,
        expect: Sequence[Mapping[str, object]] | Mapping[str, object] | None = None,
        gate: str | None = None,
        settle_budget: float = DEFAULT_SETTLE,
    ) -> str:
        state = self.state(agent)
        entry = state.refs.lookup(ref)
        surface = entry.surface
        self.claims.acquire(agent, surface)
        state.current = surface
        kind = self._action_kind(action)
        started = time.monotonic()
        try:
            async with self._lock(surface):
                text, delivery, evidence, url = await self._act_locked(
                    agent, entry.ref, kind, value, expect, gate, settle_budget
                )
        except LayerError as exc:
            self._trace.write(TraceRecord(
                agent=agent, surface=surface, op="act", ok=False, action=kind.value,
                locator=entry.locator, ms=int((time.monotonic() - started) * 1000),
                delivery=exc.delivery.value, code=exc.code,
                text_len=len(value) if value is not None else None,
            ))
            raise
        self._trace.write(TraceRecord(
            agent=agent, surface=surface, op="act", ok=True, action=kind.value,
            locator=entry.locator, ms=int((time.monotonic() - started) * 1000),
            delivery=delivery.value, evidence=evidence.label, url=url,
            text_len=len(value) if value is not None else None,
        ))
        return text

    async def _act_locked(
        self,
        agent: str,
        ref: Ref,
        kind: ActionKind,
        value: str | None,
        expect: Sequence[Mapping[str, object]] | Mapping[str, object] | None,
        gate_token: str | None,
        settle_budget: float,
    ) -> tuple[str, Delivery, Evidence, str]:
        state = self.state(agent)
        before, first = await self.read_surface(agent, state.refs.lookup(ref).surface)
        entry = state.refs.lookup(ref)
        surface = entry.surface
        backend = self.backend_for(surface)
        target = next((n for n in before if n.ref == ref), None)
        if target is None:
            raise LayerError(NOT_ACTIONABLE, f"{ref} is not on the surface any more")
        warnings: list[str] = []
        if ref in first.rebound:
            warnings.append(f"{ref} was re-rendered; acted on the same role, name and scope")

        if kind is ActionKind.TOGGLE and self._already(target, value):
            return await self._noop(agent, surface, target, before, kind, expect)

        await self._check_commit(agent, surface, entry, target, kind, gate_token)
        mark = backend.begin(surface)
        delivery = await backend.perform(surface, entry.handle, self._action(kind, value))
        settle = await backend.settle(surface, mark, settle_budget)
        after, sync = await self.read_surface(agent, surface)
        info = await backend.describe(surface)
        effect = diff(before, after, sync, settle)
        expects = self._expects(after, info, settle, ref, expect)
        evidence, more = self._evidence(kind, value, ref, after, expects)
        warnings += more
        view = self._replaced_view(after) if effect.page_replaced else None
        text = render.action_text(
            kind.value, target, delivery, evidence, effect, expects, warnings, view
        )
        return text, delivery, evidence, info.location

    async def _noop(
        self,
        agent: str,
        surface: SurfaceId,
        target: Node,
        before: list[Node],
        kind: ActionKind,
        expect: Sequence[Mapping[str, object]] | Mapping[str, object] | None,
    ) -> tuple[str, Delivery, Evidence, str]:
        backend = self.backend_for(surface)
        info = await backend.describe(surface)
        settle = Settle(True, 0)
        effect = Effect(settle=settle, after=tuple(before))
        expects = self._expects(before, info, settle, target.ref, expect)
        text = render.action_text(
            kind.value, target, Delivery.NOT_DELIVERED, Evidence.TREE, effect, expects,
            ["already in that state; nothing was sent"],
        )
        return text, Delivery.NOT_DELIVERED, Evidence.TREE, info.location

    async def _check_commit(
        self,
        agent: str,
        surface: SurfaceId,
        entry: RefEntry,
        target: Node,
        kind: ActionKind,
        token: str | None,
    ) -> None:
        if kind not in {ActionKind.PRESS, ActionKind.DOUBLE}:
            return
        backend = self.backend_for(surface)
        reason = commit_reason(target, await backend.commit_facts(surface, entry.handle))
        if reason is not None:
            self.gate.require(
                agent=agent, surface=surface, ref=entry.ref, action=kind.value,
                what=f"{render.describe_node(target)} ({reason})", token=token,
            )
        await backend.arm_commit(surface, ARM_MS)

    def _action_kind(self, action: str) -> ActionKind:
        kind = _ACTIONS.get(action)
        if kind is None:
            raise LayerError(INVALID_ARGS, f"unknown action {action!r}; one of {sorted(_ACTIONS)}")
        return kind

    def _action(self, kind: ActionKind, value: str | None) -> Action:
        if kind in {ActionKind.SET, ActionKind.APPEND, ActionKind.SELECT} and value is None:
            raise LayerError(INVALID_ARGS, f"{kind.value} needs value=")
        if kind is ActionKind.UPLOAD:
            if not value:
                raise LayerError(INVALID_ARGS, "upload needs value=<path>")
            return Action(kind, paths=tuple(value.split("\n")))
        if kind is ActionKind.SCROLL:
            direction = value or "down"
            if direction not in {"up", "down", "left", "right"}:
                raise LayerError(INVALID_ARGS, "scroll value is one of up, down, left, right")
            return Action(kind, direction=direction)
        if kind is ActionKind.TOGGLE:
            return Action(ActionKind.PRESS)
        return Action(kind, text=value)

    def _already(self, target: Node, value: str | None) -> bool:
        wanted = (value or "on").lower() not in {"off", "false", "0"}
        current = bool({State.CHECKED, State.PRESSED} & target.states)
        return current == wanted

    def _expects(
        self,
        nodes: Sequence[Node],
        info: Surface,
        settle: Settle,
        ref: Ref | None,
        expect: Sequence[Mapping[str, object]] | Mapping[str, object] | None,
    ) -> tuple[ExpectResult, ...]:
        if expect is None:
            return ()
        specs = [expect] if isinstance(expect, Mapping) else list(expect)
        scene = Scene(nodes, info.location, settle, ref)
        return tuple(check(scene, spec) for spec in specs)

    def _evidence(
        self,
        kind: ActionKind,
        value: str | None,
        ref: Ref,
        after: Sequence[Node],
        expects: Sequence[ExpectResult],
    ) -> tuple[Evidence, list[str]]:
        best = Evidence.NONE
        warnings: list[str] = []
        if expects:
            best = (
                max(r.evidence for r in expects) if all(r.ok for r in expects) else Evidence.NONE
            )
        target = next((n for n in after if n.ref == ref), None)
        if kind in {ActionKind.SET, ActionKind.APPEND} and target is not None:
            wanted = " ".join((value or "").split())
            reads = " ".join((target.value or "").split())
            matches = reads == wanted if kind is ActionKind.SET else reads.endswith(wanted)
            if matches:
                best = max(best, Evidence.TREE)
            elif State.SECURE not in target.states:
                warnings.append(f'field reads "{reads[:80]}"; wanted "{wanted[:80]}"')
        if kind is ActionKind.TOGGLE and target is not None:
            best = max(best, Evidence.TREE)
        return best, warnings

    def _replaced_view(self, nodes: Sequence[Node]) -> str:
        shown, _, _ = prune.select(nodes, prune.Mode.AUTO)
        return render.nodes_text(shown)

    async def key(
        self,
        agent: str,
        keys: str,
        surface: str | None = None,
        gate: str | None = None,
        settle_budget: float = DEFAULT_SETTLE,
    ) -> str:
        chosen = self._surface(agent, surface)
        backend = self.backend_for(chosen)
        started = time.monotonic()
        async with self._lock(chosen):
            before, _ = await self.read_surface(agent, chosen)
            context = await backend.key_context(chosen, keys)
            if context.implicit_submit:
                self.gate.require(
                    agent=agent, surface=chosen, ref=None, action=f"key:{keys}",
                    what=f"{keys} would submit the form ({context.detail})", token=gate,
                )
            if context.implicit_submit or self.gate.policy is Policy.ALLOW:
                await backend.arm_commit(chosen, ARM_MS)
            mark = backend.begin(chosen)
            delivery = await backend.key(chosen, keys)
            settle = await backend.settle(chosen, mark, settle_budget)
            after, sync = await self.read_surface(agent, chosen)
            info = await backend.describe(chosen)
        effect = diff(before, after, sync, settle)
        view = self._replaced_view(after) if effect.page_replaced else None
        self._trace.write(TraceRecord(
            agent=agent, surface=chosen, op="key", ok=True, action=keys,
            ms=int((time.monotonic() - started) * 1000), delivery=delivery.value, url=info.location,
        ))
        return render.action_text(
            f"key {keys}", None, delivery, Evidence.NONE, effect, (), (), view
        )

    # Lifecycle

    async def release(self, agent: str, surface: str | None = None, close: bool = False) -> str:
        chosen = self._surface(agent, surface)
        state = self.state(agent)
        state.refs.forget_surface(chosen)
        state.views.pop(chosen, None)
        if state.current == chosen:
            state.current = None
        self.claims.release(agent, chosen)
        if close:
            await self.backend_for(chosen).close_surface(chosen)
            self.claims.forget(chosen)
        return f"released {chosen}"

    def disconnect(self, agent: str) -> list[SurfaceId]:
        """The agent's connection dropped: everything it held becomes available again."""
        return self.claims.release_all(agent)

    def gate_list(self) -> str:
        lines = [
            f"{r.id} {r.agent} {r.surface} {r.what}" for r in self.gate.pending()
        ]
        return "\n".join(lines) or "no pending gates"

    def gate_approve(self, gate_id: str) -> str:
        return f"approved {self.gate.approve(gate_id).id}"

    def gate_reject(self, gate_id: str) -> str:
        return f"rejected {self.gate.reject(gate_id).id}"


def jsonable_expect(value: Any) -> Sequence[Mapping[str, object]] | Mapping[str, object] | None:
    """Validate an ``expect`` argument that crossed a process boundary as JSON."""
    if value is None or isinstance(value, Mapping):
        return value
    if isinstance(value, list) and all(isinstance(item, Mapping) for item in value):
        return value
    raise LayerError(INVALID_ARGS, "expect is an object or a list of objects")
