"""Find and start a Chrome that this process controls over a pipe.

Chrome ignores the remote-debugging switches on its default profile (since Chrome 136), so a
dedicated user-data-dir is always used. Logins made there persist between runs.
"""

from __future__ import annotations

import asyncio
import fcntl
import os
import shutil
import signal
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from agentlayer.backends.web.transport import PipeTransport

_MAC_PATHS = (
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    "/Applications/Chromium.app/Contents/MacOS/Chromium",
)
_LINUX_NAMES = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser")
# Background and occluded windows must keep running: agents work in windows nobody looks at.
_ALWAYS = (
    "--remote-debugging-pipe",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-timer-throttling",
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-features=Translate,AutofillServerCommunication",
)
_FIRST_SAFE_FD = 10
_TARGET_READ_FD = 3
_TARGET_WRITE_FD = 4
_GRACE_SECONDS = 3.0


class ChromeNotFound(RuntimeError):
    pass


def default_profile() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library/Application Support/agent-layer/chrome-profile"
    return Path.home() / ".local/share/agent-layer/chrome-profile"


def find_chrome() -> str:
    configured = os.environ.get("AGENTLAYER_CHROME")
    if configured:
        return configured
    for path in _MAC_PATHS:
        if Path(path).exists():
            return path
    for name in _LINUX_NAMES:
        found = shutil.which(name)
        if found:
            return found
    raise ChromeNotFound("no Chrome found; set AGENTLAYER_CHROME to its executable")


@dataclass
class ChromeProcess:
    pid: int
    transport: PipeTransport

    def exited(self) -> bool:
        try:
            pid, _ = os.waitpid(self.pid, os.WNOHANG)
        except ChildProcessError:
            return True
        return pid != 0

    async def stop(self) -> None:
        """Wait for a browser that was already asked to close, then signal it if it lingers."""
        for sig in (None, signal.SIGTERM, signal.SIGKILL):
            if sig is not None and not self.exited():
                try:
                    os.kill(self.pid, sig)
                except ProcessLookupError:
                    return
            deadline = time.monotonic() + _GRACE_SECONDS
            while time.monotonic() < deadline:
                if self.exited():
                    return
                await asyncio.sleep(0.05)


def _high_fd(fd: int) -> int:
    """A copy of ``fd`` above the numbers the child's dup2 targets, so they cannot collide."""
    copy = fcntl.fcntl(fd, fcntl.F_DUPFD_CLOEXEC, _FIRST_SAFE_FD)
    os.close(fd)
    return copy


async def launch_chrome(
    profile: Path,
    *,
    headless: bool,
    chrome: str | None = None,
    extra_args: tuple[str, ...] = (),
) -> ChromeProcess:
    profile.mkdir(parents=True, mode=0o700, exist_ok=True)
    binary = chrome or find_chrome()
    args = [binary, f"--user-data-dir={profile}", *_ALWAYS]
    if headless:
        # Keeps test runs from prompting for, or touching, the login keychain.
        args += ["--headless=new", "--use-mock-keychain"]
    args += [*extra_args, "about:blank"]

    command_read, command_write = (_high_fd(fd) for fd in os.pipe())
    reply_read, reply_write = (_high_fd(fd) for fd in os.pipe())
    devnull = "/dev/null"
    pid = os.posix_spawn(
        binary,
        args,
        dict(os.environ),
        file_actions=[
            (os.POSIX_SPAWN_OPEN, 0, devnull, os.O_RDONLY, 0),
            (os.POSIX_SPAWN_OPEN, 1, devnull, os.O_WRONLY, 0),
            (os.POSIX_SPAWN_OPEN, 2, devnull, os.O_WRONLY, 0),
            (os.POSIX_SPAWN_DUP2, command_read, _TARGET_READ_FD),
            (os.POSIX_SPAWN_DUP2, reply_write, _TARGET_WRITE_FD),
        ],
        setsid=True,
    )
    os.close(command_read)
    os.close(reply_write)
    transport = PipeTransport(reply_read, command_write)
    await transport.open()
    return ChromeProcess(pid=pid, transport=transport)
