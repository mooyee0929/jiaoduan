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
import subprocess
import sys
import tempfile
import time
import tomllib
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from bench.check import Task, Verdict, check, load_tasks
from bench.fixture_server import serve

ARMS = Path(__file__).parent / "arms.toml"
RESULTS = Path(__file__).parent / "results"
DEFAULT_MODEL = "claude-sonnet-5-5"
RUN_TIMEOUT = 600.0
RESUME = b"%PDF-1.4\n% benchmark resume\n"
_DROPPED_ENV = ("CLAUDE_CODE_PLUGIN_DIRS",)


@dataclass(frozen=True)
class Arm:
    id: str
    label: str
    server: str
    command: tuple[str, ...]
    tools: tuple[str, ...]
    only_kinds: tuple[str, ...] = ()
    hidden: tuple[str, ...] = ()

    def applies(self, task: Task) -> bool:
        return not self.only_kinds or task.kind in self.only_kinds


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


def prompt_for(task: Task, host: str, resume: Path) -> str:
    return task.prompt.format(url=f"http://{host}/{task.page}", resume=resume)


def _stop_daemon(home: Path) -> None:
    env = {**os.environ, "AGENTLAYER_HOME": str(home)}
    subprocess.run([sys.executable, "-m", "agentlayer.cli", "daemon", "stop"], env=env,
                   capture_output=True, check=False, timeout=30)


def run_once(task: Task, arm: Arm, model: str) -> dict[str, Any]:
    with workspace() as home, serve() as (host, recorder):
        resume = home / "resume.pdf"
        resume.write_bytes(RESUME)
        config = home / "mcp.json"
        config.write_text(json.dumps(mcp_config(arm, home, task.commit)), encoding="utf-8")
        command = claude_command(arm, config, prompt_for(task, host, resume), model)
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
        time.sleep(0.3)
        verdict = check(task, recorder.snapshot(), usage.answer)
    return {
        "task": task.id, "arm": arm.id, "model": model, "ok": verdict.ok,
        "reasons": list(verdict.reasons), **asdict(usage), "at": time.time(),
    }


# --- smoke: no model ------------------------------------------------------------------------

async def _scripted(task: Task, host: str, resume: Path) -> tuple[str, dict[str, int]]:
    """What a perfect agent would do through arm D, to prove each task can be passed."""
    from agentlayer.gate import Policy
    from tests.web_harness import engine_with_chrome

    sizes: dict[str, int] = {}
    async with engine_with_chrome(Policy.ALLOW) as engine:
        await engine.open("bench", f"http://{host}/{task.page}")
        sizes["observe"] = len(await engine.observe("bench"))
        if task.kind == "form":
            sizes["form"] = len(await engine.form("bench"))
        if task.id == "contact-send":
            await engine.fill("bench", {"Topic": "Support",
                                        "Message": "The export button does nothing."})
            found = await engine.observe("bench", query="Send message", role="button")
            ref = found.split("[", 1)[1].split("]", 1)[0]
            await engine.act("bench", ref, "press")
            return "done", sizes
        if task.id == "application-fill":
            values = {
                "Resume/CV": str(resume), "First name": "Ada", "Last name": "Lovelace",
                "Email": "ada@example.com", "Phone": "555 123 4567", "Country": "Taiwan",
                "Degree": "Master of Science", "School": "Riverbend University",
                "Will you require visa sponsorship?": "No",
                "Are you at least 18 years old?": "Yes", "I agree to the privacy notice": "on",
            }
            report = await engine.fill("bench", values)
            sizes["fill"] = len(report)
            if "mismatch" in report:
                await engine.fill("bench", {"First name": "Ada"})
            return "done", sizes
        if task.id == "orders-lookup":
            found = await engine.observe("bench", query="#1043")
            sizes["query"] = len(found)
            return found.rsplit("|", 1)[-1].strip().strip('"'), sizes
    raise ValueError(f"no scripted solution for {task.id}")


def smoke(tasks: Sequence[Task]) -> bool:
    passed = True
    for task in tasks:
        with workspace() as home, serve() as (host, recorder):
            resume = home / "resume.pdf"
            resume.write_bytes(RESUME)
            answer, sizes = asyncio.run(_scripted(task, host, resume))
            time.sleep(0.3)
            verdict: Verdict = check(task, recorder.snapshot(), answer)
        shown = ", ".join(f"{k} {v} chars" for k, v in sizes.items())
        print(f"{'ok  ' if verdict.ok else 'FAIL'} {task.id}: {shown}")
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
