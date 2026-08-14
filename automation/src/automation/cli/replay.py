"""CLI entrypoint: deterministically replay a saved Artifact against a live
target, with no LLM in the decision loop.

    uv run replay --artifact artifacts/lookup_member_savings_balance.v1.json --input memberId=67890

This is the production execution path an AI agent would trigger to invoke a
capability. It logs in the same way discover.py does (a fixed precondition,
not part of the replayed flow itself), then hands control to
automation.replay.engine.replay().
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dotenv import load_dotenv
from playwright.sync_api import sync_playwright

from automation.agent.browser import BrowserSession
from automation.artifact.schema import Artifact
from automation.cli.discover import REPO_ROOT, _build_allowlist, _login
from automation.policy.redact import redact_mapping
from automation.replay.engine import DEFAULT_LOCATOR_RETRIES, DEFAULT_LOCATOR_RETRY_DELAY_SECONDS, replay
from automation.replay.result import BusinessOutcome, Failure, Success

DEFAULT_EVIDENCE_ROOT = REPO_ROOT / "evidence" / "runs"


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="replay",
        description="Deterministically replay a saved artifact against a live target -- no LLM in the loop.",
    )
    parser.add_argument("--artifact", required=True, type=Path, help="Path to a saved artifact JSON file.")
    parser.add_argument(
        "--input",
        action="append",
        default=[],
        dest="inputs",
        metavar="NAME=VALUE",
        help="An input parameter, e.g. --input memberId=12345. Repeatable.",
    )
    parser.add_argument("--username", default="replay-agent", help="Username to log in with.")
    parser.add_argument("--password", default="replay-agent", help="Password to log in with.")
    parser.add_argument("--headed", action="store_true", help="Run the browser with a visible window.")
    parser.add_argument("--locator-retries", type=int, default=DEFAULT_LOCATOR_RETRIES)
    parser.add_argument("--locator-retry-delay-seconds", type=float, default=DEFAULT_LOCATOR_RETRY_DELAY_SECONDS)
    parser.add_argument(
        "--evidence-dir",
        type=Path,
        default=None,
        help="Directory to write this replay's evidence into (default: evidence/runs/<timestamp>-replay-<artifact-name>/).",
    )
    parser.add_argument(
        "--allowed-path",
        action="append",
        default=[],
        dest="allowed_paths",
        metavar="PATTERN",
        help="An fnmatch-style path pattern permitted on the artifact's app_target domain. Repeatable. "
        "Defaults to '/login' and '/members/*' if omitted.",
    )
    parser.add_argument(
        "--no-allowlist",
        action="store_true",
        help="Disable allowlist enforcement entirely (not recommended -- see automation.policy.allowlist).",
    )
    parser.add_argument(
        "--allow-unapproved",
        action="store_true",
        help="Permit replaying a risky-classified artifact that has not been approved. "
        "Without this flag, an unapproved risky artifact is refused before any step runs.",
    )
    return parser.parse_args(argv)


def _parse_inputs(pairs: list[str]) -> dict[str, str]:
    inputs: dict[str, str] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ValueError(f'Invalid --input "{pair}" -- expected NAME=VALUE.')
        name, value = pair.split("=", 1)
        inputs[name] = value
    return inputs


def _result_to_dict(result) -> dict:
    if isinstance(result, Success):
        return {"kind": "success", "outputs": result.outputs, "stepsCompleted": result.steps_completed}
    if isinstance(result, BusinessOutcome):
        return {
            "kind": "business_outcome",
            "name": result.name,
            "description": result.description,
            "outputs": result.outputs,
            "stepsCompleted": result.steps_completed,
        }
    if isinstance(result, Failure):
        return {
            "kind": "failure",
            "failureKind": result.kind.value,
            "stepId": result.step_id,
            "expected": result.expected,
            "observed": result.observed,
            "detail": result.detail,
        }
    raise AssertionError(f"unreachable: unhandled result type {result!r}")


def _write_evidence(evidence_dir: Path, artifact: Artifact, inputs: dict, result, final_screenshot: bytes | None) -> None:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    sensitive_names = frozenset(p.name for p in artifact.inputs if p.sensitive)
    log = {
        "artifact": {"name": artifact.name, "version": artifact.version, "id": artifact.id},
        "inputs": redact_mapping(inputs, sensitive_names),
        "result": _result_to_dict(result),
    }
    # Shape-based backstop over the whole log, in case a secret leaked into
    # some field redact_mapping's name-based pass didn't target directly
    # (e.g. echoed into a Failure's `observed` diagnostic text).
    log = redact_mapping(log, sensitive_names)
    (evidence_dir / "log.json").write_text(json.dumps(log, indent=2))
    if final_screenshot is not None:
        (evidence_dir / "final.png").write_bytes(final_screenshot)


def main(argv: list[str] | None = None) -> int:
    load_dotenv(REPO_ROOT / ".env")
    args = parse_args(sys.argv[1:] if argv is None else argv)

    artifact = Artifact.model_validate_json(args.artifact.read_text())
    try:
        inputs = _parse_inputs(args.inputs)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 2

    evidence_dir = args.evidence_dir
    if evidence_dir is None:
        import re as _re
        from datetime import datetime, timezone

        timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        slug = _re.sub(r"[^a-z0-9]+", "-", artifact.name.lower()).strip("-")
        evidence_dir = DEFAULT_EVIDENCE_ROOT / f"{timestamp}-replay-{slug}"

    print(f"Artifact: {artifact.name} v{artifact.version} ({args.artifact})")
    print(f"Inputs:   {inputs}")
    print(f"Evidence will be written to: {evidence_dir}")
    print()

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=not args.headed)
        page = browser.new_page()
        session = BrowserSession(page)

        print("Logging in...")
        _login(session, artifact.app_target.base_url, args.username, args.password)

        allowlist = None if args.no_allowlist else _build_allowlist(artifact.app_target.base_url, args.allowed_paths)

        print("Replaying artifact (no LLM in the loop)...")
        result = replay(
            artifact,
            inputs,
            page,
            locator_retries=args.locator_retries,
            locator_retry_delay_seconds=args.locator_retry_delay_seconds,
            allowlist=allowlist,
            allow_unapproved=args.allow_unapproved,
        )

        final_screenshot = page.screenshot()
        browser.close()

    _write_evidence(evidence_dir, artifact, inputs, result, final_screenshot)

    print()
    result_dict = _result_to_dict(result)
    print(f"Result kind: {result_dict['kind']}")
    print(json.dumps(result_dict, indent=2))
    print(f"Evidence written to: {evidence_dir}")

    return 0 if isinstance(result, (Success, BusinessOutcome)) else 1


if __name__ == "__main__":
    raise SystemExit(main())
