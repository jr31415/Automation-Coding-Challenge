"""Tests for automation.agent.loop: the observe-decide-act orchestrator.

Uses a real headless browser (so `observe` and `act` are the genuine
Playwright-backed implementations) with a scripted FakeAnthropicClient
standing in for `decide`, so these tests exercise the loop's actual control
flow without needing network access or a live API key.
"""

from __future__ import annotations

import time

import pytest
from playwright.sync_api import Page

from automation.agent.browser import BrowserSession
from automation.agent.loop import (
    GoalUnreachableError,
    RunResult,
    StepRecord,
    StopReason,
    run_discovery,
)
from fake_anthropic import FakeAnthropicClient, FakeResponse, tool_use_response


def _button_ref(session: BrowserSession) -> str:
    snap = session.snapshot()
    for ref in snap.refs:
        if session.resolve(ref, snap).evaluate("el => el.tagName") == "BUTTON":
            return ref
    raise AssertionError("no button ref found")


# ---------------------------------------------------------------------------
# done
# ---------------------------------------------------------------------------


def test_immediate_done_stops_with_outputs_and_summary(page: Page):
    page.set_content("<html><body><p>hi</p></body></html>")
    session = BrowserSession(page)
    client = FakeAnthropicClient(
        [tool_use_response("t1", "done", {"summary": "nothing to do", "outputs": {"x": "1"}})]
    )

    result = run_discovery(goal="do nothing", session=session, client=client)

    assert result.stop_reason == StopReason.DONE
    assert result.outputs == {"x": "1"}
    assert result.summary == "nothing to do"
    assert len(result.steps) == 1
    assert result.steps[0].tool_name == "done"
    assert result.steps[0].result is None  # done is never dispatched to the executor


def test_click_then_done_executes_the_click_and_records_both_steps(page: Page):
    page.set_content(
        '<html><body><button onclick="document.getElementById(\'o\').textContent=\'clicked\'">Go</button>'
        '<p id="o">no</p></body></html>'
    )
    session = BrowserSession(page)
    ref = _button_ref(session)
    client = FakeAnthropicClient(
        [
            tool_use_response("t1", "click", {"ref": ref}),
            tool_use_response("t2", "done", {"summary": "clicked it", "outputs": {}}),
        ]
    )

    result = run_discovery(goal="click the button", session=session, client=client)

    assert result.stop_reason == StopReason.DONE
    assert len(result.steps) == 2
    assert result.steps[0].tool_name == "click"
    assert result.steps[0].result.ok is True
    assert page.locator("#o").text_content() == "clicked"


def test_failed_action_is_recorded_but_loop_continues(page: Page):
    page.set_content("<html><body><p>x</p></body></html>")
    session = BrowserSession(page)
    client = FakeAnthropicClient(
        [
            tool_use_response("t1", "click", {"ref": "e999"}),  # stale ref -> fails
            tool_use_response("t2", "done", {"summary": "gave up gracefully", "outputs": {}}),
        ]
    )

    result = run_discovery(goal="click something that does not exist", session=session, client=client)

    assert result.stop_reason == StopReason.DONE
    assert result.steps[0].tool_name == "click"
    assert result.steps[0].result.ok is False
    assert "e999" in result.steps[0].result.error


# ---------------------------------------------------------------------------
# stuck
# ---------------------------------------------------------------------------


def test_stuck_stops_with_reason_and_details(page: Page):
    page.set_content("<html><body><p>x</p></body></html>")
    session = BrowserSession(page)
    client = FakeAnthropicClient(
        [tool_use_response("t1", "stuck", {"reason": "permission_denied", "details": "account restricted"})]
    )

    result = run_discovery(goal="do something forbidden", session=session, client=client)

    assert result.stop_reason == StopReason.STUCK
    assert result.stuck_reason == "permission_denied"
    assert result.stuck_details == "account restricted"
    assert result.steps[-1].result is None  # stuck is never dispatched to the executor


# ---------------------------------------------------------------------------
# stopping conditions: max_steps / timeout
# ---------------------------------------------------------------------------


def test_max_steps_stops_the_loop_when_model_never_finishes(page: Page):
    page.set_content('<html><body><input type="text"></body></html>')
    session = BrowserSession(page)
    ref = next(iter(session.snapshot().refs))
    # Script more turns than max_steps allows -- the loop must stop early,
    # not exhaust the script.
    script = [tool_use_response(f"t{i}", "type", {"ref": ref, "value": "x"}) for i in range(10)]
    client = FakeAnthropicClient(script)

    result = run_discovery(goal="never finishes", session=session, client=client, max_steps=3)

    assert result.stop_reason == StopReason.MAX_STEPS
    assert len(result.steps) == 3


def test_timeout_stops_the_loop_before_calling_the_model_again(page: Page):
    page.set_content('<html><body><input type="text"></body></html>')
    session = BrowserSession(page)
    ref = next(iter(session.snapshot().refs))
    script = [tool_use_response("t1", "type", {"ref": ref, "value": "x"})] * 5
    client = FakeAnthropicClient(script)

    # First turn happens immediately (elapsed ~0 < timeout), but a very
    # small timeout should stop the loop well before max_steps is reached.
    result = run_discovery(goal="slow", session=session, client=client, max_steps=100, timeout_seconds=0.0)

    assert result.stop_reason == StopReason.TIMEOUT
    assert len(result.steps) == 0  # timeout checked before the first observe/decide/act


# ---------------------------------------------------------------------------
# malformed model responses
# ---------------------------------------------------------------------------


def test_zero_tool_use_blocks_raises_goal_unreachable(page: Page):
    page.set_content("<html><body><p>x</p></body></html>")
    session = BrowserSession(page)
    client = FakeAnthropicClient([FakeResponseNoTool()])

    with pytest.raises(GoalUnreachableError):
        run_discovery(goal="g", session=session, client=client)


def test_multiple_tool_use_blocks_raises_goal_unreachable(page: Page):
    page.set_content("<html><body><p>x</p></body></html>")
    session = BrowserSession(page)
    from fake_anthropic import FakeBlock

    response = FakeResponse(
        [
            FakeBlock("tool_use", id="t1", name="click", input={"ref": "e1"}),
            FakeBlock("tool_use", id="t2", name="done", input={"summary": "x", "outputs": {}}),
        ]
    )
    client = FakeAnthropicClient([response])

    with pytest.raises(GoalUnreachableError):
        run_discovery(goal="g", session=session, client=client)


def FakeResponseNoTool() -> FakeResponse:
    from fake_anthropic import FakeBlock

    return FakeResponse([FakeBlock("text", text="I'm thinking out loud instead of calling a tool.")])


# ---------------------------------------------------------------------------
# transcript growth (full-history strategy)
# ---------------------------------------------------------------------------


def test_transcript_grows_with_each_turn_and_carries_prior_results(page: Page):
    page.set_content('<html><body><input type="text"></body></html>')
    session = BrowserSession(page)
    ref = next(iter(session.snapshot().refs))
    client = FakeAnthropicClient(
        [
            tool_use_response("t1", "type", {"ref": ref, "value": "12345"}),
            tool_use_response("t2", "done", {"summary": "done", "outputs": {}}),
        ]
    )

    run_discovery(goal="type into the field", session=session, client=client)

    # Second call's message list must be longer than the first's -- proof
    # the full history (snapshot + assistant tool_use + tool_result) is
    # being carried forward each turn, not reset.
    first_call_messages = client.calls[0]["messages"]
    second_call_messages = client.calls[1]["messages"]
    assert len(second_call_messages) > len(first_call_messages)


def test_sensitive_typed_value_is_redacted_in_transcript_sent_to_model(page: Page):
    page.set_content('<html><body><input type="password"></body></html>')
    session = BrowserSession(page)
    ref = next(iter(session.snapshot().refs))
    client = FakeAnthropicClient(
        [
            tool_use_response("t1", "type", {"ref": ref, "value": "hunter2", "sensitive": True}),
            tool_use_response("t2", "done", {"summary": "done", "outputs": {}}),
        ]
    )

    run_discovery(goal="log in", session=session, client=client)

    # The tool_result fed back to the model must not contain the raw secret.
    second_call_messages = client.calls[1]["messages"]
    serialized = str(second_call_messages)
    assert "hunter2" not in serialized
    assert "REDACTED" in serialized
