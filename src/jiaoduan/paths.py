"""Where the daemon keeps its socket, lock and log."""

from __future__ import annotations

import os
import sys
from pathlib import Path

HOME_ENV = "JIAODUAN_HOME"
_SOCKET_PATH_LIMIT = 100


def home() -> Path:
    configured = os.environ.get(HOME_ENV)
    if configured:
        path = Path(configured)
    elif sys.platform == "darwin":
        path = Path.home() / "Library/Application Support/jiaoduan"
    else:
        path = Path.home() / ".local/share/jiaoduan"
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    return path


def socket_path() -> Path:
    path = home() / "d.sock"
    if len(str(path)) > _SOCKET_PATH_LIMIT:
        raise RuntimeError(f"socket path too long for a Unix socket; shorten {HOME_ENV}: {path}")
    return path


def lock_path() -> Path:
    return home() / "d.lock"


def log_path() -> Path:
    return home() / "daemon.log"
