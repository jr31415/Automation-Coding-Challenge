"""The pause / expose / resume mechanism: how control of the live session
actually transfers from automation to a human operator and back.

The seam this implies (per the assignment brief): automation must be able
to pause, cede control, and resume on the *same* session, and there must be
a way to know who is (or should be) in control.

Concretely, this only works because the discovery browser is always
launched headed with Chromium's remote-debugging port open (see
automation.cli.discover) -- the same browser process, with the same
cookies/session/DOM, that the agent was just driving. When `stuck` fires:

  1. PAUSE -- the agent loop has already stopped calling tools; nothing else
     to do here except stop touching the page.
  2. EXPOSE -- the operator brings up their own window pointed at the CDP
     endpoint (chrome://inspect, or `playwright.chromium.connect_over_cdp`
     from a separate process/script) and drives the *actual* window that's
     already on screen. Ownership is tracked explicitly (see
     ControlState below) so the automation knows not to touch the page
     while a human has it, and the human's actions are recorded via a
     one-line-per-action operator log the human is asked to fill in --
     this is the part that is deliberately mocked: a full co-browsing
     console with automatic action capture is out of scope (see brief
     Section 3.6 scope note), but the pause/expose/resume/record mechanism
     itself is real.
  3. RESUME -- the operator signals done via the CLI prompt; control passes
     back and the caller decides whether to retry the failed step, treat
     the goal as satisfied, or abort.

This module owns steps 1-3's bookkeeping; automation.cli.discover owns the
actual terminal I/O (what gets printed, the input() prompt), since that's
presentation, not the handoff mechanism itself.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum

from automation.agent.loop import RunResult
from automation.escalation.models import HandoffResult, InterventionRequest


class ControlState(str, Enum):
    """Who currently owns the live session -- the explicit answer to the
    brief's "there must be a way to know who is (or should be) in control"
    requirement. Not just documentation: automation.cli.discover checks
    this before allowing the run to resume, so a human forgetting to
    signal handback can't result in the automation and a human both
    believing they're in control at once.
    """

    AUTOMATION = "automation"
    AWAITING_HUMAN = "awaiting_human"
    HUMAN = "human"
    RETURNED_TO_AUTOMATION = "returned_to_automation"


def build_intervention_request(
    *,
    goal: str,
    run_result: RunResult,
    current_url: str,
    screenshot_path: str | None,
    cdp_endpoint: str,
) -> InterventionRequest:
    """Translate a `stuck` RunResult into the context payload a human
    operator (or an operator console) needs to act on it.
    """
    return InterventionRequest(
        goal=goal,
        reason=run_result.stuck_reason or "unknown",
        details=run_result.stuck_details or "",
        current_url=current_url,
        steps_completed=len(run_result.steps),
        screenshot_path=screenshot_path,
        requested_at=datetime.now(timezone.utc),
        session_endpoint=cdp_endpoint,
    )


def format_intervention_summary(request: InterventionRequest) -> str:
    """Human-readable summary of an intervention request, for terminal
    output or an operator console. Kept separate from the dataclass itself
    so evidence logs can serialize the structured data while the terminal
    gets prose.
    """
    lines = [
        "=" * 70,
        "HUMAN INTERVENTION REQUESTED",
        "=" * 70,
        f"Goal:            {request.goal}",
        f"Reason:          {request.reason}",
        f"Details:         {request.details}",
        f"Current URL:     {request.current_url}",
        f"Steps completed: {request.steps_completed}",
        f"Requested at:    {request.requested_at.isoformat()}",
    ]
    if request.screenshot_path:
        lines.append(f"Screenshot:      {request.screenshot_path}")
    lines.extend(
        [
            "",
            "The live browser session is already open on this machine and paused.",
            f"Attach to it directly at: {request.session_endpoint}",
            "  - Chrome/Chromium: open chrome://inspect, add the target, click 'inspect'",
            "  - Or connect programmatically: playwright.chromium.connect_over_cdp(...)",
            "",
            "This is the SAME session the automation was driving -- same cookies,",
            "same page, same in-progress state. Take whatever action is needed,",
            "then return here.",
            "=" * 70,
        ]
    )
    return "\n".join(lines)


def build_handoff_result(*, resumed: bool, operator_notes: str, actions_taken: list[str]) -> HandoffResult:
    return HandoffResult(
        resumed=resumed,
        operator_notes=operator_notes,
        actions_taken=actions_taken,
        handled_at=datetime.now(timezone.utc),
    )
