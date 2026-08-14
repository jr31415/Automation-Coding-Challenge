"""Tool executor: the "act" half of the discovery agent's observe-decide-act
loop.

Dispatches one validated tool call (navigate/click/type/select/wait_for/
extract) onto a BrowserSession and reports back what happened as a typed
ToolResult -- never by raising, since a failed click or a timeout is an
*expected* outcome the model needs to see and react to (retry, pick a
different ref, or call `stuck`), not a Python exception unwinding the loop.

`done` and `stuck` are loop-control signals, not page actions -- the agent
loop intercepts those tool_use blocks itself and never calls execute() for
them, so this module only knows about the six tools that actually touch the
browser.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from automation.agent.browser import BrowserSession, Snapshot, StaleRefError
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
from automation.policy.allowlist import Allowlist

# Tools the executor actually dispatches. `done` and `stuck` are deliberately
# excluded -- see module docstring.
EXECUTABLE_TOOLS = frozenset({"navigate", "click", "type", "select", "wait_for", "extract"})

DEFAULT_ACTION_TIMEOUT_MS = 5_000


@dataclass(frozen=True)
class ToolResult:
    """Outcome of dispatching one tool call. Fed back into the next turn's
    context so the model can see exactly what happened and decide what to
    do next -- this is not a Python-level success/exception split, it's the
    same "report what happened, let the caller decide" contract the replay
    engine's result types follow (see automation.replay, once built).
    """

    ok: bool
    tool: str
    # Free-form details for a successful call, e.g. {"outputKey": "...", "value": "..."}
    # for extract, or {} for click/type/select/navigate.
    data: dict[str, Any]
    # Present only when ok is False: a short, model-readable explanation of
    # what went wrong (stale ref, element not found, timeout, navigation
    # error) so the model can adjust rather than repeat the same mistake.
    error: str | None = None


def execute(
    session: BrowserSession,
    snapshot: Snapshot,
    tool_name: str,
    tool_input: Any,
    allowlist: Allowlist | None = None,
) -> ToolResult:
    """Dispatch a single validated tool call. `tool_input` must already be
    the parsed pydantic model for `tool_name` (see automation.agent.tools).

    If `allowlist` is given, the action type is checked before dispatch, and
    for `navigate` the destination URL is checked before the browser is
    told to go there at all -- an agent must not even attempt a disallowed
    navigation, not just have its result discarded after the fact.
    """
    if tool_name not in EXECUTABLE_TOOLS:
        return ToolResult(
            ok=False,
            tool=tool_name,
            data={},
            error=f'"{tool_name}" is not an executable browser tool (expected one of {sorted(EXECUTABLE_TOOLS)}).',
        )

    if allowlist is not None:
        type_allowed, type_reason = allowlist.check_action_type(tool_name)
        if not type_allowed:
            return ToolResult(ok=False, tool=tool_name, data={}, error=f"Blocked by allowlist: {type_reason}")
        if tool_name == "navigate":
            url_allowed, url_reason = allowlist.check_url(tool_input.url)
            if not url_allowed:
                return ToolResult(ok=False, tool=tool_name, data={}, error=f"Blocked by allowlist: {url_reason}")

    try:
        if tool_name == "navigate":
            result = _navigate(session, tool_input)
        elif tool_name == "click":
            result = _click(session, snapshot, tool_input)
        elif tool_name == "type":
            result = _type(session, snapshot, tool_input)
        elif tool_name == "select":
            result = _select(session, snapshot, tool_input)
        elif tool_name == "wait_for":
            result = _wait_for(session, snapshot, tool_input)
        elif tool_name == "extract":
            result = _extract(session, snapshot, tool_input)
    except StaleRefError as e:
        return ToolResult(ok=False, tool=tool_name, data={}, error=str(e))
    except PlaywrightTimeoutError as e:
        return ToolResult(ok=False, tool=tool_name, data={}, error=f"Timed out: {e}")
    except PlaywrightError as e:
        return ToolResult(ok=False, tool=tool_name, data={}, error=f"Browser error: {e}")

    # A click or select can trigger navigation (a link, a form submit) just
    # as easily as an explicit `navigate` call -- re-check the allowlist
    # against wherever we actually ended up, not just the URL we started
    # from, so an in-allowlist page can't be used as a stepping stone off
    # the allowlist via a click.
    if allowlist is not None and result.ok and tool_name in ("click", "select"):
        url_allowed, url_reason = allowlist.check_url(session.page.url)
        if not url_allowed:
            return ToolResult(
                ok=False,
                tool=tool_name,
                data=result.data,
                error=f"Blocked by allowlist after action landed outside permitted scope: {url_reason}",
            )

    return result

    raise AssertionError(f"unreachable: unhandled executable tool {tool_name!r}")


def _navigate(session: BrowserSession, inp: NavigateInput) -> ToolResult:
    session.page.goto(inp.url, timeout=DEFAULT_ACTION_TIMEOUT_MS * 2)
    return ToolResult(ok=True, tool="navigate", data={"url": session.page.url})


def _click(session: BrowserSession, snapshot: Snapshot, inp: ClickInput) -> ToolResult:
    locator = session.resolve(inp.ref, snapshot)
    locator.click(timeout=DEFAULT_ACTION_TIMEOUT_MS)
    return ToolResult(ok=True, tool="click", data={"ref": inp.ref})


def _type(session: BrowserSession, snapshot: Snapshot, inp: TypeInput) -> ToolResult:
    locator = session.resolve(inp.ref, snapshot)
    locator.fill(inp.value, timeout=DEFAULT_ACTION_TIMEOUT_MS)
    # Redact the typed value from the result payload when marked sensitive --
    # ToolResult.data flows into evidence/logs, so this is the first line of
    # defense against persisting credentials/PII (see automation.policy for
    # the full redaction story).
    logged_value = "[REDACTED]" if inp.sensitive else inp.value
    return ToolResult(ok=True, tool="type", data={"ref": inp.ref, "value": logged_value})


def _select(session: BrowserSession, snapshot: Snapshot, inp: SelectInput) -> ToolResult:
    locator = session.resolve(inp.ref, snapshot)
    try:
        selected = locator.select_option(value=inp.value, timeout=DEFAULT_ACTION_TIMEOUT_MS)
    except PlaywrightError:
        # Fall back to matching by visible label, since legacy <select> markup
        # often has option text that differs from its `value` attribute (or
        # no value attribute at all).
        selected = locator.select_option(label=inp.value, timeout=DEFAULT_ACTION_TIMEOUT_MS)
    return ToolResult(ok=True, tool="select", data={"ref": inp.ref, "selected": selected})


def _wait_for(session: BrowserSession, snapshot: Snapshot, inp: WaitForInput) -> ToolResult:
    condition = inp.condition
    if isinstance(condition, UrlMatchesWait):
        pattern = re.compile(condition.pattern)
        try:
            session.page.wait_for_url(
                lambda url: bool(pattern.search(url)), timeout=inp.timeout_ms
            )
        except PlaywrightTimeoutError:
            return ToolResult(
                ok=False,
                tool="wait_for",
                data={},
                error=f'URL never matched pattern "{condition.pattern}" (current: {session.page.url}).',
            )
        return ToolResult(ok=True, tool="wait_for", data={"url": session.page.url})

    if isinstance(condition, ElementVisibleWait):
        locator = session.resolve(condition.ref, snapshot)
        try:
            locator.wait_for(state="visible", timeout=inp.timeout_ms)
        except PlaywrightTimeoutError:
            return ToolResult(
                ok=False,
                tool="wait_for",
                data={},
                error=f'Element "{condition.ref}" did not become visible within {inp.timeout_ms}ms.',
            )
        return ToolResult(ok=True, tool="wait_for", data={"ref": condition.ref})

    raise AssertionError(f"unreachable: unhandled wait condition {condition!r}")


def _extract(session: BrowserSession, snapshot: Snapshot, inp: ExtractInput) -> ToolResult:
    locator = session.resolve(inp.ref, snapshot)
    text = (locator.text_content(timeout=DEFAULT_ACTION_TIMEOUT_MS) or "").strip()
    return ToolResult(ok=True, tool="extract", data={"outputKey": inp.output_key, "value": text})
