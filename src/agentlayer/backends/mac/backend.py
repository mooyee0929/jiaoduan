"""The macOS backend: native apps through the Accessibility API.

Headless: no pointer motion, no focus changes, no clipboard. Keys go to one process with
CGEventPostToPid, so a person can keep working in another app.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import TYPE_CHECKING, Any, TypeVar

from agentlayer.backends.mac import apps as applist
from agentlayer.backends.mac import keys as keymap
from agentlayer.backends.mac import tree as axtree
from agentlayer.backends.mac.ax import (
    AX_ACTION_UNSUPPORTED,
    AX_API_DISABLED,
    AX_ATTRIBUTE_UNSUPPORTED,
    AX_CANNOT_COMPLETE,
    AX_INVALID_ELEMENT,
    AX_SUCCESS,
    ENHANCED_USER_INTERFACE,
    MANUAL_ACCESSIBILITY,
    AXApi,
    SystemAX,
)
from agentlayer.backends.mac.tree import MacHandle
from agentlayer.errors import (
    INVALID_ARGS,
    NO_SURFACE,
    NOT_ACTIONABLE,
    NOT_SUPPORTED,
    PERMISSION_DENIED,
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
    Settle,
    Surface,
    SurfaceId,
)

if TYPE_CHECKING:
    from agentlayer.forms.schema import FormSupport

log = logging.getLogger(__name__)

T = TypeVar("T")

_LAUNCH_WAIT = 10.0
_RENDERER_SETTLE = 2.0
_SETTLE_POLL = 0.12
_SETTLE_MIN = 0.1
_SCROLL_ACTIONS = {
    "up": "AXScrollUpByPage", "down": "AXScrollDownByPage",
    "left": "AXScrollLeftByPage", "right": "AXScrollRightByPage",
}


def _post_key(pid: int, keycode: int, flags: int) -> None:
    import Quartz

    for down in (True, False):
        event = Quartz.CGEventCreateKeyboardEvent(None, keycode, down)
        Quartz.CGEventSetFlags(event, flags)
        Quartz.CGEventPostToPid(pid, event)


def _post_text(pid: int, text: str) -> None:
    import Quartz

    for character in text:
        for down in (True, False):
            event = Quartz.CGEventCreateKeyboardEvent(None, 0, down)
            Quartz.CGEventKeyboardSetUnicodeString(
                event, len(character.encode("utf-16-le")) // 2, character
            )
            Quartz.CGEventPostToPid(pid, event)
        time.sleep(0.01)


def _check(status: int, what: str) -> Delivery:
    if status == AX_SUCCESS:
        return Delivery.DELIVERED
    if status == AX_INVALID_ELEMENT:
        raise LayerError(STALE_REF, f"{what}: the element no longer exists", hint="call observe")
    if status in {AX_ACTION_UNSUPPORTED, AX_ATTRIBUTE_UNSUPPORTED}:
        raise LayerError(NOT_SUPPORTED, f"{what}: the element does not support it")
    if status == AX_CANNOT_COMPLETE:
        # Typical when the action opened a menu or a dialog that is now waiting.
        return Delivery.UNCERTAIN
    if status == AX_API_DISABLED:
        raise LayerError(PERMISSION_DENIED, "Accessibility access is off for this process")
    raise LayerError(NOT_ACTIONABLE, f"{what}: the app refused (AXError {status})")


class MacBackend:
    name = "mac"

    def __init__(self, api: AXApi | None = None) -> None:
        self._api = api
        self._surfaces: dict[SurfaceId, applist.RunningApp] = {}
        self._executors: dict[int, ThreadPoolExecutor] = {}
        self._woken: set[int] = set()
        self._forms: FormSupport | None = None
        self._sequence = 0

    @property
    def api(self) -> AXApi:
        if self._api is None:
            self._api = SystemAX()
        return self._api

    async def start(self) -> None:
        if not await asyncio.to_thread(lambda: self.api.trusted()):
            raise LayerError(
                PERMISSION_DENIED,
                "this process has no Accessibility access",
                hint="System Settings > Privacy & Security > Accessibility: allow the terminal",
            )

    async def stop(self) -> None:
        for executor in self._executors.values():
            executor.shutdown(wait=False, cancel_futures=True)
        self._executors.clear()

    async def _run(self, pid: int, work: Callable[[], T]) -> T:
        """One thread per app: a hung app blocks only the agent using it."""
        executor = self._executors.get(pid)
        if executor is None:
            executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix=f"ax-{pid}")
            self._executors[pid] = executor
        return await asyncio.get_running_loop().run_in_executor(executor, work)

    def _app(self, surface: SurfaceId) -> applist.RunningApp:
        app = self._surfaces.get(surface)
        if app is None:
            raise LayerError(NO_SURFACE, f"{surface} is not open", hint="call open(app)")
        return app

    # Surfaces

    async def surfaces(self) -> list[Surface]:
        return [await self.describe(s) for s in self._surfaces]

    async def open(self, target: str, *, guard: bool = True) -> Surface:
        app = await self._find_or_launch(target)
        surface = SurfaceId(f"mac:{app.pid}")
        self._surfaces[surface] = app
        await self._wake(app)
        return await self.describe(surface)

    async def _find_or_launch(self, target: str) -> applist.RunningApp:
        try:
            return applist.find_app(target, await asyncio.to_thread(applist.running_apps))
        except LayerError as exc:
            if exc.code != "NOT_FOUND" or target.startswith("pid:"):
                raise
        await asyncio.to_thread(applist.launch_in_background, target)
        deadline = time.monotonic() + _LAUNCH_WAIT
        while time.monotonic() < deadline:
            await asyncio.sleep(0.25)
            try:
                return applist.find_app(target, await asyncio.to_thread(applist.running_apps))
            except LayerError:
                continue
        raise LayerError(NOT_ACTIONABLE, f"{target} did not start within {_LAUNCH_WAIT:g}s")

    async def _wake(self, app: applist.RunningApp) -> None:
        kind = applist.renderer(app)
        if kind is None or app.pid in self._woken:
            return
        self._woken.add(app.pid)
        attribute = ENHANCED_USER_INTERFACE if kind == "chromium" else MANUAL_ACCESSIBILITY

        def switch() -> bool:
            element = self.api.app(app.pid)
            if self.api.attribute(element, attribute) is True:
                return False
            return self.api.set(element, attribute, True) == AX_SUCCESS

        if await self._run(app.pid, switch):
            await asyncio.sleep(_RENDERER_SETTLE)

    async def navigate(self, surface: SurfaceId, target: str) -> None:
        raise LayerError(NOT_SUPPORTED, "apps have no URL to navigate to")

    async def describe(self, surface: SurfaceId) -> Surface:
        app = self._app(surface)

        def title() -> str:
            element = self.api.app(app.pid)
            window = self.api.attribute(element, "AXFocusedWindow") or self.api.attribute(
                element, "AXMainWindow"
            )
            name = self.api.attribute(window, "AXTitle") if window is not None else None
            return str(name) if isinstance(name, str) and name else app.name

        return Surface(surface, "mac", await self._run(app.pid, title), app.name)

    async def close_surface(self, surface: SurfaceId) -> None:
        raise LayerError(NOT_SUPPORTED, "the layer never quits a person's app; release it instead")

    def form_support(self) -> FormSupport:
        from agentlayer.forms.drivers.mac import MacForms

        if self._forms is None:
            self._forms = MacForms(self)
        return self._forms

    # Reading

    async def read_tree(self, surface: SurfaceId) -> list[RawNode]:
        app = self._app(surface)

        def work() -> list[RawNode]:
            element = self.api.app(app.pid)
            return axtree.read(self.api, app.pid, axtree.window_roots(self.api, element))

        return await self._run(app.pid, work)

    # Acting

    def _handle(self, handle: Handle) -> MacHandle:
        if not isinstance(handle, MacHandle):
            raise LayerError(INVALID_ARGS, "that element does not belong to the macOS backend")
        return handle

    async def perform(self, surface: SurfaceId, handle: Handle, action: Action) -> Delivery:
        mac = self._handle(handle)
        api = self.api
        kind = action.kind

        def work() -> Delivery:
            element = mac.element
            if api.attribute(element, "AXRole") is None:
                raise LayerError(STALE_REF, "the element no longer exists", hint="call observe")
            if kind is ActionKind.PRESS:
                return _check(api.perform(element, self._press_action(element)), "press")
            if kind is ActionKind.DOUBLE:
                return _check(api.perform(element, "AXOpen"), "double")
            if kind is ActionKind.RIGHT:
                return _check(api.perform(element, "AXShowMenu"), "right")
            if kind is ActionKind.FOCUS:
                return _check(api.set(element, "AXFocused", True), "focus")
            if kind in {ActionKind.SET, ActionKind.APPEND}:
                return self._write_text(element, action.text or "", kind is ActionKind.APPEND)
            if kind is ActionKind.SCROLL:
                return self._scroll(element, action.direction)
            raise LayerError(NOT_SUPPORTED, f"{kind.value} has no accessibility route on macOS")

        return await self._run(mac.pid, work)

    def _press_action(self, element: Any) -> str:
        available = self.api.actions(element)
        for name in ("AXPress", "AXPick", "AXConfirm", "AXOpen"):
            if name in available:
                return name
        raise LayerError(NOT_SUPPORTED, "the element has no press action")

    def _write_text(self, element: Any, text: str, append: bool) -> Delivery:
        if not self.api.settable(element, "AXValue"):
            raise LayerError(NOT_SUPPORTED, "this field's value cannot be written")
        current = self.api.attribute(element, "AXValue")
        value = (str(current) if isinstance(current, str) else "") + text if append else text
        self.api.set(element, "AXFocused", True)
        return _check(self.api.set(element, "AXValue", value), "set")

    def _scroll(self, element: Any, direction: str) -> Delivery:
        action = _SCROLL_ACTIONS[direction]
        current: Any = element
        for _ in range(8):
            if current is None:
                break
            if action in self.api.actions(current):
                return _check(self.api.perform(current, action), "scroll")
            current = self.api.attribute(current, "AXParent")
        raise LayerError(NOT_SUPPORTED, "nothing around the element scrolls")

    async def key(self, surface: SurfaceId, keys: str) -> Delivery:
        app = self._app(surface)
        strokes = keymap.parse_keys(keys)

        def work() -> Delivery:
            element = self.api.app(app.pid)
            focused = self.api.attribute(element, "AXFocusedUIElement")
            if focused is None:
                raise LayerError(
                    NOT_ACTIONABLE, f"{app.name} has no focused element to receive keys",
                    hint="focus a field first with act(ref, focus)",
                )
            for stroke in strokes:
                semantic = "AXConfirm" if stroke.is_return else "AXCancel" if (
                    stroke.is_escape
                ) else None
                if semantic and semantic in self.api.actions(focused):
                    self.api.perform(focused, semantic)
                    continue
                _post_key(app.pid, stroke.keycode, stroke.flags)
            return Delivery.DELIVERED

        return await self._run(app.pid, work)

    async def type_keys(self, surface: SurfaceId, text: str) -> None:
        app = self._app(surface)
        await self._run(app.pid, lambda: _post_text(app.pid, text))

    async def key_context(self, surface: SurfaceId, keys: str) -> KeyContext:
        if not any(stroke.is_return for stroke in keymap.parse_keys(keys)):
            return KeyContext()
        app = self._app(surface)

        def work() -> KeyContext:
            element = self.api.app(app.pid)
            focused = self.api.attribute(element, "AXFocusedUIElement")
            window = self.api.attribute(element, "AXFocusedWindow")
            default = self.api.attribute(window, "AXDefaultButton") if window else None
            role = self.api.attribute(focused, "AXRole") if focused is not None else None
            if default is not None and role in {"AXTextField", "AXComboBox"}:
                return KeyContext(True, "Return in this window presses its default button")
            return KeyContext()

        return await self._run(app.pid, work)

    async def commit_facts(self, surface: SurfaceId, handle: Handle) -> CommitFacts:
        mac = self._handle(handle)

        def work() -> CommitFacts:
            window = self.api.attribute(mac.element, "AXWindow")
            default = self.api.attribute(window, "AXDefaultButton") if window else None
            return CommitFacts(submit_button=default is not None and default == mac.element)

        return await self._run(mac.pid, work)

    async def arm_commit(self, surface: SurfaceId, milliseconds: int) -> None:
        return None

    # Settling

    def begin(self, surface: SurfaceId) -> Mark:
        self._sequence += 1
        return Mark(self._sequence, time.monotonic())

    async def _fingerprint(self, surface: SurfaceId) -> str:
        nodes = await self.read_tree(surface)
        digest = hashlib.sha1(usedforsecurity=False)
        stack = list(nodes)
        while stack:
            node = stack.pop()
            digest.update(f"{node.role}|{node.name}|{node.value}|{sorted(node.states)}".encode())
            stack.extend(node.children)
        return digest.hexdigest()

    async def settle(self, surface: SurfaceId, mark: Mark, budget: float) -> Settle:
        """Done when two readings in a row match. No notifications yet: this polls."""
        await asyncio.sleep(_SETTLE_MIN)
        previous = await self._fingerprint(surface)
        while True:
            elapsed = time.monotonic() - mark.at
            if elapsed >= budget:
                return Settle(False, int(elapsed * 1000))
            await asyncio.sleep(_SETTLE_POLL)
            current = await self._fingerprint(surface)
            if current == previous:
                return Settle(True, int((time.monotonic() - mark.at) * 1000))
            previous = current

    # Used by drivers

    async def value_of(self, handle: Handle) -> Any:
        mac = self._handle(handle)
        return await self._run(mac.pid, lambda: self.api.attribute(mac.element, "AXValue"))

    async def attribute(self, handle: Handle, name: str) -> Any:
        mac = self._handle(handle)
        return await self._run(mac.pid, lambda: self.api.attribute(mac.element, name))
