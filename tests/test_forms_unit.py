from __future__ import annotations

import pytest

from agentlayer.forms.drivers.web import marked_member
from agentlayer.forms.fill import confirmed, toggle_word
from agentlayer.forms.schema import FieldKind, match_option, same_value


@pytest.mark.parametrize("classes, chosen", [
    (["opt selected", "opt"], "Yes"),
    (["opt", "opt is-active"], "No"),
    (["opt", "opt"], None),
    (["opt on", "opt off"], None),
    (["btn", "btn primary chosen", "btn"], "No"),
])
def test_the_styled_member_is_the_one_with_extra_class_tokens(
    classes: list[str], chosen: str | None
) -> None:
    labels = ["Yes", "No", "Maybe"][: len(classes)]
    assert marked_member(labels, classes) == chosen


def test_a_doubled_text_value_is_not_confirmed() -> None:
    assert not confirmed("AdaAda", "Ada", FieldKind.TEXT)
    assert confirmed("(555) 123-4567", "5551234567", FieldKind.TEXT)
    assert confirmed("Master of Science (MS)", "Master of Science", FieldKind.COMBOBOX)
    assert confirmed("resume.pdf", "/tmp/x/resume.pdf", FieldKind.FILE)
    assert confirmed("on", "yes", FieldKind.TOGGLE)


def test_same_value_ignores_formatting_but_not_content() -> None:
    assert same_value("  Ada  Lovelace ", "ada lovelace")
    assert not same_value("Ada", "Adam")


def test_match_option_prefers_exact_then_unique_prefix() -> None:
    options = ["No", "Not sure", "Master of Science", "Master of Business Administration"]
    assert match_option(options, "no") == ("No", ())
    chosen, candidates = match_option(options, "Master")
    assert chosen is None and len(candidates) == 2
    assert match_option(options, "Master of Sci")[0] == "Master of Science"


def test_toggle_words() -> None:
    assert toggle_word("Checked") == "on" and toggle_word("0") == "off"
