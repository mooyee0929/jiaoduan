"""A run plan: agents, their tasks, and which ones wait for which."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from agentlayer.errors import INVALID_ARGS, LayerError

_NAME = re.compile(r"^[a-z][a-z0-9-]{0,31}$")


@dataclass(frozen=True)
class AgentSpec:
    name: str
    task: str
    after: tuple[str, ...]
    model: str | None
    timeout: float
    retries: int


@dataclass(frozen=True)
class Plan:
    agents: tuple[AgentSpec, ...]
    concurrency: int

    def waves(self) -> list[list[AgentSpec]]:
        """Agents grouped so each group only depends on earlier groups."""
        done: set[str] = set()
        remaining = list(self.agents)
        waves: list[list[AgentSpec]] = []
        while remaining:
            ready = [a for a in remaining if set(a.after) <= done]
            if not ready:
                names = ", ".join(a.name for a in remaining)
                raise LayerError(INVALID_ARGS, f"the plan has a dependency cycle among {names}")
            waves.append(ready)
            done.update(a.name for a in ready)
            remaining = [a for a in remaining if a not in ready]
        return waves


def parse_plan(text: str) -> Plan:
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise LayerError(INVALID_ARGS, f"plan is not valid TOML: {exc}") from exc
    defaults = data.get("defaults", {})
    raw_agents = data.get("agent", [])
    if not raw_agents:
        raise LayerError(INVALID_ARGS, "a plan needs at least one [[agent]]")
    agents: list[AgentSpec] = []
    for index, raw in enumerate(raw_agents):
        name = raw.get("name")
        if not isinstance(name, str) or not _NAME.match(name):
            raise LayerError(INVALID_ARGS, f"agent[{index}] name must be lower-case, a-z0-9-")
        task = raw.get("task")
        if not isinstance(task, str) or not task.strip():
            raise LayerError(INVALID_ARGS, f"agent {name} needs a task")
        agents.append(AgentSpec(
            name=name,
            task=task.strip(),
            after=tuple(raw.get("after", ())),
            model=raw.get("model", defaults.get("model")),
            timeout=float(raw.get("timeout", defaults.get("timeout", 600))),
            retries=int(raw.get("retries", defaults.get("retries", 0))),
        ))
    names = [a.name for a in agents]
    if len(set(names)) != len(names):
        raise LayerError(INVALID_ARGS, "agent names must be unique")
    for agent in agents:
        missing = set(agent.after) - set(names)
        if missing:
            raise LayerError(INVALID_ARGS, f"{agent.name} waits for unknown {sorted(missing)}")
    plan = Plan(tuple(agents), int(defaults.get("concurrency", 4)))
    plan.waves()
    return plan


def load_plan(path: str) -> Plan:
    return parse_plan(Path(path).read_text(encoding="utf-8"))
