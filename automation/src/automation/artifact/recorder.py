"""Turns a completed discovery RunResult into a saved, versioned Artifact.

This is the "record" step in the record-once/replay-many model: it reads
the raw StepRecord transcript (refs, tool calls, live snapshots -- all only
meaningful within the run that produced them) and translates it into
durable Steps addressed by role/accessible-name locators, which is what the
replay engine (a separate module) can execute without an LLM or a live
browser session.

Only `done` runs are recordable -- a `stuck`/`max_steps`/`timeout` run has
no successful flow to capture.
"""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

from automation.agent.loop import RunResult, StepRecord, StopReason
from automation.artifact.schema import (
    Action,
    Artifact,
    AppTarget,
    ClickAction,
    ExtractAction,
    InputParam,
    LabelForLocator,
    NavigateAction,
    OutputField,
    ParamType,
    RoleLocator,
    SelectAction,
    Step,
    StructuralLocator,
    TargetLocator,
    TextLocator,
    TypeAction,
    UrlMatchesCondition,
    WaitForAction,
)

# Matches one accessibility-tree line, e.g.:
#   textbox [ref=e10]
#   button "Look Up" [ref=e12]
#   cell "Savings Balance" [ref=e20]
#   link "Open New Sub-Account" [ref=e41] [cursor=pointer]:
_TREE_LINE_RE = re.compile(
    r'^\s*-\s*(?P<role>[a-zA-Z]+)(?:\s+"(?P<name>[^"]*)")?[^\[]*\[ref=(?P<ref>[^\]]+)\]'
)

# StructuralLocator.tag must be a real HTML tag Playwright can use as a CSS
# selector (page.locator(tag)), not an ARIA role -- the accessibility
# snapshot only gives us the role (e.g. "textbox"), so this maps the
# standard, spec-defined ARIA roles for form controls back to the HTML tag
# that produces them (per the HTML-AAM spec). Recording never has live DOM
# access (see design discussion: keeping the recorder a pure function of a
# RunResult, not requiring an open browser session), so this static table is
# the deliberate trade-off -- it covers every role this system's flows
# actually produce; an unmapped role falls back to using the role name
# itself as a last resort (better than nothing, but flagged in reasoning).
_ROLE_TO_HTML_TAG = {
    "textbox": "input",
    "searchbox": "input",
    "combobox": "select",
    "checkbox": "input",
    "radio": "input",
    "button": "button",
    "link": "a",
    "cell": "td",
    "columnheader": "th",
    "rowheader": "th",
    "row": "tr",
    "table": "table",
    "heading": "h1, h2, h3, h4, h5, h6",
    "paragraph": "p",
}


class RecordingError(Exception):
    """Raised when a run cannot be translated into an artifact."""


def _find_tree_line_index(lines: list[str], ref: str) -> int | None:
    for i, line in enumerate(lines):
        if f"[ref={ref}]" in line:
            return i
    return None


def _line_indent(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


# A value cell's text "looks like data" (a balance, a name, a status pulled
# from a record) rather than a stable UI label when it's mostly digits/
# currency/punctuation, or when a sibling cell in the same row reads like a
# label (ends in nothing alphabetic-only and is followed immediately by
# another cell). We only really need the digit/currency signal here since
# that covers the concrete case (balances) this system's flows extract.
_LOOKS_LIKE_DATA_RE = re.compile(r"^[\$\d][\d,.\s]*$")


def _sibling_cell_label(lines: list[str], value_line_index: int) -> str | None:
    """If the ref at value_line_index is a `cell` whose nearest preceding
    sibling (same indentation, same parent `row`) is also a `cell` with a
    stable-looking accessible name, return that name -- e.g. for
    `row: [cell "Savings Balance", cell "$4823.11"]`, given the balance
    cell's line index, returns "Savings Balance".
    """
    target_indent = _line_indent(lines[value_line_index])
    # Walk backward at the same indentation level, stopping once we leave
    # the enclosing row (indentation decreases below target_indent).
    for i in range(value_line_index - 1, -1, -1):
        indent = _line_indent(lines[i])
        if indent < target_indent:
            break
        if indent != target_indent:
            continue
        match = _TREE_LINE_RE.match(lines[i])
        if match and match.group("role") == "cell" and match.group("name"):
            return match.group("name")
        break
    return None


def _locator_for_ref(tree_text: str, ref: str) -> TargetLocator:
    """Build a fallback-chain locator for a ref by reading its line (and, if
    it has no accessible name, its position) in the snapshot tree text.
    """
    lines = tree_text.splitlines()
    line_index = _find_tree_line_index(lines, ref)
    if line_index is None:
        raise RecordingError(f'ref "{ref}" not found in its own snapshot -- cannot record a locator for it.')
    line = lines[line_index]

    match = _TREE_LINE_RE.match(line)
    strategies = []
    reasoning_parts = []

    if match and match.group("role") == "cell" and match.group("name") and _LOOKS_LIKE_DATA_RE.match(match.group("name")):
        # This cell's own text is record data (a balance, an amount, an id),
        # not a stable label -- locating by that text would only ever match
        # this exact value again, breaking replay for any other input.
        # Anchor on the adjacent label cell instead, which is what a human
        # reading the table would also use to find the right row.
        label = _sibling_cell_label(lines, line_index)
        if label is not None:
            strategies.append(LabelForLocator(label=label))
            reasoning_parts.append(
                f'Primary: the cell\'s own text ("{match.group("name")}") is record data that will differ on '
                f'every replay, so this locates by the adjacent label cell ("{label}") instead -- the stable '
                "part of a label/value table row."
            )
            # nth=1, not 0: within the row scoped by the label text, index 0
            # is the label cell itself ("Savings Balance") -- the value
            # we actually want is the next cell over.
            strategies.append(StructuralLocator(tag="td", nth=1, within_container_text=label))
            reasoning_parts.append(
                "Fallback: structural position (second cell) within the row identified by that same label."
            )
            return TargetLocator(strategies=strategies, reasoning=" ".join(reasoning_parts))
        # No sibling label found -- fall through to the generic name-based
        # strategy below, but the reasoning should flag the risk.
        reasoning_parts.append(
            f'Warning: this cell\'s text ("{match.group("name")}") looks like record data, but no adjacent '
            "label cell was found to anchor on, so this falls back to matching by that value's own text -- "
            "will only match this exact value again."
        )

    if match and match.group("name"):
        role, name = match.group("role"), match.group("name")
        strategies.append(RoleLocator(role=role, accessible_name=name))
        reasoning_parts.append(f'Primary: role "{role}" with accessible name "{name}", read directly off the element.')
        strategies.append(TextLocator(text=name, exact=True))
        reasoning_parts.append("Fallback: exact visible text match, in case the role changes but the label doesn't.")
    elif match:
        role = match.group("role")
        # No accessible name (e.g. a bare <input> with no label/placeholder).
        # Role alone is rarely unique, so lead with structural position
        # among same-role, same-ref-context elements, using ref order as a
        # stable-enough proxy for DOM order within this run.
        html_tag = _ROLE_TO_HTML_TAG.get(role, role)
        strategies.append(StructuralLocator(tag=html_tag, nth=0))
        reasoning_parts.append(
            f'Primary: no accessible name was available for this "{role}" element at recording time '
            "(likely an unlabeled input), so this falls back to structural position immediately -- "
            "the least robust strategy. Recommend adding a label or aria-label to this control."
        )
        if html_tag == role and role not in _ROLE_TO_HTML_TAG:
            reasoning_parts.append(
                f'Note: role "{role}" has no known HTML-tag mapping, so the role name itself was used as the '
                "tag selector -- verify this resolves correctly."
            )
    else:
        strategies.append(StructuralLocator(tag="*", nth=0))
        reasoning_parts.append(f"Could not parse the snapshot line for ref {ref!r}; recorded as an unstructured fallback.")

    return TargetLocator(strategies=strategies, reasoning=" ".join(reasoning_parts))


def _url_path(url: str) -> str:
    parsed = urlparse(url)
    return parsed.path or "/"


def _templated_url_pattern(path: str, namer: "_ParamNamer") -> str:
    """Build a regex pattern for the success_condition by substituting any
    literal values that were typed as inputs during this run (e.g. a member
    id) with their {{param}} placeholder wherever they appear in the final
    URL path -- e.g. "/members/12345" with memberId="12345" becomes
    "/members/\\{\\{memberId\\}\\}$" -> rendered at replay time via the same
    _render_template the engine already uses for step values.

    Without this, a success_condition recorded from one run's specific
    input (e.g. member 12345) would never match a replay with a different
    input (e.g. member 67890), even though the flow correctly reached that
    member's equivalent page -- the checkpoint would over-fit to the
    exact value seen during discovery instead of asserting the *shape* of
    a correct end state.
    """
    # Substitute longest values first so a shorter value that happens to be
    # a substring of a longer one (e.g. "123" inside "12345") doesn't
    # corrupt the longer substitution.
    for value, param_name in sorted(namer.value_to_name.items(), key=lambda kv: -len(kv[0])):
        if value and value in path:
            path = path.replace(value, f"{{{{{param_name}}}}}")
    return _escape_outside_templates(path) + "$"


def _escape_outside_templates(path: str) -> str:
    """re.escape the literal parts of a path that still contains {{param}}
    placeholders, without escaping the placeholders' braces themselves --
    they need to survive as literal "{{name}}" text for _render_template
    (used by the replay engine) to substitute later, and must not be
    treated as regex syntax before that substitution happens.
    """
    parts = re.split(r"(\{\{\w+\}\})", path)
    return "".join(part if re.fullmatch(r"\{\{\w+\}\}", part) else re.escape(part) for part in parts)


class _ParamNamer:
    """Assigns stable {{paramName}} names to literal values typed during the
    run, reusing the same name if the same literal value is typed more than
    once (e.g. a member ID typed once but referenced conceptually elsewhere).
    """

    def __init__(self):
        self.value_to_name: dict[str, str] = {}
        self._used_names: set[str] = set()

    def name_for(self, value: str, hint: str) -> str:
        if value in self.value_to_name:
            return self.value_to_name[value]
        base = re.sub(r"[^a-zA-Z0-9]", "", hint) or "param"
        base = base[0].lower() + base[1:] if base else "param"
        name = base
        i = 2
        while name in self._used_names:
            name = f"{base}{i}"
            i += 1
        self._used_names.add(name)
        self.value_to_name[value] = name
        return name


def record_artifact(
    *,
    goal: str,
    run: RunResult,
    name: str,
    app_target: AppTarget,
    risk_level,
    description: str | None = None,
) -> Artifact:
    """Translate a successful (stop_reason == DONE) RunResult into a
    versioned Artifact.
    """
    if run.stop_reason != StopReason.DONE:
        raise RecordingError(
            f"Cannot record an artifact from a run that did not complete successfully "
            f"(stop_reason={run.stop_reason.value})."
        )

    executable_records = [s for s in run.steps if s.tool_name != "done"]
    if not executable_records:
        raise RecordingError("Run completed with `done` immediately -- no actions to record.")

    namer = _ParamNamer()
    steps: list[Step] = []
    output_fields: list[OutputField] = []
    input_params: dict[str, InputParam] = {}

    for record in executable_records:
        action = _translate_action(record, namer, input_params)
        steps.append(
            Step(
                id=f"step-{record.index}",
                description=_describe(record),
                action=action,
            )
        )
        if record.tool_name == "extract":
            output_key = record.tool_input["output_key"]
            output_fields.append(
                OutputField(name=output_key, type=ParamType.STRING, description=f'Value extracted for "{output_key}".')
            )

    final_url = run.steps[-1].snapshot.url
    success_condition = UrlMatchesCondition(pattern=_templated_url_pattern(_url_path(final_url), namer))

    return Artifact(
        id=str(uuid.uuid4()),
        name=name,
        version=1,
        description=description or f'Recorded from discovery goal: "{goal}"',
        app_target=app_target,
        inputs=list(input_params.values()),
        outputs=output_fields,
        steps=steps,
        success_condition=success_condition,
        risk_level=risk_level,
        discovered_at=datetime.now(timezone.utc),
        discovered_from_goal=goal,
    )


def _describe(record: StepRecord) -> str:
    if record.tool_name == "navigate":
        return f"Navigate to {record.tool_input.get('url')}"
    if record.tool_name == "click":
        return f"Click element (ref {record.tool_input.get('ref')} at recording time)"
    if record.tool_name == "type":
        return "Type a value into a field"
    if record.tool_name == "select":
        return f"Select \"{record.tool_input.get('value')}\" in a dropdown"
    if record.tool_name == "wait_for":
        return "Wait for a condition on the page"
    if record.tool_name == "extract":
        return f"Extract the value for \"{record.tool_input.get('output_key')}\""
    return f"{record.tool_name} step"


def _translate_action(record: StepRecord, namer: _ParamNamer, input_params: dict[str, InputParam]) -> Action:
    tool_input = record.tool_input
    tree_text = record.snapshot.tree_text

    if record.tool_name == "navigate":
        return NavigateAction(url=tool_input["url"])

    if record.tool_name == "click":
        target = _locator_for_ref(tree_text, tool_input["ref"])
        return ClickAction(target=target)

    if record.tool_name == "type":
        target = _locator_for_ref(tree_text, tool_input["ref"])
        value = tool_input["value"]
        sensitive = tool_input.get("sensitive", False)
        if sensitive:
            # Sensitive literals are never turned into named, inspectable
            # params with their value retained anywhere -- the artifact
            # declares a sensitive input by name only; the actual value is
            # supplied fresh at replay time by the caller.
            param_name = namer.name_for(value, "secret")
            input_params[param_name] = InputParam(
                name=param_name, type=ParamType.STRING, description="Sensitive value supplied at replay time.", sensitive=True
            )
        else:
            param_name = namer.name_for(value, _guess_param_hint(target))
            input_params.setdefault(
                param_name,
                InputParam(name=param_name, type=ParamType.STRING, description=f'Value typed during discovery: "{value}".'),
            )
        return TypeAction(target=target, value=f"{{{{{param_name}}}}}", sensitive=sensitive)

    if record.tool_name == "select":
        target = _locator_for_ref(tree_text, tool_input["ref"])
        return SelectAction(target=target, value=tool_input["value"])

    if record.tool_name == "wait_for":
        condition = tool_input["condition"]
        if condition["kind"] == "urlMatches":
            cond = UrlMatchesCondition(pattern=condition["pattern"])
        else:
            target = _locator_for_ref(tree_text, condition["ref"])
            from automation.artifact.schema import ElementVisibleCondition

            cond = ElementVisibleCondition(target=target)
        return WaitForAction(condition=cond, timeout_ms=tool_input.get("timeout_ms", 10_000))

    if record.tool_name == "extract":
        target = _locator_for_ref(tree_text, tool_input["ref"])
        return ExtractAction(target=target, output_key=tool_input["output_key"])

    raise RecordingError(f'Unknown tool "{record.tool_name}" cannot be translated into an artifact action.')


def _guess_param_hint(target: TargetLocator) -> str:
    for strategy in target.strategies:
        if isinstance(strategy, RoleLocator) and strategy.accessible_name:
            return strategy.accessible_name
    return "value"
