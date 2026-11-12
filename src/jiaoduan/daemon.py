"""The long-lived owner of the browser and the claims; agents and the CLI connect to it.

A Chrome controlled over a pipe has exactly one owner, so several agents can only share a
browser through a single process. Claims live here too, which means a dropped connection
releases them with no cleanup step.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import json
import logging
import os
import signal
import sys
import time
from collections import defaultdict
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from jiaoduan import __version__, paths
from jiaoduan.backends.web.backend import WebBackend, WebOptions
from jiaoduan.backends.web.cdp import CdpDisconnected, CdpError, CdpTimeout
from jiaoduan.backends.web.launch import default_profile
from jiaoduan.engine import Engine, jsonable_expect
from jiaoduan.errors import BACKEND_UNAVAILABLE, INVALID_ARGS, LayerError
from jiaoduan.gate import POLICY_ENV, Gate, Policy
from jiaoduan.model import Backend, Delivery
from jiaoduan.trace import Trace

log = logging.getLogger(__name__)

JsonObject = dict[str, Any]
Op = Callable[[str, JsonObject], Awaitable[str]]

_MAX_LINE = 1 << 24
_IDLE_SECONDS = 1800.0
_IDLE_CHECK = 15.0
HEADED_ENV = "JIAODUAN_HEADED"
PROFILE_ENV = "JIAODUAN_PROFILE"
MOCK_KEYCHAIN_ENV = "JIAODUAN_MOCK_KEYCHAIN"


def web_options() -> WebOptions:
    profile = os.environ.get(PROFILE_ENV)
    return WebOptions(
        profile=Path(profile) if profile else default_profile(),
        headless=os.environ.get(HEADED_ENV) != "1",
        mock_keychain=os.environ.get(MOCK_KEYCHAIN_ENV) == "1",
        page_guard=os.environ.get(POLICY_ENV, Policy.ASK.value) != Policy.ALLOW.value,
    )


def build_backends() -> dict[str, Backend]:
    backends: dict[str, Backend] = {"web": WebBackend(web_options())}
    if sys.platform == "darwin":
        try:
            from jiaoduan.backends.mac.backend import MacBackend
        except ImportError as exc:
            log.info("macOS backend unavailable: %s", exc)
        else:
            backends["mac"] = MacBackend()
    return backends


class Daemon:
    def __init__(self, engine: Engine, idle_seconds: float = _IDLE_SECONDS) -> None:
        self.engine = engine
        self._idle_seconds = idle_seconds
        self._connections: dict[str, int] = defaultdict(int)
        self._last_active = time.monotonic()
        self._stop = asyncio.Event()
        self._ops = self._operations()

    def stop(self) -> None:
        self._stop.set()

    def _operations(self) -> dict[str, Op]:
        engine = self.engine

        async def surfaces(agent: str, _: JsonObject) -> str:
            return await engine.list_surfaces(agent)

        async def open_surface(agent: str, a: JsonObject) -> str:
            return await engine.open(agent, str(a["target"]), a.get("kind"))

        async def observe(agent: str, a: JsonObject) -> str:
            return await engine.observe(
                agent, a.get("surface"), a.get("root"), a.get("mode", "auto"), a.get("query"),
                a.get("role"), int(a.get("max_nodes", 300)),
            )

        async def act(agent: str, a: JsonObject) -> str:
            return await engine.act(
                agent, str(a["ref"]), str(a["action"]), a.get("value"),
                jsonable_expect(a.get("expect")), a.get("gate"),
            )

        async def key(agent: str, a: JsonObject) -> str:
            return await engine.key(agent, str(a["keys"]), a.get("surface"), a.get("gate"))

        async def read(agent: str, a: JsonObject) -> str:
            return await engine.read(
                agent, a.get("ref"), a.get("surface"), int(a.get("max_chars", 8000))
            )

        async def wait(agent: str, a: JsonObject) -> str:
            expect = jsonable_expect(a.get("expect"))
            if not isinstance(expect, dict):
                raise LayerError(INVALID_ARGS, "wait needs expect={...}")
            return await engine.wait(agent, expect, a.get("surface"), float(a.get("timeout", 10)))

        async def release(agent: str, a: JsonObject) -> str:
            return await engine.release(agent, a.get("surface"), bool(a.get("close", False)))

        async def status(_: str, __: JsonObject) -> str:
            agents = ", ".join(k for k, v in self._connections.items() if v) or "none"
            return f"jiaoduan {__version__} pid {os.getpid()} agents: {agents}"

        return {
            "surfaces": surfaces, "open": open_surface, "observe": observe, "act": act, "key": key,
            "read": read, "wait": wait, "release": release, "status": status,
            **self._form_ops(), **self._adapter_ops(),
        }

    def _form_ops(self) -> dict[str, Op]:
        engine = self.engine

        async def form(agent: str, a: JsonObject) -> str:
            return await engine.form(agent, a.get("surface"), a.get("root"))

        async def fill(agent: str, a: JsonObject) -> str:
            values = a.get("values")
            if not isinstance(values, dict):
                raise LayerError(INVALID_ARGS, "fill needs values={field: value}")
            return await engine.fill(
                agent, {str(k): str(v) for k, v in values.items()}, a.get("surface"),
                a.get("root"),
            )

        return {"form": form, "fill": fill}

    def _adapter_ops(self) -> dict[str, Op]:
        engine = self.engine

        async def tools(agent: str, a: JsonObject) -> str:
            return await engine.tools(agent, a.get("surface"))

        async def call(agent: str, a: JsonObject) -> str:
            arguments = a.get("args") or {}
            if not isinstance(arguments, dict):
                raise LayerError(INVALID_ARGS, "call needs args={...}")
            return await engine.call_tool(
                agent, str(a["name"]), arguments, a.get("surface"), a.get("gate")
            )

        async def report(agent: str, a: JsonObject) -> str:
            return engine.report(
                agent, str(a["status"]), a.get("note"), list(a.get("artifacts") or [])
            )

        return {"tools": tools, "call": call, "report": report}

    def _operator_ops(self) -> dict[str, Op]:
        engine = self.engine

        async def gate_list(_: str, __: JsonObject) -> str:
            return engine.gate_list()

        async def gate_approve(_: str, a: JsonObject) -> str:
            return engine.gate_approve(str(a["id"]))

        async def gate_reject(_: str, a: JsonObject) -> str:
            return engine.gate_reject(str(a["id"]))

        async def shutdown(_: str, __: JsonObject) -> str:
            self.stop()
            return "stopping"

        async def reports(_: str, __: JsonObject) -> str:
            return engine.reports_json()

        return {
            "gate_list": gate_list, "gate_approve": gate_approve, "gate_reject": gate_reject,
            "shutdown": shutdown, "reports": reports,
        }

    async def handle(self, agent: str, operator: bool, request: JsonObject) -> JsonObject:
        op_name = str(request.get("op", ""))
        response: JsonObject = {"id": request.get("id")}
        table = {**self._ops, **self._operator_ops()} if operator else self._ops
        op = table.get(op_name)
        try:
            if op is None:
                raise LayerError(INVALID_ARGS, f"unknown operation {op_name!r}")
            arguments = request.get("args") or {}
            response.update(ok=True, text=await op(agent, arguments))
        except LayerError as exc:
            response.update(ok=False, error=_encode(exc))
        except KeyError as exc:
            response.update(ok=False, error=_encode(
                LayerError(INVALID_ARGS, f"{op_name} is missing {exc}")
            ))
        except (CdpDisconnected, CdpTimeout, CdpError) as exc:
            log.warning("browser error during %s: %s", op_name, exc)
            response.update(ok=False, error=_encode(LayerError(
                BACKEND_UNAVAILABLE, f"the browser did not answer: {exc}",
                delivery=Delivery.UNCERTAIN,
            )))
        return response

    async def _serve_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        agent: str | None = None
        try:
            hello = json.loads(await reader.readline())
            agent = str(hello["agent"])
            operator = hello.get("role") == "operator"
            self._connections[agent] += 1
            while line := await reader.readline():
                self._last_active = time.monotonic()
                response = await self.handle(agent, operator, json.loads(line))
                writer.write(json.dumps(response, ensure_ascii=False).encode("utf-8") + b"\n")
                await writer.drain()
        except (json.JSONDecodeError, KeyError) as exc:
            log.warning("bad client message: %s", exc)
        except ConnectionError as exc:
            log.info("client went away: %s", exc)
        finally:
            if agent is not None:
                self._connections[agent] -= 1
                if self._connections[agent] == 0:
                    self.engine.disconnect(agent)
            self._last_active = time.monotonic()
            writer.close()

    async def _watch_idle(self) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(min(_IDLE_CHECK, self._idle_seconds))
            quiet = time.monotonic() - self._last_active
            if not any(self._connections.values()) and quiet >= self._idle_seconds:
                log.info("no clients for %.0fs; stopping", quiet)
                self.stop()

    async def run(self, socket: Path) -> None:
        loop = asyncio.get_running_loop()
        for number in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(number, self.stop)
        server = await asyncio.start_unix_server(
            self._serve_client, path=str(socket), limit=_MAX_LINE
        )
        os.chmod(socket, 0o600)
        watcher = asyncio.create_task(self._watch_idle())
        try:
            await self._stop.wait()
        finally:
            watcher.cancel()
            server.close()
            await self.engine.stop()
            socket.unlink(missing_ok=True)


def _encode(error: LayerError) -> JsonObject:
    return {
        "code": error.code, "message": error.message, "delivery": error.delivery.value,
        "hint": error.hint,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="jiaoduan daemon")
    parser.add_argument("--idle", type=float, default=_IDLE_SECONDS)
    options = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    lock = paths.lock_path().open("w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return  # another daemon got here first, and that is the one clients will reach
    socket = paths.socket_path()
    socket.unlink(missing_ok=True)

    async def run() -> None:
        engine = Engine(build_backends(), gate=Gate(), trace=Trace.from_env())
        await Daemon(engine, options.idle).run(socket)

    asyncio.run(run())


if __name__ == "__main__":
    main()
