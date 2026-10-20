---
name: layer2i-change-impact-testability
description: Assess change impact blast radius and verification-surface weakness for multi-repo remediation.
allowed-tools: []
---

# Change Impact and Testability

Identify change surfaces whose blast radius is too large for the available
verification surface. The analytical perspective is **blast-radius-from-change**:
start from a code element whose change would propagate widely and ask whether
the verification seams in scope are adequate to make that change safely.

Focus on:
- downstream callers and impacted components for a given change surface
- inadequate test seams or integration-test boundaries for a change surface
- contract publication points whose change blast radius is unbounded
- planned migrations or `[Obsolete]` elements with no removal-verification plan

---

## Finding Identity Contract

Every finding must identify its change-readiness mechanism using exactly this
registry-backed `finding_kind` value:

| `finding_kind` | `subject_key` rule | `shared_with` | Use when |
|---|---|---|---|
| `change_verification_gap` | Required. Include a non-empty `subject_key` naming the change surface (activity, method, module, integration point, contract, or migration step) whose blast radius exceeds available verification. | `layer3c-testability-specialist` | A change surface has observable broad downstream coupling, integration breadth, or published-contract reach, combined with observable absence of branch coverage, integration-test boundary, contract test, or removal-verification plan. |

Rules:
- Do not invent other `finding_kind` values.
- `subject_key` is required for `change_verification_gap`.
- `category` is the descriptive metadata string `"change-impact-testability"`
  on every finding. It is not a discriminator; the discriminator is
  `finding_kind`. (Parallel to layer2e/f/g/h.)

### Scope boundary — when to exit to a different emitter

The close-call pair for this emitter is `change_verification_gap` vs.
`hidden_dependency`. Per registry §6, the discriminator is the **root
mechanism**:

- `change_verification_gap` (this emitter, and also layer3c): the root
  mechanism is *blast radius without adequate verification surface*. The
  code could be tested in principle, but the verification surface in scope
  is too weak relative to the change blast radius.
- `hidden_dependency` (**layer3c-exclusive — not in this emitter's
  registry**): the root mechanism is *implicit collaboration with time,
  randomness, environment, network, disk, or global state* that prevents
  writing any test, regardless of blast radius.

Exit rule:
- If the root mechanism is implicit collaboration with time, randomness,
  environment, network, disk, or global state preventing any test, exit to
  `hidden_dependency` — emitted by `layer3c-testability-specialist`, not by
  this emitter. Return empty findings for that code rather than re-encoding
  the issue as `change_verification_gap`.

This exit rule differs in nature from layer2g's and layer2h's exit rules.
Layer2g/2h exit rules redirect the same analytical perspective to a sibling
emitter that owns a different mechanism. This exit rule redirects to a
different analytical perspective owned by a different emitter on the same
piece of code.

---

## Table 2 — layer2i's `change_verification_gap` vs `layer3c-testability-specialist`'s `change_verification_gap`

Both emitters can produce `change_verification_gap` for the same code. This
is correct and intended. The two emitters apply different analytical
perspectives to the same change-readiness mechanism.

| Aspect | layer2i (this emitter) | layer3c-testability-specialist |
|---|---|---|
| Analytical path | Change-impact tracing: starts from a proposed or anticipated change and traces downstream blast radius vs. available verification seams. | Structural testability analysis: looks at existing code in isolation and asks whether it can be tested without restructuring. |
| Trigger | High-blast-radius change surface (broad downstream coupling, integration boundary, contract publication, planned migration) with inadequate verification. | Code that cannot be tested in isolation regardless of change context (tight coupling, missing seams, non-determinism, boundary problems, test-hostile structure). |
| Finding shape | Standard additional-assessment shape: `finding_kind`, `subject_key`, `summary`, `description`, `location`, `suggested_fix`, `severity`, `category`. | Layer3c-specialist shape adds per-finding `test_that_fails` and `tdd_signal`, plus top-level `testability_score`, `estimated_test_coverage_blocker`, and `quick_wins`. Uses a layer3c-specific `category` enum and restricted severity set. |
| Overlap behavior | Overlap with layer3c is expected and correct. Downstream dedup resolves it. | Same. |

Behavioral rules:
- Do not suppress a `change_verification_gap` finding because
  `layer3c-testability-specialist` might also identify the same verification
  gap. Produce the finding from the change-impact tracing perspective.
- Do not add layer3c-specific fields (`test_that_fails`, `tdd_signal`,
  `testability_score`, `estimated_test_coverage_blocker`, `quick_wins`) to
  layer2i findings. Do not adopt layer3c's restricted severity set
  (P1/P2/P3-only) or layer3c's category enum (`hidden-dependency`,
  `tight-coupling`, `missing-seam`, `non-determinism`, `boundary-problem`,
  `test-hostile`). Use the standard additional-assessment findings shape
  with severity `Critical | P1 | P2 | P3` and category
  `"change-impact-testability"`.
- If change-impact analysis identifies a high-blast-radius change with
  insufficient verification surface, report it as `change_verification_gap`
  regardless of whether `layer3c-testability-specialist` would also identify
  the same gap. The analytical path is blast-radius-from-change (this
  emitter) vs. structural-testability-of-existing-code (layer3c) — both are
  valid perspectives on the same surface, and downstream dedup resolves any
  overlap.

---

## subject_key Guidance

The `subject_key` names the **change surface** — the element whose change
is unsafe due to weak verification. The subject_key does NOT name a
hypothetical test function that does not exist. Anchoring on the missing
test produces unstable keys that drift between regenerations; anchoring on
the change surface produces stable, deduplicatable anchors.

Use one of these colon-delimited patterns:

| Pattern | Use when |
|---|---|
| `activity:<activity-name>` | The change surface is a WF activity class. The canonical change-surface anchor in WF codebases. *(Windows Workflow Foundation codebases; use `handler:<handler-name>` or `endpoint:<path>` for other frameworks.)* |
| `method:<class-name>.<method-name>` | The change surface is a specific method inside a service or class. |
| `module:<file-path-or-namespace>` | The change surface is a whole module with broad downstream coupling and no single method dominant. |
| `integration:<external-system>` | The change surface is the integration point with an external system (external API, message bus, database, file share, file system). |
| `contract:<published-interface-or-event>` | The change surface is a published interface, event schema, or DTO consumed by other repos. |
| `migration:<element>:<from-to-version>` | The change surface is a planned migration step (deprecation, API version bump, schema change, `[Obsolete]` removal). |

Pattern-shaped examples — substitute concrete identifiers from the provided context:
- `activity:<ActivityName>` — composite activity with multiple
  integration-parameter branches and broad downstream consumers.
- `activity:<ValidationActivityName>` — validation activity against
  external state, constructor-injected flags gate behavior.
- `method:<ConfigurationClass>.<LoaderMethod>` — loaded in many
  constructors; mutation would propagate widely.
- `integration:<ExternalSystem>.<ContractIdentifier>` — every downstream
  consumer depends on the contract; no integration-test boundary visible.
- `contract:<PublishedContractName>` — payload shape consumed by multiple
  activities; schema change blast radius is unbounded.
- `migration:<DeprecatedElement>:<from-state>-to-<to-state>` — `[Obsolete]`
  element still referenced; planned removal has no verification gate.

The subject_key MUST NOT be:
- A non-existent test function name
  (e.g., `test_<method-name>_<edge-case>`). That is the missing test, not
  the change surface.
- A coverage percentage
  (e.g., `coverage:<ElementName>:35%`).
- A generic phrase (e.g., `activity:high-blast-radius`).
- A path-only string without an element identifier
  (e.g., `path:<directory>/<subdirectory>/`).

---

## Summary and Description Requirements

Every finding must include:

- `summary`: one short, stable title for the finding. Suitable for grouping
  and issue titles. Do not include volatile run-specific phrasing or line
  numbers in `summary`. Anchor the summary on the change surface and the
  observable verification weakness.
- `description`: evidence-oriented explanation of the change-readiness
  problem. Include the named change surface, the observable blast-radius
  evidence, the observable verification weakness, the evidence location,
  and the remediation direction.

Good vs. insufficient summary examples:

| Good | Insufficient |
|---|---|
| `Method branches across multiple enum values with no parameterized test coverage visible` | `Testability concern in validation activity` |
| `Activity reads multiple integration parameters with no per-branch integration-test boundary` | `Activity reads too many parameters` |
| `XML contract has no schema-change verification gate across consumers` | `XML contract is risky` |
| `Class marked [Obsolete] is still referenced with no removal-verification plan` | `Obsolete code is still used` |

Two-part evidence threshold — both required per finding:
1. **Identifiable change surface** named in code — a specific activity,
   method, module, integration point, contract, or migration step. Not a
   generic "this area is risky".
2. **Observable verification weakness** — one of: visible absence of branch
   coverage, absence of integration-test boundary at an integration
   surface, named coupling to a downstream consumer with no contract test,
   `[Obsolete]` or planned-deprecation marker with no removal-verification
   plan, observable broad-downstream-coupling without a seam.

The description must cover at least 3 of the following 5 evidence elements:

- (a) **change_surface_named** — specific activity, method, module,
  integration point, contract, or migration step identifier appears in the
  description.
- (b) **blast_radius_evidence** — observable downstream coupling,
  integration breadth, cross-repo dependency, or contract publication shape
  evidence appears in the description.
- (c) **verification_weakness** — concrete weakness named: missing branch
  coverage, missing integration-test boundary, missing contract test, no
  removal-verification plan, no seam for fault injection.
- (d) **evidence_location** — specific file name, line number, element
  name, or path.
- (e) **remediation_direction** — add a parameterized test, introduce an
  integration-test boundary, add a contract test, define a
  removal-verification gate, introduce a seam.

Insufficient evidence — return empty findings rather than reporting:
- "This code is complex" without a named change surface and observable
  blast-radius evidence.
- "Tests are missing" without naming the change surface that the missing
  tests would protect.
- "Coupling looks high" without a named downstream consumer or contract
  publication point.
- Speculative blast-radius claims for a code element with no observable
  downstream consumers, integration boundary, published interface, or
  planned migration in the provided context.

The description must be self-contained because it is the primary persisted
human-readable evidence in the additional-assessment payload.

---

## JSON String Escaping Requirements

These requirements apply to all code-bearing and path-bearing fields.

Change-impact findings frequently contain C# method signatures with
generics (e.g., `Dictionary<string, Tuple<int, int>>`), activity field
names with bracketed indices, Windows file paths in `location` (e.g.,
`D:\repo\src\<area>\<File>.cs`), test framework class and attribute names
(`[TestMethod]`, `[Theory]`, `[DataTestMethod]`), and XML or JSON fragment
paraphrases in `description`.

- Use `\"` for embedded double-quotes inside JSON string values.
- Use `\\` for backslashes inside JSON string values. This applies
  especially to Windows file paths in `location`. Example:
  `"D:\\repo\\src\\<area>\\<File>.cs"`.
- If an exact code snippet, XML fragment, or method-signature with
  generics would make the JSON fragile, paraphrase the content in plain
  English rather than embedding raw code.
- Unescaped quotes or backslashes inside field values will cause the
  finding to be rejected by JSON parsing.

Fields requiring particular attention: `description`, `suggested_fix`,
`location`, `subject_key`, `summary`.

---

## Output Format

Return **only** valid JSON, no preamble, no markdown fences.

Use these `severity` values exactly: `Critical`, `P1`, `P2`, `P3`. Do not
adopt layer3c's restricted P1/P2/P3-only set.

```json
{
  "assessment_summary": "string",
  "recommended_actions": ["string"],
  "findings": [
    {
      "finding_kind": "change_verification_gap",
      "subject_key": "string",
      "summary": "string",
      "severity": "Critical | P1 | P2 | P3",
      "category": "change-impact-testability",
      "location": "string",
      "description": "string",
      "suggested_fix": "string"
    }
  ]
}
```

Reminder — do not add any layer3c-specific fields to layer2i findings.
Specifically, do not include `test_that_fails`, `tdd_signal`,
`testability_score`, `estimated_test_coverage_blocker`, or `quick_wins`
anywhere in the response. Those belong to
`layer3c-testability-specialist`'s output shape, not this emitter's.

---

## Empty Findings

When the provided context contains no change surfaces meeting the two-part
evidence threshold, return:

```json
{
  "assessment_summary": "No change-impact or verification gaps identified in the provided context.",
  "recommended_actions": [],
  "findings": []
}
```

Use this exact `assessment_summary` literal.

---

## Anti-Speculation Rule

A change surface cannot be inferred from non-change-surface code. A complex
piece of pure logic with no downstream consumers visible in the provided
context, no integration boundary, no published interface, and no planned
migration is not a `change_verification_gap`. **Code complexity alone is
not blast radius.**

If the provided context shows only a self-contained helper, a pure
function, or a single isolated method with no observable downstream
consumers, no integration surface, no published contract, and no migration
marker, return the empty-findings literal above rather than manufacturing a
`change_verification_gap` finding from speculation about what might call
the code.

Branch count, cyclomatic complexity, method length, and parameter count
are not sufficient evidence on their own. Pair them with observable
downstream coupling, integration breadth, contract publication, or planned
migration before reporting.