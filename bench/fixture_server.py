"""Serves the fixture pages and records what they post, for tests and for the benchmark.

Recorded state is never served back over HTTP, so an agent under test cannot read the answer.
"""

from __future__ import annotations

import http.server
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

FIXTURES = Path(__file__).parent / "fixtures" / "web"


@dataclass
class Recorder:
    """The latest state and every submission, per fixture page."""

    state: dict[str, dict[str, Any]] = field(default_factory=dict)
    submissions: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, path: str, body: dict[str, Any]) -> None:
        page = str(body.get("page", "unknown"))
        with self.lock:
            if path == "/api/state":
                self.state[page] = dict(body.get("store", {}))
            elif path == "/api/submit":
                self.submissions.setdefault(page, []).append(dict(body.get("store", {})))

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            return {"state": json.loads(json.dumps(self.state)),
                    "submissions": json.loads(json.dumps(self.submissions))}


def _handler(recorder: Recorder, root: Path) -> type[http.server.SimpleHTTPRequestHandler]:
    class Handler(http.server.SimpleHTTPRequestHandler):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, directory=str(root), **kwargs)

        def log_message(self, format: str, *args: Any) -> None:
            return None

        def do_GET(self) -> None:  # noqa: N802
            if self.path.startswith("/api/"):
                self.send_error(404)
                return
            super().do_GET()

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            if self.path.endswith("/fail"):
                self.send_response(500)
            else:
                try:
                    body = json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    body = {}
                if isinstance(body, dict):
                    recorder.record(self.path, body)
                self.send_response(200)
            self.send_header("Content-Length", "0")
            self.end_headers()

    return Handler


@contextmanager
def serve(root: Path = FIXTURES) -> Iterator[tuple[str, Recorder]]:
    """``host:port`` and the recorder, for as long as the block runs."""
    recorder = Recorder()
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _handler(recorder, root))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"127.0.0.1:{server.server_address[1]}", recorder
    finally:
        server.shutdown()
        thread.join()
