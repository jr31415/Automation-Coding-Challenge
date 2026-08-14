"""Tests for automation.replay.locator: resolving a durable TargetLocator
fallback chain into a live Playwright Locator during replay.
"""

from __future__ import annotations

from playwright.sync_api import Page

from automation.artifact.schema import (
    LabelForLocator,
    RoleLocator,
    StructuralLocator,
    TargetLocator,
    TextLocator,
)
from automation.replay.locator import resolve


def _load(page: Page, html: str) -> None:
    page.set_content(html)


# ---------------------------------------------------------------------------
# role locator
# ---------------------------------------------------------------------------


def test_role_locator_resolves_unique_match(page: Page):
    _load(page, "<html><body><button>Look Up</button></body></html>")
    target = TargetLocator(strategies=[RoleLocator(role="button", accessible_name="Look Up")], reasoning="x")
    res = resolve(page, target)
    assert res.matched_strategy_index == 0
    assert res.locator.text_content() == "Look Up"


def test_role_locator_fails_when_name_does_not_match(page: Page):
    _load(page, "<html><body><button>Something Else</button></body></html>")
    target = TargetLocator(strategies=[RoleLocator(role="button", accessible_name="Look Up")], reasoning="x")
    res = resolve(page, target)
    assert res.locator is None
    assert res.matched_strategy_index is None


# ---------------------------------------------------------------------------
# fallback chain: first successful strategy wins
# ---------------------------------------------------------------------------


def test_falls_through_to_second_strategy_when_first_fails(page: Page):
    # A styled <a> has role "link", not "button" -- role lookup for
    # "button" fails, so this should fall through to the text strategy.
    _load(page, '<html><body><a href="/x">Open New Sub-Account</a></body></html>')
    target = TargetLocator(
        strategies=[
            RoleLocator(role="button", accessible_name="Open New Sub-Account"),
            TextLocator(text="Open New Sub-Account", exact=True),
        ],
        reasoning="x",
    )
    res = resolve(page, target)
    assert res.matched_strategy_index == 1
    assert res.locator.get_attribute("href") == "/x"


def test_ambiguous_match_falls_through_to_next_strategy(page: Page):
    _load(page, "<html><body><p>Save</p><p>Save</p><button>Save</button></body></html>")
    target = TargetLocator(
        strategies=[
            TextLocator(text="Save", exact=True),  # matches 3 elements -- ambiguous
            RoleLocator(role="button", accessible_name="Save"),  # matches exactly 1
        ],
        reasoning="x",
    )
    res = resolve(page, target)
    assert res.matched_strategy_index == 1


def test_all_strategies_fail_returns_none_locator_with_attempts_logged(page: Page):
    _load(page, "<html><body><p>Nothing relevant</p></body></html>")
    target = TargetLocator(strategies=[RoleLocator(role="button", accessible_name="Ghost")], reasoning="x")
    res = resolve(page, target)
    assert res.locator is None
    assert res.matched_strategy_index is None
    assert len(res.attempts) == 1
    assert "0 matches" in res.attempts[0]


# ---------------------------------------------------------------------------
# labelFor locator (legacy label/value table row pattern)
# ---------------------------------------------------------------------------


def test_label_for_resolves_sibling_cell(page: Page):
    _load(
        page,
        "<html><body><table><tr><td>Savings Balance</td><td>$4823.11</td></tr></table></body></html>",
    )
    target = TargetLocator(strategies=[LabelForLocator(label="Savings Balance")], reasoning="x")
    res = resolve(page, target)
    assert res.locator is not None
    assert res.locator.text_content() == "$4823.11"


def test_label_for_generalizes_to_a_different_value_in_the_same_shape(page: Page):
    # Same locator strategy, different underlying page content -- this is
    # exactly what makes replay reusable across different input parameters.
    _load(
        page,
        "<html><body><table><tr><td>Savings Balance</td><td>$99.20</td></tr></table></body></html>",
    )
    target = TargetLocator(strategies=[LabelForLocator(label="Savings Balance")], reasoning="x")
    res = resolve(page, target)
    assert res.locator.text_content() == "$99.20"


def test_label_for_fails_when_label_not_present(page: Page):
    _load(page, "<html><body><table><tr><td>Other Field</td><td>x</td></tr></table></body></html>")
    target = TargetLocator(strategies=[LabelForLocator(label="Savings Balance")], reasoning="x")
    res = resolve(page, target)
    assert res.locator is None


# ---------------------------------------------------------------------------
# structural locator (last resort)
# ---------------------------------------------------------------------------


def test_structural_locator_by_tag_and_index(page: Page):
    _load(page, "<html><body><input value='first'><input value='second'></body></html>")
    target = TargetLocator(strategies=[StructuralLocator(tag="input", nth=1)], reasoning="x")
    res = resolve(page, target)
    assert res.locator.get_attribute("value") == "second"


def test_structural_locator_scoped_within_container_text_nth0_is_the_label_cell_itself(page: Page):
    # <tr>/<td> outside a real <table> are normalized away by the browser's
    # HTML parser and silently vanish from the DOM entirely -- always wrap
    # table fixtures in an actual <table>, matching the mock app's real markup.
    _load(
        page,
        "<html><body><table>"
        "<tr><td>Savings Balance</td><td>row1</td></tr>"
        "<tr><td>Checking Balance</td><td>row2</td></tr>"
        "</table></body></html>",
    )
    # Within the row scoped by "Checking Balance", index 0 is that same
    # label cell -- this is what the recorder's fallback strategy relies on
    # (see automation.artifact.recorder: it uses nth=1 to get the value).
    target = TargetLocator(
        strategies=[StructuralLocator(tag="td", nth=0, within_container_text="Checking Balance")],
        reasoning="x",
    )
    res = resolve(page, target)
    assert res.locator is not None
    assert res.locator.text_content() == "Checking Balance"


def test_structural_locator_nth1_within_container_gets_the_value_cell(page: Page):
    _load(
        page,
        "<html><body><table>"
        "<tr><td>Savings Balance</td><td>row1</td></tr>"
        "<tr><td>Checking Balance</td><td>row2</td></tr>"
        "</table></body></html>",
    )
    target = TargetLocator(
        strategies=[StructuralLocator(tag="td", nth=1, within_container_text="Checking Balance")],
        reasoning="x",
    )
    res = resolve(page, target)
    assert res.locator is not None
    assert res.locator.text_content() == "row2"


# ---------------------------------------------------------------------------
# legacy/hostile markup -- no test ids, table-based layout
# ---------------------------------------------------------------------------


def test_resolves_correctly_in_table_based_markup_with_no_test_ids(page: Page):
    _load(
        page,
        "<html><body><table><tr><td>Member ID:</td><td><input type='text' name='q'></td>"
        "<td><button>Look Up</button></td></tr></table></body></html>",
    )
    target = TargetLocator(strategies=[RoleLocator(role="button", accessible_name="Look Up")], reasoning="x")
    res = resolve(page, target)
    assert res.locator is not None
