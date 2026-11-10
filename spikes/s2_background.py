"""S2: do two overlapping, unfocused windows both take input while the OS cursor stays put?

Prints the frontmost app and cursor before launch, after launch and after the input, so the
result shows whether this layer disturbs a person using the same machine.
"""

from __future__ import annotations

import asyncio
import subprocess
import tempfile
import time
from pathlib import Path

import Quartz

from agentlayer.backends.web.cdp import Connection, Session
from agentlayer.backends.web.launch import launch_chrome

PAGE = """data:text/html,<title>%s</title><input id=i autofocus>
<button id=b onclick="document.title='clicked'">Go</button>
<script>
window.ticks=0;setInterval(()=>window.ticks++,50);
window.focusSeen=[];addEventListener('focus',()=>focusSeen.push('w'));
</script>"""


def frontmost() -> str:
    asn = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True, check=True)
    info = subprocess.run(
        ["lsappinfo", "info", "-only", "name", asn.stdout.strip()],
        capture_output=True, text=True, check=True,
    )
    return info.stdout.strip()


def cursor() -> tuple[float, float]:
    point = Quartz.CGEventGetLocation(Quartz.CGEventCreate(None))
    return (point.x, point.y)


async def evaluate(page: Session, expr: str) -> object:
    result = await page.send("Runtime.evaluate", {"expression": expr, "returnByValue": True})
    return result["result"].get("value")


async def open_window(conn: Connection, attached: list[dict[str, object]], name: str) -> Session:
    created = await conn.send("Target.createTarget", {"url": PAGE % name, "newWindow": True})
    for _ in range(100):
        match = [a for a in attached if a["targetInfo"]["targetId"] == created["targetId"]]  # type: ignore[index]
        if match:
            break
        await asyncio.sleep(0.05)
    page = conn.session(str(match[0]["sessionId"]))
    await page.send("Page.enable")
    await page.send("Runtime.enable")
    await page.send("DOM.enable")
    await page.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
    return page


async def type_and_click(page: Session, text: str) -> None:
    doc = await page.send("DOM.getDocument", {"depth": -1})
    for selector, action in (("#i", "type"), ("#b", "click")):
        found = await page.send(
            "DOM.querySelector", {"nodeId": doc["root"]["nodeId"], "selector": selector}
        )
        node_id = found["nodeId"]
        if action == "type":
            await page.send("DOM.focus", {"nodeId": node_id})
            await page.send("Input.insertText", {"text": text})
        else:
            quads = await page.send("DOM.getContentQuads", {"nodeId": node_id})
            q = quads["quads"][0]
            x, y = (q[0] + q[4]) / 2, (q[1] + q[5]) / 2
            await page.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
            for kind in ("mousePressed", "mouseReleased"):
                await page.send(
                    "Input.dispatchMouseEvent",
                    {"type": kind, "x": x, "y": y, "button": "left", "clickCount": 1},
                )


async def main() -> None:
    print("before  :", frontmost(), cursor())
    with tempfile.TemporaryDirectory(prefix="al-s2-") as tmp:
        chrome = await launch_chrome(
            Path(tmp), headless=False,
            extra_args=("--use-mock-keychain", "--window-size=420,300", "--window-position=30,30"),
        )
        conn = Connection(chrome.transport, timeout=15)
        conn.start()
        attached: list[dict[str, object]] = []
        conn.on("Target.attachedToTarget", attached.append)
        await conn.send(
            "Target.setAutoAttach",
            {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
        )
        a = await open_window(conn, attached, "AAA")
        b = await open_window(conn, attached, "BBB")  # opened last, so it covers A
        await asyncio.sleep(1.0)
        print("launched:", frontmost(), cursor())

        ticks_before = [await evaluate(p, "ticks") for p in (a, b)]
        start = time.perf_counter()
        for round_number in range(5):
            await type_and_click(a, f"a{round_number}")
            await type_and_click(b, f"b{round_number}")
        await asyncio.sleep(1.0)
        elapsed = time.perf_counter() - start
        ticks_after = [await evaluate(p, "ticks") for p in (a, b)]

        for label, page, t0, t1 in zip("ab", (a, b), ticks_before, ticks_after, strict=True):
            value = await evaluate(page, "document.getElementById('i').value")
            title = await evaluate(page, "document.title")
            hidden = await evaluate(page, "document.visibilityState")
            focus = await evaluate(page, "document.hasFocus()")
            rate = (int(t1) - int(t0)) / elapsed  # type: ignore[call-overload]
            print(f"window {label}: value={value!r} title={title!r} visibility={hidden} "
                  f"hasFocus={focus} ticks/s={rate:.1f} (ideal 20)")
        print("after   :", frontmost(), cursor())
        await conn.send("Browser.close")
        await chrome.stop()
    await asyncio.sleep(0.5)
    print("closed  :", frontmost(), cursor())


asyncio.run(main())
