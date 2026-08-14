"""Tests for automation.escalation.handoff: the pure logic of translating a
stuck RunResult into an intervention request, and building the handoff
result once a human has acted.
"""

from __future__ import annotations

from datetime import datetime, timezone

from automation.agent.browser import Snapshot
from automation.agent.loop import RunResult, StepRecord, StopReason
from automation.escalation.handoff import (
    ControlState,
    build_handoff_result,
    build_intervention_request,
    format_intervention_summary,
)


def _snap(url: str) -> Snapshot:
    return Snapshot(url=url, title="t", tree_text="- generic [ref=e1]:", refs=frozenset({"e1"}))


def _stuck_run(reason="permission_denied", details="account restricted", n_steps=3) -> RunResult:
    steps = [
        StepRecord(index=i, snapshot=_snap(f"http://localhost:4000/step{i}"), tool_name="click", tool_input={}, result=None)
        for i in range(n_steps)
    ]
    return RunResult(
        stop_reason=StopReason.STUCK,
        steps=steps,
        stuck_reason=reason,
        stuck_details=details,
    )


# ---------------------------------------------------------------------------
# build_intervention_request
# ---------------------------------------------------------------------------


def test_intervention_request_carries_goal_reason_and_details():
    run = _stuck_run(reason="permission_denied", details="account restricted")
    req = build_intervention_request(
        goal="open a new sub-account",
        run_result=run,
        current_url="http://localhost:4000/members/11111",
        screenshot_path="/tmp/shot.png",
        cdp_endpoint="ws://127.0.0.1:9222/devtools/browser/abc",
    )
    assert req.goal == "open a new sub-account"
    assert req.reason == "permission_denied"
    assert req.details == "account restricted"
    assert req.current_url == "http://localhost:4000/members/11111"


def test_intervention_request_steps_completed_matches_run_steps():
    run = _stuck_run(n_steps=5)
    req = build_intervention_request(
        goal="g", run_result=run, current_url="http://x", screenshot_path=None, cdp_endpoint="ws://x"
    )
    assert req.steps_completed == 5


def test_intervention_request_defaults_reason_when_missing():
    run = RunResult(stop_reason=StopReason.STUCK, steps=[], stuck_reason=None, stuck_details=None)
    req = build_intervention_request(
        goal="g", run_result=run, current_url="http://x", screenshot_path=None, cdp_endpoint="ws://x"
    )
    assert req.reason == "unknown"
    assert req.details == ""


def test_intervention_request_screenshot_path_optional():
    run = _stuck_run()
    req = build_intervention_request(
        goal="g", run_result=run, current_url="http://x", screenshot_path=None, cdp_endpoint="ws://x"
    )
    assert req.screenshot_path is None


def test_intervention_request_carries_session_endpoint():
    run = _stuck_run()
    req = build_intervention_request(
        goal="g",
        run_result=run,
        current_url="http://x",
        screenshot_path=None,
        cdp_endpoint="ws://127.0.0.1:9222/devtools/browser/xyz",
    )
    assert req.session_endpoint == "ws://127.0.0.1:9222/devtools/browser/xyz"


def test_intervention_request_timestamp_is_recent_utc():
    run = _stuck_run()
    before = datetime.now(timezone.utc)
    req = build_intervention_request(
        goal="g", run_result=run, current_url="http://x", screenshot_path=None, cdp_endpoint="ws://x"
    )
    after = datetime.now(timezone.utc)
    assert before <= req.requested_at <= after


# ---------------------------------------------------------------------------
# format_intervention_summary
# ---------------------------------------------------------------------------


def test_summary_includes_reason_details_and_endpoint():
    run = _stuck_run(reason="unexpected_dialog", details="a confirm() popup appeared")
    req = build_intervention_request(
        goal="close the account",
        run_result=run,
        current_url="http://localhost:4000/members/12345",
        screenshot_path="/tmp/x.png",
        cdp_endpoint="ws://127.0.0.1:9222/devtools/browser/abc",
    )
    summary = format_intervention_summary(req)
    assert "unexpected_dialog" in summary
    assert "a confirm() popup appeared" in summary
    assert "ws://127.0.0.1:9222/devtools/browser/abc" in summary
    assert "close the account" in summary
    assert "SAME session" in summary


def test_summary_handles_missing_screenshot_gracefully():
    run = _stuck_run()
    req = build_intervention_request(
        goal="g", run_result=run, current_url="http://x", screenshot_path=None, cdp_endpoint="ws://x"
    )
    summary = format_intervention_summary(req)
    assert "Screenshot:" not in summary


def test_summary_includes_screenshot_path_when_present():
    run = _stuck_run()
    req = build_intervention_request(
        goal="g", run_result=run, current_url="http://x", screenshot_path="/tmp/evidence.png", cdp_endpoint="ws://x"
    )
    summary = format_intervention_summary(req)
    assert "/tmp/evidence.png" in summary


# ---------------------------------------------------------------------------
# build_handoff_result
# ---------------------------------------------------------------------------


def test_handoff_result_resumed_true():
    result = build_handoff_result(resumed=True, operator_notes="cleared the restriction", actions_taken=["clicked approve"])
    assert result.resumed is True
    assert result.operator_notes == "cleared the restriction"
    assert result.actions_taken == ["clicked approve"]
    assert result.handled_at is not None


def test_handoff_result_resumed_false_for_abort():
    result = build_handoff_result(resumed=False, operator_notes="cannot be fixed", actions_taken=[])
    assert result.resumed is False
    assert result.actions_taken == []


# ---------------------------------------------------------------------------
# ControlState
# ---------------------------------------------------------------------------


def test_control_state_has_all_four_named_states():
    assert {s.value for s in ControlState} == {"automation", "awaiting_human", "human", "returned_to_automation"}
