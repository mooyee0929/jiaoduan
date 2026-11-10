"""Summarise benchmark results: success with Wilson intervals, cost with bootstrap intervals.

    uv run python -m bench.report bench/results/*.jsonl
"""

from __future__ import annotations

import argparse
import json
import math
import random
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

Z95 = 1.959964
BOOTSTRAP_SAMPLES = 2000
BOOTSTRAP_SEED = 7
BASELINE = "B"


def wilson(successes: int, total: int) -> tuple[float, float]:
    """95% interval for a success rate; sane at 0/n and n/n, unlike the normal approximation."""
    if total == 0:
        return (0.0, 1.0)
    rate = successes / total
    denominator = 1 + Z95**2 / total
    centre = (rate + Z95**2 / (2 * total)) / denominator
    spread = Z95 * math.sqrt(rate * (1 - rate) / total + Z95**2 / (4 * total**2)) / denominator
    return (max(0.0, centre - spread), min(1.0, centre + spread))


def bootstrap_mean(values: Sequence[float], seed: int = BOOTSTRAP_SEED) -> tuple[float, float]:
    """95% percentile interval for the mean. Seeded, so a report is reproducible."""
    if not values:
        return (math.nan, math.nan)
    rng = random.Random(seed)
    means = sorted(
        sum(rng.choices(values, k=len(values))) / len(values) for _ in range(BOOTSTRAP_SAMPLES)
    )
    return (means[int(0.025 * BOOTSTRAP_SAMPLES)], means[int(0.975 * BOOTSTRAP_SAMPLES) - 1])


@dataclass(frozen=True)
class Row:
    task: str
    arm: str
    runs: int
    successes: int
    input_mean: float
    input_interval: tuple[float, float]
    calls_mean: float
    seconds_mean: float
    cost_mean: float | None


def aggregate(records: Sequence[dict[str, Any]]) -> list[Row]:
    groups: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        groups[(record["task"], record["arm"])].append(record)
    rows: list[Row] = []
    for (task, arm), group in sorted(groups.items()):
        tokens = [float(r["input_tokens"]) for r in group]
        costs = [float(r["cost_usd"]) for r in group if r.get("cost_usd") is not None]
        rows.append(Row(
            task=task, arm=arm, runs=len(group), successes=sum(1 for r in group if r["ok"]),
            input_mean=sum(tokens) / len(tokens), input_interval=bootstrap_mean(tokens),
            calls_mean=sum(float(r["tool_calls"]) for r in group) / len(group),
            seconds_mean=sum(float(r["seconds"]) for r in group) / len(group),
            cost_mean=sum(costs) / len(costs) if costs else None,
        ))
    return rows


def table(rows: Sequence[Row]) -> str:
    baseline = {r.task: r for r in rows if r.arm == BASELINE}
    lines = [
        "| task | arm | success (95% CI) | input tokens (95% CI) | vs B | tool calls | s | $ |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        low, high = wilson(row.successes, row.runs)
        tok_low, tok_high = row.input_interval
        base = baseline.get(row.task)
        versus = (
            f"{(row.input_mean / base.input_mean - 1) * 100:+.0f}%"
            if base and base.input_mean and row.arm != BASELINE else ""
        )
        cost = f"{row.cost_mean:.3f}" if row.cost_mean is not None else ""
        lines.append(
            f"| {row.task} | {row.arm} | {row.successes}/{row.runs} ({low:.0%}-{high:.0%}) "
            f"| {row.input_mean:,.0f} ({tok_low:,.0f}-{tok_high:,.0f}) | {versus} "
            f"| {row.calls_mean:.1f} | {row.seconds_mean:.0f} | {cost} |"
        )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bench.report")
    parser.add_argument("files", nargs="+")
    options = parser.parse_args(argv)
    records = [
        json.loads(line) for name in options.files
        for line in Path(name).read_text(encoding="utf-8").splitlines() if line.strip()
    ]
    print(table(aggregate(records)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
