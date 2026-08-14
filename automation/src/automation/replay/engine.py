"""Deterministic replay engine: executes a saved Artifact against a live
page with no LLM in the decision loop.

This is the production execution path an AI agent triggers to invoke a
capability. Given an artifact and a set of input parameters, it:
  1. validates inputs against the artifact's declared InputParams,
  2. resolves each step's {{param}} templates,
  3. runs steps in order, retrying locator resolution (not the action
     itself) on transient not-found, detecting session-expiry after every
     step, checking each step's post_condition if declared, and checking
     each declared KnownOutcome after every step,
  4. after all steps, checks the artifact's success_condition and returns
     Success/BusinessOutcome/Failure.
"""

from __future__ import annotations

import re
import time
from typing import Any

from playwright.sync_api import Page

from automation.artifact.schema import (
    Artifact,
    ClickAction,
    ExtractAction,
    NavigateAction,
    SelectAction,
    Step,
    TypeAction,
    WaitForAction,
)
from automation.policy.allowlist import Allowlist
from automation.policy.risk import ApprovalRequiredError, check_replay_allowed
from automation.replay.checkpoint import evaluate as evaluate_checkpoint
from automation.replay.locator import resolve
from automation.replay.result import BusinessOutcome, Failure, FailureKind, ReplayResult, Success

DEFAULT_LOCATOR_RETRIES = 3
DEFAULT_LOCATOR_RETRY_DELAY_SECONDS = 0.5
DEFAULT_ACTION_TIMEOUT_MS = 5_000


def _looks_like_session_expired(url: str) -> bool:
    # The mock app redirects to /login?...&reason=timeout on session expiry.
    # A generic legacy app might instead just redirect to a bare /login with
    # no query marker, so this also treats an unadorned /login mid-flow as
    # suspicious.
    return "/login" in url


class _TemplateError(Exception):
    pass


class _StepFailed(Exception):
    def __init__(self, failure: Failure):
        self.failure = failure


def _validate_inputs(artifact: Artifact, inputs: dict[str, Any]) -> str | None:
    for param in artifact.inputs:
        if param.required and param.name not in inputs:
            return f'Missing required input "{param.name}".'
    unknown = set(inputs) - {p.name for p in artifact.inputs}
    if unknown:
        return f"Unknown input(s) not declared by this artifact: {sorted(unknown)}."
    return None


def _render_template(value: str, inputs: dict[str, Any]) -> str:
    def _sub(match: re.Match) -> str:
        name = match.group(1)
        if name not in inputs:
            raise _TemplateError(f'Step references undeclared input "{{{{{name}}}}}".')
        return str(inputs[name])

    return re.sub(r"\{\{(\w+)\}\}", _sub, value)


def replay(
    artifact: Artifact,
    inputs: dict[str, Any],
    page: Page,
    *,
    locator_retries: int = DEFAULT_LOCATOR_RETRIES,
    locator_retry_delay_seconds: float = DEFAULT_LOCATOR_RETRY_DELAY_SECONDS,
    allowlist: Allowlist | None = None,
    allow_unapproved: bool = False,
) -> ReplayResult:
    try:
        check_replay_allowed(artifact, allow_unapproved=allow_unapproved)
    except ApprovalRequiredError as e:
        return Failure(
            kind=FailureKind.POLICY_BLOCKED,
            step_id=None,
            step_index=None,
            expected="a safe/sensitive artifact, or a risky artifact that is approved (or explicitly overridden)",
            observed=str(e),
        )

    error = _validate_inputs(artifact, inputs)
    if error is not None:
        return Failure(
            kind=FailureKind.INVALID_INPUT,
            step_id=None,
            step_index=None,
            expected="inputs satisfying the artifact's declared InputParams",
            observed=error,
        )

    collected_outputs: dict[str, Any] = {}

    for index, step in enumerate(artifact.steps):
        try:
            _run_step(
                step,
                inputs,
                page,
                collected_outputs,
                locator_retries=locator_retries,
                locator_retry_delay_seconds=locator_retry_delay_seconds,
                allowlist=allowlist,
            )
        except _StepFailed as e:
            return e.failure
        except _TemplateError as e:
            return Failure(
                kind=FailureKind.INVALID_INPUT,
                step_id=step.id,
                step_index=index,
                expected="all {{param}} references in this step to be declared inputs",
                observed=str(e),
            )

        if _looks_like_session_expired(page.url):
            return Failure(
                kind=FailureKind.SESSION_EXPIRED,
                step_id=step.id,
                step_index=index,
                expected="an active session",
                observed=f'redirected to "{page.url}" after step {step.id}',
            )

        if step.post_condition is not None:
            holds, observed = evaluate_checkpoint(page, step.post_condition, inputs)
            if not holds:
                return Failure(
                    kind=FailureKind.CHECKPOINT_FAILED,
                    step_id=step.id,
                    step_index=index,
                    expected=f"post_condition for step {step.id} to hold",
                    observed=observed,
                )

        outcome = _check_known_outcomes(artifact, page, inputs, index, collected_outputs)
        if outcome is not None:
            return outcome

    holds, observed = evaluate_checkpoint(page, artifact.success_condition, inputs)
    if not holds:
        return Failure(
            kind=FailureKind.CHECKPOINT_FAILED,
            step_id=None,
            step_index=None,
            expected="artifact success_condition to hold after all steps",
            observed=observed,
        )

    return Success(outputs=collected_outputs, steps_completed=len(artifact.steps))


def _check_known_outcomes(
    artifact: Artifact, page: Page, inputs: dict[str, Any], steps_completed: int, collected_outputs: dict[str, Any]
) -> BusinessOutcome | None:
    for outcome in artifact.known_outcomes:
        holds, _observed = evaluate_checkpoint(page, outcome.detect, inputs)
        if holds:
            outputs = dict(collected_outputs)
            if outcome.outputs:
                outputs.update(outcome.outputs)
            return BusinessOutcome(
                name=outcome.name,
                description=outcome.description,
                outputs=outputs,
                steps_completed=steps_completed,
            )
    return None


def _resolve_with_retry(
    page: Page,
    step: Step,
    target,
    *,
    retries: int,
    retry_delay_seconds: float,
):
    attempts_log: list[str] = []
    for attempt in range(retries + 1):
        resolution = resolve(page, target)
        if resolution.locator is not None:
            return resolution.locator
        attempts_log.append(f"attempt {attempt + 1}: {'; '.join(resolution.attempts)}")
        if attempt < retries:
            time.sleep(retry_delay_seconds)

    raise _StepFailed(
        Failure(
            kind=FailureKind.LOCATOR_NOT_FOUND,
            step_id=step.id,
            step_index=None,
            expected=f"exactly one element to resolve for step {step.id}",
            observed=" | ".join(attempts_log),
        )
    )


def _check_allowlist_action_type(allowlist: Allowlist | None, step: Step, action_type: str) -> None:
    if allowlist is None:
        return
    allowed, reason = allowlist.check_action_type(action_type)
    if not allowed:
        raise _StepFailed(
            Failure(
                kind=FailureKind.POLICY_BLOCKED,
                step_id=step.id,
                step_index=None,
                expected="action type permitted by the allowlist",
                observed=f"Blocked by allowlist: {reason}",
            )
        )


def _check_allowlist_url(allowlist: Allowlist | None, step: Step, url: str, *, after_action: bool) -> None:
    if allowlist is None:
        return
    allowed, reason = allowlist.check_url(url)
    if not allowed:
        context = "after action landed outside permitted scope" if after_action else "before navigating"
        raise _StepFailed(
            Failure(
                kind=FailureKind.POLICY_BLOCKED,
                step_id=step.id,
                step_index=None,
                expected="destination URL permitted by the allowlist",
                observed=f"Blocked by allowlist {context}: {reason}",
            )
        )


def _run_step(
    step: Step,
    inputs: dict[str, Any],
    page: Page,
    collected_outputs: dict[str, Any],
    *,
    locator_retries: int,
    locator_retry_delay_seconds: float,
    allowlist: Allowlist | None = None,
) -> None:
    action = step.action
    _check_allowlist_action_type(allowlist, step, action.type)

    if isinstance(action, NavigateAction):
        url = _render_template(action.url, inputs)
        _check_allowlist_url(allowlist, step, url, after_action=False)
        try:
            page.goto(url, timeout=DEFAULT_ACTION_TIMEOUT_MS * 2)
        except Exception as e:
            raise _StepFailed(
                Failure(
                    kind=FailureKind.ACTION_ERROR,
                    step_id=step.id,
                    step_index=None,
                    expected=f'navigation to "{url}" to succeed',
                    observed=str(e),
                )
            )
        return

    if isinstance(action, ClickAction):
        locator = _resolve_with_retry(
            page, step, action.target, retries=locator_retries, retry_delay_seconds=locator_retry_delay_seconds
        )
        try:
            locator.click(timeout=DEFAULT_ACTION_TIMEOUT_MS)
        except Exception as e:
            raise _StepFailed(
                Failure(
                    kind=FailureKind.ACTION_ERROR,
                    step_id=step.id,
                    step_index=None,
                    expected="click to succeed",
                    observed=str(e),
                )
            )
        # A click can trigger navigation (a link, a form submit) just like
        # an explicit navigate step -- re-check wherever we actually ended
        # up, not just the action type, so an allowlisted page can't be
        # used as a stepping stone off the allowlist via a click.
        _check_allowlist_url(allowlist, step, page.url, after_action=True)
        return

    if isinstance(action, TypeAction):
        locator = _resolve_with_retry(
            page, step, action.target, retries=locator_retries, retry_delay_seconds=locator_retry_delay_seconds
        )
        value = _render_template(action.value, inputs)
        try:
            locator.fill(value, timeout=DEFAULT_ACTION_TIMEOUT_MS)
        except Exception as e:
            raise _StepFailed(
                Failure(
                    kind=FailureKind.ACTION_ERROR,
                    step_id=step.id,
                    step_index=None,
                    expected="type/fill to succeed",
                    observed=str(e),
                )
            )
        return

    if isinstance(action, SelectAction):
        locator = _resolve_with_retry(
            page, step, action.target, retries=locator_retries, retry_delay_seconds=locator_retry_delay_seconds
        )
        value = _render_template(action.value, inputs)
        try:
            try:
                locator.select_option(value=value, timeout=DEFAULT_ACTION_TIMEOUT_MS)
            except Exception:
                locator.select_option(label=value, timeout=DEFAULT_ACTION_TIMEOUT_MS)
        except Exception as e:
            raise _StepFailed(
                Failure(
                    kind=FailureKind.ACTION_ERROR,
                    step_id=step.id,
                    step_index=None,
                    expected="select to succeed",
                    observed=str(e),
                )
            )
        return

    if isinstance(action, WaitForAction):
        holds, observed = evaluate_checkpoint(page, action.condition, inputs)
        deadline = time.monotonic() + action.timeout_ms / 1000
        while not holds and time.monotonic() < deadline:
            time.sleep(0.1)
            holds, observed = evaluate_checkpoint(page, action.condition, inputs)
        if not holds:
            raise _StepFailed(
                Failure(
                    kind=FailureKind.TIMEOUT,
                    step_id=step.id,
                    step_index=None,
                    expected="waitFor condition to hold before timeout",
                    observed=observed,
                )
            )
        return

    if isinstance(action, ExtractAction):
        locator = _resolve_with_retry(
            page, step, action.target, retries=locator_retries, retry_delay_seconds=locator_retry_delay_seconds
        )
        try:
            text = (locator.text_content(timeout=DEFAULT_ACTION_TIMEOUT_MS) or "").strip()
        except Exception as e:
            raise _StepFailed(
                Failure(
                    kind=FailureKind.ACTION_ERROR,
                    step_id=step.id,
                    step_index=None,
                    expected="extract to succeed",
                    observed=str(e),
                )
            )
        collected_outputs[action.output_key] = text
        return

    raise AssertionError(f"unreachable: unhandled action {action!r}")
