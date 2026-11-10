"""Start the Cocoa fixture app as its own process and read the state it writes."""

from __future__ import annotations

import json
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
START_WAIT = 10.0


@dataclass(frozen=True)
class RunningFixture:
    pid: int
    state_path: Path

    @property
    def target(self) -> str:
        return f"pid:{self.pid}"

    def state(self) -> dict[str, Any]:
        return dict(json.loads(self.state_path.read_text(encoding="utf-8")))


@contextmanager
def fixture_app(state: Path) -> Iterator[RunningFixture]:
    process = subprocess.Popen(
        [sys.executable, "-m", "bench.fixtures.mac.fixture_app", str(state)], cwd=ROOT
    )
    try:
        deadline = time.monotonic() + START_WAIT
        while not state.exists():
            if process.poll() is not None:
                raise RuntimeError(f"the fixture app exited with {process.returncode}")
            if time.monotonic() > deadline:
                raise RuntimeError("the fixture app did not write its state in time")
            time.sleep(0.05)
        yield RunningFixture(process.pid, state)
    finally:
        process.terminate()
        process.wait(timeout=5)
