"""The macOS backend's pure parts, against a scripted AX tree (no real apps)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from agentlayer.backends.mac import apps, keys
from agentlayer.backends.mac import tree as axtree
from agentlayer.backends.mac.ax import AX_SUCCESS
from agentlayer.errors import LayerError
from agentlayer.model import Role, State


class Element:
    def __init__(self, role: str, actions: Sequence[str] = (), settable: bool = False,
                 **attributes: Any) -> None:
        self.attributes = {"AXRole": role, **attributes}
        self.action_names = list(actions)
        self.value_settable = settable

    def __repr__(self) -> str:
        return f"Element({self.attributes['AXRole']})"


class FakeAX:
    def trusted(self) -> bool:
        return True

    def app(self, pid: int) -> Any:
        raise AssertionError("not used here")

    def attribute(self, element: Any, name: str) -> Any:
        return element.attributes.get(name)

    def attributes(self, element: Any, names: Sequence[str]) -> dict[str, Any]:
        return {name: element.attributes.get(name) for name in names}

    def settable(self, element: Any, name: str) -> bool:
        return name == "AXValue" and element.value_settable

    def set(self, element: Any, name: str, value: Any) -> int:
        element.attributes[name] = value
        return AX_SUCCESS

    def actions(self, element: Any) -> list[str]:
        return element.action_names

    def perform(self, element: Any, action: str) -> int:
        return AX_SUCCESS


def window() -> Element:
    title = Element("AXStaticText", AXValue="Recipient")
    sheet = Element("AXSheet", AXChildren=[Element("AXButton", ["AXPress"], AXTitle="Replace")])
    return Element("AXWindow", AXTitle="Mail", AXChildren=[
        Element("AXTextField", settable=True, AXValue="ada@example.com", AXTitleUIElement=title),
        Element("AXTextField", subrole := "AXSecureTextField", settable=True,
                AXSubrole=subrole, AXValue="hunter2", AXDescription="Password"),
        Element("AXCheckBox", ["AXPress"], AXTitle="Remember me", AXValue=1),
        Element("AXPopUpButton", ["AXPress"], AXTitle="Account", AXValue="Work"),
        Element("AXButton", ["AXPress"], AXSubrole="AXCloseButton"),
        Element("AXScrollBar"),
        Element("AXGroup", ["AXPress"], AXIdentifier="send-row"),
        Element("AXGroup", AXIdentifier="_NS:42"),
        sheet,
    ])


def test_the_tree_maps_roles_names_and_states() -> None:
    nodes = axtree.read(FakeAX(), 7, [window()])
    root = nodes[0]
    assert root.role is Role.WINDOW and root.name == "Mail"
    children = root.children
    assert children[0].role is Role.DIALOG, "a sheet is listed first"
    email, password, remember, account, close, clickable, unnamed = children[1:]
    assert (email.role, email.name, email.value) == (Role.TEXTBOX, "Recipient", "ada@example.com")
    assert State.EDITABLE in email.states
    assert password.value is None and State.SECURE in password.states
    assert State.CHECKED in remember.states and remember.value is None
    assert account.role is Role.COMBOBOX and account.value == "Work"
    assert State.HASPOPUP in account.states
    assert close.name == "close"
    assert clickable.actionable and State.CLICKABLE in clickable.states
    assert clickable.name == "#send-row"
    assert unnamed.name == "", "generated identifiers are not names"
    assert all(n.native_role != "AXScrollBar" for n in children)


def test_reading_stops_at_the_visit_budget() -> None:
    rows = [Element("AXStaticText", AXValue=f"row {i}") for i in range(50)]
    nodes = axtree.read(FakeAX(), 7, [Element("AXGroup", AXChildren=rows)], max_visit=10)
    assert len(nodes[0].children) == 9


def test_tables_read_only_visible_rows() -> None:
    visible = [Element("AXRow", AXChildren=[Element("AXCell")])]
    table = Element("AXTable", AXChildren=[*visible, Element("AXRow")] * 100, AXVisibleRows=visible)
    nodes = axtree.read(FakeAX(), 7, [table])
    assert len(nodes[0].children) == 1


def test_key_chords_map_to_virtual_key_codes() -> None:
    stroke = keys.parse_chord("cmd+shift+z")
    assert stroke.keycode == 6 and stroke.flags == (1 << 20) | (1 << 17)
    assert keys.parse_chord("Enter").is_return
    assert [s.keycode for s in keys.parse_keys("tab escape")] == [48, 53]


@pytest.mark.parametrize("chord, words", [
    ("cmd+q", "quit or close"), ("cmd+w", "quit or close"), ("hyper+a", "unknown modifier"),
    ("cmd+f13", "unknown key"),
])
def test_dangerous_or_unknown_chords_are_refused(chord: str, words: str) -> None:
    with pytest.raises(LayerError) as caught:
        keys.parse_chord(chord)
    assert words in caught.value.message


RUNNING = [
    apps.RunningApp(10, "Notes", "com.apple.Notes", "/System/Applications/Notes.app", True),
    apps.RunningApp(11, "Notes Helper", "com.apple.Notes.helper", "", False),
    apps.RunningApp(12, "Visual Studio Code", "com.microsoft.VSCode", "/Applications/VSC.app",
                    True),
    apps.RunningApp(13, "Code Helper", "com.microsoft.VSCode.helper", "", False),
]


def test_apps_are_found_by_exact_name_unique_substring_or_pid() -> None:
    assert apps.find_app("notes", RUNNING).pid == 10
    assert apps.find_app("Studio", RUNNING).pid == 12
    assert apps.find_app("pid:13", RUNNING).pid == 13
    with pytest.raises(LayerError) as several:
        apps.find_app("code", RUNNING)
    assert several.value.code == "AMBIGUOUS"
    with pytest.raises(LayerError) as missing:
        apps.find_app("Mail", RUNNING)
    assert missing.value.code == "NOT_FOUND"


def test_chromium_browsers_are_recognised_by_bundle_id() -> None:
    chrome = apps.RunningApp(1, "Google Chrome", "com.google.Chrome", "/Applications/C.app", True)
    assert apps.renderer(chrome) == "chromium"
    assert apps.renderer(RUNNING[0]) is None
