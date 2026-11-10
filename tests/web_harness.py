"""Shared helpers for tests that drive a real headless Chrome."""

from __future__ import annotations

import asyncio
import http.server
import tempfile
import threading
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from pathlib import Path

from agentlayer.backends.web.backend import WebBackend, WebOptions
from agentlayer.engine import Engine
from agentlayer.gate import Gate, Policy

PAGES = Path(__file__).parent / "pages"


@asynccontextmanager
async def engine_with_chrome(policy: Policy = Policy.ASK) -> AsyncIterator[Engine]:
    with tempfile.TemporaryDirectory(prefix="al-test-") as tmp:
        backend = WebBackend(WebOptions(Path(tmp), headless=True, mock_keychain=True))
        await backend.start()
        engine = Engine({"web": backend}, gate=Gate(policy))
        try:
            yield engine
        finally:
            await engine.stop()


class _Quiet(http.server.SimpleHTTPRequestHandler):
    log = []  # type: ignore[var-annotated]

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, directory=str(PAGES), **kwargs)  # type: ignore[arg-type]

    def log_message(self, format: str, *args: object) -> None:
        return None

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        self.rfile.read(length)
        status = 500 if self.path.endswith("/fail") else 200
        self.send_response(status)
        self.send_header("Content-Length", "0")
        self.end_headers()


@contextmanager
def pages_server() -> Iterator[str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Quiet)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        thread.join()


def run(coroutine: object) -> None:
    asyncio.run(coroutine)  # type: ignore[arg-type]
