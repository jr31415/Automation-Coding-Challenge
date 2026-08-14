"""Tests for automation.replay.engine: the deterministic replay engine.

Uses Playwright's page.route to serve a small synthetic multi-page site
in-process (no real server needed) so these tests can exercise real
navigation, session-timeout redirects, and multi-step flows exactly as
replay would encounter them against the mock app.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from playwright.sync_api import Page

from automation.artifact.schema import (
    AppTarget,
    ApprovalState,
    Artifact,
    ClickAction,
    ElementTextContainsCondition,
    ExtractAction,
    InputParam,
    KnownOutcome,
    LabelForLocator,
    NavigateAction,
    ParamType,
    RiskLevel,
    RoleLocator,
    Step,
    StructuralLocator,
    TargetLocator,
    TypeAction,
    UrlMatchesCondition,
    WaitForAction,
)
from automation.replay.engine import DEFAULT_LOCATOR_RETRY_DELAY_SECONDS, replay
from automation.replay.result import BusinessOutcome, Failure, FailureKind, Success

PAGES = {
    "https://app.test/search": """
        <html><body>
        <input type="text" name="q">
        <button>Look Up</button>
        </body></html>
    """,
    "https://app.test/members/12345": """
        <html><body>
        <table><tr><td>Savings Balance</td><td>$4823.11</td></tr></table>
        </body></html>
    """,
    "https://app.test/members/67890": """
        <html><body>
        <table><tr><td>Savings Balance</td><td>$99.20</td></tr></table>
        </body></html>
    """,
    "https://app.test/members/99999": """
        <html><body><p>No member found with ID "99999".</p></body></html>
    """,
    "https://app.test/login": """
        <html><body><p>Your session has expired. Please sign in again.</p></body></html>
    """,
}


@pytest.fixture
def routed_page(page: Page):
    def handler(route):
        url = route.request.url
        body = PAGES.get(url)
        if body is None:
            route.fulfill(status=404, body="not found")
        else:
            route.fulfill(status=200, content_type="text/html", body=body)

    page.route("https://app.test/**", handler)
    yield page


def _base_artifact(**overrides) -> Artifact:
    defaults = dict(
        id="a1",
        name="test_capability",
        version=1,
        description="test",
        app_target=AppTarget(vendor_product="test-vendor", base_url="https://app.test"),
        inputs=[InputParam(name="memberId", type=ParamType.STRING, description="member id")],
        outputs=[],
        steps=[],
        success_condition=UrlMatchesCondition(pattern="/members/{{memberId}}$"),
        risk_level=RiskLevel.SAFE,
        discovered_at=datetime.now(timezone.utc),
        discovered_from_goal="test goal",
    )
    defaults.update(overrides)
    return Artifact(**defaults)


LOOKUP_STEPS = [
    Step(
        id="s1",
        description="navigate to search",
        action=NavigateAction(url="https://app.test/search"),
    ),
    Step(
        id="s2",
        description="type member id",
        action=TypeAction(
            target=TargetLocator(strategies=[StructuralLocator(tag="input", nth=0)], reasoning="x"),
            value="{{memberId}}",
        ),
    ),
    Step(
        id="s3",
        description="click look up",
        action=ClickAction(
            target=TargetLocator(strategies=[RoleLocator(role="button", accessible_name="Look Up")], reasoning="x")
        ),
    ),
    Step(
        id="s4",
        description="navigate directly to member (stand-in for the click's real navigation, since our fake Look Up button has no href/js)",
        action=NavigateAction(url="https://app.test/members/{{memberId}}"),
    ),
    Step(
        id="s5",
        description="extract savings balance",
        action=ExtractAction(
            target=TargetLocator(strategies=[LabelForLocator(label="Savings Balance")], reasoning="x"),
            output_key="savingsBalance",
        ),
    ),
]


# ---------------------------------------------------------------------------
# success path
# ---------------------------------------------------------------------------


def test_success_returns_outputs_and_steps_completed(routed_page: Page):
    artifact = _base_artifact(steps=LOOKUP_STEPS)
    result = replay(artifact, {"memberId": "12345"}, routed_page)
    assert isinstance(result, Success)
    assert result.outputs == {"savingsBalance": "$4823.11"}
    assert result.steps_completed == len(LOOKUP_STEPS)


def test_success_generalizes_to_a_different_input_value(routed_page: Page):
    artifact = _base_artifact(steps=LOOKUP_STEPS)
    result = replay(artifact, {"memberId": "67890"}, routed_page)
    assert isinstance(result, Success)
    assert result.outputs == {"savingsBalance": "$99.20"}


# ---------------------------------------------------------------------------
# invalid input
# ---------------------------------------------------------------------------


def test_missing_required_input_returns_invalid_input_failure(routed_page: Page):
    artifact = _base_artifact(steps=LOOKUP_STEPS)
    result = replay(artifact, {}, routed_page)
    assert isinstance(result, Failure)
    assert result.kind == FailureKind.INVALID_INPUT
    assert "memberId" in result.observed


def test_unknown_input_returns_invalid_input_failure(routed_page: Page):
    artifact = _base_artifact(steps=LOOKUP_STEPS)
    result = replay(artifact, {"memberId": "12345", "bogus": "x"}, routed_page)
    assert isinstance(result, Failure)
    assert result.kind == FailureKind.INVALID_INPUT


# ---------------------------------------------------------------------------
# locator not found
# ---------------------------------------------------------------------------


def test_locator_not_found_returns_failure_with_step_id(routed_page: Page):
    broken_steps = [
        Step(id="s1", description="navigate", action=NavigateAction(url="https://app.test/search")),
        Step(
            id="s2",
            description="click something that doesn't exist",
            action=ClickAction(
                target=TargetLocator(strategies=[RoleLocator(role="button", accessible_name="Ghost")], reasoning="x")
            ),
        ),
    ]
    artifact = _base_artifact(steps=broken_steps)
    result = replay(artifact, {"memberId": "12345"}, routed_page, locator_retries=1, locator_retry_delay_seconds=0.05)
    assert isinstance(result, Failure)
    assert result.kind == FailureKind.LOCATOR_NOT_FOUND
    assert result.step_id == "s2"


def test_locator_retries_before_failing(routed_page: Page):
    # A locator that only becomes available after the page has had a moment
    # to "settle" -- retries give the engine a chance to catch up rather
    # than failing on the very first check. We simulate this by pointing at
    # a page where the element genuinely isn't there, and confirming the
    # observed log shows multiple attempts (proving retries actually ran).
    broken_steps = [
        Step(id="s1", description="navigate", action=NavigateAction(url="https://app.test/search")),
        Step(
            id="s2",
            description="click ghost",
            action=ClickAction(
                target=TargetLocator(strategies=[RoleLocator(role="button", accessible_name="Ghost")], reasoning="x")
            ),
        ),
    ]
    artifact = _base_artifact(steps=broken_steps)
    result = replay(artifact, {"memberId": "12345"}, routed_page, locator_retries=2, locator_retry_delay_seconds=0.05)
    assert isinstance(result, Failure)
    assert "attempt 1" in result.observed
    assert "attempt 2" in result.observed
    assert "attempt 3" in result.observed


# ---------------------------------------------------------------------------
# checkpoint failure (post_condition and final success_condition)
# ---------------------------------------------------------------------------


def test_post_condition_failure_stops_immediately(routed_page: Page):
    steps = [
        Step(
            id="s1",
            description="navigate to search",
            action=NavigateAction(url="https://app.test/search"),
            post_condition=UrlMatchesCondition(pattern="this-will-never-match"),
        ),
    ]
    artifact = _base_artifact(steps=steps)
    result = replay(artifact, {"memberId": "12345"}, routed_page)
    assert isinstance(result, Failure)
    assert result.kind == FailureKind.CHECKPOINT_FAILED
    assert result.step_id == "s1"


def test_final_success_condition_failure_when_flow_lands_on_wrong_page(routed_page: Page):
    steps = [Step(id="s1", description="navigate", action=NavigateAction(url="https://app.test/search"))]
    artifact = _base_artifact(steps=steps)  # success_condition expects /members/{{memberId}}
    result = replay(artifact, {"memberId": "12345"}, routed_page)
    assert isinstance(result, Failure)
    assert result.kind == FailureKind.CHECKPOINT_FAILED
    assert result.step_id is None  # final checkpoint, not tied to a specific step


# ---------------------------------------------------------------------------
# session expiry
# ---------------------------------------------------------------------------


def test_session_expired_detected_after_a_step(routed_page: Page):
    steps = [Step(id="s1", description="navigate to login by mistake", action=NavigateAction(url="https://app.test/login"))]
    artifact = _base_artifact(steps=steps)
    result = replay(artifact, {"memberId": "12345"}, routed_page)
    assert isinstance(result, Failure)
    assert result.kind == FailureKind.SESSION_EXPIRED
    assert result.step_id == "s1"


# ---------------------------------------------------------------------------
# known business outcomes
# ---------------------------------------------------------------------------


def test_known_outcome_detected_short_circuits_before_success_condition(routed_page: Page):
    steps = [
        Step(id="s1", description="navigate to a not-found member", action=NavigateAction(url="https://app.test/members/99999")),
    ]
    artifact = _base_artifact(
        steps=steps,
        known_outcomes=[
            KnownOutcome(
                name="member_not_found",
                description="No member exists with the given ID.",
                detect=ElementTextContainsCondition(
                    target=TargetLocator(strategies=[StructuralLocator(tag="p", nth=0)], reasoning="x"),
                    substring="No member found",
                ),
            )
        ],
    )
    result = replay(artifact, {"memberId": "99999"}, routed_page)
    assert isinstance(result, BusinessOutcome)
    assert result.name == "member_not_found"


def test_no_known_outcome_declared_falls_through_to_checkpoint_failure(routed_page: Page):
    # Land on the not-found page for member 99999, but declare a
    # success_condition that expects a *different* member's page -- this
    # confirms an undeclared not-found condition does NOT silently succeed
    # or silently produce a BusinessOutcome that was never declared; it
    # fails loudly instead, which is the correct behavior here.
    steps = [Step(id="s1", description="navigate", action=NavigateAction(url="https://app.test/members/99999"))]
    artifact = _base_artifact(steps=steps, success_condition=UrlMatchesCondition(pattern="/members/12345$"))
    result = replay(artifact, {"memberId": "99999"}, routed_page)
    assert isinstance(result, Failure)
    assert result.kind == FailureKind.CHECKPOINT_FAILED


# ---------------------------------------------------------------------------
# waitFor / timeout
# ---------------------------------------------------------------------------


def test_wait_for_url_matches_that_never_holds_times_out(routed_page: Page):
    steps = [
        Step(id="s1", description="navigate", action=NavigateAction(url="https://app.test/search")),
        Step(
            id="s2",
            description="wait for something that never happens",
            action=WaitForAction(condition=UrlMatchesCondition(pattern="never-gonna-happen"), timeout_ms=200),
        ),
    ]
    artifact = _base_artifact(steps=steps)
    result = replay(artifact, {"memberId": "12345"}, routed_page)
    assert isinstance(result, Failure)
    assert result.kind == FailureKind.TIMEOUT
    assert result.step_id == "s2"


def test_wait_for_url_matches_that_already_holds_succeeds_immediately(routed_page: Page):
    steps = [
        Step(id="s1", description="navigate", action=NavigateAction(url="https://app.test/search")),
        Step(
            id="s2",
            description="wait for the page we are already on",
            action=WaitForAction(condition=UrlMatchesCondition(pattern="search"), timeout_ms=1000),
        ),
    ]
    artifact = _base_artifact(
        steps=steps,
        success_condition=UrlMatchesCondition(pattern="search"),
    )
    result = replay(artifact, {"memberId": "12345"}, routed_page)
    assert isinstance(result, Success)


# ---------------------------------------------------------------------------
# allowlist enforcement
# ---------------------------------------------------------------------------


def _allowlist_permitting(*action_types, domain="app.test", path_patterns=("*",)):
    from automation.policy.allowlist import Allowlist, AllowlistEntry

    return Allowlist(
        entries=(AllowlistEntry(domain=domain, path_patterns=path_patterns),),
        allowed_action_types=frozenset(action_types),
    )


def test_replay_blocked_when_navigate_action_type_not_allowlisted(routed_page: Page):
    steps = [Step(id="s1", description="navigate", action=NavigateAction(url="https://app.test/search"))]
    artifact = _base_artifact(steps=steps)
    al = _allowlist_permitting("click")  # navigate not included
    result = replay(artifact, {"memberId": "12345"}, routed_page, allowlist=al)
    assert isinstance(result, Failure)
    assert result.kind == FailureKind.POLICY_BLOCKED
    assert result.step_id == "s1"


def test_replay_blocked_when_navigate_destination_not_allowlisted(routed_page: Page):
    steps = [Step(id="s1", description="navigate off-domain", action=NavigateAction(url="https://evil.example.com/x"))]
    artifact = _base_artifact(steps=steps)
    al = _allowlist_permitting("navigate", domain="app.test")
    result = replay(artifact, {"memberId": "12345"}, routed_page, allowlist=al)
    assert isinstance(result, Failure)
    assert result.kind == FailureKind.POLICY_BLOCKED


def test_replay_succeeds_when_full_flow_matches_allowlist(routed_page: Page):
    artifact = _base_artifact(steps=LOOKUP_STEPS)
    al = _allowlist_permitting(
        "navigate", "type", "click", "extract", domain="app.test", path_patterns=("/search", "/members/*")
    )
    result = replay(artifact, {"memberId": "12345"}, routed_page, allowlist=al)
    assert isinstance(result, Success)


def test_replay_with_no_allowlist_is_unrestricted(routed_page: Page):
    artifact = _base_artifact(steps=LOOKUP_STEPS)
    result = replay(artifact, {"memberId": "12345"}, routed_page, allowlist=None)
    assert isinstance(result, Success)


# ---------------------------------------------------------------------------
# risk / approval gate
# ---------------------------------------------------------------------------


def test_risky_unapproved_artifact_refused_before_any_step_runs(routed_page: Page):
    artifact = _base_artifact(steps=LOOKUP_STEPS, risk_level=RiskLevel.RISKY)
    assert artifact.approval_state == ApprovalState.DRAFT

    result = replay(artifact, {"memberId": "12345"}, routed_page)

    assert isinstance(result, Failure)
    assert result.kind == FailureKind.POLICY_BLOCKED
    assert result.step_id is None  # refused up front, no step even attempted
    # No navigation should have happened -- the page should still be blank.
    assert routed_page.url in ("about:blank", "")


def test_risky_approved_artifact_runs_normally(routed_page: Page):
    artifact = _base_artifact(steps=LOOKUP_STEPS, risk_level=RiskLevel.RISKY, approval_state=ApprovalState.APPROVED)
    result = replay(artifact, {"memberId": "12345"}, routed_page)
    assert isinstance(result, Success)


def test_risky_draft_artifact_runs_with_explicit_override(routed_page: Page):
    artifact = _base_artifact(steps=LOOKUP_STEPS, risk_level=RiskLevel.RISKY)
    result = replay(artifact, {"memberId": "12345"}, routed_page, allow_unapproved=True)
    assert isinstance(result, Success)


def test_safe_artifact_ignores_approval_state_entirely(routed_page: Page):
    artifact = _base_artifact(steps=LOOKUP_STEPS, risk_level=RiskLevel.SAFE, approval_state=ApprovalState.DRAFT)
    result = replay(artifact, {"memberId": "12345"}, routed_page)
    assert isinstance(result, Success)
