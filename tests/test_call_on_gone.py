from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from jiaoduan.backends.web.backend import WebBackend, WebOptions
from jiaoduan.backends.web.cdp import CdpError
from jiaoduan.backends.web.tree import WebHandle
from jiaoduan.errors import STALE_REF, LayerError
from jiaoduan.model import SurfaceId


class _Session:
    session_id = "s"

    def __init__(self, error: str) -> None:
        self.error = error

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        raise CdpError(method, -32000, self.error)


class _Page:
    def __init__(self, error: str) -> None:
        self.session = _Session(error)

    def session_for(self, session_id: str) -> _Session:
        return self.session


def _call(tmp_path: Path, error: str) -> None:
    backend = WebBackend(WebOptions(tmp_path))
    page = _Page(error)
    backend._page = lambda surface: page  # type: ignore[assignment, method-assign, return-value]
    asyncio.run(backend.call_on(SurfaceId("web:1"), WebHandle("s", 7), "function() {}"))


def test_a_removed_node_is_a_stale_ref(tmp_path: Path) -> None:
    with pytest.raises(LayerError) as caught:
        _call(tmp_path, "No node with given id found")
    assert caught.value.code == STALE_REF


def test_other_browser_errors_pass_through(tmp_path: Path) -> None:
    with pytest.raises(CdpError):
        _call(tmp_path, "Internal error")
