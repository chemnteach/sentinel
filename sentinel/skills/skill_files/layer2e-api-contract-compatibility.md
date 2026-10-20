---
name: layer2e-api-contract-compatibility
description: Detect API contract incompatibilities across modules and repos; identify breaking changes and interface drift.
allowed-tools: []
---

# API Contract Compatibility

Review provided module/dependency context for interface compatibility risk.

Focus on:
- signature mismatches
- schema/field drift
- versioning and backward-compatibility gaps
- undocumented behavior coupling

Find concrete API, schema, interface, versioning, and behavioral contract risks. Do not report speculative compatibility concerns unless the provided context shows an identifiable producer-consumer contract and a plausible current or near-term consumer impact.

---

## Finding Identity Contract

Every finding must identify its API-contract mechanism using exactly one of these registry-backed `finding_kind` values:

| `finding_kind` | `subject_key` rule | Use when |
|---|---|---|
| `api_contract_mismatch` | Required. Include a non-empty `subject_key` naming the incompatible API, endpoint, signature, schema, DTO/message field, interface, versioned contract, or producer-consumer contract. | A producer-consumer interface changed or drifted incompatibly in signature, schema, field shape, field requiredness, type, endpoint, message format, protocol version, or compatibility version. |
| `contract_violation` | Required. Include a non-empty `subject_key` naming the violated precondition, postcondition, return-value guarantee, behavioral invariant, workflow I/O promise, or interface behavior. | Cross-module or cross-repo behavioral contract is violated even if the visible signature, schema, or endpoint shape still appears compatible. |

Rules:
- Do not invent other `finding_kind` values.
- `subject_key` is required for both allowed `finding_kind` values.
- `category` remains descriptive metadata for the additional-assessment payload. Keep `category` as `api-contract`, but treat `finding_kind` as the identity field.
- Choose the most specific kind for the root cause. Do not emit both kinds for the same root cause unless there are genuinely separate shape/version and behavioral-contract failures.

### `subject_key` guidance

Use short, stable, colon-delimited contract identities. Prefer the contract identity over the local file location.

Good `subject_key` examples:
- `api:<service>.<operation>`
- `endpoint:<method> <path>`
- `schema:<message-or-dto>:<field>`
- `signature:<type-or-interface>.<method>`
- `version:<contract-name>:<version>`
- `return-contract:<method>`
- `precondition:<operation>:<condition>`
- `postcondition:<operation>:<guarantee>`
- `invariant:<component>:<promise>`
- `workflow-io:<activity>:<input-or-output>`

Pattern-shaped examples — substitute concrete identifiers from the provided context:
- `signature:I<ServiceName>.<MethodName>` — service interface signature.
- `endpoint:POST /<resource>/{<id-param>}/<action>` — versioned REST endpoint with path parameter.
- `schema:<RequestDtoName>.<fieldName>` — DTO or message field identity.
- `version:<ContractName>:v<n>` — versioned contract identity.
- `workflow-io:<WorkflowName>.<OutputName>` — workflow activity input/output identity.
- `return-contract:<MethodName>.<returnedField>` — method return-value guarantee.

---

## Discriminator Rules

Use this discriminator when deciding between the two allowed kinds:

| Scenario | Use |
|---|---|
| Producer and consumer disagree on signature, schema shape, field requiredness, type, endpoint, message format, protocol version, or compatibility version. | `api_contract_mismatch` |
| The visible interface shape still appears compatible, but implementation violates a documented or implied behavior promise. | `contract_violation` |
| A DTO, message, or schema removes or renames a field that consumers still require. | `api_contract_mismatch` |
| A method still returns the same DTO shape but no longer populates a promised field correctly. | `contract_violation` |
| A versioned endpoint changes behavior without a compatible versioning path. | Usually `api_contract_mismatch` if versioning or compatibility drift is the primary defect; use `contract_violation` if the primary defect is a violated behavior guarantee despite compatible version/shape. |

Short rule:
- Shape, schema, signature, endpoint, protocol, or version compatibility problem → `api_contract_mismatch`.
- Behavioral promise breach with compatible shape → `contract_violation`.

Avoid duplicate findings:
- Do not report the same root cause once as `api_contract_mismatch` and again as `contract_violation`.
- If both seem possible, choose the kind that describes the earliest contract boundary failure.

---

## Breaking-Change Semantics

### Likely `api_contract_mismatch`

Report `api_contract_mismatch` when the provided context shows an incompatible contract drift such as:

- removing a public field that consumers still read;
- renaming a public field without a compatibility alias;
- changing field type, units, encoding, enum domain, or nullability incompatibly;
- changing requiredness of a request, response, DTO, or message field;
- changing method parameter order, type, requiredness, or meaning;
- changing endpoint path, HTTP method, route parameter, or message name incompatibly;
- changing event/message schema without compatible versioning;
- changing response shape without a consumer migration path;
- requiring a newer contract version while visible callers still use the old version;
- making an interface narrower in a way current consumers cannot satisfy.

### Compatible evolution — do not report unless concrete evidence shows breakage

Do not manufacture findings for compatible evolution such as:

- adding an optional field while existing fields remain stable;
- adding a backward-compatible overload while preserving the old signature;
- adding a new endpoint while retaining the old endpoint;
- adding a new schema version while the old version remains supported;
- widening an accepted input range without violating existing consumers;
- adding a new enum value when visible consumers safely handle unknown values;
- adding metadata that consumers can ignore safely.

If the context does not show a consumer, contract, or compatibility impact, return an empty findings array rather than speculating.

### Likely `contract_violation`

Report `contract_violation` when a visible contract shape remains compatible but behavior violates a promise such as:

- documented status codes, result codes, or error semantics are no longer honored;
- an idempotency, ordering, retry, or transactional guarantee is broken;
- an operation accepts the same inputs but produces behavior outside the documented range;
- a return value remains the same type but violates required semantics;
- a compatibility shim or adapter exists but maps fields or behavior incorrectly;
- a workflow activity input/output contract is accepted syntactically but violates a caller-visible invariant.

---

## Summary and Description Requirements

Every finding must include:

- `summary`: one short, stable title for the finding. It should be suitable for grouping and issue titles. Do not include volatile run-specific phrasing or line numbers in `summary`.
- `description`: evidence-oriented explanation of the contract problem. Include the contract identity, producer-consumer evidence, expected contract, observed mismatch or violation, compatibility impact, and why the chosen `finding_kind` fits.

Acceptable evidence includes:
- producer signature vs. consumer call mismatch;
- schema field expected by a consumer but missing, renamed, typed differently, or made newly required;
- documented behavior or version contract contradicted by implementation;
- dependency metadata or version mismatch paired with incompatible API usage;
- internal interface contract and caller assumptions visible in the provided context;
- cross-module or cross-repo dependency context from the additional Phase 1 assessment payload.

Insufficient evidence includes:
- “this might break consumers” without identifying a contract or consumer;
- generic “needs documentation” comments;
- purely internal cleanup not tied to a caller-visible contract;
- theoretical future versioning concern with no incompatible change;
- broad maintainability concerns that are not API contract compatibility problems.

The description must be self-contained because it is the primary persisted human-readable evidence in the additional-assessment payload.

---

## JSON String Escaping Requirements

Return only valid JSON.

These fields may contain code, signatures, endpoint paths, schema fragments, version strings, or quoted contract text and must be escaped carefully:
- `description`
- `location`
- `subject_key`
- `summary`
- `suggested_fix`

Escaping rules:
- Use `\"` for embedded double-quotes inside JSON string values.
- Use `\\` for backslashes inside JSON string values.
- Unescaped quotes inside field values will cause the response to be rejected by JSON parsing.
- If an exact signature, schema snippet, request/response example, or code-level fix would make the JSON fragile, paraphrase the contract evidence or fix in plain English instead.

---

## Output Format

Return **only** valid JSON, no preamble, no markdown fences.

Use these severity values exactly:
- `Critical`: incompatible contract or behavior can cause production data corruption, unsafe action, security exposure, or widespread operational failure.
- `P1`: incompatible contract or behavior breaks a common or important current consumer path.
- `P2`: contract drift can break an edge path, deployment/upgrade path, or integration under realistic conditions.
- `P3`: low-risk compatibility hardening with concrete evidence, not speculation.

Use this JSON shape:

```json
{
  "assessment_summary": "<1-3 sentences summarizing API contract compatibility posture>",
  "recommended_actions": ["<actionable compatibility remediation or validation step>"],
  "findings": [
    {
      "severity": "Critical | P1 | P2 | P3",
      "category": "api-contract",
      "finding_kind": "api_contract_mismatch | contract_violation",
      "subject_key": "<required stable contract identity>",
      "summary": "<short stable finding title>",
      "location": "<file, type, function, endpoint, schema, or contract location>",
      "description": "<specific contract evidence, expected-vs-actual behavior, consumer impact, and chosen-kind rationale>",
      "suggested_fix": "<specific compatibility-preserving fix or migration path>"
    }
  ]
}
```

Field rules:
- `assessment_summary`: summarize contract compatibility posture for the reviewed context.
- `recommended_actions`: list follow-up actions such as adding compatibility tests, restoring old contract shape, adding a compatibility adapter, versioning the contract, or confirming consumers.
- `category`: keep `api-contract` as descriptive metadata.
- `finding_kind`: must be exactly `api_contract_mismatch` or `contract_violation`.
- `subject_key`: required and non-empty for every finding.
- `summary`: stable title separate from `description`.
- `location`: identify where the contract evidence appears.
- `description`: include concrete contract evidence and avoid speculation.
- `suggested_fix`: recommend a compatibility-preserving change, migration path, versioning strategy, adapter, or contract test.

If no concrete API contract compatibility findings are supported by the provided context, return this empty-findings JSON shape:

```json
{
  "assessment_summary": "No concrete API contract compatibility findings were identified in the provided context.",
  "recommended_actions": [],
  "findings": []
}
```

Do not manufacture findings to avoid an empty array. This is especially important for single-repo contexts where true cross-repo API contract pressure may be thin. Internal API, workflow I/O, service interface, schema, or version contracts may still be valid findings when concrete evidence exists.