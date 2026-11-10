"""The platform-neutral vocabulary: what a UI looks like and what an action did.

Roles use ARIA names as the canonical vocabulary; each backend maps its native roles onto them.
"""

from __future__ import annotations

from collections.abc import Hashable, Mapping
from dataclasses import dataclass, field
from enum import IntEnum, StrEnum
from typing import TYPE_CHECKING, NewType, Protocol

if TYPE_CHECKING:
    from jiaoduan.forms.schema import FormSupport

Ref = NewType("Ref", str)
SurfaceId = NewType("SurfaceId", str)
Handle = Hashable


class Role(StrEnum):
    WINDOW = "window"
    DOCUMENT = "document"
    BUTTON = "button"
    LINK = "link"
    TEXTBOX = "textbox"
    SEARCHBOX = "searchbox"
    COMBOBOX = "combobox"
    LISTBOX = "listbox"
    OPTION = "option"
    CHECKBOX = "checkbox"
    RADIO = "radio"
    RADIOGROUP = "radiogroup"
    SWITCH = "switch"
    SLIDER = "slider"
    SPINBUTTON = "spinbutton"
    TAB = "tab"
    TABLIST = "tablist"
    TABPANEL = "tabpanel"
    MENU = "menu"
    MENUBAR = "menubar"
    MENUITEM = "menuitem"
    TREE = "tree"
    TREEITEM = "treeitem"
    TABLE = "table"
    ROW = "row"
    CELL = "cell"
    COLUMNHEADER = "columnheader"
    LIST = "list"
    LISTITEM = "listitem"
    HEADING = "heading"
    TEXT = "text"
    IMAGE = "image"
    GROUP = "group"
    FORM = "form"
    REGION = "region"
    NAVIGATION = "navigation"
    MAIN = "main"
    DIALOG = "dialog"
    ALERT = "alert"
    STATUS = "status"
    TOOLBAR = "toolbar"
    SCROLLAREA = "scrollarea"
    FRAME = "frame"
    OTHER = "other"


class State(StrEnum):
    CHECKED = "checked"
    MIXED = "mixed"
    PRESSED = "pressed"
    SELECTED = "selected"
    EXPANDED = "expanded"
    COLLAPSED = "collapsed"
    FOCUSED = "focused"
    DISABLED = "disabled"
    READONLY = "readonly"
    REQUIRED = "required"
    INVALID = "invalid"
    EDITABLE = "editable"
    MULTILINE = "multiline"
    SECURE = "secure"
    MODAL = "modal"
    SCROLLABLE = "scrollable"
    CLICKABLE = "clickable"
    HASPOPUP = "haspopup"


# Named ancestors of these roles tell otherwise identical elements apart.
SCOPE_ROLES = frozenset({
    Role.WINDOW, Role.FORM, Role.DIALOG, Role.REGION, Role.GROUP, Role.RADIOGROUP, Role.ROW,
    Role.TABPANEL, Role.TABLE, Role.NAVIGATION, Role.LISTITEM,
})
SCOPE_DEPTH = 3


@dataclass(frozen=True)
class Locator:
    """A description of an element that survives re-rendering.

    Rebinding, the trace, adapters and form fields all use this one concept. ``ordinal`` is
    only set when an adapter author writes it; rebinding ignores it because positions shifting
    is exactly how the wrong element gets clicked.
    """

    role: Role
    name: str
    scope: tuple[tuple[Role, str], ...] = ()
    ordinal: int | None = None

    def key(self) -> tuple[Role, str, tuple[tuple[Role, str], ...]]:
        return (self.role, self.name, self.scope)


@dataclass
class RawNode:
    """One element as a backend reads it, before pruning."""

    handle: Handle | None
    role: Role
    native_role: str
    name: str = ""
    value: str | None = None
    states: frozenset[State] = frozenset()
    actionable: bool = False
    children: list[RawNode] = field(default_factory=list)


@dataclass
class Node:
    """One element shown to an agent, in document order."""

    handle: Handle | None
    role: Role
    native_role: str
    name: str
    value: str | None
    states: frozenset[State]
    depth: int
    actionable: bool
    container: bool
    locator: Locator
    ref: Ref | None = None
    hidden_children: int = 0

    @property
    def wants_ref(self) -> bool:
        return self.handle is not None and (self.actionable or (self.container and bool(self.name)))


@dataclass(frozen=True)
class Surface:
    """A tab or an app window set that one agent can own."""

    id: SurfaceId
    kind: str
    title: str
    location: str


class Delivery(StrEnum):
    NOT_DELIVERED = "not_delivered"  # safe to retry
    DELIVERED = "delivered"
    UNCERTAIN = "uncertain"  # read the UI before doing anything else


class Evidence(IntEnum):
    NONE = 0  # the input was delivered and nothing more is known
    TREE = 1  # the accessibility tree shows the intended state
    DRIVER = 2  # the widget's own source of truth shows it
    NETWORK = 3  # the app's own request returned success

    @property
    def label(self) -> str:
        return self.name.lower()


class ActionKind(StrEnum):
    PRESS = "press"
    DOUBLE = "double"
    RIGHT = "right"
    HOVER = "hover"
    FOCUS = "focus"
    SET = "set"
    APPEND = "append"
    TOGGLE = "toggle"
    SELECT = "select"
    SCROLL = "scroll"
    UPLOAD = "upload"


@dataclass(frozen=True)
class Action:
    kind: ActionKind
    text: str | None = None
    direction: str = "down"
    paths: tuple[str, ...] = ()


@dataclass(frozen=True)
class Mark:
    """A point in time on one surface; settling looks only at what happened after it."""

    sequence: int
    at: float


@dataclass(frozen=True)
class RequestRecord:
    method: str
    path: str
    status: int | None


@dataclass(frozen=True)
class Settle:
    settled: bool
    elapsed_ms: int
    navigated_to: str | None = None
    requests: tuple[RequestRecord, ...] = ()
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class CommitFacts:
    """What a backend knows about whether pressing an element commits something."""

    submit_button: bool = False
    search_form: bool = False


@dataclass(frozen=True)
class KeyContext:
    """Would this key press commit? Only Return in a single-line field of a form can."""

    implicit_submit: bool = False
    detail: str = ""


class Backend(Protocol):
    """One platform. Everything above this works on any backend."""

    name: str

    async def start(self) -> None: ...

    async def stop(self) -> None: ...

    async def surfaces(self) -> list[Surface]: ...

    async def open(self, target: str, *, guard: bool = True) -> Surface: ...

    async def navigate(self, surface: SurfaceId, target: str) -> None: ...

    def form_support(self) -> FormSupport | None: ...

    async def describe(self, surface: SurfaceId) -> Surface: ...

    async def read_tree(self, surface: SurfaceId) -> list[RawNode]: ...

    async def perform(self, surface: SurfaceId, handle: Handle, action: Action) -> Delivery: ...

    async def key(self, surface: SurfaceId, keys: str) -> Delivery: ...

    async def key_context(self, surface: SurfaceId, keys: str) -> KeyContext: ...

    async def commit_facts(self, surface: SurfaceId, handle: Handle) -> CommitFacts: ...

    async def arm_commit(self, surface: SurfaceId, milliseconds: int) -> None: ...

    def begin(self, surface: SurfaceId) -> Mark: ...

    async def settle(self, surface: SurfaceId, mark: Mark, budget: float) -> Settle: ...

    async def close_surface(self, surface: SurfaceId) -> None: ...


Backends = Mapping[str, Backend]
