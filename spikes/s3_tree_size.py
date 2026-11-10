"""S3: how much does pruning save on real pages, and what does a read cost?

Compares this layer's output with a Playwright-style rendering of the same page: every
non-ignored node on one indented line with a ref, which is what an aria snapshot looks like.
"""

from __future__ import annotations

import asyncio
import tempfile
import time
from pathlib import Path

from agentlayer import prune, render
from agentlayer.backends.web.backend import WebBackend, WebOptions
from agentlayer.refs import RefTable

URLS = [
    "https://en.wikipedia.org/wiki/Accessibility",
    "https://news.ycombinator.com/",
    "https://github.com/microsoft/playwright-mcp",
    "https://www.python.org/downloads/",
    "https://developer.mozilla.org/en-US/docs/Web/HTML/Element/form",
]


def aria_style(raw_nodes: list[dict[str, object]]) -> int:
    """Characters in a one-line-per-node rendering, as an aria snapshot would print."""
    total = 0
    for node in raw_nodes:
        if node.get("ignored"):
            continue
        role = node.get("role", {}).get("value", "")  # type: ignore[union-attr]
        if role in {"InlineTextBox", "generic", "none"}:
            continue
        name = node.get("name", {}).get("value", "")  # type: ignore[union-attr]
        total += len(f'  - {role} "{name}" [ref=e1234]\n')
    return total


async def main() -> None:
    with tempfile.TemporaryDirectory(prefix="al-s3-") as tmp:
        backend = WebBackend(WebOptions(Path(tmp), headless=True, mock_keychain=True))
        await backend.start()
        header = f"{'page':44} {'ax':>6} {'read ms':>8} {'aria chars':>11} {'full':>7} {'interact':>9} {'skel':>7}"
        print(header)
        for url in URLS:
            try:
                surface = await backend.open(url)
            except Exception as exc:
                print(url, "failed", exc)
                continue
            await asyncio.sleep(1.5)
            page = backend.page(surface.id)
            ax = await page.session.send("Accessibility.getFullAXTree")
            start = time.perf_counter()
            raw = await backend.read_tree(surface.id)
            ms = (time.perf_counter() - start) * 1000
            nodes = prune.flatten(raw)
            RefTable().sync(surface.id, nodes)
            sizes = []
            for mode in (prune.Mode.FULL, prune.Mode.INTERACTIVE, prune.Mode.SKELETON):
                shown, _, _ = prune.select(nodes, mode, 100000)
                sizes.append(len(render.nodes_text(shown)))
            print(f"{url[8:50]:44} {len(ax['nodes']):>6} {ms:>8.1f} {aria_style(ax['nodes']):>11} "
                  f"{sizes[0]:>7} {sizes[1]:>9} {sizes[2]:>7}")
            await backend.close_surface(surface.id)
        await backend.stop()


asyncio.run(main())
