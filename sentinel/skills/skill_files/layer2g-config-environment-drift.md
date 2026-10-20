---
name: layer2g-config-environment-drift
description: Detect configuration and environment drift risks across repositories and deployment contexts.
allowed-tools: []
---

# Config and Environment Drift

Assess whether config contracts are aligned across modules/repos.

Focus on:
- missing/renamed config keys
- divergent defaults and environment assumptions
- fragile startup/runtime dependencies on environment
- migration gaps between versions

---

## Finding Identity Contract

Every finding must identify its configuration-drift mechanism using exactly this registry-backed
`finding_kind` value:

| `finding_kind` | `subject_key` rule | Use when |
|---|---|---|
| `configuration_drift` | Required. Include a non-empty `subject_key` naming the config key, environment variable, default setting, startup dependency, or migration artifact that drifts, is missing, or is hardcoded when it should be driven by configuration. | Configuration keys, defaults, or environment assumptions diverge across repos or deployment targets, or a value that should be configuration-driven is baked into code, without primarily weakening a security control. |

Rules:
- Do not invent other `finding_kind` values.
- `subject_key` is required for `configuration_drift`.
- `category` remains descriptive metadata. Keep `category` as `config-drift`, but treat
  `finding_kind` as the identity field.

### Scope boundary

**In scope for `configuration_drift`:** Config keys absent from the target environment; keys
renamed between versions without migration; default values that diverge from deployment
expectations; hardcoded values that should be driven by configuration; fragile startup
dependencies on environment facts (path, service name, connection string) without fallback;
stale config entries that no longer have consuming code; cross-repo config key inconsistency.

**Exit rule:** If the divergent or missing configuration weakens a security control
(authentication, authorization, encryption, secret management, TLS validation), report
`security_misconfiguration` instead of `configuration_drift`. Security framing wins when
both apply.

**Code-smell emitter overlap note:** A hardcoded value may also be flagged by code-smell or
maintainability emitters as `primitive_obsession` or a magic-number pattern — this is expected
and correct. The `configuration_drift` finding's identity is the config item that should exist
to drive the value; the code-smell finding's identity is the code structure problem. Different
kinds, different perspectives, same code. Do not suppress either.

---

### `subject_key` guidance

Name the **configuration item** — the specific key, variable, constant, or setting that drifts
or is missing. Do not use the consumer's class or method name as the primary identity; the
consuming location belongs in `location` or the `description` body.

Use short, stable, colon-delimited config identities:

| Pattern | Use when |
|---|---|
| `config:<key-name>` | A specific named config key is absent, renamed, or drifts in value. |
| `env:<variable-name>` | An environment variable assumption diverges between targets or lacks a defined default. |
| `default:<component>.<setting>` | A default value or assumed constant diverges from the configured or expected value. |
| `hardcoded:<class-or-activity>.<value-category>` | A deployment-specific value is baked into code. |
| `startup:<component>.<dependency>` | A startup or runtime dependency on an environment fact is fragile or undeclared. |
| `migration:<component>.<key-or-version>` | A config key or migration step is stale or no longer matches code expectations across a version. |

Pattern-shaped examples — substitute concrete identifiers from the provided context:
- `config:<ConstantClassName>.<KeyName>` — named configuration key on a constants holder.
- `config:<ConfigurationClassName>.<MappingName>` — named mapping or compound configuration entry.
- `default:<ConsumingComponent>.<SettingName>` — default value assumed by a consuming component.
- `hardcoded:<ConsumingMethod>.<ValueCategory>` — deployment-specific value baked into a method body.
- `env:<EnvironmentVariableName>` — environment variable consumed without a defined fallback.
- `startup:<ConsumingService>.<DependencyName>` — fragile startup dependency on an environment fact.

---

## Summary and Description Requirements

### Summary

Write a one-sentence, stable summary that names the config item and the drift mechanism.
The summary is used as a stable deduplication anchor across regenerations — it should not
change if the underlying code evidence is the same.

Good summary: `Configuration constant consumed without a verified default value`
Insufficient summary: `Configuration issue found in validation component`

### Description requirements

Every `configuration_drift` description must supply both:

1. **Identifiable configuration item** — name the specific key, constant, variable, section,
   or setting observable in the provided code.
2. **Observable inconsistency or gap** — one of: the key is read without a verified default
   or fallback; the constant names an external parameter that may drift between deployments;
   the value is hardcoded where configuration is expected; the consuming code has no defined
   behavior on missing or null config.

The description should include, where visible in the provided context:
- The config key name, constant, or variable being consumed.
- The consuming location (activity, class, or method).
- The specific drift mechanism (missing default, renamed key, hardcoded value, cross-repo
  inconsistency, stale entry, migration gap).
- What behavior the code exhibits on missing or drifted config (silent null, exception,
  undefined default, incorrect assumption).
- The deployment scope or environment dimension where the drift is observable (cross-repo,
  cross-environment, cross-version), if visible in the provided context.

Insufficient evidence — return empty findings rather than reporting:
- "This value could drift" without a specific config item or observable gap in the provided code.
- Generic "externalize your configuration" advice not tied to a specific key or assumption.
- Runtime behavior concerns that are not config-driven (prefer `trust_boundary_gap` or
  `partial_failure_handling_gap` for non-config assumptions).
- Security-relevant config divergence (exit to `security_misconfiguration`).

The description must be self-contained because it is the primary persisted human-readable
evidence in the additional-assessment payload.

---

## JSON String Escaping Requirements

These requirements apply to all code-bearing and path-bearing fields.

Configuration drift findings frequently contain config key names with dots and colons,
Windows file paths in `location` (backslashes), configuration values that may include
double-quotes (connection strings, format strings), and code snippets showing config reads
in `suggested_fix`.

- Use `\"` for embedded double-quotes inside JSON string values.
- Use `\\` for backslashes inside JSON string values. This applies especially to Windows file
  paths such as `D:\\repo\\src\\config\\...` in `location`.
- If a config value or code snippet would make the JSON fragile, paraphrase in plain English
  rather than embedding the literal value.
- Unescaped backslashes or quotes will cause the finding to be rejected by JSON parsing.

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
      "finding_kind": "configuration_drift",
      "subject_key": "<required non-empty config item identity>",
      "summary": "<short stable finding title>",
      "category": "config-drift",
      "location": "string",
      "description": "string",
      "suggested_fix": "string"
    }
  ]
}
```

---

## Empty Findings

When no identifiable configuration drift is present in the provided code, return this exact
shape — do not omit the keys, do not return null:

```json
{
  "assessment_summary": "No configuration drift identified in the provided context.",
  "recommended_actions": [],
  "findings": []
}
```

---

## No Manufactured Findings

- Do not invent drift concerns the provided context does not evidence. An observable config
  item must be identifiable by name in the code.
- If the context lacks config-related code (no config reads, no constants naming external
  parameters, no startup dependencies, no hardcoded deployment-specific values), return empty
  findings.
- Configuration assumptions cannot be inferred from non-config code. A method signature or
  data type does not constitute config evidence.