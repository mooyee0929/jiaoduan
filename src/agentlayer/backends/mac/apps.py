"""Running apps, looked up fresh each time.

NSWorkspace's app lists are caches refreshed by the main run loop, which a daemon never
spins: an app launched later would stay invisible. Asking per pid is always current.
"""

from __future__ import annotations

import ctypes
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from agentlayer.errors import AMBIGUOUS, NOT_FOUND, LayerError

CHROMIUM_BUNDLES = frozenset({
    "com.google.Chrome", "org.chromium.Chromium", "com.microsoft.edgemac", "com.brave.Browser",
    "com.vivaldi.Vivaldi", "com.operasoftware.Opera", "company.thebrowser.Browser",
})
ELECTRON_FRAMEWORK = "Contents/Frameworks/Electron Framework.framework"


@dataclass(frozen=True)
class RunningApp:
    pid: int
    name: str
    bundle_id: str
    bundle_path: str
    regular: bool


def _pids() -> list[int]:
    libproc = ctypes.CDLL("/usr/lib/libproc.dylib")
    count = int(libproc.proc_listallpids(None, 0))
    if count <= 0:
        raise LayerError(NOT_FOUND, "could not list processes")
    buffer = (ctypes.c_int * (count + 64))()
    filled = int(libproc.proc_listallpids(buffer, ctypes.sizeof(buffer)))
    return [pid for pid in buffer[: max(filled, 0)] if pid > 0]


def running_apps() -> list[RunningApp]:
    import AppKit

    apps: list[RunningApp] = []
    for pid in _pids():
        app = AppKit.NSRunningApplication.runningApplicationWithProcessIdentifier_(pid)
        if app is None:
            continue
        url = app.bundleURL()
        apps.append(RunningApp(
            pid=pid,
            name=str(app.localizedName() or ""),
            bundle_id=str(app.bundleIdentifier() or ""),
            bundle_path=str(url.path()) if url is not None else "",
            regular=int(app.activationPolicy()) == 0,
        ))
    return apps


def find_app(name: str, apps: Sequence[RunningApp]) -> RunningApp:
    """An exact name wins, then a unique substring; regular apps before helpers."""
    if name.startswith("pid:") and name[4:].isdigit():
        pid = int(name[4:])
        match = next((a for a in apps if a.pid == pid), None)
        if match is None:
            raise LayerError(NOT_FOUND, f"no app with pid {pid}")
        return match
    needle = name.strip().casefold()
    ordered = sorted(apps, key=lambda a: (not a.regular, a.pid))
    exact = [a for a in ordered if a.name.casefold() == needle]
    if exact:
        return exact[0]
    partial = [a for a in ordered if needle and needle in a.name.casefold()]
    if len(partial) == 1:
        return partial[0]
    if partial:
        names = ", ".join(sorted({a.name for a in partial}))
        raise LayerError(AMBIGUOUS, f"{name!r} matches several apps: {names}")
    raise LayerError(NOT_FOUND, f"no running app named {name!r}")


def launch_in_background(name: str) -> None:
    """Start an app without bringing it to the front, so a person's focus stays put."""
    result = subprocess.run(["open", "-g", "-a", name], capture_output=True, text=True,
                            check=False)
    if result.returncode != 0:
        raise LayerError(NOT_FOUND, f"could not open {name!r}: {result.stderr.strip()}")


def renderer(app: RunningApp) -> str | None:
    """Electron and Chromium build their accessibility tree only when asked to."""
    if app.bundle_id in CHROMIUM_BUNDLES:
        return "chromium"
    if app.bundle_path and (Path(app.bundle_path) / ELECTRON_FRAMEWORK).exists():
        return "electron"
    return None
