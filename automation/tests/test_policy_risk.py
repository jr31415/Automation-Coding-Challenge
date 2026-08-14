"""Tests for automation.policy.risk: action-type and artifact-level risk
classification, and the approval gate for risky replay.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from automation.artifact.schema import (
    AppTarget,
    ApprovalState,
    Artifact,
    ClickAction,
    NavigateAction,
    RiskLevel,
    RoleLocator,
    Step,
    TargetLocator,
    UrlMatchesCondition,
)
from automation.policy.risk import (
    ApprovalRequiredError,
    RISKY_ACTION_TYPES,
    SAFE_ACTION_TYPES,
    SENSITIVE_ACTION_TYPES,
    check_replay_allowed,
    classify_action_type,
)


# ---------------------------------------------------------------------------
# classify_action_type
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("action_type", sorted(SAFE_ACTION_TYPES))
def test_safe_action_types_classify_as_safe(action_type):
    assert classify_action_type(action_type) == "safe"


@pytest.mark.parametrize("action_type", sorted(SENSITIVE_ACTION_TYPES))
def test_sensitive_action_types_classify_as_sensitive(action_type):
    assert classify_action_type(action_type) == "sensitive"


@pytest.mark.parametrize("action_type", sorted(RISKY_ACTION_TYPES))
def test_risky_action_types_classify_as_risky(action_type):
    assert classify_action_type(action_type) == "risky"


def test_the_three_action_type_sets_are_disjoint():
    assert not (SAFE_ACTION_TYPES & SENSITIVE_ACTION_TYPES)
    assert not (SAFE_ACTION_TYPES & RISKY_ACTION_TYPES)
    assert not (SENSITIVE_ACTION_TYPES & RISKY_ACTION_TYPES)


def test_all_six_executable_tool_types_are_classified():
    all_known = SAFE_ACTION_TYPES | SENSITIVE_ACTION_TYPES | RISKY_ACTION_TYPES
    assert all_known == {"navigate", "click", "type", "select", "wait_for", "extract"}


# ---------------------------------------------------------------------------
# check_replay_allowed
# ---------------------------------------------------------------------------


def _artifact(risk_level: RiskLevel, approval_state: ApprovalState = ApprovalState.DRAFT) -> Artifact:
    return Artifact(
        id="a1",
        name="test_capability",
        version=1,
        description="test",
        app_target=AppTarget(vendor_product="v", base_url="http://localhost:4000"),
        inputs=[],
        outputs=[],
        steps=[Step(id="s1", description="x", action=NavigateAction(url="/x"))],
        success_condition=UrlMatchesCondition(pattern=".*"),
        risk_level=risk_level,
        discovered_at=datetime.now(timezone.utc),
        discovered_from_goal="g",
        approval_state=approval_state,
    )


def test_safe_artifact_always_allowed_regardless_of_approval_state():
    check_replay_allowed(_artifact(RiskLevel.SAFE, ApprovalState.DRAFT))  # must not raise


def test_sensitive_artifact_always_allowed_regardless_of_approval_state():
    check_replay_allowed(_artifact(RiskLevel.SENSITIVE, ApprovalState.DRAFT))  # must not raise


def test_risky_draft_artifact_is_refused_by_default():
    with pytest.raises(ApprovalRequiredError):
        check_replay_allowed(_artifact(RiskLevel.RISKY, ApprovalState.DRAFT))


def test_risky_approved_artifact_is_allowed():
    check_replay_allowed(_artifact(RiskLevel.RISKY, ApprovalState.APPROVED))  # must not raise


def test_risky_draft_artifact_allowed_with_explicit_override():
    check_replay_allowed(_artifact(RiskLevel.RISKY, ApprovalState.DRAFT), allow_unapproved=True)  # must not raise


def test_approval_required_error_names_artifact_and_version():
    try:
        check_replay_allowed(_artifact(RiskLevel.RISKY, ApprovalState.DRAFT))
        assert False, "expected ApprovalRequiredError"
    except ApprovalRequiredError as e:
        assert e.artifact_name == "test_capability"
        assert e.version == 1
        assert "test_capability" in str(e)
