"""S4 and S5 against real React and real react-select.

S4: does Input.insertText reach React state, and what does a plain JS write do by comparison?
    Where does react-select's chosen value appear?
S5: when React remounts a form, do backend node ids change, and can (role, name) re-find them?
"""

from __future__ import annotations

import asyncio
import json
import tempfile
from collections import Counter
from pathlib import Path
from typing import Any

from jiaoduan.backends.web.cdp import Connection, Session
from jiaoduan.backends.web.launch import launch_chrome

APP = """<!doctype html><title>r</title><div id=root></div>
<script type=module>
import React from 'https://esm.sh/react@18.3.1';
import {createRoot} from 'https://esm.sh/react-dom@18.3.1/client';
import Select from 'https://esm.sh/react-select@5.8.0?deps=react@18.3.1,react-dom@18.3.1';
const h = React.createElement;
const options = ['Bachelor','Master of Science','Master of Business Administration','PhD']
  .map(v => ({value: v, label: v}));
function App() {
  const [text, setText] = React.useState('');
  const [other, setOther] = React.useState('');
  const [degree, setDegree] = React.useState(null);
  const [gen, setGen] = React.useState(0);
  window.__state = {text, other, degree: degree && degree.value, gen};
  return h('form', {key: gen, 'aria-label': 'Application'},
    h('label', null, 'Full name', h('input', {id:'t', value:text, onChange:e=>setText(e.target.value)})),
    h('label', null, 'Other', h('input', {id:'o', value:other, onChange:e=>setOther(e.target.value)})),
    h('label', {id:'dl'}, 'Degree'),
    h(Select, {inputId:'d', 'aria-labelledby':'dl', options, value:degree, onChange:setDegree}),
    h('button', {type:'button', id:'re', onClick:()=>setGen(g=>g+1)}, 'Remount'));
}
createRoot(document.getElementById('root')).render(h(App));
window.__ready = true;
</script>"""


async def evaluate(page: Session, expr: str) -> Any:
    result = await page.send("Runtime.evaluate", {"expression": expr, "returnByValue": True})
    return result["result"].get("value")


async def backend_of(page: Session, selector: str) -> int:
    doc = await page.send("DOM.getDocument", {"depth": 0})
    found = await page.send(
        "DOM.querySelector", {"nodeId": doc["root"]["nodeId"], "selector": selector}
    )
    described = await page.send("DOM.describeNode", {"nodeId": found["nodeId"]})
    return int(described["node"]["backendNodeId"])


async def click(page: Session, backend: int) -> None:
    await page.send("DOM.scrollIntoViewIfNeeded", {"backendNodeId": backend})
    quads = await page.send("DOM.getContentQuads", {"backendNodeId": backend})
    q = quads["quads"][0]
    x, y = (q[0] + q[4]) / 2, (q[1] + q[5]) / 2
    await page.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y})
    for kind in ("mousePressed", "mouseReleased"):
        await page.send(
            "Input.dispatchMouseEvent",
            {"type": kind, "x": x, "y": y, "button": "left", "clickCount": 1},
        )


def summarize(tree: dict[str, Any]) -> list[dict[str, Any]]:
    wanted = {"textbox", "combobox", "button", "listbox", "option", "form", "log", "status"}
    out = []
    for node in tree["nodes"]:
        if node.get("ignored"):
            continue
        role = node.get("role", {}).get("value")
        if role in wanted:
            props = {
                p["name"]: p["value"].get("value")
                for p in node.get("properties", [])
                if p["name"] in {"expanded", "hasPopup", "autocomplete", "activedescendant"}
            }
            out.append({
                "role": role,
                "name": node.get("name", {}).get("value"),
                "value": node.get("value", {}).get("value"),
                "backend": node.get("backendDOMNodeId"),
                **props,
            })
    return out


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="al-s4-") as tmp:
        chrome = await launch_chrome(Path(tmp), headless=True)
        conn = Connection(chrome.transport, timeout=20)
        conn.start()
        attached: list[dict[str, Any]] = []
        conn.on("Target.attachedToTarget", attached.append)
        await conn.send(
            "Target.setAutoAttach",
            {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
        )
        created = await conn.send("Target.createTarget", {"url": "about:blank"})
        await asyncio.sleep(0.3)
        info = next(a for a in attached if a["targetInfo"]["targetId"] == created["targetId"])
        page = conn.session(info["sessionId"])
        for domain in ("Page", "Runtime", "DOM", "Accessibility"):
            await page.send(f"{domain}.enable")
        await page.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
        frame = (await page.send("Page.getFrameTree"))["frameTree"]["frame"]["id"]
        await page.send("Page.setDocumentContent", {"frameId": frame, "html": APP})
        for _ in range(200):
            if await evaluate(page, "!!document.getElementById('t')"):
                break
            await asyncio.sleep(0.1)
        else:
            print("react app did not render; network blocked?")
            return

        print("== S4: controlled input")
        await page.send("DOM.focus", {"backendNodeId": await backend_of(page, "#t")})
        await page.send("Input.insertText", {"text": "Ada Lovelace"})
        await asyncio.sleep(0.1)
        print("insertText -> state:", await evaluate(page, "__state.text"))
        await evaluate(page, "document.getElementById('o').value='written by js'; 1")
        await evaluate(
            page, "document.getElementById('o').dispatchEvent(new Event('input',{bubbles:true})); 1"
        )
        await asyncio.sleep(0.1)
        print("JS write + untrusted input event -> DOM:",
              await evaluate(page, "document.getElementById('o').value"),
              "| state:", repr(await evaluate(page, "__state.other")))

        print("== S4: react-select")
        await page.send("DOM.focus", {"backendNodeId": await backend_of(page, "#d")})
        await page.send("Input.insertText", {"text": "Master"})
        await asyncio.sleep(0.3)
        tree = await page.send("Accessibility.getFullAXTree")
        print(json.dumps(summarize(tree), ensure_ascii=False, indent=0)[:1800])
        options = [n for n in tree["nodes"] if n.get("role", {}).get("value") == "option"]
        target = next(n for n in options if n["name"]["value"] == "Master of Science")
        await click(page, int(target["backendDOMNodeId"]))
        await asyncio.sleep(0.2)
        print("state.degree:", await evaluate(page, "__state.degree"))
        tree = await page.send("Accessibility.getFullAXTree")
        for row in summarize(tree):
            if row["role"] == "combobox":
                print("combobox after pick:", row)
        texts = [
            n["name"]["value"] for n in tree["nodes"]
            if n.get("role", {}).get("value") == "StaticText" and not n.get("ignored")
        ]
        print("static text after pick:", texts)

        print("== S5: remount")
        before = summarize(await page.send("Accessibility.getFullAXTree"))
        await click(page, await backend_of(page, "#re"))
        await asyncio.sleep(0.3)
        after = summarize(await page.send("Accessibility.getFullAXTree"))
        kept = {b["backend"] for b in before} & {a["backend"] for a in after}
        keys_after = Counter((a["role"], a["name"]) for a in after)
        unique = [b for b in before if keys_after[(b["role"], b["name"])] == 1]
        print(f"before {len(before)} nodes, after {len(after)}, same backend id: {len(kept)}")
        print(f"unique (role,name) re-find: {len(unique)}/{len(before)}")
        print("state after remount:", await evaluate(page, "JSON.stringify(__state)"))
        await conn.send("Browser.close")
        await chrome.stop()


asyncio.run(main())
