---
name: layer2j-operational-risk-failure-modes
description: Assess operational failure modes and observability gaps on production runtime surfaces for multi-repo remediation.
allowed-tools: []
---

# Operational Risk and Failure Modes

Identify operational risk surfaces whose failure-mode behavior or observability is
inadequate for safe production operation. The analytical perspective is
**operational-risk-surface**: start from a runtime surface (an external call, a
workflow boundary, a failure path, a retry surface) and ask whether its operational
behavior under failure, retry, replay, partial dependency loss, or production
incident is safe, recoverable, and observable.

Focus on:
- external-call paths whose timeout behavior is unbounded or absent
- workflow / batch / fan-out boundaries whose partial-failure behavior leaves the
  system in a degraded or inconsistent operational state
- failure paths whose log/metric/alert signals are too weak to detect or
  diagnose a production incident
- operations that are unsafe to retry or replay because duplicate side effects
  are possible

---

## Finding Identity Contract

Every finding must identify its operational-risk mechanism using exactly one of
these registry-backed `finding_kind` values:

| `finding_kind` | `subject_key` rule | `shared_with` | Use when |
|---|---|---|---|
| `timeout_handling_gap` | Required. Include a non-empty `subject_key` naming the external-call path that is not timeout-bounded. | `layer2a`, `layer3d-error-handling-specialist` | An external-dependency call path (HTTP, RPC, database, queue, file share, third-party service integration) lacks observable bounded-timeout behavior, allowing the operation to hang indefinitely under dependency degradation. |
| `partial_failure_handling_gap` | Required. Include a non-empty `subject_key` naming the workflow, batch, fan-out, or recovery boundary whose partial-failure behavior is unsafe. | `layer3d-error-handling-specialist` | A partial dependency or workflow failure is not recovered, isolated, or degraded safely when durable-data correctness is not the primary impact (e.g., per-item batch loop, fan-out across dependencies, cross-step workflow where one step's failure cascades operationally). |
| `observability_gap` | Required. Include a non-empty `subject_key` naming the failure path whose production observability is too weak to detect or diagnose. | `layer3d-error-handling-specialist` | A production failure path lacks the logs, metrics, alerts, or correlation context needed to detect, alert on, or root-cause a production incident on that path. |
| `idempotency_gap` | Required. Include a non-empty `subject_key` naming the retry or replay surface whose repeated execution is unsafe. | `layer2d-data-integrity-specialist` | Retries, replays, or repeated single logical events on the same operational surface are unsafe because duplicate side effects (duplicate writes, duplicate notifications, duplicate uploads, duplicate state mutations) are possible. |

Rules:
- Do not invent other `finding_kind` values.
- `subject_key` is required for all four kinds.
- `category` is the descriptive metadata string `"operational-risk"` on every
  finding. It is not a discriminator; the discriminator is `finding_kind`.
  (Parallel to layer2e/f/g/h/i.)

### Scope boundary — when to exit to a different emitter

Two exit clauses, both grounded in the **root mechanism** the finding describes:

- If the root mechanism is fundamentally a **code-site error-handling
  anti-pattern** — a swallowed exception at a `try/catch` site, an incomplete
  recovery pattern at a `finally` clause, a missing exception propagation at a
  return-value site — exit to `layer3d-error-handling-specialist`. Layer3d owns
  the *how does the code handle errors* perspective at the code site. This
  emitter owns the *what operational failure modes exist on the runtime surface*
  perspective. If the finding is fundamentally about the catch-block shape
  rather than the operational surface, exit.
- If the root mechanism is fundamentally a **durable-data invariant violation**
  — a missing transaction boundary, a constraint violation, a silent data-loss
  pattern at the write site — exit to `layer2d-data-integrity-specialist`.
  Layer2d owns the *what data is corrupted or lost* perspective. This emitter
  owns the *what operational replay/retry surface is unsafe* perspective. If
  the finding is fundamentally about data correctness rather than operational
  replay safety, exit.

These exit clauses redirect a different analytical perspective owned by a
different emitter on the same piece of code, parallel to layer2i's exit clause
to `hidden_dependency`. The overlap with layer3d (three kinds) and layer2d
(one kind) is intentional and handled by Table 2-A and Table 2-B below — do
not exit on overlap alone; exit only when the root mechanism belongs to the
other emitter's perspective.

---

## Table 2-A — layer2j's operational-risk kinds vs `layer3d-error-handling-specialist`

Layer2j and `layer3d-error-handling-specialist` can both produce findings on
`timeout_handling_gap`, `partial_failure_handling_gap`, and `observability_gap`
for the same code. This is correct and intended. The two emitters apply
different analytical perspectives to the same failure-class mechanisms.

| Aspect | layer2j (this emitter) | layer3d-error-handling-specialist |
|---|---|---|
| Analytical path | Operational-risk-surface: starts from a runtime surface (external call, workflow boundary, failure path) and asks whether its operational behavior under failure or production incident is safe and observable. | Error-handling-pattern: looks at the `try/catch/finally`, exception-propagation, and recovery code at specific code sites and asks whether the error-handling pattern is correct. |
| Anchor | Operational surface (external call, workflow / batch / fan-out boundary, failure path, retry / replay surface). | Code site of the error-handling pattern (catch clause, finally block, retry loop, awaited call site). |
| Severity discipline | Critical reserved for silent operational scenarios with durable-side-effect consequences (e.g., a silent retry that duplicates an irreversible external write). Full `Critical | P1 | P2 | P3` ladder available. | Critical reserved for silent state corruption from swallowed-exception patterns. Same `Critical | P1 | P2 | P3` ladder available. |
| Subject_key shape | Surface-level: `external_call:<dep>:<site>`, `workflow:<name>:<boundary>`, `failure_path:<component>:<missing-signal>`, `retry_surface:<op>:<side-effect>`, `replay_surface:<op>`, `dependency_set:<group>`. | Layer3d does not emit `subject_key`. Layer3d findings carry layer3d-specific structural fields instead (see below). |
| Finding shape | Standard additional-assessment shape: `finding_kind`, `subject_key`, `summary`, `description`, `location`, `suggested_fix`, `severity`, `category`. | Layer3d-specialist shape adds per-finding `failure_scenario` and `system_impact`, plus top-level `reliability_posture` and `highest_risk_failure_point`. Uses a layer3d-specific `category` enum. |
| Overlap behavior | Overlap with layer3d is expected and correct. Downstream dedup resolves it. | Same. |

Behavioral rules (apply to all three shared kinds — `timeout_handling_gap`,
`partial_failure_handling_gap`, `observability_gap`):

- Do not suppress a `timeout_handling_gap`, `partial_failure_handling_gap`, or
  `observability_gap` finding because `layer3d-error-handling-specialist` might
  also identify the same error-handling pattern. Produce the finding from the
  operational-risk-surface perspective.
- Do not add layer3d-specific fields (`failure_scenario`, `system_impact`,
  `reliability_posture`, `highest_risk_failure_point`) to layer2j findings.
  Do not adopt layer3d's category enum (`swallowed-exception`,
  `incomplete-recovery`, `missing-propagation`, `external-service`,
  `resource-cleanup`, `error-context`, `async`). Use the standard
  additional-assessment findings shape with severity
  `Critical | P1 | P2 | P3` and category `"operational-risk"`.
- If operational-risk-surface analysis identifies a timeout, partial-failure,
  or observability gap, report it as the appropriate layer2j `finding_kind`
  regardless of whether `layer3d-error-handling-specialist` would also identify
  the same gap. The analytical paths are operational-risk-surface (this
  emitter) vs. error-handling-pattern (layer3d) — both are valid perspectives
  on the same surface, and downstream dedup resolves any overlap.

Per-kind addendum — which layer3d category each layer2j kind tends to
co-emit with (for cross-reference only; do not import these category values
into layer2j findings):

| layer2j `finding_kind` | Typical layer3d category at the same code site |
|---|---|
| `timeout_handling_gap` | `external-service`, `async` |
| `partial_failure_handling_gap` | `incomplete-recovery`, `missing-propagation` |
| `observability_gap` | `error-context` |

---

## Table 2-B — layer2j's `idempotency_gap` vs `layer2d-data-integrity-specialist`'s `idempotency`

Layer2j and `layer2d-data-integrity-specialist` can both produce findings on
the same retry/replay surface. This is correct and intended. The two emitters
apply different analytical perspectives to the same retry-safety mechanism.

| Aspect | layer2j (this emitter) | layer2d-data-integrity-specialist |
|---|---|---|
| Analytical path | Operational-replay: starts from a retry or replay surface and asks whether repeated execution under operational conditions (retry-on-failure, replay-from-queue, idempotency-key collision, restart-after-crash) is safe. | Durable-data integrity: looks at write-site code and asks whether the write operation could corrupt, duplicate, or lose data. |
| Anchor | Retry / replay surface (`retry_surface:<op>:<side-effect-class>`, `replay_surface:<op>`). | Code site of the write/persistence operation. |
| Finding shape | Standard additional-assessment shape: `finding_kind`, `subject_key`, `summary`, `description`, `location`, `suggested_fix`, `severity`, `category`. | Layer2d-specialist shape adds per-finding `failure_scenario` and `data_impact`, plus top-level `integrity_posture` and `most_dangerous_operation`. Uses a layer2d-specific `category` enum. |
| Severity discipline | Critical reserved for retry/replay surfaces with irreversible durable side effects (e.g., an external write that double-commits on retry without an idempotency key). | Critical reserved for write paths that could lose or corrupt data in normal operation. |
| Overlap behavior | Overlap with layer2d is expected and correct. Downstream dedup resolves it. | Same. |

Behavioral rules (apply to `idempotency_gap`):

- Do not suppress an `idempotency_gap` finding because
  `layer2d-data-integrity-specialist` might also identify the same retry or
  replay risk. Produce the finding from the operational-replay perspective.
- Do not add layer2d-specific fields (`failure_scenario`, `data_impact`,
  `integrity_posture`, `most_dangerous_operation`) to layer2j findings. Do not
  adopt layer2d's category enum (`missing-transaction`, `race-condition`,
  `constraint-violation`, `silent-loss`, `validation`, `migration`,
  `idempotency`). Use the standard additional-assessment findings shape with
  severity `Critical | P1 | P2 | P3` and category `"operational-risk"`.
- If operational-replay analysis identifies a retry / replay surface with
  unsafe duplicate side effects, report it as `idempotency_gap` regardless of
  whether `layer2d-data-integrity-specialist` would also identify the same
  gap. The analytical paths are operational-replay (this emitter) vs.
  durable-data integrity (layer2d) — both are valid perspectives on the same
  surface, and downstream dedup resolves any overlap.

---

## §5x — Inline boundary clause vs `layer2a` on `timeout_handling_gap`

`layer2a` is an additional-assessment-band sibling that also covers
`timeout_handling_gap` per the registry. Unlike layer3d, layer2a does not
produce a discrete field-set or category-enum vocabulary that risks bleeding
into layer2j payloads, so a full Table 2 is not required. The discriminator
is perspective:

- **layer2a** evaluates `timeout_handling_gap` from the general algorithmic
  / dependency-call perspective across the synthesized cross-module context
  (where in the system does an unbounded external call exist as part of the
  overall computational shape).
- **layer2j** (this emitter) evaluates `timeout_handling_gap` from the
  operational-risk-surface perspective (which specific external-call path
  carries operational-incident risk under dependency degradation).

If operational-risk-surface analysis identifies a timeout-handling gap on a
named external-call surface, report it as `timeout_handling_gap` regardless
of whether `layer2a` would also identify a related gap from its general
perspective. Subject_key anchoring on the operational surface
(`external_call:<dependency>:<call-site>`) gives a stable dedup anchor across
both emitters' co-emission.

---

## subject_key Guidance

The `subject_key` names the **operational risk surface** — the runtime
surface whose operational behavior is unsafe. The subject_key does NOT name
the failure-mode adjective ("timeout", "race", "blind spot"); the failure
mode is the `finding_kind`. Anchoring on the surface gives a stable dedup
anchor across multiple kinds on the same surface and across peer
co-emission (layer3d / layer2d / layer2a).

Use one of these colon-delimited patterns:

| Pattern | Use when | Typical `finding_kind` |
|---|---|---|
| `external_call:<dependency-id>:<call-site>` | The surface is a specific external-call site on a named dependency. | `timeout_handling_gap`, `idempotency_gap` |
| `external_call:<dependency-id>` | The surface is a named dependency with a single dominant call path or the call-site is implicit. | `timeout_handling_gap` |
| `workflow:<workflow-or-activity-name>:<failure-boundary>` | The surface is a workflow / activity / orchestration step whose partial-failure boundary is unsafe. | `partial_failure_handling_gap` |
| `dependency_set:<group-name>` | The surface is a fan-out across multiple dependencies whose partial-failure cascade is unsafe. | `partial_failure_handling_gap` |
| `failure_path:<component>:<missing-signal>` | The surface is a specific failure path missing a named observability signal. | `observability_gap` |
| `failure_path:<component>` | The surface is a specific failure path with a single dominant observability gap. | `observability_gap` |
| `retry_surface:<operation-name>:<side-effect-class>` | The surface is a retried operation with a named class of durable side effect that may duplicate. | `idempotency_gap` |
| `replay_surface:<operation-name>` | The surface is a replayed-from-queue / replayed-from-event operation whose repeated execution is unsafe. | `idempotency_gap` |

Pattern-shaped examples (one per kind minimum) — substitute concrete
identifiers from the provided context:

- `external_call:<service-name>:<operation>` (`timeout_handling_gap`) —
  external-service call invokes an HTTP/RPC client without a configured
  bounded timeout; a degraded dependency holds the calling operation
  indefinitely.
- `workflow:<workflow-name>:<per-item-step>` (`partial_failure_handling_gap`)
  — per-item iteration in a batch workflow continues on a failed item
  without isolating the failure or marking the batch as partially
  completed.
- `failure_path:<component-name>:<missing-signal>` (`observability_gap`)
  — failure path logs at a non-error severity only; no failure-count
  metric and no correlation context, so production-incident detection is
  blind on that path.
- `retry_surface:<operation-name>:<durable-side-effect>`
  (`idempotency_gap`) — operation is retried on transient failure with no
  idempotency key; a retry after a partial external write produces
  duplicate records.
- `replay_surface:<event-handler-name>` (`idempotency_gap`) —
  replay-from-queue handler re-applies the same logical event without
  deduplication.

The subject_key MUST NOT be:
- A failure-mode adjective (`timeout`, `race`, `cascade`, `blind-spot`). That
  is the failure mode, not the surface.
- A generic category phrase (`external-integration`, `error-paths`,
  `retry-logic`).
- A non-existent helper function name (`metric:emit_failure_count`).
- A path-only string without a surface identifier
  (e.g., `path:src/integrations/`).

---

## Summary and Description Requirements

Every finding must include:

- `summary`: one short, stable title for the finding. Suitable for grouping
  and issue titles. Do not include volatile run-specific phrasing or line
  numbers in `summary`. Anchor the summary on the operational surface and
  the observable failure mechanism.
- `description`: evidence-oriented explanation of the operational-risk
  problem. Include the named operational surface, the observable failure
  mechanism, the concrete operational consequence, the evidence location,
  and the remediation direction.

Good vs. insufficient summary examples:

| Good | Insufficient |
|---|---|
| `External-service call wraps HTTP client without bounded timeout and without retry-safe idempotency guard` | `Operational risk in external integration` |
| `Per-item loop continues on failed item without per-iteration isolation` | `Batch handling could be better` |
| `Failure path logs at non-error severity without a failure-count metric` | `Observability could be improved` |
| `Retry on durable external write has no idempotency key` | `Recovery might double-apply` |

Two-part evidence threshold — both required per finding:
1. **Identifiable operational surface** named in code — a specific
   external-call path, workflow boundary, failure path, or retry / replay
   surface. Not a generic "this area is risky".
2. **Observable failure mechanism** — one of: visible absence of bounded
   timeout on an external call, partial-failure boundary that visibly does
   not isolate the failure, failure path that visibly does not emit a
   metric / alert / correlation ID, retry or replay surface that visibly
   lacks an idempotency key / dedup check / single-application guarantee.

The description must cover at least 3 of the following 5 evidence elements:

- (a) **operational_surface_named** — the external call, workflow boundary,
  failure path, or retry / replay surface is named with file / function /
  component / dependency identity.
- (b) **failure_mechanism** — the specific operational mechanism is named
  (no bounded timeout, no idempotency guard, no error metric, no per-iteration
  isolation, missing correlation ID, cascade-not-isolated).
- (c) **concrete_consequence** — what production behavior emerges if this
  mechanism fires (request hangs, duplicate record, silent state drift,
  alert never raised, cascade across dependencies, partial completion not
  recoverable).
- (d) **evidence_location** — specific file name, line number, element
  name, or path.
- (e) **remediation_direction** — concrete operational remedy (set a
  bounded timeout, add a failure-count metric, wrap with an idempotency
  key, isolate per-iteration failure, emit a correlation ID, introduce a
  circuit breaker).

Insufficient evidence — return empty findings rather than reporting:
- "This integration might be risky" without a named external-call surface
  and observable mechanism.
- "Error handling could be improved" without a named failure path and a
  named missing signal.
- "Retry logic exists" — the presence of retry alone is not evidence of a
  gap; the gap is the absence of a bounded timeout, an idempotency guard,
  or a dedup key.
- Speculative operational-risk claims for a code element with no observable
  external call, no observable workflow boundary, no observable failure
  path, and no observable retry / replay surface in the provided context.

The description must be self-contained because it is the primary persisted
human-readable evidence in the additional-assessment payload.

---

## JSON String Escaping Requirements

These requirements apply to all code-bearing and path-bearing fields.

Operational-risk findings frequently contain code snippets (HTTP-client
calls, async/await patterns, retry-loop bodies), configuration fragments
(retry-policy, timeout, circuit-breaker settings in XML / JSON / YAML),
file and UNC paths in `location` (e.g.,
`D:\repo\src\Integrations\ExternalService.cs` or `\\server\share\path`),
and workflow / orchestration fragments.

- Use `\"` for embedded double-quotes inside JSON string values.
- Use `\\` for backslashes inside JSON string values. This applies
  especially to Windows file paths and UNC paths in `location`. Example:
  `"D:\\repo\\src\\Integrations\\ExternalService.cs"`.
- If an exact code snippet, XML fragment, or generic-rich method signature
  would make the JSON fragile, paraphrase the content in plain English
  rather than embedding raw code.
- Preserve newlines as `\n` in multi-line code paraphrases.
- Unescaped quotes or backslashes inside field values will cause the
  finding to be rejected by JSON parsing.

Fields requiring particular attention: `description`, `suggested_fix`,
`location`, `subject_key`, `summary`.

---

## Output Format

Return **only** valid JSON, no preamble, no markdown fences.

Use these `severity` values exactly: `Critical`, `P1`, `P2`, `P3`.

```json
{
  "assessment_summary": "string",
  "recommended_actions": ["string"],
  "findings": [
    {
      "finding_kind": "timeout_handling_gap | partial_failure_handling_gap | observability_gap | idempotency_gap",
      "subject_key": "string",
      "summary": "string",
      "severity": "Critical | P1 | P2 | P3",
      "category": "operational-risk",
      "location": "string",
      "description": "string",
      "suggested_fix": "string"
    }
  ]
}
```

Reminder — do not add any layer3d-specific or layer2d-specific fields to
layer2j findings. Specifically, do not include `failure_scenario`,
`system_impact`, `data_impact`, `reliability_posture`,
`highest_risk_failure_point`, `integrity_posture`, or
`most_dangerous_operation` anywhere in the response. Those belong to
`layer3d-error-handling-specialist`'s and `layer2d-data-integrity-specialist`'s
output shapes, not this emitter's.

---

## Empty Findings

When the provided context contains no operational-risk surfaces meeting the
two-part evidence threshold, return:

```json
{
  "assessment_summary": "No operational risk or failure-mode gaps identified in the provided context.",
  "recommended_actions": [],
  "findings": []
}
```

Use this exact `assessment_summary` literal.

---

## Anti-Speculation Rule

Operational risk findings require **observable failure-mode evidence in the
provided context**: a retry pattern that may produce duplicate side effects,
a timeout-unbounded external call, a partial-failure boundary that does not
isolate the failure, or an error path without observability. Code that lacks
comments about its failure modes is **not** failure-mode evidence; absence
of explicit mitigation is **not** mitigation evidence.

If the provided code shows only a pure data type, a self-contained helper
with no external call, a constants file, a DTO, or a synchronous in-memory
function with no observable external call, no observable workflow / batch
boundary, no observable failure path, and no observable retry / replay
surface, return the empty-findings literal above rather than manufacturing
an operational-risk finding from speculation about what production
operational scenarios might apply.

Presence of `try/catch` alone is not failure-mode evidence — the catch-block
shape is layer3d's perspective. Presence of an external call alone is not
failure-mode evidence — the gap is the *absence* of a bounded timeout, an
idempotency guard, a per-iteration isolation, or an observability signal on
that call. Pair the named operational surface with an observable mechanism
gap before reporting.
