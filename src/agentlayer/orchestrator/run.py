"""Run a plan: one `claude -p` per agent, all sharing one daemon, in a star topology.

Workers never talk to each other. Each one finishes with the `report` tool; the orchestrator
reads those reports from the daemon and hands earlier results to the agents that wait on them.
Coordination is code, not conversation: claims and locks live in the daemon.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from agentlayer import paths
from agentlayer.client import Client
from agentlayer.errors import LayerError
from agentlayer.orchestrator.plan import AgentSpec, Plan, load_plan

SERVER = "agent-layer"
_GATE_POLL = 5.0


@dataclass(frozen=True)
class Finished:
    returncode: int | None
    stdout: str
    stderr: str
    seconds: float


Runner = Callable[[Sequence[str], dict[str, str], float], Awaitable[Finished]]


@dataclass(frozen=True)
class Outcome:
    name: str
    status: str
    note: str
    artifacts: tuple[str, ...]
    seconds: float
    cost: float | None
    turns: int | None


def mcp_config(agent: str) -> dict[str, object]:
    return {"mcpServers": {SERVER: {
        "type": "stdio",
        "command": sys.executable,
        "args": ["-m", "agentlayer.mcp_server"],
        "env": {"AGENTLAYER_AGENT": agent, paths.HOME_ENV: str(paths.home())},
    }}}


def prompt_for(spec: AgentSpec, inputs: Sequence[Outcome]) -> str:
    parts = [spec.task]
    if inputs:
        parts.append("Results from agents that ran before you:")
        for item in inputs:
            files = f" files: {', '.join(item.artifacts)}" if item.artifacts else ""
            parts.append(f"- {item.name} ({item.status}): {item.note}{files}")
    parts.append(
        "Use only the agent-layer tools. Open your own surface; do not touch another agent's. "
        "When you finish, call report(status, note, artifacts) once."
    )
    return "\n\n".join(parts)


def command_for(spec: AgentSpec, config: Path, prompt: str, claude: str = "claude") -> list[str]:
    command = [
        claude, "-p", prompt, "--mcp-config", str(config), "--strict-mcp-config",
        "--allowedTools", f"mcp__{SERVER}", "--output-format", "json",
    ]
    if spec.model:
        command += ["--model", spec.model]
    return command


async def subprocess_runner(
    command: Sequence[str], env: dict[str, str], timeout: float
) -> Finished:
    started = time.monotonic()
    process = await asyncio.create_subprocess_exec(
        *command, env=env, stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    try:
        stdout, stderr = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError:
        process.kill()
        await process.wait()
        return Finished(None, "", "timed out", time.monotonic() - started)
    return Finished(
        process.returncode, stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace"),
        time.monotonic() - started,
    )


def _usage(stdout: str) -> tuple[float | None, int | None, str]:
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError:
        return None, None, ""
    cost = payload.get("total_cost_usd")
    turns = payload.get("num_turns")
    return (
        float(cost) if isinstance(cost, int | float) else None,
        int(turns) if isinstance(turns, int) else None,
        str(payload.get("result", ""))[:300],
    )


async def _reports(agent: str) -> list[dict[str, object]]:
    async with Client("orchestrator", role="operator") as client:
        found = json.loads(await client.call("reports"))
    return [r for r in found if r.get("agent") == agent]


class Orchestrator:
    def __init__(self, plan: Plan, run_dir: Path, runner: Runner = subprocess_runner) -> None:
        self._plan = plan
        self._run_dir = run_dir
        self._runner = runner
        self._outcomes: dict[str, Outcome] = {}
        self._slots = asyncio.Semaphore(plan.concurrency)
        self._events: list[str] = []

    def log(self, text: str) -> None:
        self._events.append(text)
        print(text, flush=True)

    async def run(self) -> list[Outcome]:
        watcher = asyncio.create_task(self._watch_gates())
        try:
            for wave in self._plan.waves():
                await asyncio.gather(*(self._run_agent(spec) for spec in wave))
        finally:
            watcher.cancel()
        return [self._outcomes[a.name] for a in self._plan.agents]

    async def _run_agent(self, spec: AgentSpec) -> None:
        inputs = [self._outcomes[name] for name in spec.after]
        blocked = [i.name for i in inputs if i.status not in {"done", "partial"}]
        if blocked:
            self._outcomes[spec.name] = Outcome(
                spec.name, "skipped", f"waits on {', '.join(blocked)}", (), 0.0, None, None
            )
            self.log(f"[{spec.name}] skipped: {', '.join(blocked)} did not finish")
            return
        config = self._run_dir / f"{spec.name}.mcp.json"
        config.write_text(json.dumps(mcp_config(spec.name)), encoding="utf-8")
        command = command_for(spec, config, prompt_for(spec, inputs))
        env = {**os.environ, "AGENTLAYER_AGENT": spec.name}
        attempts = spec.retries + 1
        for attempt in range(1, attempts + 1):
            async with self._slots:
                self.log(f"[{spec.name}] started (attempt {attempt})")
                finished = await self._runner(command, env, spec.timeout)
            (self._run_dir / f"{spec.name}.{attempt}.out.json").write_text(
                finished.stdout, encoding="utf-8"
            )
            outcome = await self._outcome(spec, finished)
            if outcome.status != "failed" or attempt == attempts:
                break
        self._outcomes[spec.name] = outcome
        cost = f", ${outcome.cost:.2f}" if outcome.cost is not None else ""
        self.log(f"[{spec.name}] {outcome.status} ({outcome.seconds:.0f}s{cost}) {outcome.note}")

    async def _outcome(self, spec: AgentSpec, finished: Finished) -> Outcome:
        cost, turns, result = _usage(finished.stdout)
        if finished.returncode != 0:
            note = finished.stderr.strip()[:200] or f"exit {finished.returncode}"
            return Outcome(spec.name, "failed", note, (), finished.seconds, cost, turns)
        try:
            reports = await _reports(spec.name)
        except LayerError as exc:
            reports = []
            result = result or f"could not read reports: {exc.message}"
        if not reports:
            note = f"no report; final text: {result}"
            return Outcome(spec.name, "failed", note, (), finished.seconds, cost, turns)
        last = reports[-1]
        artifacts = last.get("artifacts") or []
        return Outcome(
            spec.name, str(last["status"]), str(last.get("note", "")),
            tuple(str(a) for a in artifacts) if isinstance(artifacts, list) else (),
            finished.seconds, cost, turns,
        )

    async def _watch_gates(self) -> None:
        seen: set[str] = set()
        while True:
            await asyncio.sleep(_GATE_POLL)
            try:
                async with Client("orchestrator", role="operator", spawn=False) as client:
                    pending = await client.call("gate_list")
            except LayerError:
                continue  # the daemon starts with the first agent
            for line in pending.splitlines():
                gate_id = line.split(" ", 1)[0]
                if gate_id.startswith("g") and gate_id not in seen:
                    seen.add(gate_id)
                    self.log(f"gate waiting: {line}  (agentlayer gate approve {gate_id})")


def summary(outcomes: Sequence[Outcome]) -> str:
    counts: dict[str, int] = {}
    for outcome in outcomes:
        counts[outcome.status] = counts.get(outcome.status, 0) + 1
    totals = ", ".join(f"{n} {status}" for status, n in sorted(counts.items()))
    return f"{len(outcomes)} agents: {totals}"


def run_plan(path: str, *, dry_run: bool = False) -> str:
    plan = load_plan(path)
    if dry_run:
        lines = [f"concurrency {plan.concurrency}"]
        for number, wave in enumerate(plan.waves(), start=1):
            lines.append(f"wave {number}: " + ", ".join(a.name for a in wave))
        return "\n".join(lines)
    run_dir = paths.home() / "runs" / time.strftime("%Y%m%d-%H%M%S")
    run_dir.mkdir(parents=True, exist_ok=True)
    outcomes = asyncio.run(Orchestrator(plan, run_dir).run())
    return f"{summary(outcomes)}\nrun directory: {run_dir}"
