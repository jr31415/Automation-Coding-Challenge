"""Resolves an artifact's durable TargetLocator (role/text/labelFor/
structural fallback chain) into a live Playwright Locator during replay.

This is the deterministic counterpart to automation.agent.browser's ref
resolution: discovery resolves a `ref` from a fresh accessibility snapshot,
because refs only exist within an LLM-driven session. Replay has no LLM and
no refs -- it re-derives the same element using the locator strategies
recorded at discovery time, trying each in the artifact's declared fallback
order and taking the first one that resolves to exactly one element.

None of these strategies depend on CSS classes, ids, or test ids, matching
the environment constraint this whole system is built around (legacy apps
essentially never have test ids).
"""

from __future__ import annotations

from dataclasses import dataclass

from playwright.sync_api import Locator, Page

from automation.artifact.schema import (
    LabelForLocator,
    LocatorStrategy,
    RoleLocator,
    StructuralLocator,
    TargetLocator,
    TextLocator,
)


@dataclass(frozen=True)
class LocatorResolution:
    """The outcome of trying to resolve a TargetLocator's fallback chain."""

    locator: Locator | None
    # Which strategy (by index into target.strategies) succeeded, or None if
    # every strategy failed. Recorded for diagnostics -- a replay that keeps
    # falling through to strategy index 2+ on every run is a signal the
    # artifact needs re-recording even though it technically still passes.
    matched_strategy_index: int | None
    # One line per attempted strategy, e.g. "role: 0 matches", "text: 2
    # matches (ambiguous)" -- surfaced in Failure results for debugging.
    attempts: list[str]


def resolve(page: Page, target: TargetLocator) -> LocatorResolution:
    attempts: list[str] = []
    for index, strategy in enumerate(target.strategies):
        locator, note = _try_strategy(page, strategy)
        attempts.append(f"[{index}] {_describe_strategy(strategy)}: {note}")
        if locator is not None:
            return LocatorResolution(locator=locator, matched_strategy_index=index, attempts=attempts)
    return LocatorResolution(locator=None, matched_strategy_index=None, attempts=attempts)


def _try_strategy(page: Page, strategy: LocatorStrategy) -> tuple[Locator | None, str]:
    try:
        if isinstance(strategy, RoleLocator):
            candidate = page.get_by_role(strategy.role, name=strategy.accessible_name, exact=True)
        elif isinstance(strategy, TextLocator):
            candidate = page.get_by_text(strategy.text, exact=strategy.exact)
        elif isinstance(strategy, LabelForLocator):
            candidate = _resolve_label_for(page, strategy)
        elif isinstance(strategy, StructuralLocator):
            candidate = _resolve_structural(page, strategy)
        else:
            return None, f"unrecognized strategy kind {strategy!r}"
    except Exception as e:  # Playwright can raise on a malformed selector, etc.
        return None, f"error: {e}"

    if candidate is None:
        return None, "no candidate produced"

    count = candidate.count()
    if count == 0:
        return None, "0 matches"
    if count > 1:
        return None, f"{count} matches (ambiguous)"
    return candidate, "1 match"


def _resolve_label_for(page: Page, strategy: LabelForLocator) -> Locator | None:
    """Find the element whose text is exactly `strategy.label`, then return
    its next sibling element -- the mock app's (and many legacy apps')
    <tr><td>Label</td><td>Value</td></tr> pattern.
    """
    label_el = page.get_by_text(strategy.label, exact=True)
    if label_el.count() != 1:
        return None
    # xpath sibling works across the table-cell / generic-div legacy shapes
    # this system targets, without assuming a specific tag.
    return label_el.locator("xpath=following-sibling::*[1]")


def _resolve_structural(page: Page, strategy: StructuralLocator) -> Locator | None:
    scope = page
    if strategy.within_container_text:
        container = page.get_by_text(strategy.within_container_text, exact=True)
        if container.count() != 1:
            return None
        # Scope the structural search to the row/container holding the
        # labeled cell, matching how it was recorded (see
        # automation.artifact.recorder._sibling_cell_label).
        scope = container.locator("xpath=..")
    return scope.locator(strategy.tag).nth(strategy.nth)


def _describe_strategy(strategy: LocatorStrategy) -> str:
    if isinstance(strategy, RoleLocator):
        return f'role="{strategy.role}" name="{strategy.accessible_name}"'
    if isinstance(strategy, TextLocator):
        return f'text="{strategy.text}"'
    if isinstance(strategy, LabelForLocator):
        return f'labelFor="{strategy.label}"'
    if isinstance(strategy, StructuralLocator):
        return f'structural tag="{strategy.tag}" nth={strategy.nth}'
    return repr(strategy)
