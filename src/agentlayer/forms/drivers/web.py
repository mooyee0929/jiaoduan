"""Drivers for web widgets. Each one knows where its widget really keeps its value."""

from __future__ import annotations

import asyncio
import os
from collections.abc import Sequence
from typing import Any, ClassVar, cast

from agentlayer.backends.web.backend import WebBackend
from agentlayer.backends.web.tree import WebHandle
from agentlayer.errors import NOT_ACTIONABLE, LayerError
from agentlayer.forms.fill import toggle_word
from agentlayer.forms.schema import (
    DriverContext,
    Field,
    FieldKind,
    Member,
    Probe,
    WidgetDriver,
    WriteOutcome,
    WriteStatus,
    match_option,
    normal,
    same_value,
)
from agentlayer.model import Action, ActionKind, Evidence, Handle, Locator, Node, Role, State

_OPTION_WAIT = 3.0
_OPTION_POLL = 0.1
_SCROLL_PAGES = 25

PROBE_JS = """function () {
  const has = (name) => this.hasAttribute && this.hasAttribute(name);
  const own = this.getRootNode && this.getRootNode();
  const root = own && own.getElementById ? own : document;
  const outside = ((this.getAttribute && this.getAttribute('aria-labelledby')) || '')
    .split(/\\s+/).filter(Boolean).map((id) => root.getElementById(id))
    .filter((el) => el && el !== this && !this.contains(el))
    .map((el) => (el.innerText || el.textContent || '').replace(/\\s+/g, ' ').trim())
    .filter(Boolean).join(' ');
  return {
    label: outside,
    tag: this.tagName || '',
    type: (this.getAttribute && this.getAttribute('type')) || '',
    popup: (this.getAttribute && this.getAttribute('aria-haspopup')) || '',
    autocomplete: (this.getAttribute && this.getAttribute('aria-autocomplete')) || '',
    name: (this.getAttribute && this.getAttribute('name')) || '',
    pressable: ['aria-pressed', 'aria-checked', 'aria-selected', 'data-state'].some(has),
  };
}"""

TEXT_READ_JS = """function () {
  const key = Object.keys(this).find((k) => k.startsWith('__reactProps$'));
  const props = key ? this[key] : null;
  const controlled = props && Object.prototype.hasOwnProperty.call(props, 'value');
  return {
    dom: this.isContentEditable ? this.innerText : this.value,
    framework: controlled ? String(props.value === null || props.value === undefined
      ? '' : props.value) : null,
  };
}"""

SELECT_READ_JS = """function () {
  const chosen = this.selectedOptions && this.selectedOptions[0];
  return {
    text: chosen && chosen.value !== '' ? chosen.text : '',
    options: [...this.options].filter((o) => o.value !== '' && !o.disabled).map((o) => o.text),
  };
}"""

SELECT_WRITE_JS = """function (text) {
  const option = [...this.options].find((o) => o.text === text);
  if (!option) return false;
  const proto = Object.getPrototypeOf(this);
  Object.getOwnPropertyDescriptor(proto, 'value').set.call(this, option.value);
  this.dispatchEvent(new Event('input', {bubbles: true}));
  this.dispatchEvent(new Event('change', {bubbles: true}));
  return true;
}"""

TOGGLE_READ_JS = """function () {
  if (typeof this.checked === 'boolean' && this.tagName === 'INPUT') return this.checked;
  return this.getAttribute('aria-checked') === 'true';
}"""

COMBOBOX_READ_JS = """function () {
  const clean = (t) => (t || '').replace(/\\s+/g, ' ').trim();
  const skip = (el) => el.closest(
    '[aria-live],[class*="placeholder" i],[role=listbox],[role=menu],[role=alert],[hidden]')
    || (el.checkVisibility && !el.checkVisibility());
  const typed = clean(this.value);
  const expanded = this.getAttribute('aria-expanded') === 'true';
  const fields = 'input:not([type=hidden]),select,textarea';
  const labels = (this.getAttribute('aria-labelledby') || '').split(/\\s+/).filter(Boolean)
    .map((id) => document.getElementById(id)).filter(Boolean);
  let box = this.parentElement;
  for (let depth = 0; box && depth < 5; depth++, box = box.parentElement) {
    if (box.querySelectorAll(fields).length > 1 || box.querySelector('label')
        || labels.some((label) => box.contains(label))
        || ['FORM', 'FIELDSET', 'BODY', 'LABEL'].includes(box.tagName)) break;
    const walker = document.createTreeWalker(box, NodeFilter.SHOW_TEXT);
    const parts = [];
    while (walker.nextNode()) {
      if (skip(walker.currentNode.parentElement)) continue;
      const text = clean(walker.currentNode.nodeValue);
      if (text) parts.push(text);
    }
    if (parts.length) return {text: parts.join(' '), typed, expanded};
  }
  return {text: '', typed, expanded};
}"""

POPUP_READ_JS = """function () {
  return (this.innerText || this.value || '').replace(/\\s+/g, ' ').trim();
}"""

MEMBER_READ_JS = """function () {
  const on = (name) => this.getAttribute(name) === 'true';
  return {
    aria: this.hasAttribute('aria-pressed') || this.hasAttribute('aria-checked')
      || this.hasAttribute('aria-selected'),
    selected: on('aria-pressed') || on('aria-checked') || on('aria-selected')
      || this.getAttribute('data-state') === 'on'
      || this.getAttribute('data-state') === 'checked',
    className: String(this.className || ''),
  };
}"""

FILE_READ_JS = """function () {
  return [...(this.files || [])].map((f) => f.name).join(', ');
}"""

FILE_LABEL_JS = """function () {
  const clean = (t) => (t || '').replace(/\\s+/g, ' ').trim();
  const required = this.required || this.getAttribute('aria-required') === 'true';
  const done = (label) => ({label: label.slice(0, 60), required});
  if (this.labels && this.labels.length && clean(this.labels[0].innerText)) {
    return done(clean(this.labels[0].innerText));
  }
  const aria = this.getAttribute('aria-label');
  if (aria) return done(clean(aria));
  const labelledby = (this.getAttribute('aria-labelledby') || '').split(/\\s+/)
    .map((id) => document.getElementById(id)).filter(Boolean)
    .map((el) => clean(el.innerText)).join(' ');
  if (labelledby) return done(labelledby);
  let box = this.parentElement;
  for (let depth = 0; box && depth < 5; depth++, box = box.parentElement) {
    const text = clean(box.innerText);
    if (text) return done(text);
  }
  return done(this.name || 'file');
}"""


def _web(ctx: DriverContext) -> WebBackend:
    return cast(WebBackend, ctx.backend)


async def _call(
    ctx: DriverContext, handle: Handle, function: str, args: list[object] | None = None
) -> Any:
    return await _web(ctx).call_on(ctx.surface, handle, function, args)


def _ok(got: str, wanted: str, evidence: Evidence) -> WriteOutcome:
    note = ""
    if normal(got).casefold() != normal(wanted).casefold():
        note = "the field reformatted the value"
    return WriteOutcome(WriteStatus.OK, got, evidence, note=note)


class TextDriver:
    name: ClassVar[str] = "text"
    kind: ClassVar[FieldKind] = FieldKind.TEXT

    def match(self, node: Node, probe: Probe) -> int:
        if node.role not in {Role.TEXTBOX, Role.SEARCHBOX}:
            return 0
        return 5 if not probe.autocomplete else 1

    async def options(self, ctx: DriverContext, field: Field) -> tuple[str, ...]:
        return ()

    async def _facts(self, ctx: DriverContext, field: Field) -> tuple[str, str | None]:
        facts = await _call(ctx, field.handle, TEXT_READ_JS)
        framework = facts.get("framework")
        return str(facts.get("dom") or ""), None if framework is None else str(framework)

    async def read(self, ctx: DriverContext, field: Field) -> str:
        return (await self._facts(ctx, field))[0]

    def _grade(self, dom: str, framework: str | None, wanted: str) -> WriteOutcome | None:
        """Tree evidence from the DOM; driver evidence when the framework's state agrees."""
        if not same_value(dom, wanted):
            return None
        if framework is None:
            return _ok(dom, wanted, Evidence.TREE)
        return _ok(dom, wanted, Evidence.DRIVER) if same_value(framework, wanted) else None

    async def write(self, ctx: DriverContext, field: Field, value: str) -> WriteOutcome:
        dom, framework = await self._facts(ctx, field)
        graded = self._grade(dom, framework, value)
        if graded is not None:
            return WriteOutcome(WriteStatus.UNCHANGED, dom, graded.evidence)
        await ctx.act(field.handle, Action(ActionKind.SET, text=value))
        dom, framework = await self._facts(ctx, field)
        graded = self._grade(dom, framework, value)
        if graded is not None:
            return graded
        # Some fields only accept real key events.
        await ctx.act(field.handle, Action(ActionKind.SET, text=""))
        await _web(ctx).type_keys(ctx.surface, field.handle, value)
        await asyncio.sleep(0.05)
        dom, framework = await self._facts(ctx, field)
        graded = self._grade(dom, framework, value)
        if graded is not None:
            return WriteOutcome(graded.status, dom, graded.evidence, note="needed key events")
        note = f'the framework holds "{framework}"' if framework is not None else ""
        return WriteOutcome(WriteStatus.MISMATCH, dom, note=note)


class SelectDriver:
    name: ClassVar[str] = "select"
    kind: ClassVar[FieldKind] = FieldKind.SELECT

    def match(self, node: Node, probe: Probe) -> int:
        return 10 if probe.tag == "SELECT" else 0

    async def options(self, ctx: DriverContext, field: Field) -> tuple[str, ...]:
        facts = await _call(ctx, field.handle, SELECT_READ_JS)
        return tuple(str(o) for o in facts["options"])

    async def read(self, ctx: DriverContext, field: Field) -> str:
        return str((await _call(ctx, field.handle, SELECT_READ_JS))["text"])

    async def write(self, ctx: DriverContext, field: Field, value: str) -> WriteOutcome:
        chosen, candidates = match_option(await self.options(ctx, field), value)
        if chosen is None:
            status = WriteStatus.AMBIGUOUS if candidates else WriteStatus.NOT_FOUND
            return WriteOutcome(status, await self.read(ctx, field), candidates=candidates)
        if normal(await self.read(ctx, field)) == normal(chosen):
            return WriteOutcome(WriteStatus.UNCHANGED, chosen, Evidence.DRIVER)
        await _call(ctx, field.handle, SELECT_WRITE_JS, [chosen])
        got = await self.read(ctx, field)
        if normal(got) == normal(chosen):
            return WriteOutcome(WriteStatus.OK, got, Evidence.DRIVER)
        return WriteOutcome(WriteStatus.MISMATCH, got)


class ToggleDriver:
    name: ClassVar[str] = "toggle"
    kind: ClassVar[FieldKind] = FieldKind.TOGGLE

    def match(self, node: Node, probe: Probe) -> int:
        return 10 if node.role in {Role.CHECKBOX, Role.SWITCH} else 0

    async def options(self, ctx: DriverContext, field: Field) -> tuple[str, ...]:
        return ()

    async def read(self, ctx: DriverContext, field: Field) -> str:
        return "on" if await _call(ctx, field.handle, TOGGLE_READ_JS) else "off"

    async def write(self, ctx: DriverContext, field: Field, value: str) -> WriteOutcome:
        wanted = toggle_word(value)
        if await self.read(ctx, field) == wanted:
            return WriteOutcome(WriteStatus.UNCHANGED, wanted, Evidence.DRIVER)
        await ctx.act(field.handle, Action(ActionKind.PRESS))
        got = await self.read(ctx, field)
        if got == wanted:
            return WriteOutcome(WriteStatus.OK, got, Evidence.DRIVER)
        return WriteOutcome(WriteStatus.MISMATCH, got)


def marked_member(labels: Sequence[str], class_names: Sequence[str]) -> str | None:
    """The one member carrying class tokens the others lack, e.g. "opt selected" among "opt".

    None when every member looks the same, or when several carry their own extra tokens
    ("opt on" next to "opt off"), since the style alone cannot say which means chosen.
    """
    tokens = [frozenset(name.split()) for name in class_names]
    if len(tokens) < 2:
        return None
    shared = frozenset.intersection(*tokens)
    extra = [label for label, own in zip(labels, tokens, strict=True) if own - shared]
    return extra[0] if len(extra) == 1 else None


async def _fresh_members(ctx: DriverContext, field: Field) -> tuple[Member, ...]:
    """The group's members as they are now: choosing one can re-render the rest."""
    labels = {m.label for m in field.members}
    role = Role.RADIO if field.kind is FieldKind.RADIO else Role.BUTTON
    found = tuple(
        Member(n.name, n.handle, bool({State.CHECKED, State.PRESSED} & n.states))
        for n in await ctx.nodes()
        if n.role is role and n.name in labels and n.locator.scope == field.locator.scope
        and n.handle is not None
    )
    return found or field.members


def _choose(field: Field, value: str) -> tuple[Member | None, tuple[str, ...]]:
    chosen, candidates = match_option([m.label for m in field.members], value)
    member = next((m for m in field.members if m.label == chosen), None)
    return member, candidates


class RadioDriver:
    name: ClassVar[str] = "radio"
    kind: ClassVar[FieldKind] = FieldKind.RADIO

    def match(self, node: Node, probe: Probe) -> int:
        return 10 if node.role is Role.RADIO else 0

    async def options(self, ctx: DriverContext, field: Field) -> tuple[str, ...]:
        return tuple(m.label for m in field.members)

    async def read(self, ctx: DriverContext, field: Field) -> str:
        members = await _fresh_members(ctx, field)
        return next((m.label for m in members if m.checked), "")

    async def write(self, ctx: DriverContext, field: Field, value: str) -> WriteOutcome:
        member, candidates = _choose(field, value)
        if member is None:
            status = WriteStatus.AMBIGUOUS if candidates else WriteStatus.NOT_FOUND
            return WriteOutcome(status, await self.read(ctx, field), candidates=candidates)
        if await self.read(ctx, field) == member.label:
            return WriteOutcome(WriteStatus.UNCHANGED, member.label, Evidence.TREE)
        await ctx.act(member.handle, Action(ActionKind.PRESS))
        got = await self.read(ctx, field)
        if got == member.label:
            return WriteOutcome(WriteStatus.OK, got, Evidence.TREE)
        return WriteOutcome(WriteStatus.MISMATCH, got)


class ButtonGroupDriver:
    name: ClassVar[str] = "buttons"
    kind: ClassVar[FieldKind] = FieldKind.BUTTONS

    def match(self, node: Node, probe: Probe) -> int:
        return 0

    async def options(self, ctx: DriverContext, field: Field) -> tuple[str, ...]:
        return tuple(m.label for m in field.members)

    async def _selection(self, ctx: DriverContext, field: Field) -> tuple[str, Evidence]:
        """Which member looks chosen: ARIA state first, then the one styled differently."""
        members = await _fresh_members(ctx, field)
        facts = [await _call(ctx, m.handle, MEMBER_READ_JS) for m in members]
        if any(f["aria"] or f["selected"] for f in facts):
            pressed = [m.label for m, f in zip(members, facts, strict=True) if f["selected"]]
            return (pressed[0] if pressed else ""), Evidence.DRIVER
        styled = marked_member([m.label for m in members], [str(f["className"]) for f in facts])
        return (styled, Evidence.TREE) if styled is not None else ("", Evidence.NONE)

    async def read(self, ctx: DriverContext, field: Field) -> str:
        return (await self._selection(ctx, field))[0]

    async def write(self, ctx: DriverContext, field: Field, value: str) -> WriteOutcome:
        member, candidates = _choose(field, value)
        if member is None:
            status = WriteStatus.AMBIGUOUS if candidates else WriteStatus.NOT_FOUND
            return WriteOutcome(status, candidates=candidates)
        current, evidence = await self._selection(ctx, field)
        if current == member.label:  # pressing a chosen button again would deselect it
            return WriteOutcome(WriteStatus.UNCHANGED, current, evidence)
        await ctx.act(member.handle, Action(ActionKind.PRESS))
        got, evidence = await self._selection(ctx, field)
        if got == member.label:
            return WriteOutcome(WriteStatus.OK, got, evidence)
        if evidence is Evidence.NONE:
            return WriteOutcome(
                WriteStatus.OK, member.label, Evidence.NONE,
                note="pressed, but this group shows no state that can be read back",
            )
        return WriteOutcome(WriteStatus.MISMATCH, got)


def _visible_options(nodes: Sequence[Node]) -> list[Node]:
    return [
        n for n in nodes
        if n.role in {Role.OPTION, Role.MENUITEM} and n.name and n.handle is not None
        and State.DISABLED not in n.states
    ]


async def _wait_for_options(ctx: DriverContext, known: set[Handle]) -> list[Node]:
    """Options that appeared since ``known`` was taken: older ones belong to other widgets."""
    deadline = asyncio.get_running_loop().time() + _OPTION_WAIT
    while True:
        fresh = [n for n in _visible_options(await ctx.nodes()) if n.handle not in known]
        if fresh or asyncio.get_running_loop().time() >= deadline:
            return fresh
        await asyncio.sleep(_OPTION_POLL)


class ComboboxDriver:
    """A text input with a popup list: type to filter, then click the exact option."""

    name: ClassVar[str] = "combobox"
    kind: ClassVar[FieldKind] = FieldKind.COMBOBOX

    def match(self, node: Node, probe: Probe) -> int:
        if probe.tag != "INPUT":
            return 0
        if node.role is Role.COMBOBOX:
            return 8
        return 6 if probe.autocomplete in {"list", "both"} else 0

    async def options(self, ctx: DriverContext, field: Field) -> tuple[str, ...]:
        return ()

    async def read(self, ctx: DriverContext, field: Field) -> str:
        facts = await _call(ctx, field.handle, COMBOBOX_READ_JS)
        return str(facts["text"] or (facts["typed"] if not facts["expanded"] else ""))

    async def _abandon(self, ctx: DriverContext, field: Field) -> None:
        """Leave no half-typed text and no open popup. Never Return: it can submit."""
        await ctx.act(field.handle, Action(ActionKind.SET, text=""), budget=0.5)
        await ctx.key("Escape", budget=0.5)

    async def _search(self, ctx: DriverContext, field: Field, value: str) -> list[Node]:
        known = {n.handle for n in _visible_options(await ctx.nodes())}
        await ctx.act(field.handle, Action(ActionKind.PRESS), budget=0.5)
        queries = [value]
        words = value.split()
        if len(words) > 1:
            queries.append(words[0])
        for query in queries:
            await ctx.act(field.handle, Action(ActionKind.SET, text=query), budget=0.5)
            found = await _wait_for_options(ctx, known)
            if found:
                return found
        return []

    async def write(self, ctx: DriverContext, field: Field, value: str) -> WriteOutcome:
        current = await self.read(ctx, field)
        if same_value(current, value):
            return WriteOutcome(WriteStatus.UNCHANGED, current, Evidence.DRIVER)
        options = await self._search(ctx, field, value)
        chosen, candidates = match_option([n.name for n in options], value)
        option = next((n for n in options if n.name == chosen), None)
        if option is None or option.handle is None:
            await self._abandon(ctx, field)
            status = WriteStatus.AMBIGUOUS if candidates else WriteStatus.NOT_FOUND
            note = "" if options else "no options appeared"
            return WriteOutcome(
                status, await self.read(ctx, field), candidates=candidates, note=note
            )
        await ctx.act(option.handle, Action(ActionKind.PRESS))
        got = await self.read(ctx, field)
        if same_value(got, option.name) or normal(option.name).casefold() in got.casefold():
            note = "" if same_value(option.name, value) else f'picked "{option.name}"'
            return WriteOutcome(WriteStatus.OK, got, Evidence.DRIVER, note=note)
        return WriteOutcome(WriteStatus.MISMATCH, got, note=f'clicked "{option.name}"')


class ListboxPopupDriver:
    """A button that opens a list, with virtual scrolling when the list is long."""

    name: ClassVar[str] = "listbox"
    kind: ClassVar[FieldKind] = FieldKind.LISTBOX

    def match(self, node: Node, probe: Probe) -> int:
        if probe.tag in {"INPUT", "SELECT", "TEXTAREA"}:
            return 0
        popup = probe.popup in {"listbox", "menu", "true"} or State.HASPOPUP in node.states
        return 7 if popup and node.role in {Role.BUTTON, Role.COMBOBOX} else 0

    async def options(self, ctx: DriverContext, field: Field) -> tuple[str, ...]:
        return ()

    async def read(self, ctx: DriverContext, field: Field) -> str:
        text = str(await _call(ctx, field.handle, POPUP_READ_JS))
        placeholder = text.casefold().startswith(("select", "choose", "please select", "--"))
        return "" if placeholder else text

    async def _find(
        self, ctx: DriverContext, value: str, first: list[Node]
    ) -> tuple[Node | None, tuple[str, ...]]:
        options = first
        seen: set[str] = set()
        for _ in range(_SCROLL_PAGES):
            seen.update(n.name for n in options)
            chosen, candidates = match_option([n.name for n in options], value)
            if chosen is not None:
                return next(n for n in options if n.name == chosen), ()
            if candidates:
                return None, candidates
            last = options[-1]
            if last.handle is None:
                break
            await ctx.act(last.handle, Action(ActionKind.SCROLL), budget=0.4)
            options = _visible_options(await ctx.nodes())
            if not options or {n.name for n in options} <= seen:
                break
        return None, ()

    async def write(self, ctx: DriverContext, field: Field, value: str) -> WriteOutcome:
        current = await self.read(ctx, field)
        if same_value(current, value):
            return WriteOutcome(WriteStatus.UNCHANGED, current, Evidence.DRIVER)
        known = {n.handle for n in _visible_options(await ctx.nodes())}
        await ctx.act(field.handle, Action(ActionKind.PRESS), budget=0.8)
        options = await _wait_for_options(ctx, known)
        if not options:
            await ctx.key("Escape", budget=0.3)
            return WriteOutcome(WriteStatus.NOT_FOUND, current, note="the list did not open")
        option, candidates = await self._find(ctx, value, options)
        if option is None or option.handle is None:
            await ctx.key("Escape", budget=0.3)
            status = WriteStatus.AMBIGUOUS if candidates else WriteStatus.NOT_FOUND
            return WriteOutcome(status, current, candidates=candidates)
        await ctx.act(option.handle, Action(ActionKind.PRESS))
        got = await self.read(ctx, field)
        if normal(option.name).casefold() in normal(got).casefold():
            return WriteOutcome(WriteStatus.OK, got, Evidence.DRIVER)
        return WriteOutcome(WriteStatus.MISMATCH, got, note=f'clicked "{option.name}"')


class FileDriver:
    name: ClassVar[str] = "file"
    kind: ClassVar[FieldKind] = FieldKind.FILE

    def match(self, node: Node, probe: Probe) -> int:
        return 0

    async def options(self, ctx: DriverContext, field: Field) -> tuple[str, ...]:
        return ()

    async def read(self, ctx: DriverContext, field: Field) -> str:
        return str(await _call(ctx, field.handle, FILE_READ_JS))

    async def write(self, ctx: DriverContext, field: Field, value: str) -> WriteOutcome:
        path = os.path.expanduser(value)
        if not os.path.isfile(path):
            return WriteOutcome(WriteStatus.ERROR, note=f"no such file: {value}")
        await ctx.act(field.handle, Action(ActionKind.UPLOAD, paths=(path,)), budget=3.0)
        base = os.path.basename(path)
        try:
            got = await self.read(ctx, field)
        except LayerError:
            got = ""  # the widget replaced its input; the page text below decides
        if base.casefold() in got.casefold():
            return WriteOutcome(WriteStatus.OK, got, Evidence.DRIVER)
        text = " ".join(f"{n.name} {n.value or ''}" for n in await ctx.nodes())
        if base.casefold() in text.casefold():
            return WriteOutcome(WriteStatus.OK, base, Evidence.TREE, note="the page shows the name")
        return WriteOutcome(WriteStatus.MISMATCH, got)


DRIVERS: tuple[WidgetDriver, ...] = (
    TextDriver(), SelectDriver(), ToggleDriver(), RadioDriver(), ButtonGroupDriver(),
    ComboboxDriver(), ListboxPopupDriver(), FileDriver(),
)


class WebForms:
    """What the form layer needs from a web page."""

    drivers: Sequence[WidgetDriver] = DRIVERS

    def __init__(self, backend: WebBackend) -> None:
        self._backend = backend

    async def probe(self, surface: str, node: Node) -> Probe:
        if not isinstance(node.handle, WebHandle):
            raise LayerError(NOT_ACTIONABLE, "that element does not belong to a web page")
        facts = await self._backend.call_on(surface, node.handle, PROBE_JS)  # type: ignore[arg-type]
        return Probe(
            tag=str(facts["tag"]), input_type=str(facts["type"]), popup=str(facts["popup"]),
            autocomplete=str(facts["autocomplete"]), name_attr=str(facts["name"]),
            pressable_state=bool(facts["pressable"]), label=str(facts["label"]),
        )

    async def extra_fields(self, ctx: DriverContext) -> list[Field]:
        driver = next(d for d in DRIVERS if d.kind is FieldKind.FILE)
        fields: list[Field] = []
        for handle in await self._backend.file_inputs(ctx.surface):
            info = await self._backend.call_on(ctx.surface, handle, FILE_LABEL_JS)
            label = str(info["label"])
            fields.append(Field(
                label=label, kind=FieldKind.FILE, driver=driver, handle=handle,
                locator=Locator(Role.OTHER, f"file:{label}"), required=bool(info["required"]),
            ))
        return fields
