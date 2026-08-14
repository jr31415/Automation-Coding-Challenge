"""Tests for automation.policy.allowlist: the domain/path allowlist shared
by discovery and replay.
"""

from __future__ import annotations

from automation.policy.allowlist import Allowlist, AllowlistEntry, default_mock_app_allowlist


def _allowlist(**kwargs):
    defaults = dict(
        entries=(AllowlistEntry(domain="localhost:4000", path_patterns=("/members/*", "/login")),),
        allowed_action_types=frozenset({"navigate", "click"}),
    )
    defaults.update(kwargs)
    return Allowlist(**defaults)


# ---------------------------------------------------------------------------
# check_url
# ---------------------------------------------------------------------------


def test_allows_url_matching_domain_and_path_pattern():
    al = _allowlist()
    allowed, reason = al.check_url("http://localhost:4000/members/12345")
    assert allowed is True
    assert "matches allowlist entry" in reason


def test_rejects_url_with_disallowed_domain():
    al = _allowlist()
    allowed, reason = al.check_url("http://evil.example.com/members/12345")
    assert allowed is False
    assert "not in the allowlist" in reason


def test_rejects_url_on_allowed_domain_but_disallowed_path():
    al = _allowlist()
    allowed, reason = al.check_url("http://localhost:4000/admin/delete-everything")
    assert allowed is False
    assert "matches none of its patterns" in reason


def test_empty_allowlist_rejects_everything():
    al = Allowlist(entries=(), allowed_action_types=frozenset())
    allowed, _ = al.check_url("http://localhost:4000/members/12345")
    assert allowed is False


def test_wildcard_path_pattern_allows_any_path_on_domain():
    al = Allowlist(
        entries=(AllowlistEntry(domain="localhost:4000"),),  # default path_patterns=("*",)
        allowed_action_types=frozenset(),
    )
    allowed, _ = al.check_url("http://localhost:4000/anything/at/all")
    assert allowed is True


def test_different_port_on_same_host_is_a_different_domain():
    al = _allowlist()
    allowed, _ = al.check_url("http://localhost:9999/members/12345")
    assert allowed is False


# ---------------------------------------------------------------------------
# check_action_type
# ---------------------------------------------------------------------------


def test_allows_permitted_action_type():
    al = _allowlist()
    allowed, _ = al.check_action_type("click")
    assert allowed is True


def test_rejects_action_type_not_in_allowed_set():
    al = _allowlist()
    allowed, reason = al.check_action_type("type")
    assert allowed is False
    assert "type" in reason


def test_empty_action_type_set_rejects_everything():
    al = Allowlist(entries=(), allowed_action_types=frozenset())
    allowed, _ = al.check_action_type("navigate")
    assert allowed is False


# ---------------------------------------------------------------------------
# default_mock_app_allowlist
# ---------------------------------------------------------------------------


def test_default_mock_app_allowlist_permits_the_search_and_member_flow():
    al = default_mock_app_allowlist()
    for url in [
        "http://localhost:4000/login",
        "http://localhost:4000/members/search",
        "http://localhost:4000/members/12345",
        "http://localhost:4000/members/12345/sub-account/new",
    ]:
        allowed, reason = al.check_url(url)
        assert allowed is True, f"{url} should be allowed: {reason}"


def test_default_mock_app_allowlist_rejects_other_domains():
    al = default_mock_app_allowlist()
    allowed, _ = al.check_url("http://example.com/members/12345")
    assert allowed is False


def test_default_mock_app_allowlist_permits_all_six_executable_action_types():
    al = default_mock_app_allowlist()
    for action_type in ["navigate", "click", "type", "select", "wait_for", "extract"]:
        allowed, _ = al.check_action_type(action_type)
        assert allowed is True, f"{action_type} should be permitted"
