"""Tests for automation.cli.discover: argument parsing, slugification, and
evidence serialization -- the pure-logic pieces of the CLI that don't need a
live browser or a real API key.

End-to-end CLI behavior (login -> navigate -> loop -> evidence written to
disk) is exercised manually against the mock app + a real or scripted
Anthropic client; see README.md for that demo path.
"""

from __future__ import annotations

import json

import pytest

from automation.agent.executor import ToolResult
from automation.agent.loop import RunResult, StepRecord, StopReason
from automation.agent.browser import Snapshot
from automation.cli.discover import (
    DEFAULT_TARGET,
    _slugify,
    _step_record_to_dict,
    _write_evidence,
    parse_args,
)


# ---------------------------------------------------------------------------
# parse_args
# ---------------------------------------------------------------------------


def test_goal_is_required():
    with pytest.raises(SystemExit):
        parse_args([])


def test_goal_only_uses_default_target():
    args = parse_args(["--goal", "look up member 12345"])
    assert args.goal == "look up member 12345"
    assert args.target == DEFAULT_TARGET


def test_explicit_target_overrides_default():
    args = parse_args(["--goal", "g", "--target", "http://localhost:5000/start"])
    assert args.target == "http://localhost:5000/start"


def test_default_credentials_are_present():
    args = parse_args(["--goal", "g"])
    assert args.username
    assert args.password


def test_max_steps_and_timeout_are_configurable():
    args = parse_args(["--goal", "g", "--max-steps", "10", "--timeout-seconds", "30"])
    assert args.max_steps == 10
    assert args.timeout_seconds == 30.0


def test_headed_flag_defaults_false():
    args = parse_args(["--goal", "g"])
    assert args.headed is False


def test_headed_flag_can_be_set():
    args = parse_args(["--goal", "g", "--headed"])
    assert args.headed is True


def test_evidence_dir_defaults_to_none_meaning_auto_generated():
    args = parse_args(["--goal", "g"])
    assert args.evidence_dir is None


# ---------------------------------------------------------------------------
# _slugify
# ---------------------------------------------------------------------------


def test_slugify_lowercases_and_hyphenates():
    assert _slugify("Look Up Member 12345") == "look-up-member-12345"


def test_slugify_strips_punctuation():
    assert _slugify("open a new sub-account, please!") == "open-a-new-sub-account-please"


def test_slugify_truncates_to_max_len_and_has_no_trailing_hyphen():
    long_goal = "a" * 100
    slug = _slugify(long_goal, max_len=10)
    assert len(slug) <= 10
    assert not slug.endswith("-")


def test_slugify_empty_input_falls_back_to_goal():
    assert _slugify("!!!") == "goal"


# ---------------------------------------------------------------------------
# _step_record_to_dict
# ---------------------------------------------------------------------------


def _fake_snapshot(url: str = "http://localhost:4000/members/search") -> Snapshot:
    return Snapshot(url=url, title="t", tree_text="- generic [ref=e1]:", refs=frozenset({"e1"}))


def test_step_record_with_result_serializes_result_fields():
    record = StepRecord(
        index=0,
        snapshot=_fake_snapshot(),
        tool_name="click",
        tool_input={"ref": "e1"},
        result=ToolResult(ok=True, tool="click", data={"ref": "e1"}, error=None),
    )
    d = _step_record_to_dict(record)
    assert d["index"] == 0
    assert d["url"] == "http://localhost:4000/members/search"
    assert d["tool_name"] == "click"
    assert d["result"] == {"ok": True, "data": {"ref": "e1"}, "error": None}


def test_step_record_without_result_serializes_result_as_none():
    # done/stuck steps never reach the executor, so result is None.
    record = StepRecord(
        index=1,
        snapshot=_fake_snapshot(),
        tool_name="done",
        tool_input={"summary": "x", "outputs": {}},
        result=None,
    )
    d = _step_record_to_dict(record)
    assert d["result"] is None


def test_step_record_serialization_is_json_safe():
    record = StepRecord(
        index=0,
        snapshot=_fake_snapshot(),
        tool_name="extract",
        tool_input={"ref": "e1", "output_key": "balance"},
        result=ToolResult(ok=True, tool="extract", data={"outputKey": "balance", "value": "$100.00"}, error=None),
    )
    # Must not raise -- proves every field is a plain JSON-serializable type.
    json.dumps(_step_record_to_dict(record))


# ---------------------------------------------------------------------------
# _write_evidence
# ---------------------------------------------------------------------------


def test_write_evidence_creates_log_json_with_expected_shape(tmp_path):
    result = RunResult(
        stop_reason=StopReason.DONE,
        steps=[
            StepRecord(
                index=0,
                snapshot=_fake_snapshot(),
                tool_name="done",
                tool_input={"summary": "ok", "outputs": {"x": "1"}},
                result=None,
            )
        ],
        outputs={"savingsBalance": "$4823.11"},
        summary="Looked up member 12345.",
        elapsed_seconds=1.23,
    )

    evidence_dir = tmp_path / "run-1"
    _write_evidence(evidence_dir, "look up member 12345", DEFAULT_TARGET, result, final_screenshot=b"\x89PNG...")

    log_path = evidence_dir / "log.json"
    assert log_path.exists()
    log = json.loads(log_path.read_text())
    assert log["goal"] == "look up member 12345"
    assert log["stopReason"] == "done"
    assert log["outputs"] == {"savingsBalance": "$4823.11"}
    assert log["summary"] == "Looked up member 12345."
    assert len(log["steps"]) == 1


def test_write_evidence_writes_screenshot_when_provided(tmp_path):
    result = RunResult(stop_reason=StopReason.STUCK, steps=[], stuck_reason="permission_denied", stuck_details="x")
    evidence_dir = tmp_path / "run-2"

    _write_evidence(evidence_dir, "g", DEFAULT_TARGET, result, final_screenshot=b"fake-png-bytes")

    screenshot_path = evidence_dir / "final.png"
    assert screenshot_path.exists()
    assert screenshot_path.read_bytes() == b"fake-png-bytes"


def test_write_evidence_skips_screenshot_file_when_none(tmp_path):
    result = RunResult(stop_reason=StopReason.MAX_STEPS, steps=[])
    evidence_dir = tmp_path / "run-3"

    _write_evidence(evidence_dir, "g", DEFAULT_TARGET, result, final_screenshot=None)

    assert not (evidence_dir / "final.png").exists()
    assert (evidence_dir / "log.json").exists()


def test_write_evidence_creates_nested_directories(tmp_path):
    result = RunResult(stop_reason=StopReason.DONE, steps=[])
    evidence_dir = tmp_path / "a" / "b" / "c"

    _write_evidence(evidence_dir, "g", DEFAULT_TARGET, result, final_screenshot=None)

    assert evidence_dir.exists()


def test_write_evidence_stuck_run_includes_stuck_fields(tmp_path):
    result = RunResult(
        stop_reason=StopReason.STUCK,
        steps=[],
        stuck_reason="permission_denied",
        stuck_details="account restricted",
    )
    evidence_dir = tmp_path / "run-4"

    _write_evidence(evidence_dir, "g", DEFAULT_TARGET, result, final_screenshot=None)

    log = json.loads((evidence_dir / "log.json").read_text())
    assert log["stuckReason"] == "permission_denied"
    assert log["stuckDetails"] == "account restricted"
