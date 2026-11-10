"""Read an app's accessibility tree into the neutral RawNode tree."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agentlayer.backends.mac.ax import AXApi
from agentlayer.model import RawNode, Role, State

FETCH = (
    "AXRole", "AXSubrole", "AXTitle", "AXDescription", "AXValue", "AXPlaceholderValue",
    "AXIdentifier", "AXEnabled", "AXFocused", "AXSelected", "AXExpanded", "AXChildren",
    "AXVisibleRows", "AXTitleUIElement",
)
_ROLES: dict[str, Role] = {
    "AXWindow": Role.WINDOW, "AXSheet": Role.DIALOG, "AXDrawer": Role.DIALOG,
    "AXPopover": Role.DIALOG, "AXButton": Role.BUTTON, "AXMenuButton": Role.BUTTON,
    "AXPopUpButton": Role.COMBOBOX, "AXComboBox": Role.COMBOBOX, "AXCheckBox": Role.CHECKBOX,
    "AXRadioButton": Role.RADIO, "AXRadioGroup": Role.RADIOGROUP, "AXTextField": Role.TEXTBOX,
    "AXTextArea": Role.TEXTBOX, "AXStaticText": Role.TEXT, "AXLink": Role.LINK,
    "AXSlider": Role.SLIDER, "AXIncrementor": Role.SPINBUTTON, "AXTabGroup": Role.TABLIST,
    "AXTable": Role.TABLE, "AXOutline": Role.TABLE, "AXBrowser": Role.TABLE, "AXRow": Role.ROW,
    "AXCell": Role.CELL, "AXList": Role.LIST, "AXGroup": Role.GROUP,
    "AXSplitGroup": Role.GROUP, "AXLayoutArea": Role.GROUP, "AXScrollArea": Role.SCROLLAREA,
    "AXToolbar": Role.TOOLBAR, "AXMenuBar": Role.MENUBAR, "AXMenuBarItem": Role.MENUITEM,
    "AXMenu": Role.MENU, "AXMenuItem": Role.MENUITEM, "AXImage": Role.IMAGE,
    "AXHeading": Role.HEADING, "AXWebArea": Role.DOCUMENT, "AXDisclosureTriangle": Role.BUTTON,
    "AXColorWell": Role.BUTTON, "AXProgressIndicator": Role.STATUS,
    "AXBusyIndicator": Role.STATUS, "AXDateField": Role.TEXTBOX,
}
_SUBROLES: dict[str, Role] = {
    "AXSearchField": Role.SEARCHBOX, "AXSwitch": Role.SWITCH, "AXToggle": Role.SWITCH,
    "AXTabButton": Role.TAB, "AXOutlineRow": Role.TREEITEM, "AXDialog": Role.DIALOG,
    "AXSystemDialog": Role.DIALOG,
}
_SUBROLE_NAMES = {
    "AXCloseButton": "close", "AXMinimizeButton": "minimize", "AXZoomButton": "zoom",
    "AXFullScreenButton": "full screen",
}
_SKIPPED = frozenset({
    "AXScrollBar", "AXGrowArea", "AXSplitter", "AXRuler", "AXRulerMarker", "AXValueIndicator",
    "AXHandle", "AXMatte", "AXColumn",
})
_ACTIONABLE = frozenset({
    Role.BUTTON, Role.LINK, Role.TEXTBOX, Role.SEARCHBOX, Role.COMBOBOX, Role.CHECKBOX,
    Role.RADIO, Role.SWITCH, Role.SLIDER, Role.SPINBUTTON, Role.TAB, Role.MENUITEM, Role.ROW,
    Role.TREEITEM,
})
_PRIMARY_ACTIONS = frozenset({"AXPress", "AXOpen", "AXPick", "AXConfirm"})
_TEXT_ROLES = frozenset({"AXTextField", "AXTextArea", "AXComboBox", "AXDateField"})
_MENU_ROLES = frozenset({"AXMenuBar", "AXMenuBarItem", "AXMenu", "AXMenuItem"})
_ROW_CONTAINERS = frozenset({"AXTable", "AXOutline"})
_GENERATED_ID = "_NS:"
DEFAULT_MAX_VISIT = 4000
_DEPTH_CAP = 80


@dataclass(frozen=True)
class MacHandle:
    pid: int
    element: Any


def _text(value: Any) -> str:
    return str(value).strip() if isinstance(value, str) else ""


def _children(ax_role: str, attrs: dict[str, Any]) -> list[Any]:
    source = attrs.get("AXVisibleRows") if ax_role in _ROW_CONTAINERS else None
    if source is None:
        source = attrs.get("AXChildren")
    if source is None or isinstance(source, str):
        return []
    try:
        return list(source)
    except TypeError:
        return []


def _label(api: AXApi, attrs: dict[str, Any], subrole: str) -> str:
    label = _text(attrs.get("AXTitle")) or _text(attrs.get("AXDescription"))
    title_element = attrs.get("AXTitleUIElement")
    if not label and title_element is not None:
        label = _text(api.attribute(title_element, "AXValue"))
    label = label or _text(attrs.get("AXPlaceholderValue")) or _SUBROLE_NAMES.get(subrole, "")
    identifier = _text(attrs.get("AXIdentifier"))
    if not label and identifier and not identifier.startswith(_GENERATED_ID):
        label = "#" + identifier
    return label


def describe(api: AXApi, pid: int, element: Any, attrs: dict[str, Any]) -> RawNode:
    ax_role = _text(attrs.get("AXRole"))
    subrole = _text(attrs.get("AXSubrole"))
    role = _SUBROLES.get(subrole) or _ROLES.get(ax_role, Role.OTHER)
    raw_value = attrs.get("AXValue")
    states: set[State] = set()
    value: str | None = None
    name = _label(api, attrs, subrole)
    if ax_role == "AXStaticText":
        name = name or _text(raw_value)
    elif ax_role in {"AXCheckBox", "AXRadioButton"}:
        if raw_value == 1:
            states.add(State.CHECKED)
        elif raw_value == 2:
            states.add(State.MIXED)
    elif subrole == "AXSecureTextField":
        states.add(State.SECURE)
    elif isinstance(raw_value, str):
        value = raw_value.strip() if raw_value.strip() != name else None
        if role is Role.COMBOBOX and ax_role == "AXPopUpButton":
            value = raw_value.strip()
    elif isinstance(raw_value, int | float) and not isinstance(raw_value, bool):
        value = f"{raw_value:g}"

    actions = set() if ax_role == "AXStaticText" else set(api.actions(element))
    if attrs.get("AXEnabled") is False:
        states.add(State.DISABLED)
    if attrs.get("AXFocused") is True:
        states.add(State.FOCUSED)
    if attrs.get("AXSelected") is True:
        states.add(State.SELECTED)
    expanded = attrs.get("AXExpanded")
    if isinstance(expanded, bool) and ax_role not in _MENU_ROLES:
        states.add(State.EXPANDED if expanded else State.COLLAPSED)
    if ax_role in {"AXPopUpButton", "AXMenuButton"}:
        states.add(State.HASPOPUP)
    if ax_role == "AXTextArea":
        states.add(State.MULTILINE)
    if ax_role in _TEXT_ROLES and api.settable(element, "AXValue"):
        states.add(State.EDITABLE)
    if any(a.startswith("AXScroll") for a in actions):
        states.add(State.SCROLLABLE)
    actionable = role in _ACTIONABLE or bool(actions & _PRIMARY_ACTIONS)
    if actionable and role not in _ACTIONABLE:
        states.add(State.CLICKABLE)
    return RawNode(
        handle=MacHandle(pid, element), role=role, native_role=ax_role, name=name, value=value,
        states=frozenset(states), actionable=actionable,
    )


def read(
    api: AXApi, pid: int, roots: list[Any], max_visit: int = DEFAULT_MAX_VISIT
) -> list[RawNode]:
    """Depth-first, with sheets listed first so a blocking dialog is what an agent sees first."""
    visited = 0
    result: list[RawNode] = []
    stack: list[tuple[Any, list[RawNode], int]] = [
        (root, result, 0) for root in reversed(roots)
    ]
    while stack and visited < max_visit:
        element, target, depth = stack.pop()
        visited += 1
        attrs = api.attributes(element, FETCH)
        ax_role = _text(attrs.get("AXRole"))
        if not ax_role or ax_role in _SKIPPED:
            continue
        node = describe(api, pid, element, attrs)
        target.append(node)
        if depth >= _DEPTH_CAP:
            continue
        children = _children(ax_role, attrs)
        if ax_role == "AXWindow":
            sheets = [c for c in children if api.attribute(c, "AXRole") == "AXSheet"]
            children = sheets + [c for c in children if c not in sheets]
        stack.extend((child, node.children, depth + 1) for child in reversed(children))
    return result


def window_roots(api: AXApi, app: Any) -> list[Any]:
    windows = api.attribute(app, "AXWindows")
    roots = list(windows) if windows else []
    if not roots:
        # Apps launched in the background often list no windows until asked directly.
        for name in ("AXFocusedWindow", "AXMainWindow"):
            window = api.attribute(app, name)
            if window is not None:
                roots.append(window)
                break
    return roots
