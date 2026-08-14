"""Domain/path allowlist: the hard boundary on what the agent is permitted
to act on, enforced identically for both discovery (LLM-driven) and replay
(deterministic) -- the same policy module is called from both, so the two
paths can't silently diverge in what they permit.

Checked at two points:
  - before any navigate (would this take us somewhere new that's disallowed?)
  - after every action (did a click/redirect/form submit leave us somewhere
    disallowed? navigation isn't the only way to change page.url)

An empty allowlist (no entries at all) is treated as "nothing is allowed" --
fail closed, not fail open. A real deployment always configures at least
one entry; forgetting to configure one should not silently permit
everything.
"""

from __future__ import annotations

import fnmatch
from dataclasses import dataclass
from urllib.parse import urlparse


@dataclass(frozen=True)
class AllowlistEntry:
    domain: str  # exact hostname, e.g. "localhost:4000" (port included if non-default)
    path_patterns: tuple[str, ...] = ("*",)  # fnmatch-style globs against the URL path; default allows any path on the domain


@dataclass(frozen=True)
class Allowlist:
    entries: tuple[AllowlistEntry, ...]
    # Action types the agent may perform at all, independent of URL. Empty
    # means none are permitted -- fail closed, same rationale as above.
    allowed_action_types: frozenset[str]

    def check_url(self, url: str) -> tuple[bool, str]:
        """Returns (allowed, reason)."""
        parsed = urlparse(url)
        host = parsed.netloc
        for entry in self.entries:
            if entry.domain == host:
                for pattern in entry.path_patterns:
                    if fnmatch.fnmatch(parsed.path or "/", pattern):
                        return True, f'matches allowlist entry domain="{entry.domain}" pattern="{pattern}"'
                return False, f'domain "{host}" is allowlisted but path "{parsed.path}" matches none of its patterns'
        return False, f'domain "{host}" is not in the allowlist'

    def check_action_type(self, action_type: str) -> tuple[bool, str]:
        if action_type in self.allowed_action_types:
            return True, f'action type "{action_type}" is permitted'
        return False, f'action type "{action_type}" is not in the allowed set {sorted(self.allowed_action_types)}'


class AllowlistViolation(Exception):
    """Raised when an action or URL falls outside the configured allowlist.
    Both the agent loop and the replay engine treat this as an immediate,
    unrecoverable stop -- never a retryable condition.
    """

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def default_mock_app_allowlist() -> Allowlist:
    """The allowlist used for this project's demo target. A real deployment
    would load this from tenant/app configuration instead of hardcoding it.
    """
    return Allowlist(
        entries=(
            AllowlistEntry(
                domain="localhost:4000",
                path_patterns=("/login", "/members/*"),
            ),
        ),
        allowed_action_types=frozenset({"navigate", "click", "type", "select", "wait_for", "extract"}),
    )
