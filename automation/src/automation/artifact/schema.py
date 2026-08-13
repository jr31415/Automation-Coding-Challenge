"""Artifact schema: the typed, versioned, agent-invocable capability record.

An artifact is what a successful discovery run produces, and what the replay
engine consumes. It is decoupled from the raw LLM transcript -- discovery
tool calls (see automation.agent.tools) are translated into these durable,
locator-based steps at recording time.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

# ---------------------------------------------------------------------------
# Locators: how a step finds its target control on replay.
# Ordered fallback chain -- replay tries strategies in order until one
# resolves to exactly one element. Every strategy is legacy-DOM-safe: none
# depend on CSS classes/ids/test-ids, which legacy bank UIs never have.
# ---------------------------------------------------------------------------


class RoleLocator(BaseModel):
    kind: Literal["role"] = "role"
    role: str  # ARIA role, e.g. "textbox", "button", "combobox"
    accessible_name: str  # visible label / accessible name


class TextLocator(BaseModel):
    kind: Literal["text"] = "text"
    text: str  # visible text content
    exact: bool = True


class LabelForLocator(BaseModel):
    kind: Literal["labelFor"] = "labelFor"
    label: str  # e.g. "<td>Label:</td>" style adjacency association


class StructuralLocator(BaseModel):
    """Last-resort fallback: tag + nth occurrence within a named container.

    Most drift-prone strategy, so it is only ever used after role/text/label
    fallbacks fail, and it is always recorded with explicit reasoning.
    """

    kind: Literal["structural"] = "structural"
    tag: str
    nth: int = Field(ge=0)
    within_container_text: str | None = None


LocatorStrategy = Annotated[
    Union[RoleLocator, TextLocator, LabelForLocator, StructuralLocator],
    Field(discriminator="kind"),
]


class TargetLocator(BaseModel):
    # Ordered fallback chain: try [0], then [1], ... until one resolves uniquely.
    strategies: list[LocatorStrategy] = Field(min_length=1)
    # Why this chain was chosen -- captured at discovery time for human review.
    reasoning: str


# ---------------------------------------------------------------------------
# Checkpoints: conditions asserted to confirm state, not assumed from an
# action "succeeding". Used both mid-flow (post_condition) and as the final
# success condition / known-outcome detectors.
# ---------------------------------------------------------------------------


class UrlMatchesCondition(BaseModel):
    kind: Literal["urlMatches"] = "urlMatches"
    pattern: str  # regex source


class ElementVisibleCondition(BaseModel):
    kind: Literal["elementVisible"] = "elementVisible"
    target: TargetLocator


class ElementTextEqualsCondition(BaseModel):
    kind: Literal["elementText"] = "elementText"
    target: TargetLocator
    equals: str


class ElementTextContainsCondition(BaseModel):
    kind: Literal["elementTextContains"] = "elementTextContains"
    target: TargetLocator
    substring: str


CheckpointCondition = Annotated[
    Union[
        UrlMatchesCondition,
        ElementVisibleCondition,
        ElementTextEqualsCondition,
        ElementTextContainsCondition,
    ],
    Field(discriminator="kind"),
]


# ---------------------------------------------------------------------------
# Steps: the ordered actions that make up the flow.
# ---------------------------------------------------------------------------


class NavigateAction(BaseModel):
    type: Literal["navigate"] = "navigate"
    url: str  # may contain {{paramName}} template refs, resolved against inputs


class ClickAction(BaseModel):
    type: Literal["click"] = "click"
    target: TargetLocator


class TypeAction(BaseModel):
    type: Literal["type"] = "type"
    target: TargetLocator
    value: str  # literal or "{{paramName}}" template ref
    sensitive: bool = False  # if true, value is never logged verbatim


class SelectAction(BaseModel):
    type: Literal["select"] = "select"
    target: TargetLocator
    value: str


class WaitForAction(BaseModel):
    type: Literal["waitFor"] = "waitFor"
    condition: CheckpointCondition
    timeout_ms: int = Field(default=10_000, gt=0)


class ExtractAction(BaseModel):
    type: Literal["extract"] = "extract"
    target: TargetLocator
    output_key: str  # maps to a key in the artifact's outputs shape


Action = Annotated[
    Union[NavigateAction, ClickAction, TypeAction, SelectAction, WaitForAction, ExtractAction],
    Field(discriminator="type"),
]


class Step(BaseModel):
    id: str
    description: str  # human-readable summary, shown to reviewers
    action: Action
    # Optional per-step checkpoint: assert this before considering the step done.
    post_condition: CheckpointCondition | None = None


# ---------------------------------------------------------------------------
# Known outcomes: business-legitimate non-success results the flow can reach
# on purpose (e.g. "member not found"). Distinguished from hard failures.
# ---------------------------------------------------------------------------


class KnownOutcome(BaseModel):
    name: str  # e.g. "member_not_found", "permission_denied"
    description: str
    detect: CheckpointCondition
    # Static outputs to return to the caller when this outcome fires.
    outputs: dict[str, str] | None = None


# ---------------------------------------------------------------------------
# Typed I/O contract.
# ---------------------------------------------------------------------------


class ParamType(str, Enum):
    STRING = "string"
    NUMBER = "number"
    BOOLEAN = "boolean"


class InputParam(BaseModel):
    name: str
    type: ParamType
    required: bool = True
    description: str
    sensitive: bool = False  # e.g. credentials -- never persisted into logs/evidence


class OutputField(BaseModel):
    name: str
    type: ParamType
    description: str


# ---------------------------------------------------------------------------
# App target metadata -- supports the multi-tenant reuse story (see
# REPORT.md): an artifact is recorded against a vendor product + version,
# not a tenant.
# ---------------------------------------------------------------------------


class AppTarget(BaseModel):
    vendor_product: str  # e.g. "riverbend-core-admin"
    base_url: str
    min_version: str | None = None
    max_version: str | None = None


# ---------------------------------------------------------------------------
# The artifact itself.
# ---------------------------------------------------------------------------


class RiskLevel(str, Enum):
    SAFE = "safe"
    SENSITIVE = "sensitive"
    RISKY = "risky"


class ApprovalState(str, Enum):
    DRAFT = "draft"
    APPROVED = "approved"


class Artifact(BaseModel):
    schema_version: Literal["1.0"] = "1.0"
    id: str
    name: str  # capability name, e.g. "open_sub_account"
    version: int = Field(ge=1)
    description: str
    app_target: AppTarget

    inputs: list[InputParam]
    outputs: list[OutputField]

    steps: list[Step] = Field(min_length=1)
    success_condition: CheckpointCondition
    known_outcomes: list[KnownOutcome] = Field(default_factory=list)

    # Risk classification drives guardrail handling on replay (see automation.policy).
    risk_level: RiskLevel

    # Provenance: how this artifact came to exist, for human review.
    discovered_at: datetime
    discovered_from_goal: str
    approval_state: ApprovalState = ApprovalState.DRAFT
