from __future__ import annotations

import asyncio
import json
from collections.abc import Sequence
from pathlib import Path

import pytest

from jiaoduan.errors import LayerError
from jiaoduan.orchestrator import run as orchestration
from jiaoduan.orchestrator.plan import parse_plan
from jiaoduan.orchestrator.run import (
    Finished,
    Orchestrator,
    Outcome,
    command_for,
    mcp_config,
    prompt_for,
    summary,
)

PLAN = """
[defaults]
concurrency = 2
timeout = 30

[[agent]]
name = "collect"
task = "Collect the prices"

[[agent]]
name = "check"
task = "Check the totals"

[[agent]]
name = "write"
task = "Write the summary"
after = ["collect", "check"]
model = "claude-sonnet-5-5"
"""


def test_plan_groups_agents_into_waves_by_dependency() -> None:
    plan = parse_plan(PLAN)
    assert [[a.name for a in wave] for wave in plan.waves()] == [["collect", "check"], ["write"]]
    assert plan.concurrency == 2
    assert plan.agents[2].model == "claude-sonnet-5-5"
    assert plan.agents[0].timeout == 30


@pytest.mark.parametrize("broken, words", [
    ("", "at least one"),
    ('[[agent]]\nname = "A"\ntask = "x"\n', "lower-case"),
    ('[[agent]]\nname = "a"\ntask = ""\n', "needs a task"),
    ('[[agent]]\nname = "a"\ntask = "x"\n[[agent]]\nname = "a"\ntask = "y"\n', "unique"),
    ('[[agent]]\nname = "a"\ntask = "x"\nafter = ["ghost"]\n', "unknown"),
    ('[[agent]]\nname = "a"\ntask = "x"\nafter = ["b"]\n[[agent]]\nname = "b"\ntask = "y"\n'
     'after = ["a"]\n', "cycle"),
])
def test_bad_plans_are_rejected(broken: str, words: str) -> None:
    with pytest.raises(LayerError) as caught:
        parse_plan(broken)
    assert words in caught.value.message


def test_each_worker_gets_its_own_agent_name_and_only_layer_tools(tmp_path: Path) -> None:
    config = mcp_config("collect")
    server = config["mcpServers"]["jiaoduan"]  # type: ignore[index]
    assert server["env"]["JIAODUAN_AGENT"] == "collect"
    spec = parse_plan(PLAN).agents[2]
    command = command_for(spec, tmp_path / "c.json", "do it")
    assert command[command.index("--allowedTools") + 1] == "mcp__jiaoduan"
    assert "--strict-mcp-config" in command
    assert command[command.index("--tools") + 1] == ""
    assert command[command.index("--model") + 1] == "claude-sonnet-5-5"


def test_a_dependent_prompt_carries_earlier_reports() -> None:
    spec = parse_plan(PLAN).agents[2]
    earlier = [Outcome("collect", "done", "3 prices", ("prices.csv",), 1.0, None, None)]
    prompt = prompt_for(spec, earlier)
    assert "collect (done): 3 prices files: prices.csv" in prompt
    assert "report(status, note, artifacts)" in prompt


class FakeRuns:
    """Stands in for `claude -p` and for the daemon's report list."""

    def __init__(self, failing: set[str]) -> None:
        self.failing = failing
        self.started: list[str] = []
        self.reports: list[dict[str, object]] = []
        self.running = 0
        self.peak = 0

    async def runner(self, command: Sequence[str], env: dict[str, str], timeout: float) -> Finished:
        name = env["JIAODUAN_AGENT"]
        self.started.append(name)
        self.running += 1
        self.peak = max(self.peak, self.running)
        await asyncio.sleep(0.01)
        self.running -= 1
        if name in self.failing:
            return Finished(1, "", "model refused", 0.01)
        self.reports.append({"agent": name, "status": "done", "note": f"{name} ok",
                             "artifacts": [f"{name}.txt"]})
        return Finished(0, json.dumps({"total_cost_usd": 0.02, "num_turns": 3,
                                       "result": "ok"}), "", 0.01)

    async def reports_for(self, agent: str) -> list[dict[str, object]]:
        return [r for r in self.reports if r["agent"] == agent]


def run_plan_with(fake: FakeRuns, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> list[Outcome]:
    monkeypatch.setattr(orchestration, "_reports", fake.reports_for)
    monkeypatch.setattr(orchestration, "_GATE_POLL", 60.0)
    return asyncio.run(Orchestrator(parse_plan(PLAN), tmp_path, fake.runner).run())


def test_agents_run_in_waves_within_the_concurrency_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRuns(failing=set())
    outcomes = run_plan_with(fake, tmp_path, monkeypatch)
    assert fake.started.index("write") == 2
    assert fake.peak <= 2
    assert [o.status for o in outcomes] == ["done", "done", "done"]
    assert outcomes[0].cost == 0.02 and outcomes[0].artifacts == ("collect.txt",)
    assert (tmp_path / "write.mcp.json").exists()
    assert summary(outcomes) == "3 agents: 3 done"


def test_a_failed_dependency_skips_the_agents_that_wait_on_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRuns(failing={"check"})
    outcomes = run_plan_with(fake, tmp_path, monkeypatch)
    by_name = {o.name: o for o in outcomes}
    assert by_name["check"].status == "failed" and "model refused" in by_name["check"].note
    assert by_name["write"].status == "skipped"
    assert "write" not in fake.started


def test_an_agent_that_never_reports_counts_as_failed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeRuns(failing=set())

    async def silent(agent: str) -> list[dict[str, object]]:
        return []

    monkeypatch.setattr(orchestration, "_reports", silent)
    monkeypatch.setattr(orchestration, "_GATE_POLL", 60.0)
    outcomes = asyncio.run(Orchestrator(parse_plan(PLAN), tmp_path, fake.runner).run())
    assert outcomes[0].status == "failed" and "no report" in outcomes[0].note
