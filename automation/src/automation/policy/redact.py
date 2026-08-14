"""Redacts sensitive values before they reach any persisted output --
evidence logs, saved artifacts, screenshots' accompanying metadata. This is
the systematic counterpart to the ad-hoc scrubbing already done inline in
automation.agent.loop (which only covers what the LLM sees mid-run); this
module is what actually enforces the brief's "never persist secrets or raw
sensitive data" requirement at the point data gets written to disk.

Two redaction signals, applied together:
  - name-based: a value is redacted if it's associated with a field/key
    name the caller declares sensitive (typically an artifact's
    InputParam.sensitive=True names, or a step's `sensitive=True` flag).
  - shape-based: a value is redacted if it *looks* like a secret regardless
    of what it's called, as a defense-in-depth backstop for cases where a
    field wasn't correctly flagged sensitive (e.g. a password accidentally
    typed into a field named "notes"). This is heuristic and will have
    false negatives on unusual secret formats -- it is not a substitute for
    correctly flagging sensitive fields, only a second line of defense.
"""

from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"

# Shape-based backstop patterns: things that look like secrets/PII even if
# their field name wasn't flagged sensitive. Deliberately conservative --
# false positives (redacting something benign) are a much cheaper mistake
# here than false negatives (leaking a real secret).
_SHAPE_PATTERNS = [
    re.compile(r"^\d{3}-\d{2}-\d{4}$"),  # SSN-shaped
    re.compile(r"^\d{13,19}$"),  # credit-card-shaped (13-19 digits, no separators)
    re.compile(r"^\d{4}([- ]?\d{4}){3}$"),  # credit-card-shaped with separators
    re.compile(r"^[A-Za-z0-9+/]{32,}={0,2}$"),  # base64-ish token/key shaped, 32+ chars
    re.compile(r"^sk-[A-Za-z0-9]{16,}$"),  # common API-key prefix shape
]

# Field-name substrings that mark a key as sensitive regardless of its
# declared type -- catches common credential/PII field names even when the
# caller's explicit sensitive_names set doesn't happen to include them.
_SENSITIVE_NAME_HINTS = ("password", "passwd", "secret", "token", "apikey", "api_key", "ssn", "ccnum", "credential")


def _looks_like_secret(value: str) -> bool:
    return any(pattern.match(value) for pattern in _SHAPE_PATTERNS)


def _name_is_sensitive(name: str, sensitive_names: frozenset[str]) -> bool:
    if name in sensitive_names:
        return True
    lowered = name.lower()
    return any(hint in lowered for hint in _SENSITIVE_NAME_HINTS)


def redact_value(value: str) -> str:
    """Redact a single value based on its shape alone (no field name context)."""
    return REDACTED if _looks_like_secret(value) else value


def redact_mapping(data: dict[str, Any], sensitive_names: frozenset[str] = frozenset()) -> dict[str, Any]:
    """Recursively redact a dict (as produced by json.loads / model_dump):
    string values are redacted if their key is sensitive-by-name or the
    value is sensitive-by-shape; nested dicts/lists are walked the same way.
    """
    result: dict[str, Any] = {}
    for key, value in data.items():
        result[key] = _redact_field(key, value, sensitive_names)
    return result


def _redact_field(key: str, value: Any, sensitive_names: frozenset[str]) -> Any:
    if isinstance(value, dict):
        return redact_mapping(value, sensitive_names)
    if isinstance(value, list):
        return [_redact_field(key, item, sensitive_names) for item in value]
    if isinstance(value, str):
        if _name_is_sensitive(key, sensitive_names):
            return REDACTED
        return redact_value(value)
    return value
