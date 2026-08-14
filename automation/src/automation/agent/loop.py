"""The discovery agent loop: observe -> decide -> act, until the goal is met
or a stopping condition fires.

Each turn:
  1. observe  -- BrowserSession.snapshot() (automation.agent.browser)
  2. decide   -- send the running transcript + tools to Claude; it returns
                 exactly one tool_use block (automation.agent.tools)
  3. act      -- dispatch it through the executor (automation.agent.executor),
                 unless it's `done`/`stuck`, which end the loop directly

The full message history is replayed to the model every turn (see
tests/README discussion in the design write-up for why: bounded run length,
and Anthropic prompt caching makes a growing-but-capped transcript cheap --
a rolling window would evict exactly the context, like "which member ID are
we on", that a later step needs, and would defeat the cache prefix on every
eviction).

This module does not know how to turn a completed run into an Artifact --
that translation (StepRecord list -> durable role/name locators) is a
separate concern, built on top of the StepRecord log this loop produces.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol

from automation.agent.browser import BrowserSession, Snapshot
from automation.agent.executor import EXECUTABLE_TOOLS, ToolResult, execute
from automation.agent.tools import AGENT_TOOLS, anthropic_tool_definitions
from automation.policy.allowlist import Allowlist

DEFAULT_MAX_STEPS = 25
DEFAULT_TIMEOUT_SECONDS = 180.0
DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_MAX_TOKENS = 2048

SYSTEM_PROMPT_TEMPLATE = """\
You are a discovery agent operating a legacy back-office web application on \
behalf of an automated capability-recording system. Your job is to \
accomplish the stated goal by observing the page and calling exactly one \
tool per turn.

Goal: {goal}

Rules:
- You will be shown a fresh accessibility-tree snapshot of the current page \
before each decision. Elements are tagged with ref ids like "e14" -- only \
use refs from the most recently shown snapshot; refs from earlier turns are \
stale and will be rejected.
- Call exactly one tool per turn. Wait for its result before deciding the \
next action.
- If a tool call fails, read the error and adjust -- do not repeat the same \
failing call.
- When the goal is fully accomplished, call `done` with a summary and any \
requested output values.
- If you cannot safely proceed (an unexpected dialog, a permission denial, \
repeated failures, or the goal appears unreachable), call `stuck` with a \
specific reason rather than guessing or taking a risky action.
- Never invent data. Only report values you actually observed on the page.
"""


class StopReason(str, Enum):
    DONE = "done"
    STUCK = "stuck"
    MAX_STEPS = "max_steps"
    TIMEOUT = "timeout"


@dataclass(frozen=True)
class StepRecord:
    """One turn of the loop: what the model decided and what happened.

    This is the raw transcript unit. Artifact recording (a later stage)
    reads a list of these -- specifically the executable-tool ones -- and
    translates each into a durable Step with a role/name locator, which is
    why ref, tool_name, and tool_input are all kept here even though they
    are meaningless outside this run.
    """

    index: int
    snapshot: Snapshot
    tool_name: str
    tool_input: dict[str, Any]
    result: ToolResult | None  # None for done/stuck, which are not executed


@dataclass
class RunResult:
    stop_reason: StopReason
    steps: list[StepRecord]
    outputs: dict[str, Any] = field(default_factory=dict)
    summary: str | None = None  # from `done`
    stuck_reason: str | None = None  # from `stuck`
    stuck_details: str | None = None  # from `stuck`
    elapsed_seconds: float = 0.0


class AnthropicClient(Protocol):
    """Minimal surface this module needs from the Anthropic SDK client, so
    tests can inject a fake without touching the network.
    """

    def create_message(
        self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Any: ...


class SdkAnthropicClient:
    """Thin adapter over anthropic.Anthropic so the loop depends on the
    small Protocol above instead of the full SDK surface.
    """

    def __init__(self, api_key: str | None = None, model: str = DEFAULT_MODEL, max_tokens: int = DEFAULT_MAX_TOKENS):
        import anthropic

        self._client = anthropic.Anthropic(api_key=api_key)
        self._model = model
        self._max_tokens = max_tokens

    def create_message(self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Any:
        return self._client.messages.create(
            model=self._model,
            max_tokens=self._max_tokens,
            system=system,
            messages=messages,
            tools=tools,
        )


class GoalUnreachableError(Exception):
    """Raised internally when the model's response cannot be interpreted as
    exactly one valid tool call, after retries -- surfaced to the caller as
    a hard failure rather than silently guessing.
    """


def _redact_sensitive_values(tree_text: str, sensitive_refs: set[str]) -> str:
    """Scrub the literal value shown for any ref that was just filled via
    type(..., sensitive=True).

    Playwright's aria_snapshot renders a form field's *current* value inline
    (e.g. `textbox [ref=e2]: "hunter2"`), so a password typed on turn N would
    otherwise reappear in plain text in the very next observation sent back
    to the model -- defeating the point of the `sensitive` flag. This only
    scrubs refs the loop itself just wrote a sensitive value into; it is not
    a general PII scanner (see automation.policy for the broader redaction
    story applied at logging/evidence boundaries).
    """
    if not sensitive_refs:
        return tree_text

    redacted_lines = []
    for line in tree_text.splitlines():
        if any(f"[ref={ref}]" in line for ref in sensitive_refs) and ":" in line:
            prefix = line.split(":", 1)[0]
            redacted_lines.append(f"{prefix}: [REDACTED]")
        else:
            redacted_lines.append(line)
    return "\n".join(redacted_lines)


def _snapshot_to_user_message(snapshot: Snapshot, sensitive_refs: set[str]) -> dict[str, Any]:
    tree_text = _redact_sensitive_values(snapshot.tree_text, sensitive_refs)
    text = (
        f"Current page: {snapshot.url}\n"
        f"Title: {snapshot.title}\n\n"
        f"Accessibility snapshot:\n{tree_text}"
    )
    return {"role": "user", "content": [{"type": "text", "text": text}]}


def _tool_result_to_user_message(tool_use_id: str, result: ToolResult) -> dict[str, Any]:
    if result.ok:
        content = f"OK. {result.data}"
    else:
        content = f"ERROR: {result.error}"
    return {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": tool_use_id, "content": content}],
    }


def run_discovery(
    *,
    goal: str,
    session: BrowserSession,
    client: AnthropicClient,
    max_steps: int = DEFAULT_MAX_STEPS,
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS,
    allowlist: Allowlist | None = None,
) -> RunResult:
    """Run the observe-decide-act loop until `done`, `stuck`, or a stopping
    condition (max_steps / timeout_seconds) is hit.
    """
    system_prompt = SYSTEM_PROMPT_TEMPLATE.format(goal=goal)
    tools = anthropic_tool_definitions()
    messages: list[dict[str, Any]] = []
    steps: list[StepRecord] = []
    start = time.monotonic()
    # Refs most recently filled via type(..., sensitive=True): their literal
    # value must be scrubbed out of the *next* snapshot before it's shown to
    # the model. Cleared each turn and only ever holds the previous turn's
    # sensitive ref, since a fresh DOM (post-navigation) reuses ref ids for
    # unrelated elements.
    sensitive_refs: set[str] = set()

    for step_index in range(max_steps):
        elapsed = time.monotonic() - start
        if elapsed >= timeout_seconds:
            return RunResult(stop_reason=StopReason.TIMEOUT, steps=steps, elapsed_seconds=elapsed)

        snapshot = session.snapshot()
        messages.append(_snapshot_to_user_message(snapshot, sensitive_refs))
        sensitive_refs = set()

        response = client.create_message(system=system_prompt, messages=messages, tools=tools)
        tool_use = _extract_single_tool_use(response)

        messages.append({"role": "assistant", "content": response.content})

        tool_name = tool_use.name
        raw_input = tool_use.input

        if tool_name == "done":
            from automation.agent.tools import DoneInput

            parsed = DoneInput.model_validate(raw_input)
            steps.append(StepRecord(step_index, snapshot, tool_name, raw_input, None))
            return RunResult(
                stop_reason=StopReason.DONE,
                steps=steps,
                outputs=dict(parsed.outputs),
                summary=parsed.summary,
                elapsed_seconds=time.monotonic() - start,
            )

        if tool_name == "stuck":
            from automation.agent.tools import StuckInput

            parsed = StuckInput.model_validate(raw_input)
            steps.append(StepRecord(step_index, snapshot, tool_name, raw_input, None))
            return RunResult(
                stop_reason=StopReason.STUCK,
                steps=steps,
                stuck_reason=parsed.reason,
                stuck_details=parsed.details,
                elapsed_seconds=time.monotonic() - start,
            )

        if tool_name not in EXECUTABLE_TOOLS:
            raise GoalUnreachableError(f'Model called unknown tool "{tool_name}".')

        parsed_input = AGENT_TOOLS[tool_name].schema_.model_validate(raw_input)
        result = execute(session, snapshot, tool_name, parsed_input, allowlist=allowlist)
        steps.append(StepRecord(step_index, snapshot, tool_name, raw_input, result))
        messages.append(_tool_result_to_user_message(tool_use.id, result))

        if tool_name == "type" and result.ok and getattr(parsed_input, "sensitive", False):
            sensitive_refs.add(parsed_input.ref)

    return RunResult(stop_reason=StopReason.MAX_STEPS, steps=steps, elapsed_seconds=time.monotonic() - start)


def _extract_single_tool_use(response: Any) -> Any:
    tool_use_blocks = [b for b in response.content if getattr(b, "type", None) == "tool_use"]
    if len(tool_use_blocks) != 1:
        raise GoalUnreachableError(
            f"Expected exactly one tool_use block from the model, got {len(tool_use_blocks)}."
        )
    return tool_use_blocks[0]
