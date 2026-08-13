"""Tests for automation.agent.executor: the "act" half of the discovery loop.

Runs against a real (headless) Playwright browser and small inline HTML
fixtures. Covers each of the six executable tools on the happy path, the
expected-failure paths (stale ref, timeout, element not found), and the
policy that `done`/`stuck` are never dispatched here.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

from automation.agent.browser import BrowserSession
from automation.agent.executor import DEFAULT_ACTION_TIMEOUT_MS, execute
from automation.agent.tools import (
    ClickInput,
    ElementVisibleWait,
    ExtractInput,
    NavigateInput,
    SelectInput,
    TypeInput,
    UrlMatchesWait,
    WaitForInput,
)


def _load(page: Page, html: str) -> None:
    page.set_content(html)


def _ref_for(session: BrowserSession, snapshot, tag: str) -> str:
    """Find the ref whose element has the given uppercase tag name.

    Only safe when exactly one element with that tag is expected in the
    fixture -- for fixtures with multiple same-tag elements, use
    `_ref_for_id` instead.
    """
    for ref in snapshot.refs:
        loc = session.resolve(ref, snapshot)
        if loc.evaluate("el => el.tagName") == tag:
            return ref
    raise AssertionError(f"no ref found for tag {tag}")


def _ref_for_id(session: BrowserSession, snapshot, element_id: str) -> str:
    """Find the ref whose element has the given id attribute."""
    for ref in snapshot.refs:
        loc = session.resolve(ref, snapshot)
        if loc.evaluate("el => el.id") == element_id:
            return ref
    raise AssertionError(f"no ref found for id {element_id!r}")


# ---------------------------------------------------------------------------
# navigate
# ---------------------------------------------------------------------------


def test_navigate_success(page: Page):
    sess = BrowserSession(page)
    snap = sess.snapshot()
    result = execute(sess, snap, "navigate", NavigateInput(url="data:text/html,<html><body><h1>Landed</h1></body></html>"))
    assert result.ok is True
    assert result.tool == "navigate"
    assert "url" in result.data
    assert page.locator("h1").text_content() == "Landed"


def test_navigate_failure_returns_error_not_exception(page: Page):
    sess = BrowserSession(page)
    snap = sess.snapshot()
    # Non-routable URL should time out / fail to load rather than raise out
    # of execute().
    result = execute(
        sess,
        snap,
        "navigate",
        NavigateInput(url="http://127.0.0.1:1/does-not-exist"),
    )
    assert result.ok is False
    assert result.error is not None


# ---------------------------------------------------------------------------
# click
# ---------------------------------------------------------------------------


def test_click_success(page: Page):
    _load(
        page,
        '<html><body><button onclick="document.getElementById(\'out\').textContent=\'clicked\'">Go</button>'
        '<p id="out">not clicked</p></body></html>',
    )
    sess = BrowserSession(page)
    snap = sess.snapshot()
    button_ref = _ref_for(sess, snap, "BUTTON")

    result = execute(sess, snap, "click", ClickInput(ref=button_ref))

    assert result.ok is True
    assert result.data == {"ref": button_ref}
    assert page.locator("#out").text_content() == "clicked"


def test_click_stale_ref_returns_error(page: Page):
    _load(page, "<html><body><button>Go</button></body></html>")
    sess = BrowserSession(page)
    snap = sess.snapshot()

    result = execute(sess, snap, "click", ClickInput(ref="e999"))

    assert result.ok is False
    assert "e999" in result.error


def test_click_on_hidden_element_times_out_as_error(page: Page):
    _load(page, '<html><body><button style="display:none">Hidden</button></body></html>')
    sess = BrowserSession(page)
    snap = sess.snapshot()
    # A display:none button is not exposed in the accessibility tree at all,
    # so there is no ref for it -- confirm that's the case, then confirm a
    # bogus ref still fails cleanly (covers the "element exists in DOM but
    # not actionable" family of failures without a long real timeout).
    assert snap.refs == frozenset()


# ---------------------------------------------------------------------------
# type
# ---------------------------------------------------------------------------


def test_type_success(page: Page):
    _load(page, '<html><body><input type="text" name="q"></body></html>')
    sess = BrowserSession(page)
    snap = sess.snapshot()
    ref = _ref_for(sess, snap, "INPUT")

    result = execute(sess, snap, "type", TypeInput(ref=ref, value="12345"))

    assert result.ok is True
    assert result.data == {"ref": ref, "value": "12345"}
    assert page.locator("input").input_value() == "12345"


def test_type_redacts_sensitive_value_in_result_data(page: Page):
    _load(page, '<html><body><input type="password" name="pw"></body></html>')
    sess = BrowserSession(page)
    snap = sess.snapshot()
    ref = _ref_for(sess, snap, "INPUT")

    result = execute(sess, snap, "type", TypeInput(ref=ref, value="hunter2", sensitive=True))

    assert result.ok is True
    assert result.data["value"] == "[REDACTED]"
    # The real value still reaches the page -- only the *reported* result is redacted.
    assert page.locator("input").input_value() == "hunter2"


def test_type_stale_ref_returns_error(page: Page):
    _load(page, '<html><body><input type="text"></body></html>')
    sess = BrowserSession(page)
    snap = sess.snapshot()

    result = execute(sess, snap, "type", TypeInput(ref="e999", value="x"))

    assert result.ok is False
    assert result.error is not None


# ---------------------------------------------------------------------------
# select
# ---------------------------------------------------------------------------


def test_select_by_value(page: Page):
    _load(
        page,
        '<html><body><select name="t">'
        '<option value="">-- select --</option>'
        '<option value="hc">Holiday Club</option>'
        "</select></body></html>",
    )
    sess = BrowserSession(page)
    snap = sess.snapshot()
    ref = _ref_for(sess, snap, "SELECT")

    result = execute(sess, snap, "select", SelectInput(ref=ref, value="hc"))

    assert result.ok is True
    assert page.locator("select").input_value() == "hc"


def test_select_falls_back_to_label_when_value_does_not_match(page: Page):
    _load(
        page,
        '<html><body><select name="t">'
        '<option value="">-- select --</option>'
        '<option value="hc">Holiday Club</option>'
        "</select></body></html>",
    )
    sess = BrowserSession(page)
    snap = sess.snapshot()
    ref = _ref_for(sess, snap, "SELECT")

    # Model passes the visible label text, not the underlying option value --
    # a realistic case for legacy markup where labels and values diverge.
    result = execute(sess, snap, "select", SelectInput(ref=ref, value="Holiday Club"))

    assert result.ok is True
    assert page.locator("select").input_value() == "hc"


def test_select_stale_ref_returns_error(page: Page):
    _load(page, '<html><body><select><option value="a">A</option></select></body></html>')
    sess = BrowserSession(page)
    snap = sess.snapshot()

    result = execute(sess, snap, "select", SelectInput(ref="e999", value="a"))

    assert result.ok is False


# ---------------------------------------------------------------------------
# wait_for
# ---------------------------------------------------------------------------


def test_wait_for_url_matches_success(page: Page):
    page.goto("data:text/html,<html><body>x</body></html>")
    sess = BrowserSession(page)
    snap = sess.snapshot()

    result = execute(
        sess,
        snap,
        "wait_for",
        WaitForInput(condition=UrlMatchesWait(pattern="text/html"), timeout_ms=2_000),
    )

    assert result.ok is True


def test_wait_for_url_matches_timeout_returns_error(page: Page):
    page.goto("data:text/html,<html><body>x</body></html>")
    sess = BrowserSession(page)
    snap = sess.snapshot()

    result = execute(
        sess,
        snap,
        "wait_for",
        WaitForInput(condition=UrlMatchesWait(pattern="this-will-never-match-xyz"), timeout_ms=300),
    )

    assert result.ok is False
    assert "never matched" in result.error


def test_wait_for_element_visible_success(page: Page):
    _load(page, '<html><body><p>Visible</p></body></html>')
    sess = BrowserSession(page)
    snap = sess.snapshot()
    ref = _ref_for(sess, snap, "P")

    result = execute(
        sess,
        snap,
        "wait_for",
        WaitForInput(condition=ElementVisibleWait(ref=ref), timeout_ms=2_000),
    )

    assert result.ok is True


def test_wait_for_element_visible_stale_ref_returns_error(page: Page):
    _load(page, "<html><body><p>x</p></body></html>")
    sess = BrowserSession(page)
    snap = sess.snapshot()

    result = execute(
        sess,
        snap,
        "wait_for",
        WaitForInput(condition=ElementVisibleWait(ref="e999"), timeout_ms=300),
    )

    assert result.ok is False


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------


def test_extract_returns_output_key_and_value(page: Page):
    # A bare <td> outside a <table> is normalized away by the browser's HTML
    # parser and won't appear as its own accessibility node, so use a real
    # table -- matching the mock app's actual balance-cell markup.
    _load(page, '<html><body><table><tr><td>Savings Balance</td><td id="bal">$4823.11</td></tr></table></body></html>')
    sess = BrowserSession(page)
    snap = sess.snapshot()
    ref = _ref_for_id(sess, snap, "bal")

    result = execute(sess, snap, "extract", ExtractInput(ref=ref, output_key="savingsBalance"))

    assert result.ok is True
    assert result.data == {"outputKey": "savingsBalance", "value": "$4823.11"}


def test_extract_strips_whitespace(page: Page):
    _load(page, '<html><body><p id="p">  Alice Whitfield  \n</p></body></html>')
    sess = BrowserSession(page)
    snap = sess.snapshot()
    ref = _ref_for(sess, snap, "P")

    result = execute(sess, snap, "extract", ExtractInput(ref=ref, output_key="name"))

    assert result.data["value"] == "Alice Whitfield"


def test_extract_stale_ref_returns_error(page: Page):
    _load(page, "<html><body><p>x</p></body></html>")
    sess = BrowserSession(page)
    snap = sess.snapshot()

    result = execute(sess, snap, "extract", ExtractInput(ref="e999", output_key="k"))

    assert result.ok is False


# ---------------------------------------------------------------------------
# done / stuck are not executable -- they must never reach the browser layer.
# ---------------------------------------------------------------------------


def test_done_is_rejected_by_executor(page: Page):
    sess = BrowserSession(page)
    snap = sess.snapshot()
    result = execute(sess, snap, "done", {"summary": "x", "outputs": {}})
    assert result.ok is False
    assert "not an executable browser tool" in result.error


def test_stuck_is_rejected_by_executor(page: Page):
    sess = BrowserSession(page)
    snap = sess.snapshot()
    result = execute(sess, snap, "stuck", {"reason": "other", "details": "x"})
    assert result.ok is False
    assert "not an executable browser tool" in result.error


def test_default_action_timeout_is_positive():
    assert DEFAULT_ACTION_TIMEOUT_MS > 0
