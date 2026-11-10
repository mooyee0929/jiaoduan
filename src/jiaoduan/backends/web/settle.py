"""Decide when a page has finished reacting to an action."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from jiaoduan.model import Mark, RequestRecord, Settle

JsonObject = dict[str, Any]

QUIET_SECONDS = 0.15
POLL_SECONDS = 0.02
_IGNORED_TYPES = frozenset({"WebSocket", "EventSource", "Media", "Ping"})
_REPORTED_TYPES = frozenset({"XHR", "Fetch", "Document"})
_MAX_REMEMBERED = 200


@dataclass
class _Request:
    sequence: int
    method: str
    path: str
    resource: str
    status: int | None = None
    finished: bool = False


def path_of(url: str) -> str:
    """The URL's path only: queries and fragments often carry tokens."""
    parts = urlsplit(url)
    return parts.path or "/"


class SettleTracker:
    """Counts activity on one page. Fed by CDP events, read by ``wait``."""

    def __init__(self, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._sequence = 0
        self._last_activity = clock()
        self._requests: dict[str, _Request] = {}
        self._loading = False
        self._navigations: list[tuple[int, str]] = []
        self._notes: list[tuple[int, str]] = []
        self.main_frame_id: str | None = None

    def mark(self) -> Mark:
        self._sequence += 1
        return Mark(self._sequence, self._clock())

    def touch(self) -> None:
        self._last_activity = self._clock()

    def note(self, text: str) -> None:
        self._notes.append((self._sequence, text))
        self.touch()

    def on_request(self, params: JsonObject) -> None:
        resource = str(params.get("type", ""))
        if resource in _IGNORED_TYPES:
            return
        request = params["request"]
        self._requests[params["requestId"]] = _Request(
            self._sequence, str(request["method"]), path_of(str(request["url"])), resource
        )
        self.touch()
        self._trim()

    def on_response(self, params: JsonObject) -> None:
        request = self._requests.get(params["requestId"])
        if request is not None:
            request.status = int(params["response"]["status"])

    def on_finished(self, params: JsonObject) -> None:
        request = self._requests.get(params["requestId"])
        if request is not None:
            request.finished = True
            self.touch()

    def on_frame_loading(self, params: JsonObject, loading: bool) -> None:
        if params.get("frameId") == self.main_frame_id:
            self._loading = loading
            self.touch()

    def on_navigated(self, params: JsonObject) -> None:
        frame = params["frame"]
        if frame["id"] == self.main_frame_id or "parentId" not in frame:
            self.main_frame_id = frame["id"]
            self._navigations.append((self._sequence, str(frame["url"])))
            self.touch()

    def _trim(self) -> None:
        if len(self._requests) <= _MAX_REMEMBERED:
            return
        for key in [k for k, r in self._requests.items() if r.finished][:_MAX_REMEMBERED // 2]:
            del self._requests[key]

    def _busy(self, mark: Mark) -> bool:
        if self._loading:
            return True
        return any(r.sequence >= mark.sequence and not r.finished for r in self._requests.values())

    def poll(self, mark: Mark, quiet: float = QUIET_SECONDS) -> bool:
        now = self._clock()
        return not self._busy(mark) and now - max(mark.at, self._last_activity) >= quiet

    def result(self, mark: Mark, settled: bool) -> Settle:
        navigation = next(
            (url for sequence, url in reversed(self._navigations) if sequence >= mark.sequence),
            None,
        )
        reported = tuple(
            RequestRecord(r.method, r.path, r.status)
            for r in self._requests.values()
            if r.sequence >= mark.sequence and r.resource in _REPORTED_TYPES
            and (r.method != "GET" or r.resource != "Document")
        )
        notes = tuple(text for sequence, text in self._notes if sequence >= mark.sequence)
        elapsed = int((self._clock() - mark.at) * 1000)
        return Settle(settled, elapsed, navigation, reported, notes)

    async def wait(self, mark: Mark, budget: float) -> Settle:
        deadline = mark.at + budget
        while True:
            if self.poll(mark):
                return self.result(mark, True)
            if self._clock() >= deadline:
                return self.result(mark, False)
            await asyncio.sleep(POLL_SECONDS)
