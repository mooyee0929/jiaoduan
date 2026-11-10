"""Who owns what. Held in the daemon's memory, so a dropped connection releases everything."""

from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from jiaoduan.errors import SURFACE_CLAIMED, LayerError
from jiaoduan.model import SurfaceId


class Claims:
    def __init__(self) -> None:
        self._owners: dict[SurfaceId, str] = {}

    def acquire(self, agent: str, surface: SurfaceId) -> None:
        owner = self._owners.get(surface)
        if owner is not None and owner != agent:
            raise LayerError(
                SURFACE_CLAIMED, f"{surface} belongs to agent {owner}",
                hint="open your own surface instead",
            )
        self._owners[surface] = agent

    def owner(self, surface: SurfaceId) -> str | None:
        return self._owners.get(surface)

    def owned_by(self, agent: str) -> list[SurfaceId]:
        return [s for s, a in self._owners.items() if a == agent]

    def release(self, agent: str, surface: SurfaceId) -> bool:
        if self._owners.get(surface) != agent:
            return False
        del self._owners[surface]
        return True

    def release_all(self, agent: str) -> list[SurfaceId]:
        owned = self.owned_by(agent)
        for surface in owned:
            del self._owners[surface]
        return owned

    def forget(self, surface: SurfaceId) -> None:
        self._owners.pop(surface, None)


class SessionLocks:
    """Keyed concurrency limits, for sites where one account cannot act twice at once."""

    def __init__(self) -> None:
        self._semaphores: dict[str, asyncio.Semaphore] = {}
        self._limits: dict[str, int] = defaultdict(int)

    @asynccontextmanager
    async def hold(self, key: str, limit: int) -> AsyncIterator[None]:
        if key not in self._semaphores:
            self._semaphores[key] = asyncio.Semaphore(limit)
            self._limits[key] = limit
        semaphore = self._semaphores[key]
        await semaphore.acquire()
        try:
            yield
        finally:
            semaphore.release()
