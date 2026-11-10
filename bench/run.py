"""Run the benchmark: every task under every arm, a few times each, with `claude -p`.

    uv run python -m bench.run --smoke              # no model: fixtures, checkers, sizes
    uv run python -m bench.run --dry-run            # print the commands
    uv run python -m bench.run --repeat 3           # the real thing; uses subscription quota

Each run gets a fresh fixture server, a fresh browser profile and a throwaway working
directory with nothing in it, and only its arm's MCP tools.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import AsyncIterator, Callable, Iterator, Mapping, Sequence
from contextlib import asynccontextmanager, contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from agentlayer.adapters.registry import AdapterRegistry
from agentlayer.engine import Engine
from agentlayer.errors import LayerError
from agentlayer.gate import Gate, Policy
from bench.check import Task, Verdict, check, load_tasks
from bench.fixture_server import serve
from bench.fixtures.mac.launch import fixture_app
from bench.solutions import AGENT as SOLUTION_AGENT
from bench.solutions import SOLUTIONS

ARMS = Path(__file__).parent / "arms.toml"
RESULTS = Path(__file__).parent / "results"
DEFAULT_MODEL = "claude-sonnet-5-5"
RUN_TIMEOUT = 600.0
RESUME = b"%PDF-1.4\n% benchmark resume\n"
_DROPPED_ENV = ("CLAUDE_CODE_PLUGIN_DIRS",)
USAGE_LIMIT = "usage limit reached"
_LIMIT = re.compile(r"hit your [\w -]*limit", re.IGNORECASE)


@dataclass(frozen=True)
class Arm:
    id: str
    label: str
    server: str
    command: tuple[str, ...]
    tools: tuple[str, ...]
    only_kinds: tuple[str, ...] = ()
    hidden: tuple[str, ...] = ()
    platforms: tuple[str, ...] = ("web", "mac")

    def applies(self, task: Task) -> bool:
        kind_ok = not self.only_kinds or task.kind in self.only_kinds
        return kind_ok and task.platform in self.platforms


@dataclass(frozen=True)
class Usage:
    input_tokens: int
    output_tokens: int
    tool_calls: int
    turns: int | None
    cost_usd: float | None
    seconds: float
    answer: str
    error: str = ""


def load_arms(path: Path = ARMS) -> dict[str, Arm]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    arms: dict[str, Arm] = {}
    for key, a in data["arm"].items():
        server = str(a["server"])
        offered = list(data["server"][server]["tools"])
        unknown = set(a["tools"]) - set(offered)
        if unknown:
            raise ValueError(f"arm {key} allows tools {server} does not offer: {sorted(unknown)}")
        arms[key] = Arm(
            key, str(a["label"]), server,
            tuple(str(sys.executable) if p == "{python}" else str(p) for p in a["command"]),
            tuple(a["tools"]), tuple(a.get("only_kinds", ())),
            hidden=tuple(t for t in offered if t not in a["tools"]),
            platforms=tuple(a.get("platforms", ("web", "mac"))),
        )
    return arms


def mcp_config(arm: Arm, home: Path, commit: str = "ask") -> dict[str, Any]:
    env = {
        "AGENTLAYER_HOME": str(home), "AGENTLAYER_PROFILE": str(home / "profile"),
        "AGENTLAYER_COMMIT": commit, "AGENTLAYER_MOCK_KEYCHAIN": "1",
        "AGENTLAYER_AGENT": "bench",
    } if arm.server == "agent-layer" else {}
    return {"mcpServers": {arm.server: {
        "type": "stdio", "command": arm.command[0], "args": list(arm.command[1:]), "env": env,
    }}}


def claude_command(arm: Arm, config: Path, prompt: str, model: str) -> list[str]:
    allowed = [f"mcp__{arm.server}__{tool}" for tool in arm.tools]
    hidden = [f"mcp__{arm.server}__{tool}" for tool in arm.hidden]
    command = [
        "claude", "-p", prompt, "--model", model,
        "--mcp-config", str(config), "--strict-mcp-config",
        "--tools", "", "--allowedTools", *allowed,
    ]
    if hidden:
        command += ["--disallowedTools", *hidden]
    return command + [
        "--setting-sources", "project", "--no-session-persistence",
        "--output-format", "stream-json", "--verbose",
    ]


def parse_stream(lines: Sequence[str], seconds: float) -> Usage:
    """Totals from `claude -p --output-format stream-json`: tool calls and the result event."""
    tool_calls = 0
    result: Mapping[str, Any] = {}
    for line in lines:
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "assistant":
            content = event.get("message", {}).get("content", [])
            tool_calls += sum(1 for part in content if part.get("type") == "tool_use")
        elif event.get("type") == "result":
            result = event
    usage = result.get("usage", {})
    input_tokens = sum(int(usage.get(k, 0)) for k in (
        "input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens"
    ))
    cost = result.get("total_cost_usd")
    turns = result.get("num_turns")
    return Usage(
        input_tokens=input_tokens, output_tokens=int(usage.get("output_tokens", 0)),
        tool_calls=tool_calls, turns=int(turns) if isinstance(turns, int) else None,
        cost_usd=float(cost) if isinstance(cost, int | float) else None, seconds=seconds,
        answer=str(result.get("result", "")),
        error="" if result else "no result event",
    )


@contextmanager
def workspace() -> Iterator[Path]:
    # Short path: the daemon socket must fit in a Unix socket address.
    with tempfile.TemporaryDirectory(prefix="alb-", dir="/tmp") as raw:
        yield Path(raw)


@dataclass(frozen=True)
class Fixture:
    """Where a task starts, and how to read what the app ended up holding."""

    target: str
    resume: Path
    recorded: Callable[[], Mapping[str, Any]]


@contextmanager
def fixture_for(task: Task, home: Path) -> Iterator[Fixture]:
    resume = home / "resume.pdf"
    resume.write_bytes(RESUME)
    if task.platform == "mac":
        with fixture_app(home / "mac-state.json") as app:
            yield Fixture(app.target, resume, lambda: {"state": {"mac": app.state()}})
        return
    with serve() as (host, recorder):
        yield Fixture(f"http://{host}/{task.page}", resume, recorder.snapshot)


def prompt_for(task: Task, fixture: Fixture) -> str:
    return task.prompt.format(url=fixture.target, target=fixture.target, resume=fixture.resume)


def _stop_daemon(home: Path) -> None:
    env = {**os.environ, "AGENTLAYER_HOME": str(home)}
    subprocess.run([sys.executable, "-m", "agentlayer.cli", "daemon", "stop"], env=env,
                   capture_output=True, check=False, timeout=30)


def run_once(task: Task, arm: Arm, model: str) -> dict[str, Any]:
    with workspace() as home, fixture_for(task, home) as fixture:
        config = home / "mcp.json"
        config.write_text(json.dumps(mcp_config(arm, home, task.commit)), encoding="utf-8")
        command = claude_command(arm, config, prompt_for(task, fixture), model)
        env = {k: v for k, v in os.environ.items() if k not in _DROPPED_ENV}
        started = time.monotonic()
        try:
            finished = subprocess.run(command, cwd=home, env=env, capture_output=True,
                                      text=True, timeout=RUN_TIMEOUT, check=False)
            usage = parse_stream(finished.stdout.splitlines(), time.monotonic() - started)
            if finished.returncode != 0 and not usage.error:
                usage = Usage(**{**asdict(usage), "error": finished.stderr.strip()[-300:]})
        except subprocess.TimeoutExpired:
            usage = Usage(0, 0, 0, None, None, RUN_TIMEOUT, "", "timed out")
        finally:
            if arm.server == "agent-layer":
                _stop_daemon(home)
        if _LIMIT.search(usage.answer):
            usage = Usage(**{**asdict(usage), "error": USAGE_LIMIT})
        time.sleep(0.3)
        verdict = check(task, fixture.recorded(), usage.answer)
    return {
        "task": task.id, "arm": arm.id, "model": model, "ok": verdict.ok,
        "reasons": list(verdict.reasons), **asdict(usage), "at": time.time(),
    }


# --- smoke: no model ------------------------------------------------------------------------

@asynccontextmanager
async def _engine_for(task: Task) -> AsyncIterator[Engine]:
    """An in-process engine with the task's commit policy, as the arms run it."""
    policy = Policy(task.commit)
    if task.platform == "mac":
        from agentlayer.backends.mac.backend import MacBackend

        engine = Engine({"mac": MacBackend()}, gate=Gate(policy), adapters=AdapterRegistry(()))
        try:
            yield engine
        finally:
            await engine.stop()
        return
    from tests.web_harness import engine_with_chrome

    async with engine_with_chrome(policy) as engine:
        yield engine


async def _measured(task: Task, fixture: Fixture) -> tuple[str, dict[str, int]]:
    """How big the first look is, then the scripted solution in a fresh engine."""
    sizes: dict[str, int] = {}
    async with _engine_for(task) as engine:
        await engine.open(SOLUTION_AGENT, fixture.target)
        sizes["observe"] = len(await engine.observe(SOLUTION_AGENT))
        if task.kind == "form":
            sizes["form"] = len(await engine.form(SOLUTION_AGENT))
    solution = SOLUTIONS.get(task.id)
    if solution is None:
        raise ValueError(f"no scripted solution for {task.id}")
    async with _engine_for(task) as engine:
        answer = await solution(engine, SOLUTION_AGENT, fixture.target, fixture.resume)
    return answer, sizes


def smoke(tasks: Sequence[Task]) -> bool:
    passed = True
    for task in tasks:
        with workspace() as home, fixture_for(task, home) as fixture:
            try:
                answer, sizes = asyncio.run(_measured(task, fixture))
            except LayerError as exc:
                answer, sizes = "", {}
                print(f"     {task.id}: {exc.code} {exc.message}")
            time.sleep(0.3)
            verdict: Verdict = check(task, fixture.recorded(), answer)
        shown = ", ".join(f"{k} {v} chars" for k, v in sizes.items())
        print(f"{'ok  ' if verdict.ok else 'FAIL'} {task.id}: {shown}", flush=True)
        for reason in verdict.reasons:
            print(f"     {reason}")
        passed = passed and verdict.ok
    return passed


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bench.run")
    parser.add_argument("--tasks", nargs="*")
    parser.add_argument("--arms", nargs="*")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--smoke", action="store_true", help="no model; check the harness")
    parser.add_argument("--dry-run", action="store_true", help="print commands only")
    options = parser.parse_args(argv)

    tasks = [t for t in load_tasks() if not options.tasks or t.id in options.tasks]
    if options.smoke:
        return 0 if smoke(tasks) else 1
    arms = [a for k, a in load_arms().items() if not options.arms or k in options.arms]
    plan = [(t, a) for t in tasks for a in arms if a.applies(t)] * options.repeat
    if options.dry_run:
        for task, arm in plan:
            print(arm.id, task.id, " ".join(claude_command(arm, Path("mcp.json"),
                                                          "<prompt>", options.model)))
        return 0
    RESULTS.mkdir(exist_ok=True)
    out = RESULTS / f"{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    for number, (task, arm) in enumerate(plan, start=1):
        record = run_once(task, arm, options.model)
        if record["error"] == USAGE_LIMIT:
            # Not the task's failure: the model never ran. Stop rather than record noise.
            print(f"[{number}/{len(plan)}] stopped: {record['answer'].strip()[:120]}")
            print(f"results so far: {out}")
            return 2
        with out.open("a", encoding="utf-8") as sink:
            sink.write(json.dumps(record) + "\n")
        status = "ok" if record["ok"] else "FAIL " + "; ".join(record["reasons"])[:120]
        print(f"[{number}/{len(plan)}] {arm.id} {task.id}: {status} "
              f"({record['input_tokens']} in, {record['tool_calls']} calls, "
              f"{record['seconds']:.0f}s)", flush=True)
    print(f"results: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
