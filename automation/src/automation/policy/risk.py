"""Risk classification for actions and artifacts.

Two distinct things get classified:
  - an individual ACTION TYPE (navigate/click/type/select/wait_for/extract)
    -- a static, structural judgment about whether that kind of action can
    mutate backend state, independent of which artifact it's in.
  - an ARTIFACT as a whole (RiskLevel: safe/sensitive/risky), recorded at
    discovery time (see automation.artifact.recorder) -- a judgment about
    what the *flow* does overall (e.g. "opens a new account" is risky even
    though most of its individual steps are just navigation and reads).

Replay uses both: it refuses to run an artifact whose overall risk_level is
"risky" unless it has been explicitly approved (or the caller opts in with
an explicit override), and it treats a "click" action as the one action
type worth flagging specially, since in a typical CRUD back-office flow the
click on a submit/confirm button is what actually commits the mutation --
typing into a field or navigating to a page does not, by itself, change any
backend state.
"""

from __future__ import annotations

from automation.artifact.schema import Artifact, ApprovalState, RiskLevel

# Read-only action types: they observe or navigate, never mutate backend
# state by themselves.
SAFE_ACTION_TYPES = frozenset({"navigate", "wait_for", "extract"})

# Mutates local form state in the browser, but nothing is committed to the
# backend until a submit/confirm click happens.
SENSITIVE_ACTION_TYPES = frozenset({"type", "select"})

# A click is the one action type that, in a typical server-rendered CRUD
# flow, actually triggers a backend mutation (submit a form, confirm an
# action, follow a link that performs a side effect). Not every click does
# this (e.g. "Look Up" is a read), but there's no static way to tell a
# read-triggering click from a mutation-triggering one from the action type
# alone -- that judgment lives at the artifact level (risk_level), not here.
RISKY_ACTION_TYPES = frozenset({"click"})


class ApprovalRequiredError(Exception):
    """Raised when replay is refused because a risky artifact has not been
    approved and the caller did not explicitly override that check.
    """

    def __init__(self, artifact_name: str, version: int):
        super().__init__(
            f'Artifact "{artifact_name}" v{version} is classified risky and not yet approved -- '
            "unattended replay is refused. Pass allow_unapproved=True to override, or set the "
            'artifact\'s approval_state to "approved" after human review.'
        )
        self.artifact_name = artifact_name
        self.version = version


def classify_action_type(action_type: str) -> str:
    """Returns "safe", "sensitive", or "risky" for a single action type."""
    if action_type in RISKY_ACTION_TYPES:
        return "risky"
    if action_type in SENSITIVE_ACTION_TYPES:
        return "sensitive"
    return "safe"


def check_replay_allowed(artifact: Artifact, *, allow_unapproved: bool = False) -> None:
    """Raises ApprovalRequiredError if this artifact should not be replayed
    unattended given its declared risk_level and approval_state.

    safe/sensitive artifacts always run. risky artifacts require
    approval_state == APPROVED, unless the caller explicitly passes
    allow_unapproved=True (e.g. a human operator running it deliberately
    from the CLI with full awareness) -- the default is the conservative
    path, matching the brief's "handle the risky class conservatively"
    guidance.
    """
    if artifact.risk_level != RiskLevel.RISKY:
        return
    if artifact.approval_state == ApprovalState.APPROVED:
        return
    if allow_unapproved:
        return
    raise ApprovalRequiredError(artifact.name, artifact.version)
