"""Tests for automation.agent.tools: the discovery agent's tool-call schemas.

Covers per-tool validation (required fields, defaults, rejection of bad
input), the discriminated WaitCondition union, the AGENT_TOOLS registry
itself, and Anthropic tool-use JSON schema generation.
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, ValidationError

from automation.agent.tools import (
    AGENT_TOOLS,
    ClickInput,
    DoneInput,
    ElementVisibleWait,
    ExtractInput,
    NavigateInput,
    SelectInput,
    StuckInput,
    StuckReason,
    TypeInput,
    UrlMatchesWait,
    WaitForInput,
    anthropic_tool_definitions,
)


# ---------------------------------------------------------------------------
# navigate
# ---------------------------------------------------------------------------


def test_navigate_accepts_url():
    inp = NavigateInput(url="/members/search")
    assert inp.url == "/members/search"


def test_navigate_requires_url():
    with pytest.raises(ValidationError):
        NavigateInput()


# ---------------------------------------------------------------------------
# click
# ---------------------------------------------------------------------------


def test_click_accepts_ref():
    inp = ClickInput(ref="e14")
    assert inp.ref == "e14"


def test_click_rejects_empty_ref():
    with pytest.raises(ValidationError):
        ClickInput(ref="")


def test_click_requires_ref():
    with pytest.raises(ValidationError):
        ClickInput()


# ---------------------------------------------------------------------------
# type
# ---------------------------------------------------------------------------


def test_type_defaults_sensitive_false():
    inp = TypeInput(ref="e3", value="12345")
    assert inp.sensitive is False


def test_type_sensitive_can_be_set_true():
    inp = TypeInput(ref="e3", value="hunter2", sensitive=True)
    assert inp.sensitive is True


def test_type_requires_ref_and_value():
    with pytest.raises(ValidationError):
        TypeInput(ref="e3")
    with pytest.raises(ValidationError):
        TypeInput(value="12345")


# ---------------------------------------------------------------------------
# select
# ---------------------------------------------------------------------------


def test_select_accepts_ref_and_value():
    inp = SelectInput(ref="e9", value="Holiday Club")
    assert inp.ref == "e9"
    assert inp.value == "Holiday Club"


def test_select_requires_value():
    with pytest.raises(ValidationError):
        SelectInput(ref="e9")


# ---------------------------------------------------------------------------
# wait_for / WaitCondition discriminated union
# ---------------------------------------------------------------------------


def test_wait_for_url_matches_condition():
    inp = WaitForInput(condition={"kind": "urlMatches", "pattern": r"/members/\d+$"})
    assert isinstance(inp.condition, UrlMatchesWait)
    assert inp.condition.pattern == r"/members/\d+$"


def test_wait_for_element_visible_condition():
    inp = WaitForInput(condition={"kind": "elementVisible", "ref": "e5"})
    assert isinstance(inp.condition, ElementVisibleWait)
    assert inp.condition.ref == "e5"


def test_wait_for_default_timeout():
    inp = WaitForInput(condition={"kind": "urlMatches", "pattern": ".*"})
    assert inp.timeout_ms == 10_000


def test_wait_for_rejects_non_positive_timeout():
    with pytest.raises(ValidationError):
        WaitForInput(condition={"kind": "urlMatches", "pattern": ".*"}, timeout_ms=0)
    with pytest.raises(ValidationError):
        WaitForInput(condition={"kind": "urlMatches", "pattern": ".*"}, timeout_ms=-100)


def test_wait_for_rejects_unknown_condition_kind():
    with pytest.raises(ValidationError):
        WaitForInput(condition={"kind": "somethingElse", "foo": "bar"})


def test_wait_for_rejects_missing_discriminator():
    with pytest.raises(ValidationError):
        WaitForInput(condition={"pattern": ".*"})


# ---------------------------------------------------------------------------
# extract
# ---------------------------------------------------------------------------


def test_extract_accepts_ref_and_output_key():
    inp = ExtractInput(ref="e7", output_key="savingsBalance")
    assert inp.output_key == "savingsBalance"


def test_extract_requires_output_key():
    with pytest.raises(ValidationError):
        ExtractInput(ref="e7")


# ---------------------------------------------------------------------------
# done
# ---------------------------------------------------------------------------


def test_done_accepts_mixed_output_types():
    inp = DoneInput(
        summary="Looked up member 12345 and read savings balance.",
        outputs={"memberId": "12345", "savingsBalanceCents": 482311, "found": True},
    )
    assert inp.outputs["memberId"] == "12345"
    assert inp.outputs["savingsBalanceCents"] == 482311
    assert inp.outputs["found"] is True


def test_done_requires_summary_and_outputs():
    with pytest.raises(ValidationError):
        DoneInput(outputs={})
    with pytest.raises(ValidationError):
        DoneInput(summary="done")


def test_done_allows_empty_outputs_dict():
    inp = DoneInput(summary="Nothing to report.", outputs={})
    assert inp.outputs == {}


# ---------------------------------------------------------------------------
# stuck
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("reason", StuckReason.ALL)
def test_stuck_accepts_all_declared_reasons(reason):
    inp = StuckInput(reason=reason, details="explanation")
    assert inp.reason == reason


def test_stuck_rejects_unknown_reason():
    with pytest.raises(ValidationError):
        StuckInput(reason="not_a_real_reason", details="explanation")


def test_stuck_requires_details():
    with pytest.raises(ValidationError):
        StuckInput(reason=StuckReason.OTHER)


# ---------------------------------------------------------------------------
# AGENT_TOOLS registry
# ---------------------------------------------------------------------------

EXPECTED_TOOL_NAMES = {
    "navigate",
    "click",
    "type",
    "select",
    "wait_for",
    "extract",
    "done",
    "stuck",
}


def test_registry_has_exactly_the_eight_planned_tools():
    assert set(AGENT_TOOLS.keys()) == EXPECTED_TOOL_NAMES


def test_registry_entries_have_nonempty_description_and_schema():
    for name, spec in AGENT_TOOLS.items():
        assert spec.description, f"{name} missing description"
        assert issubclass(spec.schema_, BaseModel), f"{name} schema is not a pydantic model"


# ---------------------------------------------------------------------------
# Anthropic tool-use schema generation
# ---------------------------------------------------------------------------


def test_anthropic_tool_definitions_count_and_names():
    defs = anthropic_tool_definitions()
    assert len(defs) == len(EXPECTED_TOOL_NAMES)
    assert {d["name"] for d in defs} == EXPECTED_TOOL_NAMES


def test_anthropic_tool_definitions_shape():
    defs = anthropic_tool_definitions()
    for d in defs:
        assert set(d.keys()) == {"name", "description", "input_schema"}
        assert isinstance(d["description"], str) and d["description"]
        schema = d["input_schema"]
        assert schema["type"] == "object"
        assert "properties" in schema


def test_anthropic_click_schema_requires_ref():
    defs = {d["name"]: d for d in anthropic_tool_definitions()}
    click_schema = defs["click"]["input_schema"]
    assert "ref" in click_schema["properties"]
    assert click_schema["required"] == ["ref"]


def test_anthropic_type_schema_marks_only_ref_and_value_required():
    defs = {d["name"]: d for d in anthropic_tool_definitions()}
    type_schema = defs["type"]["input_schema"]
    assert set(type_schema["required"]) == {"ref", "value"}
    assert "sensitive" in type_schema["properties"]


def test_anthropic_stuck_schema_has_reason_enum():
    defs = {d["name"]: d for d in anthropic_tool_definitions()}
    stuck_schema = defs["stuck"]["input_schema"]
    reason_prop = stuck_schema["properties"]["reason"]
    reason_enum = set(reason_prop.get("enum", []))
    assert reason_enum == set(StuckReason.ALL)
