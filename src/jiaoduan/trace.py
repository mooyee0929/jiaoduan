"""The action log. Schema version 1: read by the adapter recorder, the benchmark and reports.

Typed values are never written, only their length and which field they went to.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from jiaoduan.model import Locator

SCHEMA = 1
TRACE_ENV = "JIAODUAN_TRACE"


@dataclass(frozen=True)
class TraceRecord:
    agent: str
    surface: str
    op: str
    ok: bool
    ms: int
    action: str | None = None
    locator: Locator | None = None
    text_len: int | None = None
    delivery: str | None = None
    evidence: str | None = None
    code: str | None = None
    url: str | None = None


Sink = Callable[[str], None]


def _locator_json(locator: Locator) -> dict[str, object]:
    return {
        "role": locator.role.value,
        "name": locator.name,
        "scope": [[role.value, name] for role, name in locator.scope],
    }


def encode(record: TraceRecord, at: float) -> str:
    payload: dict[str, object] = {
        "v": SCHEMA, "at": round(at, 3), "agent": record.agent, "surface": record.surface,
        "op": record.op, "ok": record.ok, "ms": record.ms,
    }
    optional: dict[str, object | None] = {
        "action": record.action,
        "locator": _locator_json(record.locator) if record.locator else None,
        "text_len": record.text_len,
        "delivery": record.delivery,
        "evidence": record.evidence,
        "code": record.code,
        "url": record.url,
    }
    payload.update({k: v for k, v in optional.items() if v is not None})
    return json.dumps(payload, ensure_ascii=False)


class Trace:
    def __init__(self, sink: Sink | None) -> None:
        self._sink = sink

    @classmethod
    def from_env(cls) -> Trace:
        path = os.environ.get(TRACE_ENV)
        return cls(file_sink(Path(path)) if path else None)

    def write(self, record: TraceRecord) -> None:
        if self._sink is not None:
            self._sink(encode(record, time.time()))


def file_sink(path: Path) -> Sink:
    path.parent.mkdir(parents=True, exist_ok=True)

    def write(line: str) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    return write
