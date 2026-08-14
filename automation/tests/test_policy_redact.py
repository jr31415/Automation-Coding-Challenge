"""Tests for automation.policy.redact: systematic redaction at
serialization/persistence boundaries.
"""

from __future__ import annotations

from automation.policy.redact import REDACTED, redact_mapping, redact_value


# ---------------------------------------------------------------------------
# redact_value (shape-based)
# ---------------------------------------------------------------------------


def test_ordinary_string_is_not_redacted():
    assert redact_value("Alice Whitfield") == "Alice Whitfield"


def test_ssn_shaped_value_is_redacted():
    assert redact_value("123-45-6789") == REDACTED


def test_credit_card_shaped_value_is_redacted():
    assert redact_value("4111111111111111") == REDACTED
    assert redact_value("4111 1111 1111 1111") == REDACTED
    assert redact_value("4111-1111-1111-1111") == REDACTED


def test_base64_token_shaped_value_is_redacted():
    assert redact_value("YWJjZGVmZ2hpamtsbW5vcHFyc3R1dnd4eXo1Njc4OTA=") == REDACTED


def test_api_key_shaped_value_is_redacted():
    assert redact_value("sk-abcdefghijklmnopqrstuvwx") == REDACTED


def test_short_numeric_value_like_a_member_id_is_not_redacted():
    # A 5-digit member id must not be caught by the credit-card heuristic
    # (13-19 digits) -- this is exactly the kind of value this system's
    # flows legitimately need to log and persist.
    assert redact_value("12345") == "12345"


def test_currency_value_is_not_redacted():
    assert redact_value("$4823.11") == "$4823.11"


# ---------------------------------------------------------------------------
# redact_mapping (name-based + shape-based, recursive)
# ---------------------------------------------------------------------------


def test_redacts_value_whose_key_is_in_sensitive_names():
    data = {"memberId": "12345", "apiToken": "abc123"}
    result = redact_mapping(data, sensitive_names=frozenset({"apiToken"}))
    assert result["memberId"] == "12345"
    assert result["apiToken"] == REDACTED


def test_redacts_by_name_hint_even_when_not_in_declared_sensitive_names():
    # "password" wasn't explicitly declared sensitive by the caller, but the
    # name-hint backstop should still catch it.
    data = {"password": "hunter2"}
    result = redact_mapping(data, sensitive_names=frozenset())
    assert result["password"] == REDACTED


def test_redacts_by_shape_even_when_key_name_is_innocuous():
    data = {"notes": "123-45-6789"}
    result = redact_mapping(data, sensitive_names=frozenset())
    assert result["notes"] == REDACTED


def test_recurses_into_nested_dicts():
    data = {"outer": {"password": "hunter2", "memberId": "12345"}}
    result = redact_mapping(data, sensitive_names=frozenset())
    assert result["outer"]["password"] == REDACTED
    assert result["outer"]["memberId"] == "12345"


def test_recurses_into_lists_of_dicts():
    data = {"steps": [{"password": "hunter2"}, {"value": "12345"}]}
    result = redact_mapping(data, sensitive_names=frozenset())
    assert result["steps"][0]["password"] == REDACTED
    assert result["steps"][1]["value"] == "12345"


def test_non_string_values_pass_through_unchanged():
    data = {"count": 3, "active": True, "ratio": 1.5, "nothing": None}
    result = redact_mapping(data, sensitive_names=frozenset())
    assert result == data


def test_does_not_mutate_the_original_dict():
    data = {"password": "hunter2"}
    redact_mapping(data, sensitive_names=frozenset())
    assert data["password"] == "hunter2"
