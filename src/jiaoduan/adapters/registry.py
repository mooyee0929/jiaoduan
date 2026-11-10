"""Find installed adapters and decide which apply to a surface."""

from __future__ import annotations

import fnmatch
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path

from jiaoduan import paths
from jiaoduan.adapters.schema import Adapter, AdapterInvalid, load
from jiaoduan.model import Surface

log = logging.getLogger(__name__)

ADAPTERS_ENV = "JIAODUAN_ADAPTERS"


@dataclass
class AdapterRegistry:
    directories: tuple[Path, ...]
    _cache: dict[Path, tuple[float, Adapter]] = field(default_factory=dict)
    problems: dict[Path, str] = field(default_factory=dict)

    @classmethod
    def from_env(cls) -> AdapterRegistry:
        extra = os.environ.get(ADAPTERS_ENV, "")
        directories = [Path(p) for p in extra.split(os.pathsep) if p]
        return cls(tuple([paths.home() / "adapters", *directories]))

    def all(self) -> list[Adapter]:
        """Every valid adapter; files are re-read when they change on disk."""
        found: list[Adapter] = []
        for directory in self.directories:
            if not directory.is_dir():
                continue
            for path in sorted(directory.glob("*.toml")):
                adapter = self._load(path)
                if adapter is not None:
                    found.append(adapter)
        return found

    def _load(self, path: Path) -> Adapter | None:
        mtime = path.stat().st_mtime
        cached = self._cache.get(path)
        if cached is not None and cached[0] == mtime:
            return cached[1]
        try:
            adapter = load(path)
        except AdapterInvalid as exc:
            self.problems[path] = exc.message
            log.warning("ignoring adapter: %s", exc.message)
            return None
        self.problems.pop(path, None)
        self._cache[path] = (mtime, adapter)
        return adapter

    def matching(self, surface: Surface) -> list[Adapter]:
        return [a for a in self.all() if applies(a, surface)]


def applies(adapter: Adapter, surface: Surface) -> bool:
    """A URL pattern for pages, an app name for everything without a URL."""
    if "://" in surface.location:
        return adapter.url is not None and fnmatch.fnmatchcase(surface.location, adapter.url)
    return adapter.app is not None and adapter.app.casefold() == surface.location.casefold()
