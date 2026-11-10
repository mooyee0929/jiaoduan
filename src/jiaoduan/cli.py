"""Command line for people: debugging, approving gates, signing in."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Callable, Sequence
from typing import Any, TextIO

from jiaoduan.client import Client
from jiaoduan.errors import LayerError

CLI_AGENT = "cli"


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="jiaoduan")
    parser.add_argument("--agent", default=CLI_AGENT, help="act as this agent")
    commands = parser.add_subparsers(dest="command", required=True)

    commands.add_parser("surfaces", help="list open tabs and apps")
    opened = commands.add_parser("open", help="open a URL or an app")
    opened.add_argument("target")
    opened.add_argument("--kind", choices=["web", "mac"])

    observed = commands.add_parser("observe", help="read the UI")
    observed.add_argument("--surface")
    observed.add_argument("--root")
    observed.add_argument("--mode", default="auto")
    observed.add_argument("--query")
    observed.add_argument("--role")

    acted = commands.add_parser("act", help="act on a ref")
    acted.add_argument("ref")
    acted.add_argument("action")
    acted.add_argument("value", nargs="?")
    acted.add_argument("--expect", help="JSON postcondition")
    acted.add_argument("--gate")

    keyed = commands.add_parser("key", help="send key chords")
    keyed.add_argument("keys")
    keyed.add_argument("--surface")
    keyed.add_argument("--gate")

    read = commands.add_parser("read", help="plain text of a region")
    read.add_argument("--ref")
    read.add_argument("--surface")

    waited = commands.add_parser("wait", help="wait for a postcondition")
    waited.add_argument("expect", help="JSON postcondition")
    waited.add_argument("--surface")
    waited.add_argument("--timeout", type=float, default=10.0)

    formed = commands.add_parser("form", help="list form fields")
    formed.add_argument("--surface")
    formed.add_argument("--root")

    filled = commands.add_parser("fill", help="fill fields from a JSON object")
    filled.add_argument("values", help='JSON such as {"f1": "Ada"}')
    filled.add_argument("--surface")
    filled.add_argument("--root")

    tools = commands.add_parser("tools", help="adapter tools on a surface")
    tools.add_argument("--surface")
    called = commands.add_parser("call", help="run an adapter tool")
    called.add_argument("name")
    called.add_argument("args", nargs="?", default="{}", help="JSON arguments")
    called.add_argument("--surface")
    called.add_argument("--gate")

    released = commands.add_parser("release", help="give up a surface")
    released.add_argument("--surface")
    released.add_argument("--close", action="store_true")

    gate = commands.add_parser("gate", help="review commits waiting for a person")
    gate_commands = gate.add_subparsers(dest="gate_command", required=True)
    gate_commands.add_parser("list")
    for name in ("approve", "reject"):
        one = gate_commands.add_parser(name)
        one.add_argument("id")

    daemon = commands.add_parser("daemon", help="daemon control")
    daemon_commands = daemon.add_subparsers(dest="daemon_command", required=True)
    daemon_commands.add_parser("status")
    daemon_commands.add_parser("stop")

    login = commands.add_parser("login", help="open a visible browser to sign in to a site")
    login.add_argument("url", nargs="?", default="about:blank")

    adapter = commands.add_parser("adapter", help="adapter tools")
    adapter_commands = adapter.add_subparsers(dest="adapter_command", required=True)
    check = adapter_commands.add_parser("check", help="validate adapter files")
    check.add_argument("paths", nargs="*")
    draft = adapter_commands.add_parser("draft", help="draft an adapter from a trace")
    draft.add_argument("trace")
    draft.add_argument("--id", default="draft/adapter")
    draft.add_argument("--tool", default="recorded_flow")

    run = commands.add_parser("run", help="run a plan of agents")
    run.add_argument("plan")
    run.add_argument("--dry-run", action="store_true")
    return parser


def _json(text: str | None) -> Any:
    return None if text is None else json.loads(text)


def _open_tty() -> TextIO:
    return open("/dev/tty", "r+", encoding="utf-8")  # noqa: SIM115


def confirm_at_terminal(question: str, opener: Callable[[], TextIO] = _open_tty) -> None:
    """Ask on the controlling terminal, not stdin, so a command run by an agent cannot answer.

    An agent's shell tool has no controlling terminal. This raises the bar; it is not a wall,
    since any process running as the same user can still reach the daemon's socket.
    """
    try:
        terminal = opener()
    except OSError as exc:
        raise LayerError(
            "PERMISSION_DENIED", "approving a commit needs a person at a terminal",
            hint="run `jiaoduan gate approve` in your own terminal window",
        ) from exc
    with terminal:
        terminal.write(f"{question} [y/N] ")
        terminal.flush()
        answer = terminal.readline().strip().lower()
    if answer not in {"y", "yes"}:
        raise LayerError("COMMIT_DENIED", "not approved")


async def _dispatch(options: argparse.Namespace) -> str:
    command = options.command
    role = "operator" if command in {"gate", "daemon"} else "agent"
    spawn = command != "daemon"
    async with Client(options.agent, role=role, spawn=spawn) as client:
        if command == "surfaces":
            return await client.call("surfaces")
        if command == "open":
            return await client.call("open", target=options.target, kind=options.kind)
        if command == "observe":
            return await client.call(
                "observe", surface=options.surface, root=options.root, mode=options.mode,
                query=options.query, role=options.role,
            )
        if command == "act":
            return await client.call(
                "act", ref=options.ref, action=options.action, value=options.value,
                expect=_json(options.expect), gate=options.gate,
            )
        if command == "key":
            return await client.call(
                "key", keys=options.keys, surface=options.surface, gate=options.gate
            )
        if command == "read":
            return await client.call("read", ref=options.ref, surface=options.surface)
        if command == "wait":
            return await client.call(
                "wait", expect=_json(options.expect), surface=options.surface,
                timeout=options.timeout,
            )
        if command == "form":
            return await client.call("form", surface=options.surface, root=options.root)
        if command == "fill":
            return await client.call(
                "fill", values=_json(options.values), surface=options.surface, root=options.root
            )
        if command == "tools":
            return await client.call("tools", surface=options.surface)
        if command == "call":
            return await client.call(
                "call", name=options.name, args=_json(options.args), surface=options.surface,
                gate=options.gate,
            )
        if command == "release":
            return await client.call("release", surface=options.surface, close=options.close)
        if command == "gate":
            if options.gate_command == "list":
                return await client.call("gate_list")
            if options.gate_command == "approve":
                pending = await client.call("gate_list")
                line = next(
                    (row for row in pending.splitlines() if row.split(" ", 1)[0] == options.id),
                    None,
                )
                if line is None:
                    raise LayerError("NOT_FOUND", f"no pending gate {options.id}")
                confirm_at_terminal(f"approve {line}?")
            return await client.call(f"gate_{options.gate_command}", id=options.id)
        if command == "daemon":
            return await client.call(
                "status" if options.daemon_command == "status" else "shutdown"
            )
    raise LayerError("INVALID_ARGS", f"unknown command {command}")


async def _login(url: str) -> str:
    """A visible browser on the agents' profile, with no gate, so a person can sign in."""
    from jiaoduan.backends.web.cdp import Connection
    from jiaoduan.backends.web.launch import default_profile, launch_chrome

    try:
        async with Client(CLI_AGENT, role="operator", spawn=False) as client:
            await client.call("shutdown")
    except LayerError:
        pass  # no daemon was running, which is what login needs
    await asyncio.sleep(1.0)
    chrome = await launch_chrome(default_profile(), headless=False, extra_args=(url,))
    connection = Connection(chrome.transport)
    closed = asyncio.Event()
    connection.on_close(closed.set)
    connection.start()
    print("Sign in, then close the browser window.")
    await closed.wait()
    await chrome.stop()
    return "signed-in state saved to the agent profile"


def _run_special(options: argparse.Namespace) -> str | None:
    if options.command == "login":
        return asyncio.run(_login(options.url))
    if options.command == "adapter":
        from jiaoduan.adapters.tools import check_paths, draft_from_trace

        if options.adapter_command == "check":
            return check_paths(options.paths)
        return draft_from_trace(options.trace, options.id, options.tool)
    if options.command == "run":
        from jiaoduan.orchestrator.run import run_plan

        return run_plan(options.plan, dry_run=options.dry_run)
    return None


def main(argv: Sequence[str] | None = None) -> None:
    options = _parser().parse_args(argv)
    try:
        special = _run_special(options)
        print(special if special is not None else asyncio.run(_dispatch(options)))
    except LayerError as exc:
        print(exc.render(), file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
