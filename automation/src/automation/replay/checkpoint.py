"""Evaluates a CheckpointCondition against the live page -- used for a
step's post_condition, the artifact's final success_condition, and each
declared KnownOutcome's detect condition.

A checkpoint is an assertion, not an action: it never mutates the page, and
returns a plain bool plus a human-readable "observed" string for failure
reporting (see automation.replay.result.Failure).
"""

from __future__ import annotations

import re
from typing import Any

from playwright.sync_api import Page

from automation.artifact.schema import (
    CheckpointCondition,
    ElementTextContainsCondition,
    ElementTextEqualsCondition,
    ElementVisibleCondition,
    UrlMatchesCondition,
)
from automation.replay.locator import resolve


def _render_pattern(pattern: str, inputs: dict[str, Any]) -> str:
    def _sub(match: re.Match) -> str:
        name = match.group(1)
        value = inputs.get(name)
        # re.escape so a param value containing regex metacharacters can't
        # corrupt the pattern -- this is matched against, not compiled from,
        # untrusted input, but the value is still caller-supplied data.
        return re.escape(str(value)) if value is not None else match.group(0)

    return re.sub(r"\{\{(\w+)\}\}", _sub, pattern)


def evaluate(page: Page, condition: CheckpointCondition, inputs: dict[str, Any] | None = None) -> tuple[bool, str]:
    """Returns (holds, observed_description).

    `inputs` renders any {{param}} references in a urlMatches pattern (e.g.
    a success_condition recorded as "/members/{{memberId}}$") against this
    replay's actual input values -- without it, a checkpoint recorded
    against one specific input (like a member id seen during discovery)
    would only ever match that same value again. Conditions with no
    template references ignore `inputs` entirely.
    """
    if isinstance(condition, UrlMatchesCondition):
        pattern = _render_pattern(condition.pattern, inputs or {})
        current = page.url
        holds = bool(re.search(pattern, current))
        return holds, f'current URL "{current}"'

    if isinstance(condition, ElementVisibleCondition):
        resolution = resolve(page, condition.target)
        if resolution.locator is None:
            return False, f"locator did not resolve ({'; '.join(resolution.attempts)})"
        try:
            visible = resolution.locator.is_visible()
        except Exception as e:
            return False, f"error checking visibility: {e}"
        return visible, "visible" if visible else "not visible"

    if isinstance(condition, ElementTextEqualsCondition):
        resolution = resolve(page, condition.target)
        if resolution.locator is None:
            return False, f"locator did not resolve ({'; '.join(resolution.attempts)})"
        text = (resolution.locator.text_content() or "").strip()
        return text == condition.equals, f'text "{text}"'

    if isinstance(condition, ElementTextContainsCondition):
        resolution = resolve(page, condition.target)
        if resolution.locator is None:
            return False, f"locator did not resolve ({'; '.join(resolution.attempts)})"
        text = (resolution.locator.text_content() or "").strip()
        return condition.substring in text, f'text "{text}"'

    raise AssertionError(f"unreachable: unhandled checkpoint condition {condition!r}")
