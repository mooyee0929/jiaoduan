from __future__ import annotations

import pytest

from jiaoduan.forms.drivers.web import marked_member
from jiaoduan.forms.fill import confirmed, toggle_word
from jiaoduan.forms.schema import FieldKind, match_option, same_phone, same_text, same_value


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


@pytest.mark.parametrize("got, wanted, same", [
    ("(626) 423-4933", "6264234933", True),
    ("+1 (626) 423-4933", "6264234933", True),
    ("626-423-4933", "+1 626 423 4933", True),
    ("62642349336264234933", "6264234933", False),
    ("16264234933", "6264234933", False),
    ("+886 626 423 4933", "6264234933", True),
    ("(626) 423-4930", "6264234933", False),
    ("ext. 6264234933", "6264234933", False),
])
def test_a_phone_reformatted_by_the_page_is_the_same_number(
    got: str, wanted: str, same: bool
) -> None:
    assert same_phone(got, wanted) is same


def test_phone_reformatting_is_only_accepted_for_tel_fields() -> None:
    assert same_text("+1 (626) 423-4933", "6264234933", "tel")
    assert not same_text("+1 (626) 423-4933", "6264234933")
    assert confirmed("+1 (626) 423-4933", "6264234933", FieldKind.TEXT, "tel")
    assert not confirmed("AdaAda", "Ada", FieldKind.TEXT, "tel")
    assert not confirmed("20265", "2026", FieldKind.NUMBER)


def test_match_option_prefers_exact_then_unique_prefix() -> None:
    options = ["No", "Not sure", "Master of Science", "Master of Business Administration"]
    assert match_option(options, "no") == ("No", ())
    chosen, candidates = match_option(options, "Master")
    assert chosen is None and len(candidates) == 2
    assert match_option(options, "Master of Sci")[0] == "Master of Science"


def test_toggle_words() -> None:
    assert toggle_word("Checked") == "on" and toggle_word("0") == "off"
