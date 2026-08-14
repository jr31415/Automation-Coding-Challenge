# Computer-Use Automation System

An LLM-driven agent discovers how to complete a goal in a legacy, no-API back-office web app, records what it did as a typed, versioned **artifact**, and that artifact replays **deterministically** — no LLM in the loop — with parameterized inputs, typed outputs, and explicit error handling. When the agent can't safely proceed, it escalates to a human who takes over the *same live browser session* and hands control back.

See [REPORT.md](REPORT.md) for the design write-up (architecture, artifact schema, determinism & error handling, heterogeneity/multi-tenant story, escalation model, safety model, and cuts).

## Repo layout

```
mock-app/     Target application: a small, deliberately legacy-styled credit-union
              back-office admin console (Express/TypeScript). Table-based markup,
              no test IDs, session timeouts, and injectable error states.
automation/   The automation system itself (Python). Discovery agent, artifact
              schema + recorder, deterministic replay engine, safety/policy
              guardrails, human escalation & handoff.
artifacts/    Saved capability artifacts (typed JSON), produced by discovery runs.
evidence/     Evidence of real runs: structured logs, screenshots.
```

## Setup

### 1. Mock app (Node.js)

```bash
cd mock-app
npm install
npm start          # listens on http://localhost:4000
```

Leave this running in its own terminal — the automation system drives it over HTTP like any real target.

### 2. Automation system (Python, via [uv](https://docs.astral.sh/uv/))

```bash
cd automation
uv sync --extra dev
uv run playwright install chromium
```

You'll need an Anthropic API key for the discovery agent (replay never calls the LLM, so it works without one). Put it in a `.env` file at the **repo root** (not inside `automation/`):

```bash
# from the repo root
cp .env.example .env
# edit .env and set ANTHROPIC_API_KEY=sk-...
```

### Running commands

`uv run discover` / `uv run replay` are registered as console scripts and are the normal way to run this. **If your checkout path contains spaces** (true of this repo's own default location under `.../Documents/GitHub/Automation Coding Challenge/...`), uv's editable-install `.pth` file can get silently truncated and drop the package off `sys.path`, causing `ModuleNotFoundError: No module named 'automation'`. If you hit that, use this equivalent form instead, which sidesteps it entirely:

```bash
cd automation
PYTHONPATH=src uv run --no-sync python -m automation.cli.discover --goal "..."
PYTHONPATH=src uv run --no-sync python -m automation.cli.replay --artifact ... --input ...
```

The commands below are written as `uv run discover ...` / `uv run replay ...` for readability — substitute the `PYTHONPATH=src ... python -m automation.cli.<name>` form if you see that error.

## Demo path

With the mock app running (`http://localhost:4000`) and `ANTHROPIC_API_KEY` set:

### 1. Run discovery (real LLM, real browser)

```bash
cd automation
uv run discover --goal "look up member 12345 and read their current savings balance" \
  --capability-name lookup_member_savings_balance
```

This logs in, drives the mock app with Claude Sonnet 5 making every click/type/extract decision, and on success:
- writes a structured step log + screenshot to `evidence/runs/<timestamp>-.../`
- saves a versioned artifact to `artifacts/lookup_member_savings_balance.v1.json`

The mock app data supports member IDs `12345`, `67890`, and `11111` (a restricted account — good for exercising the escalation path; see below).

The browser launches **headed** by default (pass `--headless` to suppress it) — this is deliberate: a visible window with an open CDP debugging port is what makes real human handoff possible if the agent gets stuck (see "Escalation" below).

### 2. Replay it deterministically (no LLM)

```bash
uv run replay --artifact ../artifacts/lookup_member_savings_balance.v1.json --input value=67890
```

Same artifact, a different member — the locator strategy (role/label-based, not the literal value seen during discovery) and the templated success condition both generalize. Replay logs in, executes the recorded steps directly via Playwright, and reports a structured `Success` / `BusinessOutcome` / `Failure` result plus evidence.

### 3. See an error path handled explicitly

```bash
uv run replay --artifact ../artifacts/lookup_member_savings_balance.v1.json --input value=99999
```

Member `99999` doesn't exist. Replay doesn't crash — it reports a clean, structured `Failure` (step, expected, observed) you can inspect in the printed result and in `evidence/runs/.../log.json`.

### 4. See escalation (agent gets stuck, hands off to a human)

```bash
uv run discover --goal "open a new sub-account for member 11111 with a \$50 initial deposit"
```

Member `11111` has a restricted account — the mock app blocks the action with an explicit permission-denied banner. The agent recognizes it can't safely proceed and calls `stuck` instead of guessing. Discovery then:
- prints an intervention request (goal, reason, current URL, screenshot, and a **real, live CDP websocket URL**)
- leaves the browser open and paused — a human could literally attach to it (`chrome://inspect`, or `playwright.chromium.connect_over_cdp(...)`) and see the exact same session, not a fresh one
- blocks on a terminal prompt for the operator to record what they did and signal resume/abort

Pass `--no-interactive-escalation` to see the intervention reported without blocking (useful for CI/scripted runs).

## Tests

```bash
cd automation
uv run pytest          # 234 tests, real headless-browser + real logic, no API key needed
```

Every module (accessibility-tree snapshotting, tool execution, the agent loop, artifact recording, locator resolution, checkpoint evaluation, the replay engine, policy/allowlist/redaction, escalation) has dedicated tests, largely run against a real (headless) Chromium instance rather than mocks, since the interesting bugs in this system live in real browser/DOM behavior.

## What's mocked / cut

- **Operator console**: a terminal prompt, not a real co-browsing UI — the brief explicitly allows this; what's real is the underlying pause/expose-live-session/resume mechanism (see REPORT.md § Escalation & handoff).
- **`known_outcomes`** (declared business-outcome detection, e.g. "member not found" as a first-class result rather than a `LOCATOR_NOT_FOUND` failure) isn't populated on the demo artifact — the recorder only knows what happened in the one successful run it's recording from. See REPORT.md § Cuts.
- **Multi-tenant / desktop-surface support**: designed, not built — see REPORT.md § Heterogeneity & multi-tenant.
