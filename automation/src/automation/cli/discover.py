"""CLI entrypoint: run a real, LLM-driven discovery run against a live
target and save evidence of it.

    uv run discover --goal "look up member 12345 and read their current savings balance"

Login is performed directly via Playwright before the agent loop starts --
it's a fixed precondition every run needs identically, not part of the goal
the LLM should spend its step budget discovering (see design discussion:
this keeps discovery focused on the actual task).
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from automation.agent.browser import BrowserSession
from automation.agent.loop import DEFAULT_MAX_STEPS, DEFAULT_TIMEOUT_SECONDS, RunResult, SdkAnthropicClient, run_discovery

DEFAULT_TARGET = "http://localhost:4000/members/search"
# src/automation/cli/discover.py -> cli -> automation(pkg) -> src -> automation(project dir) -> repo root
REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_EVIDENCE_ROOT = REPO_ROOT / "evidence" / "runs"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="discover",
        description="Run an LLM-driven discovery agent against a live target and save evidence of the run.",
    )
    parser.add_argument("--goal", required=True, help="Natural-language goal for the agent to accomplish.")
    parser.add_argument("--target", default=DEFAULT_TARGET, help=f"Entry point URL (default: {DEFAULT_TARGET}).")
    parser.add_argument("--username", default="discovery-agent", help="Username to log in with.")
    parser.add_argument("--password", default="discovery-agent", help="Password to log in with.")
    parser.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS)
    parser.add_argument("--timeout-seconds", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--headed", action="store_true", help="Run the browser with a visible window.")
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=None,
        help="Directory to write this run's evidence into (default: evidence/runs/<timestamp>-<goal-slug>/).",
    )
    return parser.parse_args(argv)


def _slugify(text: str, max_len: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "goal"


def _login(session: BrowserSession, base_url: str, username: str, password: str) -> None:
    # Login is host-relative in the mock app; derive origin from target.
    from urllib.parse import urlparse

    parsed = urlparse(base_url)
    origin = f"{parsed.scheme}://{parsed.netloc}"
    session.page.goto(f"{origin}/login")

    snapshot = session.snapshot()
    username_ref = None
    password_ref = None
    for ref in snapshot.refs:
        loc = session.resolve(ref, snapshot)
        input_type = loc.evaluate("el => el.type || ''")
        name = loc.evaluate("el => el.name || ''")
        if name == "username" or input_type == "text":
            username_ref = username_ref or ref
        if name == "password" or input_type == "password":
            password_ref = password_ref or ref

    if username_ref is None or password_ref is None:
        raise RuntimeError("Could not locate username/password fields on the login page.")

    session.resolve(username_ref, snapshot).fill(username)
    session.resolve(password_ref, snapshot).fill(password)
    session.page.locator('input[type="submit"], button[type="submit"]').first.click()
    session.page.wait_for_load_state("networkidle")


def _step_record_to_dict(record) -> dict:
    return {
        "index": record.index,
        "url": record.snapshot.url,
        "tool_name": record.tool_name,
        "tool_input": record.tool_input,
        "result": (
            {
                "ok": record.result.ok,
                "data": record.result.data,
                "error": record.result.error,
            }
            if record.result is not None
            else None
        ),
    }


def _write_evidence(evidence_dir: Path, goal: str, target: str, result: RunResult, final_screenshot: bytes | None) -> None:
    evidence_dir.mkdir(parents=True, exist_ok=True)

    log = {
        "goal": goal,
        "target": target,
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "stopReason": result.stop_reason.value,
        "elapsedSeconds": result.elapsed_seconds,
        "outputs": result.outputs,
        "summary": result.summary,
        "stuckReason": result.stuck_reason,
        "stuckDetails": result.stuck_details,
        "steps": [_step_record_to_dict(s) for s in result.steps],
    }
    (evidence_dir / "log.json").write_text(json.dumps(log, indent=2))

    if final_screenshot is not None:
        (evidence_dir / "final.png").write_bytes(final_screenshot)


def main(argv: list[str] | None = None) -> int:
    load_dotenv(REPO_ROOT / ".env")
    args = parse_args(sys.argv[1:] if argv is None else argv)

    evidence_dir = args.evidence_dir
    if evidence_dir is None:
        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        evidence_dir = DEFAULT_EVIDENCE_ROOT / f"{timestamp}-{_slugify(args.goal)}"

    print(f"Goal:   {args.goal}")
    print(f"Target: {args.target}")
    print(f"Evidence will be written to: {evidence_dir}")
    print()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        page = browser.new_page()
        session = BrowserSession(page)

        print("Logging in...")
        _login(session, args.target, args.username, args.password)

        print(f"Navigating to target: {args.target}")
        page.goto(args.target)

        print("Starting discovery loop...")
        client = SdkAnthropicClient()
        result = run_discovery(
            goal=args.goal,
            session=session,
            client=client,
            max_steps=args.max_steps,
            timeout_seconds=args.timeout_seconds,
        )

        final_screenshot = page.screenshot()
        browser.close()

    _write_evidence(evidence_dir, args.goal, args.target, result, final_screenshot)

    print()
    print(f"Stop reason: {result.stop_reason.value}")
    print(f"Steps taken: {len(result.steps)}")
    print(f"Elapsed:     {result.elapsed_seconds:.1f}s")
    if result.stop_reason.value == "done":
        print(f"Summary:     {result.summary}")
        print(f"Outputs:     {json.dumps(result.outputs)}")
    elif result.stop_reason.value == "stuck":
        print(f"Stuck reason:  {result.stuck_reason}")
        print(f"Stuck details: {result.stuck_details}")
    print(f"Evidence written to: {evidence_dir}")

    return 0 if result.stop_reason.value == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
