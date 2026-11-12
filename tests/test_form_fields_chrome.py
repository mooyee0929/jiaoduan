"""Form fields on layouts copied from real application forms, in a real headless Chrome."""

from __future__ import annotations

import json
import re
import tempfile
from pathlib import Path
from typing import Any

import pytest

from jiaoduan.engine import Engine
from jiaoduan.model import SurfaceId
from tests.web_harness import engine_with_chrome, pages_server, run

pytestmark = pytest.mark.chrome


async def page_value(engine: Engine, surface: str, expression: str) -> Any:
    backend = engine.backend_for(SurfaceId(surface))
    page = backend.page(SurfaceId(surface))  # type: ignore[attr-defined]
    result = await page.session.send(
        "Runtime.evaluate",
        {"expression": f"JSON.stringify({expression})", "returnByValue": True},
    )
    return json.loads(result["result"]["value"])


def schema_lines(schema: str) -> dict[str, str]:
    """Each field line by its quoted label."""
    found: dict[str, str] = {}
    for line in schema.splitlines()[1:]:
        match = re.match(r'f\d+ \w+ "([^"]*)"', line)
        if match:
            found[match.group(1)] = line
    return found


def test_file_fields_with_one_name_are_told_apart_by_their_group() -> None:
    async def scenario() -> tuple[str, str, dict[str, str]]:
        with tempfile.TemporaryDirectory() as tmp, pages_server() as host:
            files = {}
            for name in ("resume.pdf", "undergrad.pdf", "grad.pdf"):
                files[name] = Path(tmp) / name
                files[name].write_bytes(b"%PDF-1.4 test")
            async with engine_with_chrome() as engine:
                opened = await engine.open("a", f"http://{host}/uploads.html")
                surface = opened.split()[0]
                schema = await engine.form("a")
                lines = schema_lines(schema)
                ids = {label: lines[label].split()[0] for label in (
                    "Resume/CV", "Undergraduate Transcript", "Graduate Transcript",
                )}
                report = await engine.fill("a", {
                    ids["Resume/CV"]: str(files["resume.pdf"]),
                    ids["Undergraduate Transcript"]: str(files["undergrad.pdf"]),
                    ids["Graduate Transcript"]: str(files["grad.pdf"]),
                })
                return schema, report, await page_value(engine, surface, "window.__uploads")

    schema, report, uploads = run(scenario())
    lines = schema_lines(schema)
    assert " file " in lines["Resume/CV"] and lines["Resume/CV"].count(" *") == 1, schema
    assert " file " in lines["Graduate Transcript"], schema
    assert uploads == {"resume": "resume.pdf", "under": "undergrad.pdf", "grad": "grad.pdf"}
    assert report.startswith("fill: 3/3 ok"), report
    assert re.search(r'"Undergraduate Transcript" ok .*landed in "Undergraduate Transcript"',
                     report), report


def test_each_question_is_its_own_field_and_fill_presses_its_own_option() -> None:
    async def scenario() -> tuple[str, str, dict[str, Any]]:
        with pages_server() as host:
            async with engine_with_chrome() as engine:
                opened = await engine.open("a", f"http://{host}/questions.html")
                surface = opened.split()[0]
                schema = await engine.form("a")
                lines = schema_lines(schema)
                ack = "I Acknowledge/Accept this Statement"
                report = await engine.fill("a", {
                    "Are you legally authorized to work in the United States?": "Yes",
                    "Will you now or in the future require sponsorship?": "No",
                    "Have you worked here before?": "No",
                    "Are you at least 18 years old?": "Yes",
                    "Can you work on site three days a week?": "No",
                    lines["Arbitration Acknowledgment"].split()[0]: ack,
                    lines["GDPR Acknowledgement"].split()[0]: ack,
                })
                return schema, report, await page_value(engine, surface, "window.__answers")

    schema, report, answers = run(scenario())
    lines = schema_lines(schema)
    for label in ("Are you legally authorized to work in the United States?",
                  "Will you now or in the future require sponsorship?"):
        assert lines[label].split()[1] == "buttons", schema
        assert lines[label].endswith("options: Yes | No"), schema
    for label in ("Have you worked here before?", "Are you at least 18 years old?",
                  "Can you work on site three days a week?"):
        assert lines[label].endswith("options: Yes | No"), schema
    assert lines["GDPR Acknowledgement"].split()[1] == "radio", schema
    assert answers == {
        "auth": "Yes", "sponsor": "No", "before": "No", "adult": "Yes", "onsite": "No",
        "arbitration": True, "gdpr": True,
    }, report
    assert report.startswith("fill: 7/7 ok"), report
    assert "unchanged" not in report, report


def test_a_number_input_is_a_field_that_fill_writes_and_reads_back() -> None:
    async def scenario() -> tuple[str, str, str, dict[str, str]]:
        with pages_server() as host:
            async with engine_with_chrome() as engine:
                opened = await engine.open("a", f"http://{host}/education.html")
                surface = opened.split()[0]
                before = await engine.form("a")
                report = await engine.fill("a", {"End date year": "2026"})
                after = await engine.form("a")
                return before, report, after, await page_value(engine, surface, "window.__saved")

    before, report, after, saved = run(scenario())
    assert schema_lines(before)["End date year"].split()[1] == "number", before
    assert re.search(r'"End date year" ok "2026"', report), report
    assert 'number "End date year" * = "2026"' in after, after
    assert saved == {"end-year": "2026"}
