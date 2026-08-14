"""The replay result contract.

Mirrors the three-way split the assignment brief calls out explicitly:
  - expected business outcomes the caller needs to know about (e.g. "no such
    member" is a legitimate result, not a crash) -> BusinessOutcome
  - hard failures that should stop and surface a clear, debuggable error
    -> Failure

"Recoverable conditions" (dismiss a known interstitial, wait/retry a
transient load) are handled *within* the engine as retries around a single
step -- by the time replay returns, a condition has either been recovered
from (and replay continues normally) or it has exhausted its retries and
becomes a Failure(kind=RECOVERABLE_EXHAUSTED). There is deliberately no
separate "Recoverable" result variant: a caller either gets a clean outcome
or a clear failure, never something in between to interpret.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Union


class FailureKind(str, Enum):
    LOCATOR_NOT_FOUND = "locator_not_found"  # every strategy in the fallback chain failed to resolve
    ACTION_ERROR = "action_error"  # Playwright raised while performing the action itself
    CHECKPOINT_FAILED = "checkpoint_failed"  # a post_condition or the final success_condition did not hold
    TIMEOUT = "timeout"  # a waitFor step (or retry budget) timed out
    RECOVERABLE_EXHAUSTED = "recoverable_exhausted"  # a transient condition never cleared within its retry budget
    INVALID_INPUT = "invalid_input"  # caller-supplied inputs don't satisfy the artifact's declared InputParams
    SESSION_EXPIRED = "session_expired"  # replay landed back on a login/timeout page mid-flow
    POLICY_BLOCKED = "policy_blocked"  # allowlist or risk/approval guardrail refused the action or the whole run


@dataclass(frozen=True)
class Success:
    outputs: dict[str, Any]
    steps_completed: int


@dataclass(frozen=True)
class BusinessOutcome:
    """A named, artifact-declared non-success result the flow can legitimately
    reach (see Artifact.known_outcomes) -- e.g. "member_not_found". This is
    data the caller needs, not an error.
    """

    name: str
    description: str
    outputs: dict[str, Any] = field(default_factory=dict)
    steps_completed: int = 0


@dataclass(frozen=True)
class Failure:
    kind: FailureKind
    step_id: str | None  # None for failures before/after all steps (e.g. invalid_input, final checkpoint)
    step_index: int | None
    expected: str
    observed: str
    detail: str = ""


ReplayResult = Union[Success, BusinessOutcome, Failure]
