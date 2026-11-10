"""The daemon over its real Unix socket, with scripted backends instead of a browser."""

from __future__ import annotations

import asyncio
import io
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

from agentlayer.adapters.registry import AdapterRegistry
from agentlayer.cli import confirm_at_terminal
from agentlayer.client import Client
from agentlayer.daemon import Daemon
from agentlayer.engine import Engine
from agentlayer.errors import LayerError
from agentlayer.gate import Gate
from agentlayer.model import Surface, SurfaceId
from tests.fakes import FakeBackend, button, form, textbox


class Windows(FakeBackend):
    """One fake page per open call, so several agents can each hold their own."""

    def __init__(self) -> None:
        super().__init__([])
        self.opened: list[SurfaceId] = []
        self.lock = asyncio.Lock()

    async def open(self, target: str, *, guard: bool = True) -> Surface:
        async with self.lock:
            surface = SurfaceId(f"fake:{len(self.opened) + 1}")
            self.opened.append(surface)
        return await self.describe(surface)

    async def surfaces(self) -> list[Surface]:
        return [await self.describe(s) for s in self.opened]

    async def read_tree(self, surface: SurfaceId) -> list:  # type: ignore[type-arg]
        number = int(surface.split(":")[1])
        return [form(number * 100, f"Form {number}", textbox(number * 100 + 1, "Name"),
                     button(number * 100 + 2, "Next"))]


@asynccontextmanager
async def daemon(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Windows]:
    home = Path("/tmp") / f"al-{os.getpid()}-{tmp_path.name[-6:]}"
    home.mkdir(mode=0o700, exist_ok=True)
    monkeypatch.setenv("AGENTLAYER_HOME", str(home))
    backend = Windows()
    engine = Engine({"fake": backend}, gate=Gate(), adapters=AdapterRegistry(()))
    server = Daemon(engine, idle_seconds=60)
    socket = home / "d.sock"
    task = asyncio.create_task(server.run(socket))
    for _ in range(100):
        if socket.exists():
            break
        await asyncio.sleep(0.01)
    try:
        yield backend
    finally:
        server.stop()
        await task
        home.rmdir()


def test_four_agents_work_in_parallel_without_conflicts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def worker(name: str) -> str:
        async with Client(name, spawn=False) as client:
            opened = await client.call("open", target="fake://x", kind="fake")
            surface = opened.split()[0]
            seen = await client.call("observe", surface=surface)
            ref = next(line for line in seen.splitlines() if '"Next"' in line)
            await client.call("act", ref=ref.split("[")[1].split("]")[0], action="press")
            return surface

    async def scenario() -> tuple[list[str], int]:
        async with daemon(tmp_path, monkeypatch) as backend:
            surfaces = await asyncio.gather(*(worker(f"w{i}") for i in range(4)))
            return list(surfaces), len(backend.performed)

    surfaces, presses = asyncio.run(scenario())
    assert len(set(surfaces)) == 4
    assert presses == 4


def test_a_dropped_connection_releases_its_claims(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> str:
        async with daemon(tmp_path, monkeypatch):
            async with Client("first", spawn=False) as first:
                surface = (await first.call("open", target="fake://x", kind="fake")).split()[0]
                async with Client("second", spawn=False) as second:
                    with pytest.raises(LayerError) as caught:
                        await second.call("observe", surface=surface)
                    assert caught.value.code == "SURFACE_CLAIMED"
            await asyncio.sleep(0.05)
            async with Client("second", spawn=False) as second:
                return await second.call("observe", surface=surface)

    assert 'textbox "Name"' in asyncio.run(scenario())


def test_an_agent_connection_cannot_use_operator_operations(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def scenario() -> None:
        async with daemon(tmp_path, monkeypatch), Client("w1", spawn=False) as client:
            with pytest.raises(LayerError) as caught:
                await client.call("gate_approve", id="g1")
            assert "unknown operation" in caught.value.message

    asyncio.run(scenario())


class Terminal(io.TextIOBase):
    """Separate input and output, like a real terminal."""

    def __init__(self, typed: str) -> None:
        self.typed = io.StringIO(typed)
        self.shown = io.StringIO()

    def readline(self, size: int | None = -1) -> str:  # type: ignore[override]
        return self.typed.readline()

    def write(self, text: str) -> int:
        return self.shown.write(text)


def test_approval_needs_a_yes_typed_at_a_terminal() -> None:
    terminal = Terminal("y\n")
    confirm_at_terminal("approve g1?", lambda: terminal)  # type: ignore[arg-type,return-value]
    assert terminal.shown.getvalue() == "approve g1? [y/N] "
    with pytest.raises(LayerError) as declined:
        confirm_at_terminal("approve g1?", lambda: Terminal("\n"))  # type: ignore[arg-type,return-value]
    assert declined.value.code == "COMMIT_DENIED"

    def no_terminal() -> Terminal:
        raise OSError(6, "Device not configured")

    with pytest.raises(LayerError) as refused:
        confirm_at_terminal("approve g1?", no_terminal)
    assert refused.value.code == "PERMISSION_DENIED"
