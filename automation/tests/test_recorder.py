"""Tests for automation.artifact.recorder: translating a completed
discovery RunResult into a durable, replayable Artifact.

Uses synthetic Snapshot/StepRecord/RunResult fixtures built from real
snapshot tree-text shapes captured against the mock app, so these tests
don't need a live browser or an API key.
"""

from __future__ import annotations

import pytest

from automation.agent.browser import Snapshot
from automation.agent.executor import ToolResult
from automation.agent.loop import RunResult, StepRecord, StopReason
from automation.artifact.recorder import RecordingError, record_artifact
from automation.artifact.schema import (
    AppTarget,
    LabelForLocator,
    RiskLevel,
    RoleLocator,
    StructuralLocator,
)

APP_TARGET = AppTarget(vendor_product="riverbend-core-admin", base_url="http://localhost:4000")

SEARCH_PAGE_TREE = """- generic [active] [ref=e1]:
  - heading "Member Search" [level=2] [ref=e2]
  - table [ref=e3]:
    - rowgroup [ref=e4]:
      - row [ref=e5]:
        - cell "Member ID:" [ref=e6]
        - cell [ref=e7]:
          - textbox [ref=e8]
        - cell [ref=e9]:
          - button "Look Up" [ref=e10]
"""

MEMBER_PAGE_TREE = """- generic [active] [ref=e1]:
  - table [ref=e2]:
    - rowgroup [ref=e3]:
      - row [ref=e4]:
        - cell "Name" [ref=e5]
        - cell "Alice Whitfield" [ref=e6]
      - row [ref=e7]:
        - cell "Savings Balance" [ref=e8]
        - cell "$4823.11" [ref=e9]
"""


def _snap(url: str, tree_text: str) -> Snapshot:
    import re

    refs = frozenset(re.findall(r"\[ref=([^\]]+)\]", tree_text))
    return Snapshot(url=url, title="", tree_text=tree_text, refs=refs)


def _successful_run() -> RunResult:
    search_snap = _snap("http://localhost:4000/members/search", SEARCH_PAGE_TREE)
    member_snap = _snap("http://localhost:4000/members/12345", MEMBER_PAGE_TREE)

    steps = [
        StepRecord(
            index=0,
            snapshot=search_snap,
            tool_name="type",
            tool_input={"ref": "e8", "value": "12345", "sensitive": False},
            result=ToolResult(ok=True, tool="type", data={"ref": "e8", "value": "12345"}, error=None),
        ),
        StepRecord(
            index=1,
            snapshot=search_snap,
            tool_name="click",
            tool_input={"ref": "e10"},
            result=ToolResult(ok=True, tool="click", data={"ref": "e10"}, error=None),
        ),
        StepRecord(
            index=2,
            snapshot=member_snap,
            tool_name="extract",
            tool_input={"ref": "e9", "output_key": "savingsBalance"},
            result=ToolResult(ok=True, tool="extract", data={"outputKey": "savingsBalance", "value": "$4823.11"}, error=None),
        ),
        StepRecord(
            index=3,
            snapshot=member_snap,
            tool_name="done",
            tool_input={"summary": "Done.", "outputs": {"savingsBalance": "$4823.11"}},
            result=None,
        ),
    ]
    return RunResult(
        stop_reason=StopReason.DONE,
        steps=steps,
        outputs={"savingsBalance": "$4823.11"},
        summary="Looked up member 12345 and read savings balance.",
        elapsed_seconds=5.0,
    )


def _record(run: RunResult, **overrides):
    kwargs = dict(
        goal="look up member 12345 and read their current savings balance",
        run=run,
        name="lookup_member_savings_balance",
        app_target=APP_TARGET,
        risk_level=RiskLevel.SAFE,
    )
    kwargs.update(overrides)
    return record_artifact(**kwargs)


# ---------------------------------------------------------------------------
# happy path / overall shape
# ---------------------------------------------------------------------------


def test_records_one_step_per_executable_tool_call_excluding_done():
    artifact = _record(_successful_run())
    assert len(artifact.steps) == 3  # type, click, extract -- not done
    assert [s.action.type for s in artifact.steps] == ["type", "click", "extract"]


def test_metadata_is_carried_through():
    artifact = _record(_successful_run())
    assert artifact.name == "lookup_member_savings_balance"
    assert artifact.discovered_from_goal == "look up member 12345 and read their current savings balance"
    assert artifact.app_target == APP_TARGET
    assert artifact.risk_level == RiskLevel.SAFE
    assert artifact.approval_state.value == "draft"
    assert artifact.version == 1


def test_extract_step_becomes_an_output_field():
    artifact = _record(_successful_run())
    assert len(artifact.outputs) == 1
    assert artifact.outputs[0].name == "savingsBalance"


def test_success_condition_is_url_match_on_final_page_path():
    artifact = _record(_successful_run())
    assert artifact.success_condition.kind == "urlMatches"
    # The literal member id typed during this run ("12345") must be
    # templated back to its {{param}} placeholder -- a success_condition
    # that hardcoded "/members/12345$" would never match a replay for any
    # other member, even though the flow reached the equivalent page.
    assert "{{" in artifact.success_condition.pattern
    assert "12345" not in artifact.success_condition.pattern


def test_success_condition_pattern_renders_correctly_for_the_recorded_input_param():
    import re

    artifact = _record(_successful_run())
    param_name = artifact.inputs[0].name
    rendered = artifact.success_condition.pattern.replace(f"{{{{{param_name}}}}}", "67890")
    assert re.search(rendered, "http://localhost:4000/members/67890")
    assert not re.search(rendered, "http://localhost:4000/members/12345/sub-account/new")


# ---------------------------------------------------------------------------
# only DONE runs are recordable
# ---------------------------------------------------------------------------


def test_stuck_run_cannot_be_recorded():
    run = RunResult(stop_reason=StopReason.STUCK, steps=[], stuck_reason="permission_denied", stuck_details="x")
    with pytest.raises(RecordingError):
        _record(run)


def test_max_steps_run_cannot_be_recorded():
    run = RunResult(stop_reason=StopReason.MAX_STEPS, steps=[])
    with pytest.raises(RecordingError):
        _record(run)


def test_immediate_done_with_no_actions_raises():
    snap = _snap("http://localhost:4000/members/search", SEARCH_PAGE_TREE)
    run = RunResult(
        stop_reason=StopReason.DONE,
        steps=[StepRecord(index=0, snapshot=snap, tool_name="done", tool_input={"summary": "x", "outputs": {}}, result=None)],
        outputs={},
        summary="x",
    )
    with pytest.raises(RecordingError):
        _record(run)


# ---------------------------------------------------------------------------
# locator derivation: named elements
# ---------------------------------------------------------------------------


def test_click_locator_uses_role_and_accessible_name():
    artifact = _record(_successful_run())
    click_step = artifact.steps[1]
    assert click_step.action.type == "click"
    primary = click_step.action.target.strategies[0]
    assert isinstance(primary, RoleLocator)
    assert primary.role == "button"
    assert primary.accessible_name == "Look Up"


def test_click_locator_has_text_fallback():
    artifact = _record(_successful_run())
    click_step = artifact.steps[1]
    kinds = [s.kind for s in click_step.action.target.strategies]
    assert "text" in kinds


# ---------------------------------------------------------------------------
# locator derivation: unlabeled elements fall back to structural
# ---------------------------------------------------------------------------


def test_unlabeled_textbox_falls_back_to_structural_locator():
    artifact = _record(_successful_run())
    type_step = artifact.steps[0]
    assert type_step.action.type == "type"
    primary = type_step.action.target.strategies[0]
    assert isinstance(primary, StructuralLocator)
    # Must be a real HTML tag Playwright can use as a CSS selector, not the
    # bare ARIA role -- "textbox" is not a valid tag/selector, "input" is
    # the HTML element that produces that role.
    assert primary.tag == "input"


def test_unlabeled_element_reasoning_flags_the_missing_label():
    artifact = _record(_successful_run())
    reasoning = artifact.steps[0].action.target.reasoning
    assert "no accessible name" in reasoning.lower()


# ---------------------------------------------------------------------------
# locator derivation: the critical robustness fix -- data cells must not be
# located by their own (variable) text.
# ---------------------------------------------------------------------------


def test_extract_of_a_data_value_uses_adjacent_label_not_the_value_itself():
    artifact = _record(_successful_run())
    extract_step = artifact.steps[2]
    assert extract_step.action.type == "extract"
    primary = extract_step.action.target.strategies[0]
    assert isinstance(primary, LabelForLocator)
    assert primary.label == "Savings Balance"
    # Must NOT be locating by the balance's own (member-specific) text --
    # that would only ever match member 12345's exact balance again.
    for strategy in extract_step.action.target.strategies:
        if hasattr(strategy, "text"):
            assert strategy.text != "$4823.11"
        if hasattr(strategy, "accessible_name"):
            assert strategy.accessible_name != "$4823.11"


def test_extract_locator_reasoning_explains_the_label_anchor_choice():
    artifact = _record(_successful_run())
    reasoning = artifact.steps[2].action.target.reasoning
    assert "record data" in reasoning.lower() or "differ on every replay" in reasoning.lower()


def test_data_cell_with_no_sibling_label_falls_back_to_value_text_with_warning():
    tree = (
        "- generic [ref=e1]:\n"
        "  - row [ref=e2]:\n"
        '    - cell "$999.99" [ref=e3]\n'
    )
    snap = _snap("http://localhost:4000/x", tree)
    steps = [
        StepRecord(
            index=0,
            snapshot=snap,
            tool_name="extract",
            tool_input={"ref": "e3", "output_key": "amount"},
            result=ToolResult(ok=True, tool="extract", data={"outputKey": "amount", "value": "$999.99"}, error=None),
        ),
        StepRecord(index=1, snapshot=snap, tool_name="done", tool_input={"summary": "x", "outputs": {}}, result=None),
    ]
    run = RunResult(stop_reason=StopReason.DONE, steps=steps, outputs={"amount": "$999.99"}, summary="x")
    artifact = _record(run)
    reasoning = artifact.steps[0].action.target.reasoning
    assert "warning" in reasoning.lower()


# ---------------------------------------------------------------------------
# input parameterization
# ---------------------------------------------------------------------------


def test_typed_literal_becomes_a_templated_input_param():
    artifact = _record(_successful_run())
    type_step = artifact.steps[0]
    assert type_step.action.value.startswith("{{")
    assert type_step.action.value.endswith("}}")
    param_name = type_step.action.value.strip("{}")
    assert any(p.name == param_name for p in artifact.inputs)


def test_same_literal_value_typed_twice_reuses_the_same_param_name():
    search_snap = _snap("http://localhost:4000/members/search", SEARCH_PAGE_TREE)
    steps = [
        StepRecord(
            index=0,
            snapshot=search_snap,
            tool_name="type",
            tool_input={"ref": "e8", "value": "12345", "sensitive": False},
            result=ToolResult(ok=True, tool="type", data={}, error=None),
        ),
        StepRecord(
            index=1,
            snapshot=search_snap,
            tool_name="type",
            tool_input={"ref": "e8", "value": "12345", "sensitive": False},
            result=ToolResult(ok=True, tool="type", data={}, error=None),
        ),
        StepRecord(index=2, snapshot=search_snap, tool_name="done", tool_input={"summary": "x", "outputs": {}}, result=None),
    ]
    run = RunResult(stop_reason=StopReason.DONE, steps=steps, outputs={}, summary="x")
    artifact = _record(run)
    assert artifact.steps[0].action.value == artifact.steps[1].action.value
    assert len(artifact.inputs) == 1


def test_sensitive_typed_value_becomes_sensitive_input_param_without_the_raw_value():
    search_snap = _snap("http://localhost:4000/login", SEARCH_PAGE_TREE)
    steps = [
        StepRecord(
            index=0,
            snapshot=search_snap,
            tool_name="type",
            tool_input={"ref": "e8", "value": "hunter2", "sensitive": True},
            result=ToolResult(ok=True, tool="type", data={"ref": "e8", "value": "[REDACTED]"}, error=None),
        ),
        StepRecord(index=1, snapshot=search_snap, tool_name="done", tool_input={"summary": "x", "outputs": {}}, result=None),
    ]
    run = RunResult(stop_reason=StopReason.DONE, steps=steps, outputs={}, summary="x")
    artifact = _record(run)

    sensitive_params = [p for p in artifact.inputs if p.sensitive]
    assert len(sensitive_params) == 1
    # The raw secret must never appear anywhere in the serialized artifact.
    serialized = artifact.model_dump_json()
    assert "hunter2" not in serialized


# ---------------------------------------------------------------------------
# unknown / malformed tool calls
# ---------------------------------------------------------------------------


def test_unknown_tool_name_raises_recording_error():
    snap = _snap("http://localhost:4000/x", "- generic [ref=e1]:\n")
    steps = [
        StepRecord(index=0, snapshot=snap, tool_name="not_a_real_tool", tool_input={}, result=None),
        StepRecord(index=1, snapshot=snap, tool_name="done", tool_input={"summary": "x", "outputs": {}}, result=None),
    ]
    run = RunResult(stop_reason=StopReason.DONE, steps=steps, outputs={}, summary="x")
    with pytest.raises(RecordingError):
        _record(run)


def test_ref_missing_from_its_own_snapshot_raises():
    snap = _snap("http://localhost:4000/x", "- generic [ref=e1]:\n")
    steps = [
        StepRecord(index=0, snapshot=snap, tool_name="click", tool_input={"ref": "e999"}, result=ToolResult(ok=True, tool="click", data={}, error=None)),
        StepRecord(index=1, snapshot=snap, tool_name="done", tool_input={"summary": "x", "outputs": {}}, result=None),
    ]
    run = RunResult(stop_reason=StopReason.DONE, steps=steps, outputs={}, summary="x")
    with pytest.raises(RecordingError):
        _record(run)


# ---------------------------------------------------------------------------
# artifact round-trips through JSON (it's a saved, versioned file)
# ---------------------------------------------------------------------------


def test_artifact_round_trips_through_json():
    from automation.artifact.schema import Artifact

    artifact = _record(_successful_run())
    raw = artifact.model_dump_json()
    reloaded = Artifact.model_validate_json(raw)
    assert reloaded == artifact
