"""Fill a whole form in one call, and report what each field actually holds afterwards."""

from __future__ import annotations

import asyncio
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

from jiaoduan.errors import AMBIGUOUS, INVALID_ARGS, NOT_ACTIONABLE, STALE_REF, LayerError
from jiaoduan.forms.schema import (
    DriverContext,
    Field,
    FieldKind,
    WriteOutcome,
    WriteStatus,
    collect,
    match_option,
    same_value,
)
from jiaoduan.model import Evidence, Locator
from jiaoduan.render import one_line, quote

_MAX_SHOWN_OPTIONS = 8
LINGER_SECONDS = 0.8
# Uploads often clear text fields, so they go first; choices can reveal more fields, so last.
_ORDER = {
    FieldKind.FILE: 0, FieldKind.TEXT: 1, FieldKind.SELECT: 2, FieldKind.COMBOBOX: 3,
    FieldKind.LISTBOX: 3, FieldKind.RADIO: 4, FieldKind.BUTTONS: 4, FieldKind.TOGGLE: 4,
}


@dataclass
class FormIds:
    """Field ids that stay the same as a form re-renders."""

    ids: dict[object, str] = field(default_factory=dict)

    def assign(self, fields: Sequence[Field]) -> None:
        seen: Counter[object] = Counter()
        for item in fields:
            base = item.locator.key()
            key = (base, seen[base])
            seen[base] += 1
            if key not in self.ids:
                self.ids[key] = f"f{len(self.ids) + 1}"
            item.id = self.ids[key]


@dataclass(frozen=True)
class FillLine:
    field_id: str
    label: str
    kind: str
    outcome: WriteOutcome
    locator: Locator | None = None


def toggle_word(value: str) -> str:
    lowered = value.strip().lower()
    if lowered in {"on", "true", "yes", "1", "checked"}:
        return "on"
    if lowered in {"off", "false", "no", "0", "unchecked"}:
        return "off"
    raise LayerError(INVALID_ARGS, f"{value!r} is not on or off")


def confirmed(got: str, wanted: str, kind: FieldKind) -> bool:
    """Does what a field holds now still match what was written into it?"""
    if kind is FieldKind.TOGGLE:
        return got == toggle_word(wanted)
    if kind is FieldKind.FILE:
        return wanted.rsplit("/", 1)[-1].casefold() in got.casefold()
    if kind is FieldKind.TEXT:
        # Containment would accept "AdaAda" for "Ada": exactly the doubled-autofill failure.
        return same_value(got, wanted)
    # A picked option is often shown with extra words around it, e.g. "Master of Science (MS)".
    return same_value(got, wanted) or bool(wanted) and wanted.casefold() in got.casefold()


def resolve_field(fields: Sequence[Field], key: str) -> Field:
    """A field by id, or by label when that label is unambiguous."""
    by_id = next((f for f in fields if f.id == key), None)
    if by_id is not None:
        return by_id
    matched, candidates = match_option([f.label for f in fields], key)
    if matched is not None:
        same = [f for f in fields if f.label == matched]
        if len(same) == 1:
            return same[0]
        candidates = tuple(f"{f.id} ({f.section})" for f in same)
    if candidates:
        raise LayerError(AMBIGUOUS, f"{key!r} matches several fields: {', '.join(candidates)}")
    raise LayerError(NOT_ACTIONABLE, f"no field called {key!r}", hint="call form")


def _option_text(item: Field) -> str:
    if not item.options:
        return ""
    shown = " | ".join(item.options[:_MAX_SHOWN_OPTIONS])
    more = len(item.options) - _MAX_SHOWN_OPTIONS
    return f" options: {shown}" + (f" (+{more} more)" if more > 0 else "")


def render_schema(surface: str, title: str, fields: Sequence[Field]) -> str:
    empty = sum(1 for f in fields if f.required and not f.value)
    lines = [f"form {surface} {quote(title, 60)} | {len(fields)} fields, {empty} required empty"]
    repeated = {label for label, n in Counter(f.label for f in fields).items() if n > 1}
    for item in fields:
        mark = " *" if item.required else ""
        section = f" ({one_line(item.section, 30)})" if item.label in repeated else ""
        state = " [disabled]" if item.disabled else ""
        lines.append(
            f"{item.id} {item.kind.value} {quote(item.label, 60)}{mark}{section}"
            f" = {quote(item.value, 80)}{state}{_option_text(item)}"
        )
    return "\n".join(lines)


async def fill(
    ctx: DriverContext,
    values: Mapping[str, str],
    root: str | None,
    linger: float = LINGER_SECONDS,
) -> tuple[list[FillLine], list[Field], list[Field]]:
    """Write every value, wait ``linger`` seconds, then re-read every field.

    The wait is for changes a page makes on its own a moment later, such as a profile
    autofill appending to a field; value writes do not show up as DOM mutations, so there is
    no event to wait for. Returns the per-field results, fields that appeared along the way,
    and required fields that are still empty.
    """
    initial = await collect(ctx, root, with_values=False)
    initial_ids = {f.id for f in initial}
    plan: list[tuple[str, str]] = []
    results: dict[str, WriteOutcome] = {}
    for key, value in values.items():
        try:
            plan.append((resolve_field(initial, key).id, value))
        except LayerError as exc:
            results[key] = WriteOutcome(
                WriteStatus.AMBIGUOUS if exc.code == AMBIGUOUS else WriteStatus.NOT_FOUND,
                note=exc.message,
            )
    kinds = {f.id: f.kind for f in initial}
    plan.sort(key=lambda pair: _ORDER[kinds[pair[0]]])

    written: dict[str, Field] = {}
    for field_id, value in plan:
        results[field_id], item = await _write_one(ctx, root, field_id, value)
        if item is not None:
            written[field_id] = item
    if plan and linger > 0:
        await asyncio.sleep(linger)
    return await _final_pass(ctx, root, plan, results, initial_ids, written)


async def _write_one(
    ctx: DriverContext, root: str | None, field_id: str, value: str
) -> tuple[WriteOutcome, Field | None]:
    # Fields are re-read before each write: the previous one may have re-rendered the form,
    # and an upload often replaces the inputs after it, so each is found again by identity.
    fresh = await collect(ctx, root, with_values=False)
    item = next((f for f in fresh if f.id == field_id), None)
    if item is None:
        gone = "the field disappeared before it was written"
        return WriteOutcome(WriteStatus.ERROR, note=gone), None
    if item.disabled:
        return WriteOutcome(WriteStatus.ERROR, note="the field is disabled"), item
    try:
        return await item.driver.write(ctx, item, value), item
    except LayerError as exc:
        if exc.code == STALE_REF:
            note = f"re-rendered while writing: {exc.message}"
            return WriteOutcome(WriteStatus.ERROR, note=note), item
        return WriteOutcome(WriteStatus.ERROR, note=exc.message), item


async def _vanished_value(ctx: DriverContext, item: Field | None) -> str | None:
    """What a field whose element is gone still shows, when its kind can tell.

    An upload commonly swaps its input for the file name and a Remove button; the group
    around it still says whether the file is there.
    """
    if item is None or item.kind is not FieldKind.FILE or not item.group:
        return None
    try:
        return await item.driver.read(ctx, item)
    except LayerError:
        return None


async def _final_pass(
    ctx: DriverContext,
    root: str | None,
    plan: Sequence[tuple[str, str]],
    results: dict[str, WriteOutcome],
    initial_ids: set[str],
    written: Mapping[str, Field],
) -> tuple[list[FillLine], list[Field], list[Field]]:
    final = await collect(ctx, root)
    by_id = {f.id: f for f in final}
    for field_id, value in plan:
        outcome = results[field_id]
        if outcome.status not in {WriteStatus.OK, WriteStatus.UNCHANGED}:
            continue
        if outcome.evidence is Evidence.NONE and outcome.status is WriteStatus.OK:
            continue  # nothing could be read back when it was written; reported as such
        current = by_id.get(field_id)
        if current is None:
            shown = await _vanished_value(ctx, written.get(field_id))
            if shown is not None and confirmed(shown, value, FieldKind.FILE):
                continue
            results[field_id] = WriteOutcome(
                WriteStatus.MISMATCH, note="the field is gone after the form finished updating"
            )
        elif not confirmed(current.value, value, current.kind):
            results[field_id] = WriteOutcome(
                WriteStatus.MISMATCH, got=current.value, evidence=Evidence.NONE,
                note="changed after it was written",
            )
    known = {**written, **by_id}
    lines = [
        FillLine(
            key,
            known[key].label if key in known else key,
            known[key].kind.value if key in known else "?",
            outcome,
            known[key].locator if key in known else None,
        )
        for key, outcome in results.items()
    ]
    new = [f for f in final if f.id not in initial_ids]
    empty = [f for f in final if f.required and not f.value and f.id not in results]
    return lines, new, empty


def render_fill(
    lines: Sequence[FillLine], new: Sequence[Field], empty: Sequence[Field]
) -> str:
    counts = Counter(line.outcome.status for line in lines)
    good = counts[WriteStatus.OK] + counts[WriteStatus.UNCHANGED]
    parts = [f"fill: {good}/{len(lines)} ok"]
    parts += [
        f"{counts[status]} {status.value}"
        for status in (
            WriteStatus.MISMATCH, WriteStatus.AMBIGUOUS, WriteStatus.NOT_FOUND, WriteStatus.ERROR
        )
        if counts[status]
    ]
    if new:
        parts.append("new fields: " + ", ".join(f"{f.id} {quote(f.label, 40)}" for f in new))
    if empty:
        parts.append("required empty: " + ", ".join(f"{f.id} {quote(f.label, 40)}" for f in empty))
    output = [" | ".join(parts)]
    for line in lines:
        outcome = line.outcome
        detail = f"{outcome.status.value} {quote(outcome.got, 80)}"
        if outcome.status in {WriteStatus.OK, WriteStatus.UNCHANGED}:
            detail += f" evidence {outcome.evidence.label}"
        if outcome.candidates:
            detail += " candidates: " + " | ".join(outcome.candidates)
        if outcome.note:
            detail += f" ({one_line(outcome.note, 160)})"
        output.append(f"{line.field_id} {line.kind} {quote(line.label, 40)} {detail}")
    return "\n".join(output)
