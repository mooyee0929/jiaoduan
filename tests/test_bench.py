from __future__ import annotations

import json
from pathlib import Path

import pytest

from bench.check import Task, check, load_tasks
from bench.report import aggregate, bootstrap_mean, table, wilson
from bench.run import claude_command, load_arms, mcp_config, parse_stream


def contact_task() -> Task:
    return next(t for t in load_tasks() if t.id == "contact-send")


def test_every_task_and_arm_file_loads() -> None:
    tasks = {t.id for t in load_tasks()}
    assert tasks == {"contact-send", "application-fill", "orders-lookup"}
    arms = load_arms()
    assert set(arms) == {"A", "B", "C", "D"}
    reading = next(t for t in load_tasks() if t.kind == "read")
    assert not arms["D"].applies(reading) and arms["C"].applies(reading)


def test_no_arm_may_run_page_javascript() -> None:
    for arm in load_arms().values():
        assert not {"browser_evaluate", "browser_run_code_unsafe"} & set(arm.tools)


def test_commands_disable_built_in_tools_and_other_servers(tmp_path: Path) -> None:
    arm = load_arms()["C"]
    command = claude_command(arm, tmp_path / "c.json", "go", "claude-sonnet-5-5")
    assert command[command.index("--tools") + 1] == ""
    assert "--strict-mcp-config" in command
    allowed = command[command.index("--allowedTools") + 1:command.index("--disallowedTools")]
    hidden = command[command.index("--disallowedTools") + 1:command.index("--setting-sources")]
    assert "mcp__agent-layer__observe" in allowed
    assert "mcp__agent-layer__fill" in hidden and "mcp__agent-layer__fill" not in allowed
    vision = load_arms()["A"]
    assert "browser_snapshot" in vision.hidden and "browser_evaluate" in vision.hidden
    assert "--snapshot-mode=none" in vision.command
    env = mcp_config(arm, tmp_path)["mcpServers"]["agent-layer"]["env"]
    assert env["AGENTLAYER_COMMIT"] == "ask"
    tasks = {t.id: t.commit for t in load_tasks()}
    assert tasks == {"contact-send": "allow", "application-fill": "ask", "orders-lookup": "ask"}
    assert env["AGENTLAYER_PROFILE"].startswith(str(tmp_path))


def test_a_submission_is_checked_field_by_field() -> None:
    task = contact_task()
    good = {"submissions": {"contact": [
        {"topic": "Support", "message": "The export button does nothing."}
    ]}}
    assert check(task, good, "").ok
    wrong = {"submissions": {"contact": [{"topic": "Sales", "message": "x"}]}}
    verdict = check(task, wrong, "")
    assert not verdict.ok and any("topic" in r for r in verdict.reasons)
    assert "wanted 1 submission(s), got 0" in check(task, {}, "").reasons[0]


def test_a_form_task_fails_if_the_form_was_submitted() -> None:
    task = next(t for t in load_tasks() if t.id == "application-fill")
    state = {"application": dict(task.expect["state"]["application"])}
    assert check(task, {"state": state}, "done").ok
    submitted = check(task, {"state": state, "submissions": {"application": [{}]}}, "done")
    assert not submitted.ok


@pytest.mark.parametrize("answer, ok", [
    ("$1,997.18", True), ("The total is $1,997.18.", True), ("$1,997.18 or $12.00", False),
    ("1997.18", False),
])
def test_the_answer_must_be_exactly_one_amount(answer: str, ok: bool) -> None:
    task = next(t for t in load_tasks() if t.id == "orders-lookup")
    assert check(task, {}, answer).ok is ok


def test_stream_json_totals_tokens_tool_calls_and_cost() -> None:
    lines = [
        json.dumps({"type": "system", "subtype": "init"}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "text", "text": "looking"}, {"type": "tool_use", "name": "observe"},
        ]}}),
        json.dumps({"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "fill"}, {"type": "tool_use", "name": "act"},
        ]}}),
        "not json",
        json.dumps({"type": "result", "result": "done", "num_turns": 4, "total_cost_usd": 0.05,
                    "usage": {"input_tokens": 10, "cache_creation_input_tokens": 200,
                              "cache_read_input_tokens": 3000, "output_tokens": 90}}),
    ]
    usage = parse_stream(lines, 12.0)
    assert usage.tool_calls == 3 and usage.input_tokens == 3210 and usage.output_tokens == 90
    assert usage.cost_usd == 0.05 and usage.turns == 4 and usage.answer == "done"
    assert parse_stream([], 1.0).error == "no result event"


def test_wilson_interval_stays_inside_zero_and_one() -> None:
    low, high = wilson(0, 5)
    assert low == 0.0 and 0.4 < high < 0.5
    low, high = wilson(5, 5)
    assert 0.5 < low < 0.6 and high == 1.0


def test_bootstrap_is_reproducible_and_brackets_the_mean() -> None:
    values = [100.0, 120.0, 90.0, 110.0, 105.0]
    first, second = bootstrap_mean(values), bootstrap_mean(values)
    assert first == second
    assert first[0] <= sum(values) / len(values) <= first[1]


def test_the_report_compares_each_arm_with_the_snapshot_baseline() -> None:
    records = [
        {"task": "t", "arm": "B", "ok": True, "input_tokens": 1000, "tool_calls": 10,
         "seconds": 30, "cost_usd": 0.1},
        {"task": "t", "arm": "D", "ok": True, "input_tokens": 400, "tool_calls": 3,
         "seconds": 12, "cost_usd": 0.04},
    ]
    text = table(aggregate(records))
    assert "| t | D | 1/1" in text and "-60%" in text
