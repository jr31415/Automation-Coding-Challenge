import { z } from "zod";

// ---------------------------------------------------------------------------
// Locators: how a step finds its target control on replay.
// Ordered fallback chain -- replay tries strategies in order until one
// resolves to exactly one element. Every strategy is legacy-DOM-safe: none
// depend on CSS classes/ids/test-ids, which legacy bank UIs never have.
// ---------------------------------------------------------------------------

export const LocatorStrategySchema = z.discriminatedUnion("kind", [
  z.object({
    kind: z.literal("role"),
    role: z.string(), // ARIA role, e.g. "textbox", "button", "combobox"
    accessibleName: z.string(), // visible label / accessible name
  }),
  z.object({
    kind: z.literal("text"),
    text: z.string(), // visible text content (exact or substring, see `exact`)
    exact: z.boolean().default(true),
  }),
  z.object({
    kind: z.literal("labelFor"),
    label: z.string(), // <td>Label:</td> style association by adjacency/label text
  }),
  z.object({
    kind: z.literal("structural"),
    // Last-resort fallback: tag + nth occurrence within a named container.
    // Recorded with explicit reasoning because it's the most drift-prone strategy.
    tag: z.string(),
    nth: z.number().int().min(0),
    withinContainerText: z.string().optional(),
  }),
]);
export type LocatorStrategy = z.infer<typeof LocatorStrategySchema>;

export const TargetLocatorSchema = z.object({
  // Ordered fallback chain: try [0], then [1], ... until one resolves uniquely.
  strategies: z.array(LocatorStrategySchema).min(1),
  // Why this chain was chosen -- captured at discovery time for human review.
  reasoning: z.string(),
});
export type TargetLocator = z.infer<typeof TargetLocatorSchema>;

// ---------------------------------------------------------------------------
// Steps: the ordered actions that make up the flow.
// ---------------------------------------------------------------------------

export const ActionSchema = z.discriminatedUnion("type", [
  z.object({
    type: z.literal("navigate"),
    url: z.string(), // may contain {{paramName}} template refs, resolved against inputs
  }),
  z.object({
    type: z.literal("click"),
    target: TargetLocatorSchema,
  }),
  z.object({
    type: z.literal("type"),
    target: TargetLocatorSchema,
    value: z.string(), // literal or "{{paramName}}" template ref
    sensitive: z.boolean().default(false), // if true, value is never logged verbatim
  }),
  z.object({
    type: z.literal("select"),
    target: TargetLocatorSchema,
    value: z.string(),
  }),
  z.object({
    type: z.literal("waitFor"),
    condition: CheckpointConditionRef(),
    timeoutMs: z.number().int().positive().default(10_000),
  }),
  z.object({
    type: z.literal("extract"),
    target: TargetLocatorSchema,
    outputKey: z.string(), // maps to a key in the artifact's outputs shape
  }),
]);
export type Action = z.infer<typeof ActionSchema>;

function CheckpointConditionRef() {
  // Forward reference resolved below; kept as a function to avoid TDZ issues
  // with zod's schema graph.
  return CheckpointConditionSchema;
}

export const StepSchema = z.object({
  id: z.string(),
  description: z.string(), // human-readable summary, shown to reviewers
  action: ActionSchema,
  // Optional per-step checkpoint: assert this before considering the step done.
  postCondition: z.lazy(() => CheckpointConditionSchema).optional(),
});
export type Step = z.infer<typeof StepSchema>;

// ---------------------------------------------------------------------------
// Checkpoints: conditions asserted to confirm state, not assumed from an
// action "succeeding". Used both mid-flow (postCondition) and as the final
// success condition.
// ---------------------------------------------------------------------------

export const CheckpointConditionSchema = z.discriminatedUnion("kind", [
  z.object({ kind: z.literal("urlMatches"), pattern: z.string() }), // regex source
  z.object({ kind: z.literal("elementVisible"), target: TargetLocatorSchema }),
  z.object({ kind: z.literal("elementText"), target: TargetLocatorSchema, equals: z.string() }),
  z.object({ kind: z.literal("elementTextContains"), target: TargetLocatorSchema, substring: z.string() }),
]);
export type CheckpointCondition = z.infer<typeof CheckpointConditionSchema>;

// ---------------------------------------------------------------------------
// Known outcomes: business-legitimate non-success results the flow can reach
// on purpose (e.g. "member not found"). Distinguished from hard failures.
// Detected the same way as the success checkpoint: an assertable condition.
// ---------------------------------------------------------------------------

export const KnownOutcomeSchema = z.object({
  name: z.string(), // e.g. "member_not_found", "permission_denied"
  description: z.string(),
  detect: CheckpointConditionSchema,
  // What (if anything) to extract/return to the caller when this outcome fires.
  outputs: z.record(z.string(), z.string()).optional(), // outputKey -> literal or target ref not needed; kept simple/static
});
export type KnownOutcome = z.infer<typeof KnownOutcomeSchema>;

// ---------------------------------------------------------------------------
// Typed I/O contract.
// ---------------------------------------------------------------------------

export const ParamTypeSchema = z.enum(["string", "number", "boolean"]);

export const InputParamSchema = z.object({
  name: z.string(),
  type: ParamTypeSchema,
  required: z.boolean().default(true),
  description: z.string(),
  sensitive: z.boolean().default(false), // e.g. credentials -- never persisted into logs/evidence
});
export type InputParam = z.infer<typeof InputParamSchema>;

export const OutputFieldSchema = z.object({
  name: z.string(),
  type: ParamTypeSchema,
  description: z.string(),
});
export type OutputField = z.infer<typeof OutputFieldSchema>;

// ---------------------------------------------------------------------------
// App target metadata -- supports the multi-tenant reuse story (see REPORT.md):
// an artifact is recorded against a vendor product + version, not a tenant.
// ---------------------------------------------------------------------------

export const AppTargetSchema = z.object({
  vendorProduct: z.string(), // e.g. "riverbend-core-admin"
  baseUrl: z.string(),
  minVersion: z.string().optional(),
  maxVersion: z.string().optional(),
});
export type AppTarget = z.infer<typeof AppTargetSchema>;

// ---------------------------------------------------------------------------
// The artifact itself.
// ---------------------------------------------------------------------------

export const ArtifactSchema = z.object({
  schemaVersion: z.literal("1.0"),
  id: z.string(),
  name: z.string(), // capability name, e.g. "open_sub_account"
  version: z.number().int().min(1),
  description: z.string(),
  appTarget: AppTargetSchema,

  inputs: z.array(InputParamSchema),
  outputs: z.array(OutputFieldSchema),

  steps: z.array(StepSchema).min(1),
  successCondition: CheckpointConditionSchema,
  knownOutcomes: z.array(KnownOutcomeSchema).default([]),

  // Risk classification drives guardrail handling on replay (see src/policy).
  riskLevel: z.enum(["safe", "sensitive", "risky"]),

  // Provenance: how this artifact came to exist, for human review.
  discoveredAt: z.string(), // ISO timestamp
  discoveredFromGoal: z.string(),
  approvalState: z.enum(["draft", "approved"]).default("draft"),
});
export type Artifact = z.infer<typeof ArtifactSchema>;
