"""The web backend: drives a dedicated Chrome over CDP, using only trusted input events."""

from __future__ import annotations

import asyncio
import json
import logging
import subprocess
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentlayer.backends.web import keys as keymap
from agentlayer.backends.web.cdp import CdpDisconnected, CdpError, CdpTimeout, Connection, Session
from agentlayer.backends.web.launch import (
    ChromeNotFound,
    ChromeProcess,
    default_profile,
    launch_chrome,
)
from agentlayer.backends.web.settle import SettleTracker
from agentlayer.backends.web.tree import (
    WebHandle,
    convert,
    dom_facts,
    mark_clickable,
)
from agentlayer.errors import (
    BACKEND_UNAVAILABLE,
    INVALID_ARGS,
    NO_SURFACE,
    NOT_ACTIONABLE,
    NOT_SUPPORTED,
    STALE_REF,
    LayerError,
)
from agentlayer.model import (
    Action,
    ActionKind,
    CommitFacts,
    Delivery,
    Handle,
    KeyContext,
    Mark,
    RawNode,
    Role,
    Settle,
    Surface,
    SurfaceId,
)

log = logging.getLogger(__name__)

JsonObject = dict[str, Any]

WORLD = "agentlayer"
BINDING = "agentlayerEvent"
_INPUT_TIMEOUT = 5.0
_READ_ATTEMPTS = 4
_READ_RETRY = 0.1
_STABLE_ATTEMPTS = 12
_STABLE_WAIT = 0.03
_OPEN_BUDGET = 15.0
_SCROLL_FRACTION = 0.8
_GONE_MESSAGES = ("no node", "could not find node", "does not belong", "cannot find context")

OBSERVER_JS = f"""(() => {{
  if (window.__agentlayerObserver) return;
  window.__agentlayerObserver = true;
  let last = 0;
  new MutationObserver(() => {{
    const now = Date.now();
    if (now - last > 40) {{
      last = now;
      if (typeof {BINDING} === 'function') {BINDING}('m');
    }}
  }}).observe(document, {{subtree: true, childList: true, attributes: true, characterData: true}});
}})();"""

GUARD_JS = f"""(() => {{
  if (window.__agentlayerGuard) return;
  const guard = {{armedUntil: 0}};
  window.__agentlayerGuard = guard;
  window.addEventListener('submit', (event) => {{
    if (Date.now() < guard.armedUntil) return;
    const form = event.target;
    if (form.getAttribute && (form.getAttribute('role') === 'search'
        || (form.getAttribute('method') || '').toLowerCase() === 'get')) return;
    event.preventDefault();
    event.stopImmediatePropagation();
    if (typeof {BINDING} === 'function') {BINDING}('submit-blocked');
  }}, true);
}})();"""

COMMIT_FACTS_JS = """function () {
  const el = this.nodeType === 1 ? this : this.parentElement;
  if (!el) return {submit: false, search: false};
  const button = el.closest('button, input[type=submit], input[type=image]');
  const form = (button && button.form) || el.closest('form');
  const submit = !!(button && form && (button.type === 'submit' || button.type === 'image'));
  const search = !!form && (form.getAttribute('role') === 'search'
    || (form.getAttribute('method') || '').toLowerCase() === 'get');
  return {submit, search};
}"""

KEY_CONTEXT_JS = """function () {
  let el = document.activeElement;
  while (el && el.shadowRoot && el.shadowRoot.activeElement) el = el.shadowRoot.activeElement;
  if (!el || el === document.body) return {submit: false, detail: 'nothing focused'};
  const tag = el.tagName;
  const type = (el.getAttribute('type') || 'text').toLowerCase();
  const expanded = el.getAttribute('aria-expanded') === 'true';
  const single = tag === 'INPUT'
    && !['button', 'checkbox', 'radio', 'file', 'reset', 'submit', 'image'].includes(type);
  const form = el.form || el.closest('form');
  return {
    submit: !!form && single && !expanded,
    detail: 'focus is in a single-line field of a form and no popup is open',
  };
}"""

RELATED_JS = """function (hit) {
  let walker = hit;
  while (walker) {
    if (walker === this) return true;
    walker = walker.parentNode || walker.host || null;
  }
  walker = this;
  while (walker) {
    if (walker === hit) return true;
    walker = walker.parentNode || walker.host || null;
  }
  return hit.control === this || (this.labels && [...this.labels].includes(hit));
}"""

LABEL_OF_JS = """function () { return (this.labels && this.labels[0]) || null; }"""

SELECT_ALL_JS = """function () {
  this.focus();
  if (this.select) { this.select(); return true; }
  const selection = this.ownerDocument.getSelection();
  const range = this.ownerDocument.createRange();
  range.selectNodeContents(this);
  selection.removeAllRanges();
  selection.addRange(range);
  return true;
}"""

CARET_END_JS = """function () {
  this.focus();
  if (this.setSelectionRange && typeof this.value === 'string') {
    this.setSelectionRange(this.value.length, this.value.length);
    return true;
  }
  const selection = this.ownerDocument.getSelection();
  selection.selectAllChildren(this);
  selection.collapseToEnd();
  return true;
}"""

SCROLL_JS = """function (dx, dy) {
  let el = this.nodeType === 1 ? this : this.parentElement;
  const tall = (e) => e.scrollHeight > e.clientHeight + 1 || e.scrollWidth > e.clientWidth + 1;
  while (el && el !== document.documentElement && !tall(el)) el = el.parentElement;
  const target = el || document.scrollingElement;
  const box = target === document.scrollingElement ? {w: innerWidth, h: innerHeight}
    : {w: target.clientWidth, h: target.clientHeight};
  target.scrollBy(dx * box.w, dy * box.h);
  return true;
}"""


@dataclass
class Page:
    surface: SurfaceId
    target_id: str
    session: Session
    tracker: SettleTracker = field(default_factory=SettleTracker)
    frames: dict[str, Session] = field(default_factory=dict)
    title: str = ""
    url: str = "about:blank"
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    guard_script: str | None = None
    armed_contexts: dict[str, set[int]] = field(default_factory=dict)
    reserved: bool = False
    load_seen: asyncio.Event = field(default_factory=asyncio.Event)
    chooser: JsonObject | None = None
    chooser_event: asyncio.Event = field(default_factory=asyncio.Event)

    def session_for(self, session_id: str) -> Session:
        if session_id == self.session.session_id:
            return self.session
        for session in self.frames.values():
            if session.session_id == session_id:
                return session
        raise LayerError(STALE_REF, "the frame holding that element is gone",
                         hint="call observe")


@dataclass(frozen=True)
class WebOptions:
    profile: Path
    headless: bool = True
    mock_keychain: bool = False
    extra_args: tuple[str, ...] = ()


def default_options() -> WebOptions:
    return WebOptions(profile=default_profile())


def _frontmost_bundle() -> str | None:
    """The app a person is using, so a window opening does not take it from them."""
    if sys.platform != "darwin":
        return None
    asn = subprocess.run(["lsappinfo", "front"], capture_output=True, text=True, check=False)
    info = subprocess.run(
        ["lsappinfo", "info", "-only", "bundleid", asn.stdout.strip()],
        capture_output=True, text=True, check=False,
    )
    _, _, value = info.stdout.partition("=")
    bundle = value.strip().strip('"')
    return bundle if bundle and bundle != "NULL" else None


class WebBackend:
    name = "web"

    def __init__(self, options: WebOptions | None = None) -> None:
        self._options = options or default_options()
        self._chrome: ChromeProcess | None = None
        self._conn: Connection | None = None
        self._pages: dict[SurfaceId, Page] = {}
        self._by_target: dict[str, Page] = {}
        self._next_surface = 1
        self._tasks: set[asyncio.Task[None]] = set()
        self._open_lock = asyncio.Lock()

    @property
    def _connection(self) -> Connection:
        if self._conn is None:
            raise LayerError(BACKEND_UNAVAILABLE, "the browser is not running")
        return self._conn

    async def start(self) -> None:
        front = None if self._options.headless else _frontmost_bundle()
        extra = list(self._options.extra_args)
        if self._options.mock_keychain:
            extra.append("--use-mock-keychain")
        try:
            self._chrome = await launch_chrome(
                self._options.profile, headless=self._options.headless, extra_args=tuple(extra)
            )
        except ChromeNotFound as exc:
            raise LayerError(BACKEND_UNAVAILABLE, str(exc)) from exc
        conn = Connection(self._chrome.transport)
        self._conn = conn
        conn.start()
        conn.on("Target.attachedToTarget", self._on_attached)
        conn.on("Target.detachedFromTarget", self._on_detached)
        conn.on("Target.targetInfoChanged", self._on_target_changed)
        await conn.send("Target.setDiscoverTargets", {"discover": True})
        await conn.send(
            "Target.setAutoAttach",
            {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
        )
        if front is not None:
            await asyncio.sleep(0.5)
            subprocess.run(["open", "-b", front], check=False)

    async def stop(self) -> None:
        if self._conn is None or self._chrome is None:
            return
        try:
            await self._conn.send("Browser.close", timeout=3)
        except (CdpTimeout, CdpDisconnected) as exc:
            log.info("browser did not take Browser.close: %s", exc)
        await self._chrome.stop()
        await self._conn.close()
        self._conn = None

    # Target bookkeeping

    def _spawn(self, work: Awaitable[None]) -> None:
        async def runner() -> None:
            try:
                await work
            except (CdpError, CdpTimeout, CdpDisconnected) as exc:
                log.info("page setup interrupted: %s", exc)

        task = asyncio.create_task(runner())
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def _on_attached(self, params: JsonObject) -> None:
        info = params["targetInfo"]
        if info["type"] != "page":
            return
        session = self._connection.session(params["sessionId"])
        surface = SurfaceId(f"web:{self._next_surface}")
        self._next_surface += 1
        page = Page(surface, info["targetId"], session, title=info["title"], url=info["url"])
        self._pages[surface] = page
        self._by_target[info["targetId"]] = page
        self._spawn(self._setup(page))

    def _on_detached(self, params: JsonObject) -> None:
        target = params.get("targetId")
        page = self._by_target.pop(target, None) if target else None
        if page is not None:
            self._pages.pop(page.surface, None)

    def _on_target_changed(self, params: JsonObject) -> None:
        info = params["targetInfo"]
        page = self._by_target.get(info["targetId"])
        if page is not None:
            page.title = info["title"]
            page.url = info["url"]

    async def _setup(self, page: Page) -> None:
        await self._subscribe(page, page.session)
        await self._enable(page, page.session)
        frames = await page.session.send("Page.getFrameTree")
        page.tracker.main_frame_id = frames["frameTree"]["frame"]["id"]
        page.ready.set()

    async def _enable(self, page: Page, session: Session) -> None:
        for domain in ("Page", "Runtime", "DOM", "Accessibility", "Network"):
            await session.send(f"{domain}.enable")
        await session.send("Page.setLifecycleEventsEnabled", {"enabled": True})
        await session.send("Emulation.setFocusEmulationEnabled", {"enabled": True})
        await session.send("Runtime.addBinding", {"name": BINDING, "executionContextName": WORLD})
        await session.send(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": OBSERVER_JS, "worldName": WORLD, "runImmediately": True},
        )
        await session.send("Page.setInterceptFileChooserDialog", {"enabled": True})
        await session.send(
            "Target.setAutoAttach",
            {"autoAttach": True, "waitForDebuggerOnStart": False, "flatten": True},
        )

    def _subscribe(self, page: Page, session: Session) -> Awaitable[None]:
        tracker = page.tracker
        session.on("Network.requestWillBeSent", tracker.on_request)
        session.on("Network.responseReceived", tracker.on_response)
        session.on("Network.loadingFinished", tracker.on_finished)
        session.on("Network.loadingFailed", tracker.on_finished)
        session.on("Page.frameNavigated", tracker.on_navigated)
        session.on("Page.frameStartedLoading", lambda p: tracker.on_frame_loading(p, True))
        session.on("Page.frameStoppedLoading", lambda p: tracker.on_frame_loading(p, False))
        session.on("Page.loadEventFired", lambda _: page.load_seen.set())
        session.on("Page.javascriptDialogOpening", lambda p: self._on_dialog(page, session, p))
        session.on("Page.fileChooserOpened", lambda p: self._on_chooser(page, p))
        session.on("Runtime.bindingCalled", lambda p: self._on_binding(page, p))
        session.on(
            "Runtime.executionContextCreated", lambda p: self._on_context(page, session, p)
        )
        session.on(
            "Target.attachedToTarget", lambda p: self._on_frame_attached(page, session, p)
        )
        session.on("Target.detachedFromTarget", lambda p: self._on_frame_detached(page, p))
        return _done()

    def _on_binding(self, page: Page, params: JsonObject) -> None:
        if params.get("name") != BINDING:
            return
        if params.get("payload") == "submit-blocked":
            page.tracker.note("the page tried to submit a form and the commit guard stopped it")
        else:
            page.tracker.touch()

    def _on_context(self, page: Page, session: Session, params: JsonObject) -> None:
        context = params["context"]
        if context.get("name") == WORLD:
            contexts = page.armed_contexts.setdefault(session.session_id, set())
            contexts.add(int(context["id"]))

    def _on_dialog(self, page: Page, session: Session, params: JsonObject) -> None:
        kind = params.get("type", "dialog")
        message = str(params.get("message", ""))[:200]
        page.tracker.note(f"a {kind} dialog opened and was dismissed: {message}")
        self._spawn(_ignore(session.send("Page.handleJavaScriptDialog", {"accept": False})))

    def _on_chooser(self, page: Page, params: JsonObject) -> None:
        page.chooser = params
        page.chooser_event.set()

    def _on_frame_attached(self, page: Page, parent: Session, params: JsonObject) -> None:
        info = params["targetInfo"]
        if info["type"] != "iframe":
            return
        child = self._connection.session(params["sessionId"])
        page.frames[info["targetId"]] = child
        self._spawn(self._setup_frame(page, child))

    async def _setup_frame(self, page: Page, session: Session) -> None:
        await self._subscribe(page, session)
        await self._enable(page, session)

    def _on_frame_detached(self, page: Page, params: JsonObject) -> None:
        target = params.get("targetId")
        if target:
            page.frames.pop(target, None)

    # Surfaces

    def _page(self, surface: SurfaceId) -> Page:
        page = self._pages.get(surface)
        if page is None:
            raise LayerError(NO_SURFACE, f"{surface} is not open", hint="call surfaces()")
        return page

    async def surfaces(self) -> list[Surface]:
        return [
            self._describe(p) for p in self._pages.values()
            if not p.url.startswith(("chrome://", "devtools://", "chrome-extension://"))
        ]

    def _describe(self, page: Page) -> Surface:
        return Surface(page.surface, "web", page.title, page.url)

    async def describe(self, surface: SurfaceId) -> Surface:
        return self._describe(self._page(surface))

    async def open(self, target: str, *, guard: bool = True) -> Surface:
        url = target if "://" in target or target.startswith(("about:", "data:")) else (
            f"https://{target}"
        )
        async with self._open_lock:
            page = await self._blank_page() or await self._new_window()
            page.reserved = True
        await page.ready.wait()
        if guard:
            await self._install_guard(page)
        await self._navigate(page, url)
        return self._describe(page)

    async def _blank_page(self) -> Page | None:
        return next(
            (p for p in self._pages.values() if p.url == "about:blank" and not p.reserved), None
        )

    async def _new_window(self) -> Page:
        created = await self._connection.send(
            "Target.createTarget", {"url": "about:blank", "newWindow": True, "background": True}
        )
        for _ in range(200):
            page = self._by_target.get(created["targetId"])
            if page is not None:
                return page
            await asyncio.sleep(0.01)
        raise LayerError(BACKEND_UNAVAILABLE, "the new window did not attach")

    async def _install_guard(self, page: Page) -> None:
        added = await page.session.send(
            "Page.addScriptToEvaluateOnNewDocument",
            {"source": GUARD_JS, "worldName": WORLD, "runImmediately": True},
        )
        page.guard_script = added["identifier"]

    async def _navigate(self, page: Page, url: str) -> None:
        mark = page.tracker.mark()
        page.load_seen.clear()
        result = await page.session.send("Page.navigate", {"url": url})
        if "errorText" in result:
            raise LayerError(NOT_ACTIONABLE, f"could not load {url}: {result['errorText']}")
        try:
            await asyncio.wait_for(page.load_seen.wait(), _OPEN_BUDGET)
        except TimeoutError:
            log.info("load event not seen within budget for %s", url)
        await page.tracker.wait(mark, 2.0)

    async def close_surface(self, surface: SurfaceId) -> None:
        page = self._page(surface)
        await self._connection.send("Target.closeTarget", {"targetId": page.target_id})

    # Reading

    async def read_tree(self, surface: SurfaceId) -> list[RawNode]:
        page = self._page(surface)
        await page.ready.wait()
        for attempt in range(_READ_ATTEMPTS):
            try:
                return [await self._read_page(page)]
            except CdpError as exc:  # the document was swapped under the read
                log.debug("tree read attempt %d failed: %s", attempt, exc)
                await asyncio.sleep(_READ_RETRY)
        return [await self._read_page(page)]

    async def _read_page(self, page: Page) -> RawNode:
        tree = await page.session.send("Page.getFrameTree")
        root = await self._read_frame(page, page.session, tree["frameTree"], is_oopif=False)
        if root is None:
            return RawNode(None, Role.DOCUMENT, "RootWebArea", page.title)
        # A frame tree only lists frames in its own process; out-of-process ones are targets.
        for frame_id, child in tuple(page.frames.items()):
            await self._attach_out_of_process(page, frame_id, child, root)
        return root

    async def _attach_out_of_process(
        self, page: Page, frame_id: str, child: Session, root: RawNode
    ) -> None:
        try:
            parent, owner = await self._owner_of(page, frame_id, child)
            tree = await child.send("Page.getFrameTree")
            subtree = await self._read_frame(page, child, tree["frameTree"], is_oopif=True)
        except CdpError as exc:
            log.debug("skipping out-of-process frame %s: %s", frame_id, exc)
            return
        if subtree is not None:
            host = _find_handle(root, WebHandle(parent.session_id, owner))
            (host or root).children.append(subtree)

    async def _owner_of(
        self, page: Page, frame_id: str, child: Session
    ) -> tuple[Session, int]:
        candidates = [page.session, *(s for s in page.frames.values() if s is not child)]
        last: CdpError | None = None
        for session in candidates:
            try:
                owner = await session.send("DOM.getFrameOwner", {"frameId": frame_id})
            except CdpError as exc:
                last = exc
                continue
            return session, int(owner["backendNodeId"])
        raise last or CdpError("DOM.getFrameOwner", 0, "no session owns the frame")

    async def _read_frame(
        self, page: Page, session: Session, frame_node: JsonObject, is_oopif: bool
    ) -> RawNode | None:
        frame_id = frame_node["frame"]["id"]
        params: JsonObject = {} if is_oopif else {"frameId": frame_id}
        tree = await session.send("Accessibility.getFullAXTree", params)
        snapshot = await session.send(
            "DOMSnapshot.captureSnapshot", {"computedStyles": ["cursor"], "includeDOMRects": False}
        )
        facts = dom_facts(snapshot)
        root = convert(tree["nodes"], session.session_id, facts)
        if root is None:
            return None
        mark_clickable(root, session.session_id, facts)
        for child in frame_node.get("childFrames", []):
            await self._attach_child(page, session, child, root)
        return root

    async def _attach_child(
        self, page: Page, session: Session, child: JsonObject, parent_root: RawNode
    ) -> None:
        frame_id = child["frame"]["id"]
        try:
            owner = await session.send("DOM.getFrameOwner", {"frameId": frame_id})
            subtree = await self._read_frame(page, session, child, is_oopif=False)
        except CdpError as exc:
            log.debug("skipping frame %s: %s", frame_id, exc)
            return
        if subtree is None:
            return
        host = _find_handle(parent_root, WebHandle(session.session_id, owner["backendNodeId"]))
        (host or parent_root).children.append(subtree)

    # Commit facts

    async def _call_on(
        self,
        session: Session,
        backend_id: int,
        function: str,
        args: list[JsonObject] | None = None,
        by_value: bool = True,
    ) -> JsonObject:
        resolved = await session.send("DOM.resolveNode", {"backendNodeId": backend_id})
        object_id = resolved["object"]["objectId"]
        try:
            return await session.send("Runtime.callFunctionOn", {
                "functionDeclaration": function,
                "objectId": object_id,
                "arguments": args or [],
                "returnByValue": by_value,
            })
        finally:
            if by_value:
                await session.send("Runtime.releaseObject", {"objectId": object_id})

    async def commit_facts(self, surface: SurfaceId, handle: Handle) -> CommitFacts:
        page = self._page(surface)
        web = _web(handle)
        session = page.session_for(web.session_id)
        result = await self._call_on(session, web.backend_id, COMMIT_FACTS_JS)
        value = result["result"].get("value") or {}
        return CommitFacts(bool(value.get("submit")), bool(value.get("search")))

    async def key_context(self, surface: SurfaceId, keys: str) -> KeyContext:
        strokes = keymap.parse_keys(keys)
        if not any(stroke.is_return for stroke in strokes):
            return KeyContext()
        page = self._page(surface)
        for session in (page.session, *page.frames.values()):
            evaluated = await session.send("Runtime.evaluate", {
                "expression": f"({KEY_CONTEXT_JS}).call(window)", "returnByValue": True,
            })
            value = evaluated["result"].get("value") or {}
            if value.get("submit"):
                return KeyContext(True, str(value.get("detail", "")))
        return KeyContext()

    async def arm_commit(self, surface: SurfaceId, milliseconds: int) -> None:
        page = self._page(surface)
        expression = (
            f"window.__agentlayerGuard && (window.__agentlayerGuard.armedUntil"
            f" = Date.now() + {int(milliseconds)}); 1"
        )
        for session in (page.session, *page.frames.values()):
            for context in tuple(page.armed_contexts.get(session.session_id, ())):
                try:
                    await session.send(
                        "Runtime.evaluate", {"expression": expression, "contextId": context}
                    )
                except CdpError:
                    page.armed_contexts[session.session_id].discard(context)

    # Settling

    def begin(self, surface: SurfaceId) -> Mark:
        return self._page(surface).tracker.mark()

    async def settle(self, surface: SurfaceId, mark: Mark, budget: float) -> Settle:
        return await self._page(surface).tracker.wait(mark, budget)

    # Input

    async def perform(self, surface: SurfaceId, handle: Handle, action: Action) -> Delivery:
        page = self._page(surface)
        web = _web(handle)
        try:
            return await self._perform(page, web, action)
        except CdpTimeout as exc:
            raise LayerError(
                NOT_ACTIONABLE, f"the page did not answer: {exc}", delivery=Delivery.UNCERTAIN,
                hint="read the UI before doing anything else",
            ) from exc
        except CdpError as exc:
            detail = exc.detail.lower()
            if any(word in detail for word in _GONE_MESSAGES):
                raise LayerError(
                    STALE_REF, f"the element no longer exists: {exc.detail}",
                    hint="call observe and use a ref from it",
                ) from exc
            raise LayerError(NOT_ACTIONABLE, f"the page refused: {exc.detail}") from exc

    async def _perform(self, page: Page, web: WebHandle, action: Action) -> Delivery:
        kind = action.kind
        handlers: dict[ActionKind, Callable[[], Awaitable[Delivery]]] = {
            ActionKind.PRESS: lambda: self._click(page, web, 1, "left"),
            ActionKind.DOUBLE: lambda: self._click(page, web, 2, "left"),
            ActionKind.RIGHT: lambda: self._click(page, web, 1, "right"),
            ActionKind.HOVER: lambda: self._hover(page, web),
            ActionKind.FOCUS: lambda: self._focus(page, web),
            ActionKind.SET: lambda: self._set_text(page, web, action.text or ""),
            ActionKind.APPEND: lambda: self._append_text(page, web, action.text or ""),
            ActionKind.SCROLL: lambda: self._scroll(page, web, action.direction),
            ActionKind.UPLOAD: lambda: self._upload(page, web, action.paths),
        }
        handler = handlers.get(kind)
        if handler is None:
            raise LayerError(NOT_SUPPORTED, f"{kind.value} is handled by a widget driver")
        return await handler()

    async def _frame_offset(self, page: Page, session: Session) -> tuple[float, float]:
        """Where an out-of-process frame sits in the page; input always goes to the page."""
        if session is page.session:
            return (0.0, 0.0)
        target_id = next(t for t, s in page.frames.items() if s is session)
        owner = await page.session.send("DOM.getFrameOwner", {"frameId": target_id})
        quads = await page.session.send(
            "DOM.getContentQuads", {"backendNodeId": owner["backendNodeId"]}
        )
        quad = quads["quads"][0]
        return (float(quad[0]), float(quad[1]))

    async def _box(self, session: Session, backend_id: int) -> tuple[float, float, float, float]:
        try:
            result = await session.send("DOM.getContentQuads", {"backendNodeId": backend_id})
        except CdpError as exc:
            raise LayerError(NOT_ACTIONABLE, f"the element has no box: {exc.detail}") from exc
        for quad in result["quads"]:
            xs, ys = quad[0::2], quad[1::2]
            box = (min(xs), min(ys), max(xs), max(ys))
            if box[2] - box[0] >= 1 and box[3] - box[1] >= 1:
                return box
        raise LayerError(NOT_ACTIONABLE, "the element has no visible box")

    async def _pointer_target(self, session: Session, web: WebHandle) -> int:
        """Elements that are visually hidden, such as custom checkboxes, click through a label."""
        try:
            await self._box(session, web.backend_id)
        except LayerError:
            resolved = await self._call_on(
                session, web.backend_id, LABEL_OF_JS, by_value=False
            )
            label_object = resolved["result"].get("objectId")
            if label_object is None:
                raise
            described = await session.send("DOM.describeNode", {"objectId": label_object})
            await session.send("Runtime.releaseObject", {"objectId": label_object})
            return int(described["node"]["backendNodeId"])
        return web.backend_id

    async def _stable_point(self, session: Session, backend_id: int) -> tuple[float, float]:
        await session.send("DOM.scrollIntoViewIfNeeded", {"backendNodeId": backend_id})
        previous = None
        for _ in range(_STABLE_ATTEMPTS):
            box = await self._box(session, backend_id)
            if box == previous:
                return ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
            previous = box
            await asyncio.sleep(_STABLE_WAIT)
        raise LayerError(NOT_ACTIONABLE, "the element kept moving", hint="retry after it settles")

    async def _check_hit(
        self, session: Session, backend_id: int, x: float, y: float
    ) -> None:
        hit = await session.send("DOM.getNodeForLocation", {
            "x": int(x), "y": int(y),
            "includeUserAgentShadowDOM": True, "ignorePointerEventsNone": False,
        })
        hit_id = int(hit["backendNodeId"])
        if hit_id == backend_id:
            return
        resolved = await session.send("DOM.resolveNode", {"backendNodeId": hit_id})
        hit_object = resolved["object"]["objectId"]
        result = await self._call_on(
            session, backend_id, RELATED_JS, [{"objectId": hit_object}]
        )
        await session.send("Runtime.releaseObject", {"objectId": hit_object})
        if not result["result"].get("value"):
            name = await session.send("DOM.describeNode", {"backendNodeId": hit_id})
            raise LayerError(
                NOT_ACTIONABLE,
                f"something else is on top of the element: {name['node']['nodeName']}",
                hint="close the overlay or scroll, then retry",
            )

    async def _aim(self, page: Page, web: WebHandle) -> tuple[float, float]:
        session = page.session_for(web.session_id)
        backend = await self._pointer_target(session, web)
        x, y = await self._stable_point(session, backend)
        await self._check_hit(session, backend, x, y)
        offset_x, offset_y = await self._frame_offset(page, session)
        return x + offset_x, y + offset_y

    async def _click(self, page: Page, web: WebHandle, count: int, button: str) -> Delivery:
        x, y = await self._aim(page, web)
        send = page.session.send
        pointer = {"x": x, "y": y}
        await send("Input.dispatchMouseEvent", {"type": "mouseMoved", **pointer},
                   timeout=_INPUT_TIMEOUT)
        for click in range(1, count + 1):
            for phase in ("mousePressed", "mouseReleased"):
                await send(
                    "Input.dispatchMouseEvent",
                    {"type": phase, **pointer, "button": button, "clickCount": click},
                    timeout=_INPUT_TIMEOUT,
                )
        return Delivery.DELIVERED

    async def _hover(self, page: Page, web: WebHandle) -> Delivery:
        x, y = await self._aim(page, web)
        await page.session.send(
            "Input.dispatchMouseEvent", {"type": "mouseMoved", "x": x, "y": y},
            timeout=_INPUT_TIMEOUT,
        )
        return Delivery.DELIVERED

    async def _focus(self, page: Page, web: WebHandle) -> Delivery:
        session = page.session_for(web.session_id)
        await session.send("DOM.focus", {"backendNodeId": web.backend_id})
        return Delivery.DELIVERED

    async def _set_text(self, page: Page, web: WebHandle, text: str) -> Delivery:
        session = page.session_for(web.session_id)
        await self._call_on(session, web.backend_id, SELECT_ALL_JS)
        if text:
            await session.send("Input.insertText", {"text": text}, timeout=_INPUT_TIMEOUT)
        else:
            await self._press_stroke(session, keymap.parse_chord("Backspace"))
        return Delivery.DELIVERED

    async def _append_text(self, page: Page, web: WebHandle, text: str) -> Delivery:
        session = page.session_for(web.session_id)
        await self._call_on(session, web.backend_id, CARET_END_JS)
        await session.send("Input.insertText", {"text": text}, timeout=_INPUT_TIMEOUT)
        return Delivery.DELIVERED

    async def _scroll(self, page: Page, web: WebHandle, direction: str) -> Delivery:
        session = page.session_for(web.session_id)
        vectors = {
            "up": (0, -_SCROLL_FRACTION), "down": (0, _SCROLL_FRACTION),
            "left": (-_SCROLL_FRACTION, 0), "right": (_SCROLL_FRACTION, 0),
        }
        dx, dy = vectors[direction]
        await self._call_on(
            session, web.backend_id, SCROLL_JS, [{"value": dx}, {"value": dy}]
        )
        return Delivery.DELIVERED

    async def _upload(self, page: Page, web: WebHandle, paths: tuple[str, ...]) -> Delivery:
        missing = [p for p in paths if not Path(p).is_file()]
        if missing:
            raise LayerError(INVALID_ARGS, f"no such file: {missing[0]}")
        session = page.session_for(web.session_id)
        await session.send(
            "DOM.setFileInputFiles",
            {"files": [str(Path(p).resolve()) for p in paths], "backendNodeId": web.backend_id},
        )
        return Delivery.DELIVERED

    async def key(self, surface: SurfaceId, keys: str) -> Delivery:
        page = self._page(surface)
        for stroke in keymap.parse_keys(keys):
            await self._press_stroke(page.session, stroke)
        return Delivery.DELIVERED

    async def _press_stroke(self, session: Session, stroke: keymap.KeyStroke) -> None:
        base: JsonObject = {
            "key": stroke.key, "code": stroke.code,
            "windowsVirtualKeyCode": stroke.virtual, "nativeVirtualKeyCode": stroke.virtual,
            "modifiers": stroke.modifiers,
        }
        down: JsonObject = {**base, "type": "keyDown" if stroke.text else "rawKeyDown"}
        if stroke.text:
            down["text"] = stroke.text
            down["unmodifiedText"] = stroke.text
        if stroke.commands:
            down["commands"] = list(stroke.commands)
        await session.send("Input.dispatchKeyEvent", down, timeout=_INPUT_TIMEOUT)
        await session.send(
            "Input.dispatchKeyEvent", {**base, "type": "keyUp"}, timeout=_INPUT_TIMEOUT
        )

    # Used by widget drivers

    def page(self, surface: SurfaceId) -> Page:
        return self._page(surface)

    async def call_on(
        self, surface: SurfaceId, handle: Handle, function: str,
        args: list[object] | None = None,
    ) -> Any:
        """Run a read-only function on an element and return its JSON result."""
        page = self._page(surface)
        web = _web(handle)
        session = page.session_for(web.session_id)
        encoded = [{"value": a} for a in (args or [])]
        result = await self._call_on(session, web.backend_id, function, encoded)
        if "exceptionDetails" in result:
            raise LayerError(
                NOT_ACTIONABLE,
                f"page script failed: {json.dumps(result['exceptionDetails'])[:200]}",
            )
        return result["result"].get("value")

    async def type_keys(self, surface: SurfaceId, handle: Handle, text: str) -> None:
        """Character-by-character key events, for fields that ignore inserted text."""
        page = self._page(surface)
        web = _web(handle)
        session = page.session_for(web.session_id)
        for character in text:
            if character.isascii() and character.isprintable():
                stroke = keymap.parse_chord(character if character != " " else "space")
                await self._press_stroke(session, stroke)
            else:
                await session.send("Input.insertText", {"text": character})

    async def insert_text(self, surface: SurfaceId, handle: Handle, text: str) -> None:
        page = self._page(surface)
        web = _web(handle)
        session = page.session_for(web.session_id)
        await session.send("Input.insertText", {"text": text}, timeout=_INPUT_TIMEOUT)


def _web(handle: Handle) -> WebHandle:
    if not isinstance(handle, WebHandle):
        raise LayerError(INVALID_ARGS, "that element does not belong to the web backend")
    return handle


def _find_handle(root: RawNode, handle: WebHandle) -> RawNode | None:
    stack = [root]
    while stack:
        node = stack.pop()
        if node.handle == handle:
            return node
        stack.extend(node.children)
    return None


async def _done() -> None:
    return None


async def _ignore(work: Awaitable[JsonObject]) -> None:
    await work
