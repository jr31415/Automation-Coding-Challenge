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
from automation.artifact.recorder import RecordingError, record_artifact
from automation.artifact.schema import AppTarget, RiskLevel
from automation.escalation.handoff import build_handoff_result, build_intervention_request, format_intervention_summary
from automation.policy.allowlist import Allowlist, AllowlistEntry
from automation.policy.redact import REDACTED, redact_mapping

DEFAULT_TARGET = "http://localhost:4000/members/search"
# src/automation/cli/discover.py -> cli -> automation(pkg) -> src -> automation(project dir) -> repo root
REPO_ROOT = Path(__file__).resolve().parents[4]
DEFAULT_EVIDENCE_ROOT = REPO_ROOT / "evidence" / "runs"
DEFAULT_ARTIFACTS_DIR = REPO_ROOT / "artifacts"


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
    parser.add_argument(
        "--headed",
        action="store_true",
        default=True,
        help="Run the browser with a visible window (default: on). A headed session with an open CDP "
        "debugging port is what makes real human handoff possible if the agent gets stuck -- see "
        "automation.escalation.handoff.",
    )
    parser.add_argument(
        "--headless",
        dest="headed",
        action="store_false",
        help="Run headless instead. If the agent then calls `stuck`, escalation is reported but there is "
        "no live window a human could actually take control of -- the run is treated as unresolved.",
    )
    parser.add_argument(
        "--cdp-port",
        type=int,
        default=9222,
        help="Chromium remote-debugging port to expose for the handoff mechanism (default: 9222).",
    )
    parser.add_argument(
        "--no-interactive-escalation",
        action="store_true",
        help="If the agent calls `stuck`, don't block on a terminal prompt for a human operator -- report "
        "the intervention request and exit immediately. Useful for CI or scripted runs.",
    )
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=None,
        help="Directory to write this run's evidence into (default: evidence/runs/<timestamp>-<goal-slug>/).",
    )
    parser.add_argument(
        "--capability-name",
        default=None,
        help="Name to save the artifact under on success (default: a slug of --goal).",
    )
    parser.add_argument(
        "--risk-level",
        choices=[r.value for r in RiskLevel],
        default=RiskLevel.SAFE.value,
        help="Risk classification recorded on the artifact (default: safe). See automation.policy for how replay uses this.",
    )
    parser.add_argument(
        "--vendor-product",
        default="riverbend-core-admin",
        help="App target identifier the artifact is recorded against (default: riverbend-core-admin, the mock app).",
    )
    parser.add_argument(
        "--no-save-artifact",
        action="store_true",
        help="Don't save an artifact even if the run completes successfully (evidence is still written).",
    )
    parser.add_argument(
        "--allowed-path",
        action="append",
        default=[],
        dest="allowed_paths",
        metavar="PATTERN",
        help="An fnmatch-style path pattern the agent may navigate to on --target's domain, e.g. '/members/*'. "
        "Repeatable. If omitted, defaults to the target's own path plus '/login' and '/members/*' "
        "(the mock app's real routes).",
    )
    parser.add_argument(
        "--no-allowlist",
        action="store_true",
        help="Disable allowlist enforcement entirely (not recommended -- see automation.policy.allowlist).",
    )
    return parser.parse_args(argv)


def _build_allowlist(target: str, allowed_paths: list[str]) -> Allowlist:
    from urllib.parse import urlparse

    domain = urlparse(target).netloc
    patterns = tuple(allowed_paths) if allowed_paths else ("/login", "/members/*")
    return Allowlist(
        entries=(AllowlistEntry(domain=domain, path_patterns=patterns),),
        allowed_action_types=frozenset({"navigate", "click", "type", "select", "wait_for", "extract"}),
    )


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
    # The model's raw tool_input for a `type(..., sensitive=True)` call
    # still carries the literal secret value -- the executor only redacts
    # its own ToolResult.data, not the input that produced it. Redact here
    # too, since this is the dict that gets serialized to disk.
    tool_input = record.tool_input
    if record.tool_name == "type" and isinstance(tool_input, dict) and tool_input.get("sensitive"):
        tool_input = {**tool_input, "value": REDACTED}
    return {
        "index": record.index,
        "url": record.snapshot.url,
        "tool_name": record.tool_name,
        "tool_input": tool_input,
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


def _handoff_record_to_dict(handoff_record: dict) -> dict:
    request = handoff_record["request"]
    handoff = handoff_record["handoff"]
    return {
        "intervention": {
            "goal": request.goal,
            "reason": request.reason,
            "details": request.details,
            "currentUrl": request.current_url,
            "stepsCompleted": request.steps_completed,
            "screenshotPath": request.screenshot_path,
            "requestedAt": request.requested_at.isoformat(),
            "sessionEndpoint": request.session_endpoint,
        },
        "handoff": {
            "resumed": handoff.resumed,
            "operatorNotes": handoff.operator_notes,
            "actionsTaken": handoff.actions_taken,
            "handledAt": handoff.handled_at.isoformat() if handoff.handled_at else None,
        },
    }


def _write_evidence(
    evidence_dir: Path,
    goal: str,
    target: str,
    result: RunResult,
    final_screenshot: bytes | None,
    handoff_record: dict | None = None,
) -> None:
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
        "escalation": _handoff_record_to_dict(handoff_record) if handoff_record is not None else None,
    }
    # Shape-based backstop over the whole log, in case a secret leaked into
    # some other field (e.g. echoed into `outputs` or `summary`) that the
    # name-based check above doesn't cover.
    log = redact_mapping(log)
    (evidence_dir / "log.json").write_text(json.dumps(log, indent=2))

    if final_screenshot is not None:
        (evidence_dir / "final.png").write_bytes(final_screenshot)


def _cdp_endpoint(cdp_port: int) -> str:
    import urllib.request

    with urllib.request.urlopen(f"http://127.0.0.1:{cdp_port}/json/version", timeout=5) as resp:
        info = json.loads(resp.read())
    return info["webSocketDebuggerUrl"]


def _handle_escalation(
    *,
    goal: str,
    result: RunResult,
    page,
    evidence_dir: Path,
    cdp_port: int,
    headed: bool,
    interactive: bool,
) -> tuple[RunResult, dict]:
    """When `stuck` fires: build the intervention request, expose the live
    session, and (if interactive) block on a terminal prompt for a human
    operator to signal what happened. Returns the (possibly unchanged)
    RunResult plus a handoff record to fold into evidence.

    Whether the run can actually resume automated execution afterward is
    out of scope for this demo (see REPORT.md "Cuts") -- what this proves
    is the real part of the requirement: detect stuck, carry context to a
    human, expose the literal live session (not a fresh one) for them to
    act on directly, and record what they did before handing back.
    """
    screenshot_path = evidence_dir / "stuck.png"
    evidence_dir.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=str(screenshot_path))

    cdp_endpoint = _cdp_endpoint(cdp_port) if headed else f"http://127.0.0.1:{cdp_port} (headless -- no visible window)"

    request = build_intervention_request(
        goal=goal,
        run_result=result,
        current_url=page.url,
        screenshot_path=str(screenshot_path),
        cdp_endpoint=cdp_endpoint,
    )
    print()
    print(format_intervention_summary(request))

    if not headed:
        print()
        print("This run was headless -- there is no visible window for a human to take control of.")
        print("Re-launch with --headed (the default) to make live handoff possible.")
        handoff = build_handoff_result(resumed=False, operator_notes="headless run, no live handoff possible", actions_taken=[])
        return result, {"request": request, "handoff": handoff}

    if not interactive:
        print()
        print("--no-interactive-escalation set: reporting the intervention and exiting without blocking.")
        handoff = build_handoff_result(resumed=False, operator_notes="non-interactive mode", actions_taken=[])
        return result, {"request": request, "handoff": handoff}

    print()
    print("The automation is now PAUSED and will not touch the page while you have control.")
    actions_taken: list[str] = []
    while True:
        line = input("Operator action taken (blank to finish, or 'abort'): ").strip()
        if not line:
            break
        if line.lower() == "abort":
            actions_taken = []
            break
        actions_taken.append(line)

    resumed_input = input("Resume the automated run now that you're done? [y/N]: ").strip().lower()
    resumed = resumed_input == "y"
    notes = input("Any notes for the record? ").strip()

    handoff = build_handoff_result(resumed=resumed, operator_notes=notes, actions_taken=actions_taken)
    print(f"Control returned to automation. resumed={resumed}")
    return result, {"request": request, "handoff": handoff}


def _next_version(artifacts_dir: Path, name: str) -> int:
    """Existing artifact files are named <name>.v<version>.json; find the
    highest existing version for this name and return the next one.
    """
    existing = list(artifacts_dir.glob(f"{name}.v*.json"))
    versions = []
    for path in existing:
        match = re.match(rf"^{re.escape(name)}\.v(\d+)\.json$", path.name)
        if match:
            versions.append(int(match.group(1)))
    return max(versions, default=0) + 1


def _save_artifact(
    *,
    artifacts_dir: Path,
    goal: str,
    result: RunResult,
    capability_name: str | None,
    risk_level: str,
    vendor_product: str,
    target: str,
) -> Path:
    name = capability_name or _slugify(goal)
    artifacts_dir.mkdir(parents=True, exist_ok=True)
    version = _next_version(artifacts_dir, name)

    from urllib.parse import urlparse

    base_url = f"{urlparse(target).scheme}://{urlparse(target).netloc}"
    artifact = record_artifact(
        goal=goal,
        run=result,
        name=name,
        app_target=AppTarget(vendor_product=vendor_product, base_url=base_url),
        risk_level=RiskLevel(risk_level),
    )
    artifact = artifact.model_copy(update={"version": version})

    path = artifacts_dir / f"{name}.v{version}.json"
    path.write_text(artifact.model_dump_json(indent=2))
    return path


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

    launch_args = [f"--remote-debugging-port={args.cdp_port}"] if args.headed else []
    handoff_record: dict | None = None

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed, args=launch_args)
        page = browser.new_page()
        session = BrowserSession(page)

        print("Logging in...")
        _login(session, args.target, args.username, args.password)

        print(f"Navigating to target: {args.target}")
        page.goto(args.target)

        allowlist = None if args.no_allowlist else _build_allowlist(args.target, args.allowed_paths)

        print("Starting discovery loop...")
        client = SdkAnthropicClient()
        result = run_discovery(
            goal=args.goal,
            session=session,
            client=client,
            max_steps=args.max_steps,
            timeout_seconds=args.timeout_seconds,
            allowlist=allowlist,
        )

        if result.stop_reason.value == "stuck":
            # The browser is deliberately left open (not closed) here so the
            # human operator can attach to the exact same live session --
            # closing it before handoff would defeat the whole point.
            result, handoff_record = _handle_escalation(
                goal=args.goal,
                result=result,
                page=page,
                evidence_dir=evidence_dir,
                cdp_port=args.cdp_port,
                headed=args.headed,
                interactive=not args.no_interactive_escalation,
            )

        final_screenshot = page.screenshot()
        browser.close()

    _write_evidence(evidence_dir, args.goal, args.target, result, final_screenshot, handoff_record)

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

    if result.stop_reason.value == "done" and not args.no_save_artifact:
        try:
            artifact_path = _save_artifact(
                artifacts_dir=DEFAULT_ARTIFACTS_DIR,
                goal=args.goal,
                result=result,
                capability_name=args.capability_name,
                risk_level=args.risk_level,
                vendor_product=args.vendor_product,
                target=args.target,
            )
            print(f"Artifact saved to: {artifact_path}")
        except RecordingError as e:
            print(f"Could not record an artifact from this run: {e}")

    return 0 if result.stop_reason.value == "done" else 1


if __name__ == "__main__":
    raise SystemExit(main())
