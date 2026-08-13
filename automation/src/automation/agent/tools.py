"""Tool schemas for the discovery agent loop.

Each turn, the harness auto-injects a fresh accessibility-tree snapshot into
context (elements tagged with short-lived `ref` ids, e.g. "e14"). The model
must respond with exactly one of these tool calls. `ref` values are only
valid for the snapshot they came from -- they are never persisted; at
artifact-recording time each ref is translated into a durable
role+accessible_name locator (see automation.artifact.schema.TargetLocator).

`wait_for`'s condition is expressed inline here (ref/url based, against the
live snapshot) rather than reusing artifact.CheckpointCondition, which is
expressed in terms of durable locators -- the two are related but not the
same type, since one operates on live refs and the other on recorded,
replayable locators.
"""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, Field

Ref = Annotated[
    str,
    Field(
        min_length=1,
        description=(
            'The element ref id from the most recent accessibility snapshot '
            '(e.g. "e14"). Must come from the current snapshot -- refs from '
            "earlier turns are stale."
        ),
    ),
]


class UrlMatchesWait(BaseModel):
    kind: Literal["urlMatches"] = "urlMatches"
    pattern: str = Field(description="Regex source to match against the current page URL.")


class ElementVisibleWait(BaseModel):
    kind: Literal["elementVisible"] = "elementVisible"
    ref: Ref


WaitCondition = Annotated[
    Union[UrlMatchesWait, ElementVisibleWait],
    Field(discriminator="kind"),
]


class NavigateInput(BaseModel):
    url: str = Field(description="Absolute or relative URL to navigate to.")


class ClickInput(BaseModel):
    ref: Ref


class TypeInput(BaseModel):
    ref: Ref
    value: str = Field(description="Text to type into the field.")
    sensitive: bool = Field(
        default=False,
        description="Set true for credentials/PII so the value is redacted in logs and the recorded artifact.",
    )


class SelectInput(BaseModel):
    ref: Ref
    value: str = Field(description="The option value or visible label to select.")


class WaitForInput(BaseModel):
    condition: WaitCondition
    timeout_ms: int = Field(default=10_000, gt=0)


class ExtractInput(BaseModel):
    ref: Ref
    output_key: str = Field(description='Name for this extracted value in the goal\'s result, e.g. "savingsBalance".')


class DoneInput(BaseModel):
    summary: str = Field(description="Brief summary of how the goal was accomplished.")
    outputs: dict[str, str | float | bool] = Field(
        description="Final key/value outputs for the goal, merging any values collected via extract."
    )


class StuckReason:
    AMBIGUOUS_STATE = "ambiguous_state"
    UNEXPECTED_DIALOG = "unexpected_dialog"
    PERMISSION_DENIED = "permission_denied"
    REPEATED_ACTION_FAILURE = "repeated_action_failure"
    RISKY_ACTION_BLOCKED = "risky_action_blocked"
    GOAL_UNREACHABLE = "goal_unreachable"
    OTHER = "other"

    ALL = (
        AMBIGUOUS_STATE,
        UNEXPECTED_DIALOG,
        PERMISSION_DENIED,
        REPEATED_ACTION_FAILURE,
        RISKY_ACTION_BLOCKED,
        GOAL_UNREACHABLE,
        OTHER,
    )


class StuckInput(BaseModel):
    reason: Literal[
        "ambiguous_state",
        "unexpected_dialog",
        "permission_denied",
        "repeated_action_failure",
        "risky_action_blocked",
        "goal_unreachable",
        "other",
    ] = Field(description="Category of why the agent cannot safely proceed.")
    details: str = Field(description="Specific explanation, enough for a human operator to act on.")


# ---------------------------------------------------------------------------
# Tool registry: name -> (description, input schema). Used both to build the
# Anthropic tool-use request and to validate/dispatch the model's tool_use
# blocks.
# ---------------------------------------------------------------------------


class ToolSpec(BaseModel):
    description: str
    schema_: type[BaseModel] = Field(alias="schema")

    model_config = {"arbitrary_types_allowed": True}


AGENT_TOOLS: dict[str, ToolSpec] = {
    "navigate": ToolSpec(
        description="Navigate the browser to a URL.",
        schema=NavigateInput,
    ),
    "click": ToolSpec(
        description="Click an element identified by its ref from the current snapshot.",
        schema=ClickInput,
    ),
    "type": ToolSpec(
        description="Type text into an input/textarea identified by its ref.",
        schema=TypeInput,
    ),
    "select": ToolSpec(
        description="Choose an option in a <select> element identified by its ref.",
        schema=SelectInput,
    ),
    "wait_for": ToolSpec(
        description="Wait until a condition holds (URL match or element becomes visible), or time out.",
        schema=WaitForInput,
    ),
    "extract": ToolSpec(
        description="Read the text/value of an element identified by its ref and record it under output_key.",
        schema=ExtractInput,
    ),
    "done": ToolSpec(
        description="Declare the goal accomplished and return the final outputs.",
        schema=DoneInput,
    ),
    "stuck": ToolSpec(
        description="Declare that the agent cannot safely proceed and escalation to a human is needed.",
        schema=StuckInput,
    ),
}


def anthropic_tool_definitions() -> list[dict]:
    """Build the Anthropic Messages API `tools` payload from AGENT_TOOLS."""
    tools = []
    for name, spec in AGENT_TOOLS.items():
        input_schema = spec.schema_.model_json_schema()
        # Anthropic tool schemas don't use pydantic's "$defs"/"title" noise well;
        # keep it as-is since Claude tolerates standard JSON Schema fine.
        tools.append(
            {
                "name": name,
                "description": spec.description,
                "input_schema": input_schema,
            }
        )
    return tools
