---
name: layer2f-data-flow-trust-boundary
description: Analyze trust boundaries and data flow risks between components and repositories.
allowed-tools: []
---

# Data Flow and Trust Boundary

Review provided module and dependency context for trust boundary and data flow risks.

Focus on:
- untrusted input propagation across component or service boundaries
- missing validation or sanitization at boundary crossings
- data ownership ambiguity across modules or repos
- implicit trust placed on external service responses, file inputs, or upstream activity outputs

Find concrete trust boundary and data flow risks. Do not report speculative concerns unless
the provided context shows an identifiable untrusted source, an identifiable boundary crossing,
and an observable absence of validation or trust reduction.

---

## Finding Identity Contract

Every finding must identify its trust-boundary mechanism using exactly one of these
registry-backed `finding_kind` values:

| `finding_kind` | `subject_key` rule | Use when |
|---|---|---|
| `trust_boundary_gap` | Required. Include a non-empty `subject_key` naming the boundary crossing point: the untrusted source and the consuming component or method. | Untrusted or shared data crosses a component or service boundary without explicit validation, sanitization, or ownership discipline, and no concrete reachable sink is demonstrated in the provided context. |
| `injection_risk` | Required. Include a non-empty `subject_key` naming the concrete sink or the input-to-sink path that the trust-boundary failure reaches. | The trust-boundary analysis traces untrusted data from a boundary crossing to a concrete downstream sink that interprets, executes, queries, or resolves it unsafely. |

Rules:
- Do not invent other `finding_kind` values.
- `subject_key` is required for both allowed `finding_kind` values.
- `category` remains descriptive metadata for the additional-assessment payload. Keep `category`
  as `data-flow-trust`, but treat `finding_kind` as the identity field.
- Choose the most specific kind for the root cause.

### `subject_key` guidance

Use short, stable, colon-delimited trust-boundary identities. Prefer the boundary identity
over the local file location.

Pattern-shaped `subject_key` examples for `trust_boundary_gap` — substitute concrete identifiers from the provided context:
- `boundary:<service>.<operation>` — e.g., `boundary:<external-service-name>.<read-operation>`
- `input:<file-type>:<consuming-method>` — e.g., `input:<file-format>:<consuming-method>`
- `flow:<untrusted-source>:<consuming-component>` — e.g., `flow:<external-response>:<consuming-component>`
- `workflow-io:<activity>.<io-direction>` — e.g., `workflow-io:<producer-activity>.output:<consumer-activity>.input`
- `ownership:<shared-data>:<consumer>` — e.g., `ownership:<shared-payload>:<consumer-component>`

Pattern-shaped `subject_key` examples for `injection_risk` — substitute concrete identifiers from the provided context:
- `sink:<class>.<operation>` — e.g., `sink:<ParserClass>.<unsafe-operation>`
- `flow:<source>:<sink-method>` — e.g., `flow:<untrusted-file-format>:<ParserClass>.<resolution-operation>`
- `sink:<method>.<query-type>` — e.g., `sink:<external-response-type>.<query-type>`

---

## Discriminator Rules

### Table 1 — `trust_boundary_gap` vs `injection_risk` within this emitter

Use this discriminator when deciding between the two allowed kinds:

| Scenario | Use |
|---|---|
| Untrusted data (service response, file content, activity output, operator input) crosses a boundary and is consumed without validation, but no specific executable or interpreter sink is visible in the provided context. | `trust_boundary_gap` |
| Data ownership is ambiguous across components — no component clearly takes responsibility for validating the data — but no violation path to a concrete sink is visible. | `trust_boundary_gap` |
| Missing sanitization at a boundary is structural (no schema check, no whitelist, no trust-reduction step) but the downstream fate of the data is not visible in the provided context. | `trust_boundary_gap` |
| The trust-boundary analysis traces untrusted data from a crossing to a concrete downstream sink (XML parser with entity resolution, SQL construction, command execution, unsafe deserializer, unescaped output renderer) visible in the provided context. | `injection_risk` |

Short rule:
- Missing-validation gap without a demonstrated sink → `trust_boundary_gap`.
- Gap plus demonstrated concrete executable sink visible in context → `injection_risk`.

Do not upgrade a `trust_boundary_gap` to `injection_risk` speculatively. Only use
`injection_risk` when the provided context shows the actual sink path. If the downstream
code is not in scope, report `trust_boundary_gap`.

Avoid duplicate findings:
- Do not report the same root cause once as `trust_boundary_gap` and again as `injection_risk`.
- If both seem possible, choose the kind that describes the primary boundary failure. If the
  sink is demonstrable, use `injection_risk` and document the full path from source to sink in
  `description`.

### Table 2 — layer2f's `injection_risk` vs `layer2c-security-specialist`'s `injection_risk`

Both emitters can produce `injection_risk` for the same code. This is correct and intended.

| Aspect | layer2f (this emitter) | layer2c-security-specialist |
|---|---|---|
| Analytical path | Data-flow tracing: follows an untrusted source through a boundary crossing to a concrete sink. | Vulnerability-class analysis: identifies a CWE-class security flaw (SQL injection, command injection, XXE, path traversal, etc.) at the sink level. |
| Trigger | Trust boundary traversal that reaches a concrete sink. | Security checklist and attack-pattern recognition at the sink. |
| Finding shape | Standard additional-assessment shape (`finding_kind`, `subject_key`, `summary`, `description`, `location`, `suggested_fix`, `severity`, `category`). | Security-specialist shape (adds `vulnerability_class`, `cwe_id`, `attack_scenario`, `impact`). |
| Overlap behavior | Overlap with layer2c is expected and correct. Downstream dedup resolves it. | Same. |

Behavioral rules:
- Do not suppress an `injection_risk` finding because `layer2c-security-specialist` might also
  identify the same sink. Produce the finding from the data-flow tracing perspective.
- Do not add layer2c-specific fields (`vulnerability_class`, `cwe_id`, `attack_scenario`,
  `impact`) to layer2f findings. Use the standard additional-assessment findings shape for all
  layer2f findings.
- If trust-boundary tracing leads to a concrete sink, report it as `injection_risk` regardless
  of whether it would also appear as a security-vulnerability finding in another emitter.

---

## Summary and Description Requirements

Every finding must include:

- `summary`: one short, stable title for the finding. Suitable for grouping and issue titles.
  Do not include volatile run-specific phrasing or line numbers in `summary`.
- `description`: evidence-oriented explanation of the trust-boundary problem. Include the
  untrusted source, the boundary crossing, the validation or sanitization gap, the consuming
  component, and (for `injection_risk`) the concrete sink path. Explain why the chosen
  `finding_kind` fits.

Acceptable evidence for `trust_boundary_gap`:
- An identifiable untrusted source (external service interface, file input, workflow activity
  output, operator-entered text).
- An identifiable boundary crossing (a specific method or code path that consumes the data
  without validation).
- An observable absence of validation, sanitization, schema check, or trust-reduction step at
  that crossing.

Acceptable evidence for `injection_risk`:
- All three of the above, plus: a concrete reachable sink (SQL construction, XML parser with
  entity resolution, command execution, eval path, unsafe deserializer, unescaped output
  renderer) visible in the provided context.

Insufficient evidence — return empty findings rather than reporting:
- "Data might come from an external source" without a specific source or boundary crossing
  visible in the context.
- Generic "validate all inputs" concerns not tied to a specific boundary in the provided code.
- Trust-boundary concern that is purely architectural without code-level boundary evidence.
- Speculative sink reachability without a demonstrable path visible in the provided context.

The description must be self-contained because it is the primary persisted human-readable
evidence in the additional-assessment payload.

---

## JSON String Escaping Requirements

These requirements apply to all code-bearing and path-bearing fields.

Trust-boundary findings frequently contain data flow path notation, XML fragments showing
untrusted input shape, validation routine names and code patterns in `suggested_fix`,
Windows file paths in `location`, and method chains in `subject_key` and `description`.

- Use `\"` for embedded double-quotes inside JSON string values.
- Use `\\` for backslashes inside JSON string values. This applies especially to Windows file
  paths in `location`.
- If an exact code snippet, XML fragment, or data flow path would make the JSON fragile,
  paraphrase the content in plain English rather than embedding raw code.
- Unescaped quotes or backslashes inside field values will cause the finding to be rejected
  by JSON parsing.

Fields requiring particular attention: `description`, `suggested_fix`, `location`,
`subject_key`, `summary`.

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
      "severity": "Critical | P1 | P2 | P3",
      "finding_kind": "trust_boundary_gap | injection_risk",
      "subject_key": "<required non-empty stable trust-boundary or sink identity>",
      "summary": "<short stable finding title>",
      "category": "data-flow-trust",
      "location": "string",
      "description": "string",
      "suggested_fix": "string"
    }
  ]
}
```

Field requirements:
- `finding_kind`: must be one of the two allowed values. Do not invent other values.
- `subject_key`: required for every finding. Short, stable, colon-delimited. See subject_key
  guidance above.
- `summary`: one short, stable finding title. Do not include line numbers in `summary`.
- `category`: keep as `data-flow-trust` for all layer2f findings. It is supplemental descriptive
  metadata; `finding_kind` is the identity field.
- `description`: include the trust-boundary source, crossing, validation gap, consuming
  component, and (for `injection_risk`) the concrete sink path. This is the primary persisted
  evidence field.

If no trust-boundary or data-flow finding is present in the provided context, return this
empty-findings shape:

```json
{
  "assessment_summary": "No trust boundary or data flow findings identified in the reviewed context.",
  "recommended_actions": [],
  "findings": []
}
```

Do not manufacture findings to avoid an empty array. Return the empty shape when no
identifiable trust-boundary crossing with missing validation is observable in the provided
context.