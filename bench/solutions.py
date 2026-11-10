"""What a careful agent would do through the form-level tools, written as code.

`bench.run --smoke` runs these without a model to prove every task can be passed and that
the checker accepts a correct run; `bench.parallel` runs several at once to measure the
coordination layer. They are not used in model runs.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from pathlib import Path

from jiaoduan.engine import Engine
from jiaoduan.errors import LayerError

Solution = Callable[[Engine, str, str, Path], Awaitable[str]]
AGENT = "bench"
_COUNT = re.compile(r"\| (\d+) matches")
_FIRST_NUMBER = re.compile(r"\b(\d+)\b")

APPLICANT = {
    "First name": "Ada", "Last name": "Lovelace", "Email": "ada@example.com",
    "Phone": "555 123 4567", "Country": "Taiwan", "Degree": "Master of Science",
    "School": "Riverbend University", "Are you at least 18 years old?": "Yes",
    "I agree to the privacy notice": "on",
}


async def press(engine: Engine, agent: str, name: str) -> str:
    found = await engine.observe(agent, query=name, role="button")
    ref = found.split("[", 1)[1].split("]", 1)[0]
    return await engine.act(agent, ref, "press")


async def fill_until_kept(engine: Engine, agent: str, values: dict[str, str]) -> None:
    """Fill, then write again whatever the page changed afterwards (the doubling autofill)."""
    report = await engine.fill(agent, values)
    changed = {
        label: value for label, value in values.items()
        if re.search(rf'"{re.escape(label)}" mismatch', report)
    }
    if changed:
        await engine.fill(agent, changed)


async def contact_send(engine: Engine, agent: str, target: str, resume: Path) -> str:
    await engine.open(agent, target)
    await engine.fill(agent, {"Topic": "Support", "Message": "The export button does nothing."})
    await press(engine, agent, "Send message")
    return "done"


async def application_fill(engine: Engine, agent: str, target: str, resume: Path) -> str:
    await engine.open(agent, target)
    await fill_until_kept(engine, agent, {
        "Resume/CV": str(resume), **APPLICANT, "Will you require visa sponsorship?": "No",
    })
    return "done"


async def application_visa(engine: Engine, agent: str, target: str, resume: Path) -> str:
    await engine.open(agent, target)
    await fill_until_kept(engine, agent, {
        "Resume/CV": str(resume), **APPLICANT, "Will you require visa sponsorship?": "Yes",
    })
    await engine.fill(agent, {"Visa type": "H-1B"})  # appears only after the answer above
    return "done"


async def orders_lookup(engine: Engine, agent: str, target: str, resume: Path) -> str:
    await engine.open(agent, target)
    found = await engine.observe(agent, query="#1043")
    return found.rsplit("Total:", 1)[-1].strip().strip('"')


async def orders_count(engine: Engine, agent: str, target: str, resume: Path) -> str:
    await engine.open(agent, target)
    found = await engine.observe(agent, query="Refunded", role="cell")
    match = _COUNT.search(found)
    if match is None:
        raise ValueError(f"no match count in {found[:200]}")
    return match.group(1)


async def doc_lookup(engine: Engine, agent: str, target: str, resume: Path) -> str:
    await engine.open(agent, target)
    found = await engine.observe(agent, query="written notice")
    match = _FIRST_NUMBER.search(found.split("\n", 1)[1])
    if match is None:
        raise ValueError(f"no number in {found[:200]}")
    return match.group(1)


async def checkout_wizard(engine: Engine, agent: str, target: str, resume: Path) -> str:
    await engine.open(agent, target)
    await engine.fill(agent, {
        "Full name": "Grace Hopper", "City": "Lyon, France", "Postal code": "69002",
    })
    await press(engine, agent, "Next")
    await engine.fill(agent, {
        "Delivery speed": "Express", "Gift message": "Happy birthday",
        "Delivery instructions": "Leave at the front desk",
    })
    await press(engine, agent, "Next")
    await press(engine, agent, "Place order")
    return "done"


async def settings_save(engine: Engine, agent: str, target: str, resume: Path) -> str:
    await engine.open(agent, target)
    await engine.fill(agent, {
        "Two-factor sign-in": "on", "Email digest": "Weekly", "Display name": "Ada L.",
    })
    for _ in range(3):
        result = await press(engine, agent, "Save changes")
        if "request failed" not in result:
            return "done"
    raise LayerError("NOT_ACTIONABLE", "saving kept failing")


async def mac_fill(engine: Engine, agent: str, target: str, resume: Path) -> str:
    await engine.open(agent, target)
    await engine.fill(agent, {
        "Name": "Ada Lovelace", "Subscribe to news": "on", "Size": "Large",
        "Delivery method": "Courier",
    })
    return "done"


async def mac_save(engine: Engine, agent: str, target: str, resume: Path) -> str:
    await engine.open(agent, target)
    await engine.fill(agent, {"Name": "Grace Hopper", "Delivery method": "Pickup"})
    await press(engine, agent, "Save")
    return "done"


SOLUTIONS: dict[str, Solution] = {
    "contact-send": contact_send,
    "application-fill": application_fill,
    "application-visa": application_visa,
    "orders-lookup": orders_lookup,
    "orders-count": orders_count,
    "doc-lookup": doc_lookup,
    "checkout-wizard": checkout_wizard,
    "settings-save": settings_save,
    "mac-fill": mac_fill,
    "mac-save": mac_save,
}
