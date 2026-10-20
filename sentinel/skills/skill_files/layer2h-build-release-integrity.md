---
name: layer2h-build-release-integrity
description: Analyze build graph integrity, dependency pinning, and release-chain risks.
allowed-tools: []
---

# Build and Release Integrity

> **Ecosystem note.** Examples in this skill use .NET idioms for illustration
> (NuGet, `.csproj`, `packages.config`, MSBuild). The analytical patterns
> apply to any ecosystem — substitute the equivalent for your target
> codebase: `pyproject.toml` / `requirements.txt` for Python,
> `pom.xml` / `build.gradle` for Java/Kotlin, `Cargo.toml` for Rust,
> `go.mod` for Go, `package.json` for Node/JS/TS.

Evaluate build and release pipeline dependency integrity.

Focus on:
- unpinned/transitively risky dependencies
- build-order and artifact coupling issues
- incompatible build expectations across repos
- weak release validation boundaries

---

## Finding Identity Contract

Every finding must identify its build/release integrity mechanism using exactly this
registry-backed `finding_kind` value:

| `finding_kind` | `subject_key` rule | Use when |
|---|---|---|
| `build_release_integrity_gap` | Required. Include a non-empty `subject_key` naming the dependency, project, manifest entry, build artifact, pipeline step, or release validation gate whose integrity is unsafe. | A dependency declaration, build ordering, artifact coupling, manifest entry, pipeline step, or release validation chain is inconsistently or unsafely defined. |

Rules:
- Do not invent other `finding_kind` values.
- `subject_key` is required for `build_release_integrity_gap`.
- `category` remains descriptive metadata. Keep `category` as `build-release-integrity`,
  but treat `finding_kind` as the identity field.

### Scope boundary

**In scope for `build_release_integrity_gap`:** Unpinned or floating dependency version
expressions; transitively risky dependencies whose constraints permit drift; project
reference coupling without enforced ordering; manifest entries with unsafe or ambiguous
version specifiers; build artifact identities consumed without validation; release
validation gates that are absent where structurally needed; signing or attestation steps
that can be bypassed; cross-repo build expectations that drift between manifests.

**Exit rule (configuration_drift).** If the divergent setting weakens a deployment-time
configuration assumption rather than a build-time integrity property, prefer
`configuration_drift` from `layer2g-config-environment-drift`. Build/release integrity
is about how artifacts are produced, ordered, signed, and validated; configuration drift
is about how deployed components read their settings.

**Exit rule (security_vulnerability).** If the gap is primarily a security weakness —
signing bypass, vulnerable dependency known to be exploited, insecure release validation
that allows tampered artifacts to ship — prefer `security_vulnerability` from
`layer2c-security-specialist`. Security framing wins when both apply.

---

### `subject_key` guidance

Name the **build or release element with the integrity gap** — the specific dependency,
project, manifest entry, artifact, pipeline step, or release gate. Do not use the
runtime consumer's class or method name as the primary identity; the consuming location
belongs in `location` or the `description` body.

Use short, stable, colon-delimited build/release identities:

| Pattern | Use when |
|---|---|
| `dependency:<package-id>` | Naming a specific dependency (NuGet package, NPM module, etc.) with a pinning or version-constraint gap. |
| `dependency:<package-id>@<version-spec>` | When the unsafe version expression itself is the subject (e.g., floating version, wildcard, prerelease). |
| `project:<project-name>` | Naming a `.csproj` (or `pyproject.toml`, `pom.xml`, `Cargo.toml`, `go.mod`, `package.json`) or solution-project whose build configuration is the integrity concern. |
| `artifact:<artifact-identity>` | Naming a build output, signed binary, package, or release artifact whose coupling, ordering, or validation is the concern. |
| `pipeline:<stage-or-step>` | Naming a build/release pipeline stage (YAML job, MSBuild target, PowerShell step) where the integrity gap occurs. |
| `manifest:<file>:<element>` | Naming a build manifest element (e.g., `manifest:packages.config:<PackageId>`, or equivalently `manifest:requirements.txt:<package>`, `manifest:pom.xml:<groupId:artifactId>`, `manifest:Cargo.toml:<crate>`, `manifest:go.sum:<module>`, `manifest:package.json:<dependency>`). |
| `release_gate:<component>.<gate-name>` | Naming a release validation step, signing gate, or deployment check. |

Pattern-shaped examples — substitute concrete identifiers from the provided context:
- `dependency:<PackageId>@<version-spec>` — third-party or first-party package with a pinning/version-constraint gap.
- `dependency:<InternalPackageId>` — first-party shared library declared without a pinned version.
- `project:<ProjectName>` — `.csproj` (or `pyproject.toml`, `pom.xml`, `Cargo.toml`, `go.mod`, `package.json`) or solution-project whose build configuration is the integrity concern.
- `manifest:<manifest-file>:<PackageId>` — specific manifest entry (e.g., `packages.config`, `*.csproj`, `.deps.json`, or `requirements.txt`, `pom.xml`, `Cargo.lock`, `go.sum`, `package-lock.json`).
- `artifact:<ArtifactName>.<ext>` — compiled binary, signed package, or release output.
- `pipeline:<build-system>.<step>` — build/release pipeline stage (YAML job, MSBuild target, PowerShell step).
- `release_gate:<gate-system>.<gate-name>` — release validation, signing, or deployment-check gate.

Correct vs. incorrect subject_key choice:

| Correct | Incorrect |
|---|---|
| `dependency:<PackageId>@<version-spec>` | `method:<ConsumerClass>.<ConsumerMethod>` (names the runtime consumer, not the dependency) |
| `manifest:<manifest-file>:<PackageId>` | `class:<ConsumerClass>` (names runtime class) |
| `artifact:<ArtifactName>.<ext>` | `service:<RuntimeServiceName>` (names runtime service) |

---

## Summary and Description Requirements

### Summary

Write a one-sentence, stable summary that names the build/release element and the
integrity mechanism. The summary is used as a stable deduplication anchor across regenerations — it should not change if the underlying code evidence is the same.

Good summary: `<PackageId> declared with floating version expression in packages.config`
Insufficient summary: `Dependency management issue in build configuration`

### Description requirements

Every `build_release_integrity_gap` description must supply both:

1. **Identifiable build or release element** — name the specific dependency, project,
   manifest entry, artifact, pipeline step, or release gate observable in the provided
   context.
2. **Observable integrity gap** — one of: floating or unpinned version expression;
   missing pin on a transitive-risk surface; build ordering assumption without enforcement;
   artifact coupling without validation; release gate absence where structurally needed;
   signing or attestation bypass demonstrable from the manifest content; cross-repo
   manifest divergence.

The description should include, where visible in the provided context:
- The dependency, project, manifest entry, artifact, pipeline step, or release gate name.
- The specific integrity mechanism (unpinned version, floating constraint, missing
  validation gate, ordering gap, coupling without enforcement, signing bypass).
- The concrete consequence (the artifact may drift between builds, downstream package
  may resolve a different version, release could proceed without a signed manifest, a
  build step may run out of order).
- The evidence location — specific file and line range visible in context (`.csproj` (or `pyproject.toml`, `pom.xml`, `Cargo.toml`, `go.mod`, `package.json`),
  `packages.config` (or `requirements.txt`, `pom.xml` dependencies, `Cargo.lock`, `go.sum`), `AssemblyInfo.cs`, pipeline YAML, build script).
- The remediation direction (pin to specific version, add validation step, add signing
  gate, enforce ordering via dependency declaration).

Insufficient evidence — return empty findings rather than reporting:
- Runtime code without any build/release context (a method that calls `JsonConvert` is
  not build/release evidence; a `packages.config` entry declaring an unpinned dependency
  is).
- Generic "validate your build process" advice not tied to a specific dependency,
  manifest entry, or pipeline step.
- Deployment-time configuration concerns (exit to `configuration_drift`).
- Security-relevant gaps where the primary impact is a security weakness (exit to `security_vulnerability`).

The description must be self-contained because it is the primary persisted
human-readable evidence in the additional-assessment payload.

---

## JSON String Escaping Requirements

These requirements apply to all code-bearing and path-bearing fields.

Build/release findings frequently contain Windows file paths (backslashes), XML fragments
from `.csproj` and `packages.config`, NuGet version range expressions, and MSBuild
property references.

- Use `\"` for embedded double-quotes inside JSON string values.
- Use `\\` for backslashes inside JSON string values. This applies especially to Windows
  file paths such as `D:\\repo\\src\\<project>\\packages.config` in `location`.
- XML angle brackets from `.csproj` or `packages.config` quotations should either be
  paraphrased in plain English or escaped and quoted consistently — do not embed raw
  XML fragments that may break JSON or downstream display.
- NuGet version range expressions (`[1.0,2.0)`, `(,2.0]`, `[1.0]`) are JSON-safe but
  must be quoted as version expressions in descriptions to avoid ambiguity.
- MSBuild property references such as `$(Configuration)` are JSON-safe and should be
  preserved verbatim with code-fence or backtick framing in descriptions.
- If a manifest fragment or pipeline expression would make the JSON fragile, paraphrase
  in plain English rather than embedding the literal value.
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
      "finding_kind": "build_release_integrity_gap",
      "subject_key": "<required non-empty build/release element identity>",
      "summary": "<short stable finding title>",
      "category": "build-release-integrity",
      "location": "string",
      "description": "string",
      "suggested_fix": "string"
    }
  ]
}
```

---

## Empty Findings

When no identifiable build or release integrity gap is present in the provided context,
return this exact shape — do not omit the keys, do not return null:

```json
{
  "assessment_summary": "No build or release integrity gaps identified in the provided context.",
  "recommended_actions": [],
  "findings": []
}
```

---

## No Manufactured Findings

- Do not invent build/release concerns the provided context does not evidence. An
  observable build/release element must be identifiable by name in the code.
- Build and release integrity findings require an observable build/release element in
  the provided context — a manifest entry, project reference, version declaration,
  pipeline step, artifact identity, or release validation gate. Runtime code does not
  constitute build/release evidence unless it observably participates in the build or
  release process.
- If the context lacks build/release artifacts (no `.csproj`, no `packages.config`, no equivalent manifest in the target ecosystem — `pyproject.toml`, `pom.xml`, `Cargo.toml`, `go.mod`, `package.json`, etc., and no
  manifest entries, no version declarations, no pipeline references), return empty
  findings.
