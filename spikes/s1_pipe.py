"""S1: is a hand-written pipe client stable against the installed Chrome?"""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path

from agentlayer.backends.web.cdp import Connection
from agentlayer.backends.web.launch import launch_chrome

HTML = "data:text/html,<title>t</title><main><h1>Hi</h1><button>Go</button></main>"


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="al-s1-") as tmp:
        chrome = await launch_chrome(Path(tmp), headless=True)
        conn = Connection(chrome.transport, timeout=15)
        conn.start()
        version = await conn.send("Browser.getVersion")
        print("browser:", version["product"], "protocol", version["protocolVersion"])

        attached: list[dict[str, object]] = []
        conn.on("Target.attachedToTarget", attached.append)
        await conn.send(
            "Target.setAutoAttach",
            {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
        )
        await asyncio.sleep(0.3)
        print("auto-attached:", [a["targetInfo"]["type"] for a in attached])  # type: ignore[index]

        created = await conn.send("Target.createTarget", {"url": HTML})
        await asyncio.sleep(0.5)
        info = next(a for a in attached if a["targetInfo"]["targetId"] == created["targetId"])  # type: ignore[index]
        page = conn.session(str(info["sessionId"]))
        await page.send("Accessibility.enable")
        start = time.perf_counter()
        tree = await page.send("Accessibility.getFullAXTree")
        print(f"nodes: {len(tree['nodes'])} in {(time.perf_counter() - start) * 1000:.1f}ms")
        for node in tree["nodes"]:
            role = node.get("role", {}).get("value")
            if role in {"button", "heading", "RootWebArea"}:
                print(" ", role, repr(node.get("name", {}).get("value")),
                      "backend", node.get("backendDOMNodeId"))

        latencies = []
        for _ in range(200):
            start = time.perf_counter()
            await page.send("Runtime.evaluate", {"expression": "1+1"})
            latencies.append((time.perf_counter() - start) * 1000)
        latencies.sort()
        print(f"evaluate p50 {latencies[100]:.2f}ms p99 {latencies[197]:.2f}ms")

        await conn.send("Browser.close")
        await chrome.stop()
        print("clean shutdown:", chrome.exited())


asyncio.run(main())
