"""Data carried across the human-escalation seam.

InterventionRequest is what the automation hands to a human operator when
it can't safely proceed -- enough context to act on without having watched
the run happen. HandoffResult is what the operator hands back: what they
actually did, and whether the run should resume or abort.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class InterventionRequest:
    """Raised when the automation cannot safely continue and a human needs
    to take over. Carries everything a human operator (or an operator
    console, real or mocked) needs to understand the situation without
    having watched the run: what capability/goal was being attempted, where
    it stopped, what state the page was in, and why.
    """

    goal: str
    reason: str  # one of automation.agent.tools.StuckReason
    details: str  # the model's own explanation, in its words
    current_url: str
    steps_completed: int
    screenshot_path: str | None
    requested_at: datetime
    # How a human actually reaches the live session -- see
    # automation.escalation.handoff for what this looks like concretely
    # (a CDP endpoint the operator can attach a browser/devtools to).
    session_endpoint: str


@dataclass(frozen=True)
class HandoffResult:
    """What came back from the human operator's turn with the session."""

    resumed: bool  # True = continue the automation; False = abort the run
    operator_notes: str = ""
    actions_taken: list[str] = field(default_factory=list)
    handled_at: datetime | None = None
