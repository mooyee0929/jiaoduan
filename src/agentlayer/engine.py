"""The operations agents call. Works on any Backend; returns text ready to show a model."""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from agentlayer import prune, render
from agentlayer.adapters.registry import AdapterRegistry
from agentlayer.claims import Claims, SessionLocks
from agentlayer.diff import Effect, diff
from agentlayer.errors import (
    AMBIGUOUS,
    BACKEND_UNAVAILABLE,
    INVALID_ARGS,
    NO_ACCESSIBLE_TREE,
    NO_SURFACE,
    NOT_ACTIONABLE,
    NOT_FOUND,
    NOT_SUPPORTED,
    LayerError,
)
from agentlayer.expect import ExpectResult, Scene, check
from agentlayer.forms.fill import FillLine, FormIds, render_fill, render_schema
from agentlayer.forms.fill import fill as fill_form
from agentlayer.forms.schema import DriverContext, Field, FieldKind, WriteStatus, collect
from agentlayer.gate import Gate, Policy, commit_reason
from agentlayer.model import (
    Action,
    ActionKind,
    Backend,
    Delivery,
    Evidence,
    Node,
    Ref,
    Role,
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
_GOOD_WRITES = frozenset({WriteStatus.OK, WriteStatus.UNCHANGED})
_CHOICE_KINDS = frozenset({
    FieldKind.SELECT, FieldKind.COMBOBOX, FieldKind.LISTBOX, FieldKind.RADIO, FieldKind.BUTTONS,
})


@dataclass(frozen=True)
class Report:
    agent: str
    status: str
    note: str
    artifacts: tuple[str, ...]
    at: float


@dataclass
class AgentState:
    refs: RefTable = field(default_factory=RefTable)
    views: dict[SurfaceId, list[Node]] = field(default_factory=dict)
    forms: dict[SurfaceId, FormIds] = field(default_factory=dict)
    current: SurfaceId | None = None


class Engine:
    def __init__(
        self,
        backends: Mapping[str, Backend],
        *,
        gate: Gate | None = None,
        trace: Trace | None = None,
        claims: Claims | None = None,
        adapters: AdapterRegistry | None = None,
    ) -> None:
        self._backends = dict(backends)
        self.gate = gate or Gate()
        self._trace = trace or Trace(None)
        self.claims = claims or Claims()
        self.adapters = adapters or AdapterRegistry.from_env()
        self._agents: dict[str, AgentState] = {}
        self._locks: dict[SurfaceId, asyncio.Lock] = {}
        self._started: set[str] = set()
        self._start_lock = asyncio.Lock()
        self.session_locks = SessionLocks()
        self._reports: list[Report] = []

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
        backend = await self._ensure_started(kind or _backend_kind(target))
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
            lines, total = prune.search_text(nodes, query, role)
            limit = prune.SEARCH_LIMIT
            shown = f"{limit} of {total} matches" if total > limit else f"{total} matches"
            return "\n".join([f"{header} | {shown}", *lines])
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
        return (await self.wait_result(agent, expect, surface, timeout))[1]

    async def wait_result(
        self,
        agent: str,
        expect: Mapping[str, object],
        surface: str | None = None,
        timeout: float = 10.0,
    ) -> tuple[bool, str]:
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
                return result.ok, f"wait {result.kind}: {outcome} after {ms}ms ({result.detail})"
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
        force_commit: bool = False,
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
                    agent, entry.ref, kind, value, expect, gate, settle_budget, force_commit
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
        force_commit: bool = False,
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
        # Typing into a dropdown that takes no text does nothing; choosing is what was meant.
        # A combobox that takes text keeps `set` as typing, so it can be used to filter.
        chooses = target.role in {Role.COMBOBOX, Role.LISTBOX} and State.EDITABLE not in (
            target.states
        )
        if kind is ActionKind.SELECT or (kind is ActionKind.SET and chooses):
            return await self._select(
                agent, surface, target, before, value or "", expect, settle_budget
            )

        await self._check_commit(agent, surface, entry, target, kind, gate_token, force_commit)
        mark = backend.begin(surface)
        delivery = await backend.perform(surface, entry.handle, self._action(kind, value))
        settle = await backend.settle(surface, mark, settle_budget)
        after, sync = await self.read_surface(agent, surface)
        info = await backend.describe(surface)
        effect = diff(before, after, sync, settle)
        expects = self._expects(after, info, settle, ref, expect)
        evidence, more = self._evidence(kind, value, ref, after, expects)
        warnings += more
        warnings += _failed_requests(settle)
        view = self._replaced_view(after) if effect.page_replaced else None
        text = render.action_text(
            kind.value, target, delivery, evidence, effect, expects, warnings, view
        )
        return text, delivery, evidence, info.location

    async def _select(
        self,
        agent: str,
        surface: SurfaceId,
        target: Node,
        before: list[Node],
        value: str,
        expect: Sequence[Mapping[str, object]] | Mapping[str, object] | None,
        settle_budget: float,
    ) -> tuple[str, Delivery, Evidence, str]:
        """Choosing an option is a widget's job: the same driver `fill` would use does it."""
        backend = self.backend_for(surface)
        ctx = self.driver_context(agent, surface)
        fields = await collect(ctx, None, with_values=False)
        field = next(
            (f for f in fields if f.handle == target.handle
             or any(m.handle == target.handle for m in f.members)),
            None,
        )
        if field is None or field.kind not in _CHOICE_KINDS:
            raise LayerError(
                NOT_SUPPORTED, f"{render.describe_node(target)} is not a field with options",
                hint="use set for text; for other menus press the control, then the option",
            )
        mark = backend.begin(surface)
        outcome = await field.driver.write(ctx, field, value)
        if outcome.status in {WriteStatus.AMBIGUOUS, WriteStatus.NOT_FOUND}:
            choices = " | ".join(outcome.candidates) or "none matched"
            raise LayerError(
                AMBIGUOUS if outcome.status is WriteStatus.AMBIGUOUS else NOT_FOUND,
                f"{value!r} is not one option of {render.describe_node(target)}: {choices}",
                delivery=Delivery.NOT_DELIVERED, hint="call form to see every option",
            )
        settle = await backend.settle(surface, mark, settle_budget)
        after, sync = await self.read_surface(agent, surface)
        info = await backend.describe(surface)
        effect = diff(before, after, sync, settle)
        expects = self._expects(after, info, settle, target.ref, expect)
        good = outcome.status in _GOOD_WRITES
        warnings = [] if good else [
            f"{outcome.status.value}: shows {outcome.got!r}"
            + (f" ({outcome.note})" if outcome.note else "")
        ]
        if outcome.status is WriteStatus.UNCHANGED:
            warnings.append("already chosen; nothing was sent")
        delivery = (
            Delivery.NOT_DELIVERED if outcome.status is WriteStatus.UNCHANGED
            else Delivery.DELIVERED
        )
        evidence = outcome.evidence if good else Evidence.NONE
        if any(not e.ok for e in expects):
            evidence = Evidence.NONE
        text = render.action_text(
            "select", target, delivery, evidence, effect, expects, warnings
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
        forced: bool = False,
    ) -> None:
        if kind not in {ActionKind.PRESS, ActionKind.DOUBLE}:
            return
        backend = self.backend_for(surface)
        reason = "the adapter marks it as a commit" if forced else commit_reason(
            target, await backend.commit_facts(surface, entry.handle)
        )
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

    # Forms

    def form_ids(self, agent: str, surface: SurfaceId) -> FormIds:
        return self.state(agent).forms.setdefault(surface, FormIds())

    def driver_context(self, agent: str, surface: SurfaceId) -> DriverContext:
        support = self.backend_for(surface).form_support()
        if support is None:
            raise LayerError(NOT_SUPPORTED, f"{surface} has no form support")
        return DriverContext(self, agent, surface, support)

    async def form_fields(
        self, agent: str, surface: str | None = None, root: str | None = None
    ) -> tuple[SurfaceId, list[Field]]:
        chosen = self._surface(agent, surface)
        async with self._lock(chosen):
            return chosen, await collect(self.driver_context(agent, chosen), root)

    async def form(self, agent: str, surface: str | None = None, root: str | None = None) -> str:
        chosen, fields = await self.form_fields(agent, surface, root)
        info = await self.backend_for(chosen).describe(chosen)
        return render_schema(chosen, info.title, fields)

    async def fill_lines(
        self,
        agent: str,
        values: Mapping[str, str],
        surface: str | None = None,
        root: str | None = None,
    ) -> tuple[list[FillLine], list[Field], list[Field]]:
        chosen = self._surface(agent, surface)
        started = time.monotonic()
        async with self._lock(chosen):
            result = await fill_form(self.driver_context(agent, chosen), values, root)
        elapsed = int((time.monotonic() - started) * 1000)
        for line in result[0]:
            outcome = line.outcome
            written = values.get(line.field_id) or values.get(line.label) or ""
            self._trace.write(TraceRecord(
                agent=agent, surface=chosen, op="fill", ok=outcome.status in _GOOD_WRITES,
                ms=elapsed, action=line.kind, locator=line.locator, text_len=len(written),
                evidence=outcome.evidence.label, code=outcome.status.value,
            ))
        return result

    async def fill(
        self,
        agent: str,
        values: Mapping[str, str],
        surface: str | None = None,
        root: str | None = None,
    ) -> str:
        return render_fill(*await self.fill_lines(agent, values, surface, root))

    # Adapters and reports

    async def tools(self, agent: str, surface: str | None = None) -> str:
        from agentlayer.adapters.runner import list_tools

        chosen = self._surface(agent, surface)
        return list_tools(self.adapters.matching(await self.backend_for(chosen).describe(chosen)))

    async def call_tool(
        self,
        agent: str,
        name: str,
        arguments: Mapping[str, object],
        surface: str | None = None,
        gate: str | None = None,
    ) -> str:
        from agentlayer.adapters.runner import run_tool

        chosen = self._surface(agent, surface)
        return await run_tool(self, agent, chosen, name, arguments, gate)

    def report(
        self, agent: str, status: str, note: str | None, artifacts: Sequence[str]
    ) -> str:
        if status not in {"done", "partial", "blocked"}:
            raise LayerError(INVALID_ARGS, "status is done, partial or blocked")
        self._reports.append(Report(agent, status, note or "", tuple(artifacts), time.time()))
        return f"recorded: {status}"

    def reports_json(self) -> str:
        return json.dumps([
            {"agent": r.agent, "status": r.status, "note": r.note,
             "artifacts": list(r.artifacts), "at": r.at}
            for r in self._reports
        ])

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


def _failed_requests(settle: Settle) -> list[str]:
    """Said in words: a page often shows "Saved" before the request behind it has failed."""
    failed = [r for r in settle.requests if r.status is not None and r.status >= 400]
    if not failed:
        return []
    listed = ", ".join(f"{r.method} {r.path} {r.status}" for r in failed)
    return [f"the app's request failed ({listed}); what the page shows may not have been kept"]


def _backend_kind(target: str) -> str:
    """URLs and bare domains open in the browser; app names and ``pid:N`` are native apps."""
    if target.startswith("pid:"):
        return "mac"
    return "web" if _URL_LIKE.match(target) else "mac"


def jsonable_expect(value: Any) -> Sequence[Mapping[str, object]] | Mapping[str, object] | None:
    """Validate an ``expect`` argument that crossed a process boundary as JSON."""
    if value is None or isinstance(value, Mapping):
        return value
    if isinstance(value, list) and all(isinstance(item, Mapping) for item in value):
        return value
    raise LayerError(INVALID_ARGS, "expect is an object or a list of objects")
