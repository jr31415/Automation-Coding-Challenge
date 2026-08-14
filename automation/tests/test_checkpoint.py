"""Tests for automation.replay.checkpoint: evaluating a CheckpointCondition
against the live page, including {{param}} rendering in urlMatches patterns.
"""

from __future__ import annotations

from playwright.sync_api import Page

from automation.artifact.schema import (
    ElementTextContainsCondition,
    ElementTextEqualsCondition,
    ElementVisibleCondition,
    RoleLocator,
    TargetLocator,
    UrlMatchesCondition,
)
from automation.replay.checkpoint import evaluate


def _load(page: Page, html: str) -> None:
    page.set_content(html)


# ---------------------------------------------------------------------------
# urlMatches
# ---------------------------------------------------------------------------


def test_url_matches_holds_when_pattern_found(page: Page):
    page.goto("data:text/html,<html><body>x</body></html>")
    holds, observed = evaluate(page, UrlMatchesCondition(pattern="text/html"))
    assert holds is True
    assert "text/html" in observed


def test_url_matches_fails_when_pattern_absent(page: Page):
    page.goto("data:text/html,<html><body>x</body></html>")
    holds, _ = evaluate(page, UrlMatchesCondition(pattern="never-matches-xyz"))
    assert holds is False


def test_url_matches_renders_template_against_inputs(page: Page):
    # Use a data URL that itself contains the substituted value, so we don't
    # need a live server for this pure template-rendering behavior.
    page.goto("data:text/html,<html><body id='members/12345'>x</body></html>")
    holds, _ = evaluate(page, UrlMatchesCondition(pattern=r"members/{{memberId}}"), inputs={"memberId": "12345"})
    assert holds is True


def test_url_matches_template_uses_correct_value_not_a_different_one(page: Page):
    page.goto("data:text/html,<html><body id='members/12345'>x</body></html>")
    holds, _ = evaluate(page, UrlMatchesCondition(pattern=r"members/\{\{memberId\}\}"), inputs={"memberId": "67890"})
    assert holds is False


def test_url_matches_with_no_inputs_and_no_template_still_works(page: Page):
    page.goto("data:text/html,<html><body>x</body></html>")
    holds, _ = evaluate(page, UrlMatchesCondition(pattern="text/html"), inputs=None)
    assert holds is True


# ---------------------------------------------------------------------------
# elementVisible
# ---------------------------------------------------------------------------


def test_element_visible_holds_for_visible_element(page: Page):
    _load(page, "<html><body><button>Go</button></body></html>")
    target = TargetLocator(strategies=[RoleLocator(role="button", accessible_name="Go")], reasoning="x")
    holds, observed = evaluate(page, ElementVisibleCondition(target=target))
    assert holds is True
    assert observed == "visible"


def test_element_visible_fails_for_hidden_element(page: Page):
    _load(page, '<html><body><button style="display:none">Go</button></body></html>')
    # A display:none button has no accessible role at all, so it won't
    # resolve via role lookup -- this exercises the "locator did not
    # resolve" branch, which is the realistic outcome for a hidden element.
    target = TargetLocator(strategies=[RoleLocator(role="button", accessible_name="Go")], reasoning="x")
    holds, observed = evaluate(page, ElementVisibleCondition(target=target))
    assert holds is False
    assert "did not resolve" in observed


# ---------------------------------------------------------------------------
# elementText (equals / contains)
# ---------------------------------------------------------------------------


def test_element_text_equals_holds_on_exact_match(page: Page):
    _load(page, "<html><body><p id='msg'>No such member.</p><button>x</button></body></html>")
    target = TargetLocator(strategies=[RoleLocator(role="button", accessible_name="x")], reasoning="x")
    # Use a locator we can actually resolve (button), just checking its text.
    holds, observed = evaluate(page, ElementTextEqualsCondition(target=target, equals="x"))
    assert holds is True
    assert observed == 'text "x"'


def test_element_text_equals_fails_on_mismatch(page: Page):
    _load(page, "<html><body><button>x</button></body></html>")
    target = TargetLocator(strategies=[RoleLocator(role="button", accessible_name="x")], reasoning="x")
    holds, _ = evaluate(page, ElementTextEqualsCondition(target=target, equals="something else"))
    assert holds is False


def test_element_text_contains_holds_on_substring(page: Page):
    _load(page, "<html><body><button>No such member with that ID</button></body></html>")
    target = TargetLocator(
        strategies=[RoleLocator(role="button", accessible_name="No such member with that ID")], reasoning="x"
    )
    holds, _ = evaluate(page, ElementTextContainsCondition(target=target, substring="No such member"))
    assert holds is True


def test_element_text_condition_fails_when_locator_does_not_resolve(page: Page):
    _load(page, "<html><body><p>nothing here</p></body></html>")
    target = TargetLocator(strategies=[RoleLocator(role="button", accessible_name="Ghost")], reasoning="x")
    holds, observed = evaluate(page, ElementTextEqualsCondition(target=target, equals="x"))
    assert holds is False
    assert "did not resolve" in observed
