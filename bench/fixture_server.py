"""Serves the fixture pages and records what they post, for tests and for the benchmark.

Recorded state is never served back over HTTP, so an agent under test cannot read the answer.
"""

from __future__ import annotations

import http.server
import json
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

FIXTURES = Path(__file__).parent / "fixtures" / "web"
SUGGEST_DELAY = 0.35
CITIES = (
    "Lyon, France", "Lyons, Colorado, USA", "Lyon Mountain, New York, USA", "Lisbon, Portugal",
    "Linz, Austria", "Leeds, United Kingdom", "Lille, France", "Lima, Peru", "Lagos, Nigeria",
    "Lucerne, Switzerland", "Ljubljana, Slovenia", "Leipzig, Germany", "Lausanne, Switzerland",
    "Kyoto, Japan", "Osaka, Japan", "Taipei, Taiwan", "Tainan, Taiwan", "Toronto, Canada",
)


def suggest(query: str, limit: int = 6) -> list[str]:
    needle = query.strip().casefold()
    return [c for c in CITIES if needle and needle in c.casefold()][:limit]


@dataclass
class Recorder:
    """The latest state and every submission, per fixture page."""

    state: dict[str, dict[str, Any]] = field(default_factory=dict)
    submissions: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    failed_once: set[str] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, path: str, body: dict[str, Any]) -> int:
        """Store what a page posted; the HTTP status to answer with."""
        page = str(body.get("page", "unknown"))
        with self.lock:
            if path == "/api/state":
                self.state[page] = dict(body.get("store", {}))
            elif path == "/api/submit":
                self.submissions.setdefault(page, []).append(dict(body.get("store", {})))
            elif path == "/api/save":
                # The first save of each page fails, the way a flaky backend does, while the
                # page itself has already said "Saved".
                if page not in self.failed_once:
                    self.failed_once.add(page)
                    return 500
                self.submissions.setdefault(page, []).append(dict(body.get("store", {})))
        return 200

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
            parts = urlsplit(self.path)
            if parts.path == "/api/cities":
                time.sleep(SUGGEST_DELAY)
                query = parse_qs(parts.query).get("q", [""])[0]
                payload = json.dumps(suggest(query)).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(payload)
                return
            if parts.path.startswith("/api/"):
                self.send_error(404)  # recorded state is never served back
                return
            super().do_GET()

        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            status = 500
            if not self.path.endswith("/fail"):
                try:
                    body = json.loads(raw or b"{}")
                except json.JSONDecodeError:
                    body = {}
                status = recorder.record(self.path, body) if isinstance(body, dict) else 400
            self.send_response(status)
            self.send_header("Content-Length", "0")
            self.send_header("Access-Control-Allow-Origin", "*")
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
