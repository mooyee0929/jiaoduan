"""Several agents at once on one layer: does running them together cost correctness?

    uv run python -m bench.parallel              # 3 rounds of serial, then parallel
    uv run python -m bench.parallel --rounds 5

No model is involved: each agent runs its task's scripted solution (`bench.solutions`), so
what is measured is the coordination layer, not an agent's skill. Every agent gets its own
browser window (or the fixture app) on one shared engine, the way agents sharing a daemon do.

Reported per mode: wall time, how many tasks the checker passed, how many claims were refused
(an agent reaching another agent's surface), and whether the person's frontmost app and
pointer moved. Web pages are headless and the fixture app never activates, so neither should.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import subprocess
import tempfile
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from bench.check import Task, check, load_tasks
from bench.fixture_server import serve
from bench.fixtures.mac.launch import fixture_app
from bench.solutions import SOLUTIONS
from jiaoduan.adapters.registry import AdapterRegistry
from jiaoduan.backends.web.backend import WebBackend, WebOptions
from jiaoduan.engine import Engine
from jiaoduan.errors import LayerError
from jiaoduan.gate import Gate, Policy

TASKS = ("contact-send", "application-fill", "checkout-wizard", "mac-fill")
RESUME = b"%PDF-1.4\n% benchmark resume\n"


@dataclass(frozen=True)
class Outcome:
    task: str
    ok: bool
    reasons: tuple[str, ...]
    refused_claims: int


@dataclass(frozen=True)
class Round:
    mode: str
    seconds: float
    outcomes: tuple[Outcome, ...]
    desktop_moved: bool | None  # None: a person used the machine meanwhile, so unknown


@dataclass(frozen=True)
class Desktop:
    frontmost: str
    pointer: tuple[float, float]


def desktop() -> Desktop:
    import Quartz

    front = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True, check=True)
    location = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    return Desktop(front.stdout.strip(), (round(location.x, 1), round(location.y, 1)))


def seconds_since_a_person_acted() -> float:
    import Quartz

    return float(Quartz.CGEventSourceSecondsSinceLastEventType(
        Quartz.kCGEventSourceStateHIDSystemState, Quartz.kCGAnyInputEventType
    ))


@dataclass(frozen=True)
class Stage:
    """What the tasks run against: a fixture server, the mac app, and how to read them."""

    targets: Mapping[str, str]
    resume: Path
    recorded: Callable[[], Mapping[str, Any]]


@contextmanager
def stage(tasks: Sequence[Task], home: Path) -> Iterator[Stage]:
    resume = home / "resume.pdf"
    resume.write_bytes(RESUME)
    with ExitStack() as stack:
        host, recorder = stack.enter_context(serve())
        targets: dict[str, str] = {}
        app = None
        for task in tasks:
            if task.platform == "mac":
                app = stack.enter_context(fixture_app(home / f"{task.id}.json"))
                targets[task.id] = app.target
            else:
                targets[task.id] = f"http://{host}/{task.page}"

        def recorded() -> Mapping[str, Any]:
            snapshot = dict(recorder.snapshot())
            if app is not None:
                snapshot["state"] = {**snapshot["state"], "mac": app.state()}
            return snapshot

        yield Stage(targets, resume, recorded)


async def _one(engine: Engine, task: Task, where: Stage) -> tuple[str, int]:
    agent = f"agent-{task.id}"
    refused = 0
    try:
        answer = await SOLUTIONS[task.id](engine, agent, where.targets[task.id], where.resume)
    except LayerError as exc:
        if exc.code == "SURFACE_CLAIMED":
            refused += 1
        return f"{exc.code}: {exc.message}", refused
    return answer, refused


async def _round(mode: str, tasks: Sequence[Task]) -> Round:
    with tempfile.TemporaryDirectory(prefix="alp-", dir="/tmp") as raw:
        home = Path(raw)
        with stage(tasks, home) as where:
            web = WebBackend(WebOptions(home / "profile", headless=True, mock_keychain=True))
            await web.start()
            from jiaoduan.backends.mac.backend import MacBackend

            engine = Engine(
                {"web": web, "mac": MacBackend()}, gate=Gate(Policy.ALLOW),
                adapters=AdapterRegistry(()),
            )
            before = desktop()
            started = time.monotonic()
            try:
                if mode == "parallel":
                    results = await asyncio.gather(*(_one(engine, t, where) for t in tasks))
                else:
                    results = [await _one(engine, t, where) for t in tasks]
            finally:
                seconds = time.monotonic() - started
                await engine.stop()
            after = desktop()
            # A person moving the mouse is not the layer's doing; then the check says nothing.
            person_acted = seconds_since_a_person_acted() < seconds + 1
            time.sleep(0.3)
            snapshot = where.recorded()
    outcomes = []
    for task, (answer, refused) in zip(tasks, results, strict=True):
        verdict = check(task, snapshot, answer)
        outcomes.append(Outcome(task.id, verdict.ok, verdict.reasons, refused))
    moved = None if person_acted else before != after
    return Round(mode, seconds, tuple(outcomes), moved)


def summary(rounds: Sequence[Round]) -> str:
    lines = [
        "| mode | rounds | wall s (median) | tasks passed | refused claims | desktop moved |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for mode in ("serial", "parallel"):
        mine = [r for r in rounds if r.mode == mode]
        if not mine:
            continue
        passed = sum(o.ok for r in mine for o in r.outcomes)
        total = sum(len(r.outcomes) for r in mine)
        refused = sum(o.refused_claims for r in mine for o in r.outcomes)
        known = [r for r in mine if r.desktop_moved is not None]
        moved = sum(bool(r.desktop_moved) for r in known)
        unknown = len(mine) - len(known)
        median = statistics.median(r.seconds for r in mine)
        desk = f"{moved}/{len(known)}" + (f" ({unknown} not measurable)" if unknown else "")
        lines.append(f"| {mode} | {len(mine)} | {median:.1f} | {passed}/{total} | {refused} "
                     f"| {desk} |")
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bench.parallel")
    parser.add_argument("--rounds", type=int, default=3)
    options = parser.parse_args(argv)
    by_id = {t.id: t for t in load_tasks()}
    tasks = [by_id[t] for t in TASKS]
    rounds: list[Round] = []
    for number in range(options.rounds):
        for mode in ("serial", "parallel"):
            result = asyncio.run(_round(mode, tasks))
            rounds.append(result)
            failed = [f"{o.task}: {'; '.join(o.reasons)[:100]}" for o in result.outcomes
                      if not o.ok]
            print(f"[{number + 1}/{options.rounds}] {mode}: {result.seconds:.1f}s, "
                  f"{sum(o.ok for o in result.outcomes)}/{len(result.outcomes)} ok"
                  + (f" | {' | '.join(failed)}" if failed else ""), flush=True)
    print()
    print(summary(rounds))
    return 0 if all(o.ok for r in rounds for o in r.outcomes) else 1


if __name__ == "__main__":
    raise SystemExit(main())
