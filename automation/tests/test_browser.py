"""Tests for automation.agent.browser: the observe layer of the discovery loop.

Runs against a real (headless) Playwright browser and small inline HTML
fixtures -- no network dependency, no mock-app server required.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page

from automation.agent.browser import BrowserSession, Snapshot, StaleRefError, _extract_refs

SIMPLE_FORM_HTML = """
<html><body>
<h2>Member Search</h2>
<form>
<input type="text" name="q">
<input type="submit" value="Look Up">
</form>
</body></html>
"""

TABLE_HTML = """
<html><body>
<table>
<tr><td>Name</td><td>Alice Whitfield</td></tr>
<tr><td>Savings Balance</td><td>$4823.11</td></tr>
</table>
<a href="/members/12345/sub-account/new">Open New Sub-Account</a>
</body></html>
"""

EMPTY_HTML = "<html><body></body></html>"


def _load(page: Page, html: str) -> None:
    page.set_content(html)


# ---------------------------------------------------------------------------
# snapshot() basics
# ---------------------------------------------------------------------------


def test_snapshot_returns_url_and_title(page: Page):
    page.goto("data:text/html,<html><head><title>Riverbend Admin</title></head><body><p>hi</p></body></html>")
    sess = BrowserSession(page)
    snap = sess.snapshot()
    assert snap.title == "Riverbend Admin"
    assert snap.url.startswith("data:text/html")


def test_snapshot_is_frozen_dataclass():
    # Snapshot should be immutable -- a stale observation should never be
    # silently mutated out from under the caller mid-turn.
    assert Snapshot.__dataclass_params__.frozen is True


def test_snapshot_extracts_refs_for_interactive_elements(page: Page):
    _load(page, SIMPLE_FORM_HTML)
    sess = BrowserSession(page)
    snap = sess.snapshot()
    assert len(snap.refs) >= 2  # textbox + submit button, at minimum
    assert all(r.startswith("e") for r in snap.refs)


def test_snapshot_tree_text_contains_role_and_accessible_name(page: Page):
    _load(page, SIMPLE_FORM_HTML)
    sess = BrowserSession(page)
    snap = sess.snapshot()
    assert "textbox" in snap.tree_text
    assert 'button "Look Up"' in snap.tree_text or "Look Up" in snap.tree_text


def test_snapshot_on_empty_page_has_no_refs(page: Page):
    _load(page, EMPTY_HTML)
    sess = BrowserSession(page)
    snap = sess.snapshot()
    assert snap.refs == frozenset()


def test_extract_refs_handles_frame_prefixed_ref_ids():
    # Playwright has been observed to prefix ref ids with a frame identifier
    # after a navigation (e.g. "f2e1" instead of "e1"), not just the plain
    # "e<N>" form seen on a page's first snapshot. A regex that only matches
    # "e<N>" silently returns zero refs for every snapshot after the first
    # navigation -- this is a regression test for that real bug.
    tree_text = (
        "- generic [active] [ref=f2e1]:\n"
        '  - textbox [ref=f2e10]\n'
        '  - button "Look Up" [ref=f2e12]\n'
    )
    assert _extract_refs(tree_text) == frozenset({"f2e1", "f2e10", "f2e12"})


def test_extract_refs_handles_plain_ref_ids():
    tree_text = "- generic [ref=e1]:\n  - textbox [ref=e2]\n"
    assert _extract_refs(tree_text) == frozenset({"e1", "e2"})


def test_has_ref_true_for_present_ref_false_for_absent(page: Page):
    _load(page, SIMPLE_FORM_HTML)
    sess = BrowserSession(page)
    snap = sess.snapshot()
    ref = next(iter(snap.refs))
    assert snap.has_ref(ref) is True
    assert snap.has_ref("e_not_real") is False


# ---------------------------------------------------------------------------
# resolve()
# ---------------------------------------------------------------------------


def test_resolve_returns_working_locator_for_valid_ref(page: Page):
    _load(page, TABLE_HTML)
    sess = BrowserSession(page)
    snap = sess.snapshot()
    # Match the link by its href, not by text substring: ancestor container
    # refs (e.g. the root <body> ref) also contain the link's text as part
    # of their aggregate text_content(), so a substring check can match the
    # wrong (non-link) ref depending on frozenset iteration order.
    link_ref = None
    for ref in snap.refs:
        loc = sess.resolve(ref, snap)
        if loc.get_attribute("href") == "/members/12345/sub-account/new":
            link_ref = ref
            break
    assert link_ref is not None
    loc = sess.resolve(link_ref, snap)
    assert (loc.text_content() or "").strip() == "Open New Sub-Account"


def test_resolve_raises_stale_ref_error_for_unknown_ref(page: Page):
    _load(page, SIMPLE_FORM_HTML)
    sess = BrowserSession(page)
    snap = sess.snapshot()
    with pytest.raises(StaleRefError):
        sess.resolve("e999", snap)


def test_stale_ref_error_message_includes_the_ref(page: Page):
    _load(page, SIMPLE_FORM_HTML)
    sess = BrowserSession(page)
    snap = sess.snapshot()
    with pytest.raises(StaleRefError) as exc_info:
        sess.resolve("e42", snap)
    assert "e42" in str(exc_info.value)
    assert exc_info.value.ref == "e42"


def test_resolve_rejects_ref_from_a_snapshot_taken_before_navigation(page: Page):
    """Refs are scoped to the snapshot they came from. A ref captured before
    navigating to a new page must not silently resolve against the new DOM.
    """
    _load(page, SIMPLE_FORM_HTML)
    sess = BrowserSession(page)
    old_snap = sess.snapshot()
    old_ref = next(iter(old_snap.refs))

    _load(page, TABLE_HTML)
    new_snap = sess.snapshot()

    # The old ref should not be treated as valid against the new snapshot,
    # even if by coincidence the new page reused the same ref id.
    if old_ref not in new_snap.refs:
        with pytest.raises(StaleRefError):
            sess.resolve(old_ref, new_snap)
    else:
        # If the id happened to be reused, resolving against old_snap (the
        # correct, matching snapshot) must still succeed -- this just
        # confirms validation is snapshot-scoped, not global.
        assert sess.resolve(old_ref, old_snap) is not None


# ---------------------------------------------------------------------------
# legacy/hostile markup: tables, no test ids -- the environment this system
# targets per the assignment brief.
# ---------------------------------------------------------------------------


def test_snapshot_handles_table_based_layout_without_test_ids(page: Page):
    _load(page, TABLE_HTML)
    sess = BrowserSession(page)
    snap = sess.snapshot()
    assert "Alice Whitfield" in snap.tree_text
    assert "Savings Balance" in snap.tree_text or "$4823.11" in snap.tree_text


def test_snapshot_handles_select_with_options(page: Page):
    _load(
        page,
        '<html><body><select name="subAccountType">'
        '<option value="">-- select --</option>'
        '<option value="Holiday Club">Holiday Club</option>'
        "</select></body></html>",
    )
    sess = BrowserSession(page)
    snap = sess.snapshot()
    assert "Holiday Club" in snap.tree_text
    assert len(snap.refs) >= 1
