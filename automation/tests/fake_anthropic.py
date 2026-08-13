"""A minimal fake Anthropic client for testing the agent loop without
network access. Scripts a fixed sequence of responses, one per turn.
"""

from __future__ import annotations

from typing import Any


class FakeBlock:
    """Stands in for an SDK content block (text or tool_use)."""

    def __init__(self, type_: str, **kw: Any):
        self.type = type_
        for k, v in kw.items():
            setattr(self, k, v)


class FakeResponse:
    def __init__(self, content: list[FakeBlock]):
        self.content = content


def tool_use_response(tool_use_id: str, name: str, input_: dict[str, Any]) -> FakeResponse:
    return FakeResponse([FakeBlock("tool_use", id=tool_use_id, name=name, input=input_)])


class FakeAnthropicClient:
    """Replays a scripted sequence of responses, one per create_message call.

    Also records every call's (system, messages, tools) so tests can assert
    on what was actually sent -- e.g. that the transcript grows correctly.
    """

    def __init__(self, script: list[FakeResponse]):
        self._script = list(script)
        self.calls: list[dict[str, Any]] = []

    def create_message(self, *, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]) -> Any:
        # Snapshot the list (not a reference to it) -- the caller mutates
        # and reuses the same `messages` list across turns, so without a
        # copy every recorded call would end up pointing at its final,
        # fully-grown state.
        self.calls.append({"system": system, "messages": list(messages), "tools": tools})
        if not self._script:
            raise AssertionError("FakeAnthropicClient script exhausted -- too many turns requested")
        return self._script.pop(0)
