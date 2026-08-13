"""Browser + accessibility-snapshot layer for the discovery agent.

This is the "observe" half of the observe-decide-act loop. It wraps a
Playwright page and produces a ref-tagged accessibility-tree snapshot each
turn -- the same representation the model reads and the same `ref` ids the
model passes back into click/type/select/wait_for/extract tool calls.

We use Playwright's built-in `aria_snapshot(mode="ai")`, which was built
specifically for this purpose: it renders the accessibility tree as compact
text with stable `[ref=eN]` markers, and Playwright resolves `aria-ref=eN`
straight back to a real Locator. This works without any clean DOM, test
ids, or CSS selectors -- exactly the legacy-app constraint this system is
built around -- and the same abstraction (accessible role + name) is what
desktop accessibility APIs expose too, which keeps the door open for a
desktop surface later without changing this module's contract.

Refs are only valid for the snapshot they came from: any DOM mutation
(navigation, a re-render after a click) invalidates old refs, so the harness
must take a fresh snapshot after every action before asking the model to
act again.
"""

from __future__ import annotations

from dataclasses import dataclass

from playwright.sync_api import Locator, Page


class StaleRefError(Exception):
    """Raised when a tool call references a ref not present in the current snapshot."""

    def __init__(self, ref: str):
        super().__init__(
            f'ref "{ref}" is not present in the current snapshot -- it may be from a '
            "prior turn or the page may have changed. Take a fresh look and retry."
        )
        self.ref = ref


@dataclass(frozen=True)
class Snapshot:
    """A single observation of the page: the raw ref-tagged tree text plus
    the set of refs it contains, so callers can validate a proposed ref
    before trying to act on it.
    """

    url: str
    title: str
    tree_text: str
    refs: frozenset[str]

    def has_ref(self, ref: str) -> bool:
        return ref in self.refs


def _extract_refs(tree_text: str) -> frozenset[str]:
    import re

    # Playwright's ref ids are usually "e<N>" (e.g. "e14"), but after a
    # navigation/frame change it has been observed to prefix them with a
    # frame identifier (e.g. "f2e1"). Match the ref id as a whole rather
    # than assuming the "e<N>" shape, so refs are never silently dropped --
    # which, before this fix, made every snapshot after the first
    # navigation report zero refs.
    return frozenset(re.findall(r"\[ref=([^\]]+)\]", tree_text))


class BrowserSession:
    """Owns a single Playwright page for the duration of a discovery run."""

    def __init__(self, page: Page):
        self._page = page

    @property
    def page(self) -> Page:
        return self._page

    def snapshot(self) -> Snapshot:
        tree_text = self._page.locator("body").aria_snapshot(mode="ai")
        return Snapshot(
            url=self._page.url,
            title=self._page.title(),
            tree_text=tree_text,
            refs=_extract_refs(tree_text),
        )

    def resolve(self, ref: str, snapshot: Snapshot) -> Locator:
        """Resolve a ref to a live Locator, validated against a specific
        snapshot so stale refs from an earlier turn are rejected explicitly
        rather than silently resolving to whatever now occupies that ref id.
        """
        if not snapshot.has_ref(ref):
            raise StaleRefError(ref)
        return self._page.locator(f"aria-ref={ref}")
