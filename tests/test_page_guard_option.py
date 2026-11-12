from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from jiaoduan.backends.web.backend import WebBackend, WebOptions
from jiaoduan.daemon import web_options
from jiaoduan.gate import POLICY_ENV


@pytest.mark.parametrize(
    ("policy", "guarded"), [(None, True), ("ask", True), ("deny", True), ("allow", False)]
)
def test_page_guard_follows_commit_policy(
    monkeypatch: pytest.MonkeyPatch, policy: str | None, guarded: bool
) -> None:
    if policy is None:
        monkeypatch.delenv(POLICY_ENV, raising=False)
    else:
        monkeypatch.setenv(POLICY_ENV, policy)
    assert web_options().page_guard is guarded


class _Session:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        self.sent.append(method)
        return {"identifier": "1"}


class _Page:
    def __init__(self) -> None:
        self.session = _Session()
        self.guard_script: str | None = None


@pytest.mark.parametrize("guarded", [True, False])
def test_install_guard_respects_option(tmp_path: Path, guarded: bool) -> None:
    backend = WebBackend(WebOptions(tmp_path, page_guard=guarded))
    page = _Page()
    asyncio.run(backend._install_guard(page))  # type: ignore[arg-type]
    assert (page.session.sent == ["Page.addScriptToEvaluateOnNewDocument"]) is guarded
    assert (page.guard_script is not None) is guarded
