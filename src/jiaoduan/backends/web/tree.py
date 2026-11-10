"""Convert Chrome's accessibility tree and DOM snapshot into the neutral RawNode tree."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from jiaoduan.model import RawNode, Role, State

JsonObject = dict[str, Any]

BULLET_CHARS = frozenset("•●*")
_ROLES: dict[str, Role] = {
    "RootWebArea": Role.DOCUMENT, "WebArea": Role.DOCUMENT, "button": Role.BUTTON,
    "PopUpButton": Role.BUTTON, "link": Role.LINK, "textbox": Role.TEXTBOX,
    "searchbox": Role.SEARCHBOX, "combobox": Role.COMBOBOX, "ComboBoxMenuButton": Role.COMBOBOX,
    "ComboBoxSelect": Role.COMBOBOX, "listbox": Role.LISTBOX, "option": Role.OPTION,
    "MenuListOption": Role.OPTION, "MenuListPopup": Role.LISTBOX, "checkbox": Role.CHECKBOX,
    "radio": Role.RADIO, "radiogroup": Role.RADIOGROUP, "switch": Role.SWITCH,
    "slider": Role.SLIDER, "spinbutton": Role.SPINBUTTON, "tab": Role.TAB, "tablist": Role.TABLIST,
    "tabpanel": Role.TABPANEL, "menu": Role.MENU, "menubar": Role.MENUBAR,
    "menuitem": Role.MENUITEM, "menuitemcheckbox": Role.MENUITEM, "menuitemradio": Role.MENUITEM,
    "tree": Role.TREE, "treeitem": Role.TREEITEM, "table": Role.TABLE, "grid": Role.TABLE,
    "treegrid": Role.TABLE,
    "row": Role.ROW, "cell": Role.CELL, "gridcell": Role.CELL, "columnheader": Role.COLUMNHEADER,
    "rowheader": Role.COLUMNHEADER, "list": Role.LIST, "listitem": Role.LISTITEM,
    "heading": Role.HEADING, "StaticText": Role.TEXT, "paragraph": Role.GROUP, "image": Role.IMAGE,
    "img": Role.IMAGE, "group": Role.GROUP, "form": Role.FORM, "region": Role.REGION,
    "navigation": Role.NAVIGATION, "main": Role.MAIN, "banner": Role.REGION,
    "contentinfo": Role.REGION, "complementary": Role.REGION, "search": Role.REGION,
    "dialog": Role.DIALOG, "alertdialog": Role.DIALOG, "alert": Role.ALERT, "status": Role.STATUS,
    "toolbar": Role.TOOLBAR, "Iframe": Role.FRAME, "IframePresentational": Role.FRAME,
}
_DROPPED = frozenset({"InlineTextBox", "LineBreak", "ListMarker"})
_ACTIONABLE = frozenset({
    Role.BUTTON, Role.LINK, Role.TEXTBOX, Role.SEARCHBOX, Role.COMBOBOX, Role.LISTBOX, Role.OPTION,
    Role.CHECKBOX, Role.RADIO, Role.SWITCH, Role.SLIDER, Role.SPINBUTTON, Role.TAB, Role.MENUITEM,
    Role.TREEITEM,
})
# Pointer cursors are inherited, so only this many levels up count as "this text is clickable".
_CLICK_ANCESTOR_LEVELS = 4
_NAME_PIECES = 6


@dataclass(frozen=True)
class WebHandle:
    session_id: str
    backend_id: int


@dataclass
class DomFacts:
    """Per-node facts the accessibility tree leaves out, from one DOMSnapshot call."""

    parent: dict[int, int] = field(default_factory=dict)
    pointer: set[int] = field(default_factory=set)
    secure: set[int] = field(default_factory=set)
    frame_owner_docs: dict[int, int] = field(default_factory=dict)

    def clickable_ancestor(self, backend_id: int) -> int | None:
        """The outermost element in the pointer-cursor run that starts near this node."""
        current: int | None = backend_id
        found: int | None = None
        for _ in range(_CLICK_ANCESTOR_LEVELS):
            if current is None:
                break
            if current in self.pointer:
                found = current
            elif found is not None:
                break
            current = self.parent.get(current)
        return found


def dom_facts(snapshot: JsonObject) -> DomFacts:
    strings: list[str] = snapshot.get("strings", [])
    facts = DomFacts()
    for document in snapshot.get("documents", []):
        nodes = document["nodes"]
        backend_ids: list[int] = nodes["backendNodeId"]
        parents: list[int] = nodes["parentIndex"]
        names: list[int] = nodes["nodeName"]
        attributes: list[list[int]] = nodes.get("attributes", [])
        for index, backend in enumerate(backend_ids):
            if parents[index] >= 0:
                facts.parent[backend] = backend_ids[parents[index]]
            if strings[names[index]] == "INPUT" and index < len(attributes):
                pairs = attributes[index]
                for position in range(0, len(pairs), 2):
                    is_type = strings[pairs[position]] == "type"
                    if is_type and strings[pairs[position + 1]].lower() == "password":
                        facts.secure.add(backend)
        for backend_index in nodes.get("isClickable", {}).get("index", []):
            facts.pointer.add(backend_ids[backend_index])
        layout = document["layout"]
        for layout_index, node_index in enumerate(layout["nodeIndex"]):
            styles = layout["styles"][layout_index]
            if styles and strings[styles[0]] == "pointer":
                facts.pointer.add(backend_ids[node_index])
    return facts


def _text_of(field_value: JsonObject | None) -> str:
    if not field_value:
        return ""
    value = field_value.get("value")
    return value.strip() if isinstance(value, str) else ""


def _properties(node: JsonObject) -> dict[str, Any]:
    return {p["name"]: p["value"].get("value") for p in node.get("properties", [])}


def _states(properties: dict[str, Any], role: Role) -> set[State]:
    states: set[State] = set()
    flags = {
        "focused": State.FOCUSED, "disabled": State.DISABLED, "required": State.REQUIRED,
        "selected": State.SELECTED, "readonly": State.READONLY, "multiline": State.MULTILINE,
        "modal": State.MODAL,
    }
    for name, state in flags.items():
        if properties.get(name) is True:
            states.add(state)
    if properties.get("invalid") not in (None, False, "false"):
        states.add(State.INVALID)
    if properties.get("editable") or properties.get("settable") is True and role in {
        Role.TEXTBOX, Role.SEARCHBOX, Role.COMBOBOX, Role.SLIDER, Role.SPINBUTTON,
    }:
        states.add(State.EDITABLE)
    checked = properties.get("checked")
    if checked in (True, "true"):
        states.add(State.CHECKED)
    elif checked == "mixed":
        states.add(State.MIXED)
    pressed = properties.get("pressed")
    if pressed in (True, "true"):
        states.add(State.PRESSED)
    if properties.get("hasPopup") not in (None, False, "false"):
        states.add(State.HASPOPUP)
    expanded = properties.get("expanded")
    if expanded is True:
        states.add(State.EXPANDED)
    elif expanded is False:
        states.add(State.COLLAPSED)
    return states


def _is_masked(value: str) -> bool:
    return bool(value) and set(value) <= BULLET_CHARS


def convert(nodes: list[JsonObject], session_id: str, facts: DomFacts) -> RawNode | None:
    """One frame's accessibility nodes as a tree; ignored nodes give way to their children."""
    if not nodes:
        return None
    by_id = {n["nodeId"]: n for n in nodes}

    def build(start: str) -> list[RawNode]:
        result: list[RawNode] = []
        stack: list[tuple[str, list[RawNode]]] = [(start, result)]
        while stack:
            node_id, target = stack.pop()
            node = by_id.get(node_id)
            if node is None:
                continue
            ax_role = _text_of(node.get("role"))
            children = list(reversed(node.get("childIds", [])))
            if ax_role in _DROPPED:
                continue
            if node.get("ignored"):
                placeholder: list[RawNode] = []
                target.append(_Splice(placeholder))  # type: ignore[arg-type]
                stack.extend((child, placeholder) for child in children)
                continue
            raw = _make(node, ax_role, session_id, facts)
            target.append(raw)
            stack.extend((child, raw.children) for child in children)
        return result

    root = next((n for n in nodes if "parentId" not in n), nodes[0])
    built = _unsplice(build(root["nodeId"]))
    if not built:
        return None
    return built[0]


class _Splice:
    """Stands in for an ignored node until the tree is finished, keeping sibling order."""

    def __init__(self, children: list[RawNode]) -> None:
        self.children = children


def _unsplice(items: list[RawNode]) -> list[RawNode]:
    """Replace placeholders with their children, without recursing on deep pages."""
    pending = list(items)
    flat: list[RawNode] = []
    while pending:
        item = pending.pop(0)
        if isinstance(item, _Splice):
            pending = list(item.children) + pending
        else:
            item.children = _unsplice(item.children) if item.children else []
            flat.append(item)
    return flat


def _make(node: JsonObject, ax_role: str, session_id: str, facts: DomFacts) -> RawNode:
    role = _ROLES.get(ax_role, Role.OTHER)
    properties = _properties(node)
    states = _states(properties, role)
    backend = node.get("backendDOMNodeId")
    # A label's text is already the name of the control inside or beside it.
    name = "" if ax_role == "LabelText" else _text_of(node.get("name"))
    value: str | None = _text_of(node.get("value")) or None
    if backend in facts.secure:
        states.add(State.SECURE)
    if State.SECURE in states or (value is not None and _is_masked(value)):
        states.add(State.SECURE)
        value = None
    if role in {Role.CHECKBOX, Role.RADIO, Role.SWITCH, Role.BUTTON, Role.LINK, Role.OPTION}:
        value = None
    handle = WebHandle(session_id, backend) if isinstance(backend, int) else None
    return RawNode(
        handle=handle,
        role=role,
        native_role=ax_role,
        name=name,
        value=value,
        states=frozenset(states),
        actionable=role in _ACTIONABLE,
    )


def mark_clickable(root: RawNode, session_id: str, facts: DomFacts) -> None:
    """Surface click targets that have no role, such as a styled div with a click handler."""
    stack: list[tuple[RawNode, bool]] = [(root, False)]
    while stack:
        raw, inside_control = stack.pop()
        if raw.actionable:
            inside_control = True
        elif (
            not inside_control
            and raw.native_role != "LabelText"
            and raw.role in {Role.TEXT, Role.OTHER, Role.GROUP, Role.IMAGE}
            and isinstance(raw.handle, WebHandle)
            and raw.handle.session_id == session_id
        ):
            target = facts.clickable_ancestor(raw.handle.backend_id)
            if target is not None:
                raw.handle = WebHandle(session_id, target)
                raw.actionable = True
                raw.states = raw.states | {State.CLICKABLE}
                raw.name = raw.name or _own_text(raw)
                inside_control = True
        stack.extend((child, inside_control) for child in raw.children)


def _own_text(raw: RawNode) -> str:
    """A click target with no accessible name is known by the text inside it."""
    pieces: list[str] = []
    stack = [raw]
    while stack and len(pieces) < _NAME_PIECES:
        node = stack.pop()
        if node.role is Role.TEXT and node.name:
            pieces.append(node.name)
        stack.extend(reversed(node.children))
    return " ".join(pieces)
