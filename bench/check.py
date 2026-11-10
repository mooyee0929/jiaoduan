"""Decide whether a run succeeded, from what the fixture page posted and the final answer."""

from __future__ import annotations

import re
import tomllib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

TASKS = Path(__file__).parent / "tasks.toml"
_MONEY = re.compile(r"\$\s?[\d,]+(?:\.\d{2})?")
_NUMBER = re.compile(r"(?<![\w.])\d[\d,]*(?![\w.]\d)")


@dataclass(frozen=True)
class Task:
    id: str
    kind: str
    page: str
    prompt: str
    expect: Mapping[str, Any]
    commit: str = "ask"
    platform: str = "web"


@dataclass(frozen=True)
class Verdict:
    ok: bool
    reasons: tuple[str, ...]


def load_tasks(path: Path = TASKS) -> list[Task]:
    data = tomllib.loads(path.read_text(encoding="utf-8"))
    return [
        Task(str(t["id"]), str(t["kind"]), str(t["page"]), str(t["prompt"]), t["expect"],
             str(t.get("commit", "ask")), str(t.get("platform", "web")))
        for t in data["task"]
    ]


def _compare(where: str, got: Mapping[str, Any], wanted: Mapping[str, Any]) -> list[str]:
    return [
        f"{where}.{key}: wanted {value!r}, got {got.get(key)!r}"
        for key, value in wanted.items() if got.get(key) != value
    ]


def check(task: Task, recorded: Mapping[str, Any], answer: str) -> Verdict:
    """``recorded`` is the fixture server's snapshot: latest state and submissions per page."""
    reasons: list[str] = []
    expect = task.expect
    state: Mapping[str, Any] = recorded.get("state", {})
    submissions: Mapping[str, Sequence[Any]] = recorded.get("submissions", {})
    for page, wanted in expect.get("state", {}).items():
        reasons += _compare(f"state.{page}", state.get(page, {}), wanted)
    for page, wanted_list in expect.get("submissions", {}).items():
        got_list = list(submissions.get(page, []))
        if len(got_list) != len(wanted_list):
            reasons.append(f"{page}: wanted {len(wanted_list)} submission(s), got {len(got_list)}")
            continue
        for number, (got, wanted) in enumerate(zip(got_list, wanted_list, strict=True)):
            reasons += _compare(f"{page}[{number}]", got, wanted)
    for page in expect.get("no_submissions", []):
        if submissions.get(page):
            reasons.append(f"{page} was submitted but should not have been")
    wanted_answer = expect.get("answer")
    if wanted_answer is not None:
        found = _MONEY.findall(answer)
        if [m.replace(" ", "") for m in found] != [wanted_answer]:
            reasons.append(f"answer: wanted {wanted_answer}, got {answer.strip()[:80]!r}")
    wanted_number = expect.get("number")
    if wanted_number is not None:
        numbers = [int(n.replace(",", "")) for n in _NUMBER.findall(answer)]
        if numbers != [int(wanted_number)]:
            reasons.append(f"answer: wanted {wanted_number}, got {answer.strip()[:80]!r}")
    return Verdict(not reasons, tuple(reasons))
