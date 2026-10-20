"""
sentinel/assess/finding_schema.py

Finding Schema helpers — finding_kind registry access, registry-field
validation, deterministic IDs, category classification, and structured
location parsing.

These functions are used by ``_extract_findings()`` in ``reviewer.py``
and will later be used by the dedup pipeline.
"""

from __future__ import annotations

import ast
import hashlib
import json
import logging
import re
from typing import Mapping

from sentinel.assess.finding_kinds import (
    FindingKindDefinition,
    get_finding_kind_definition as _lookup_declared_finding_kind_definition,
    get_supported_report_finding_kind_definition as _lookup_supported_report_finding_kind_definition,
    iter_finding_kind_definitions as _iter_declared_finding_kind_definitions,
    iter_supported_report_finding_kind_definitions as _iter_supported_report_finding_kind_definitions,
)


log = logging.getLogger(__name__)

_UNKNOWN_LOCATION = "_unknown_location"
_STRICT_REPORT_CONTRACT_EMITTER_PREFIXES: tuple[str, ...] = (
    "quick-scan-",
)
_STRICT_REPORT_CONTRACT_EMITTER_NAMES: frozenset[str] = frozenset(
    {"deterministic-lane"}
)


def _requires_active_report_contract(emitter_name: str) -> bool:
    """Return True when an emitter should be gated by active report contract."""

    if emitter_name in _STRICT_REPORT_CONTRACT_EMITTER_NAMES:
        return True
    return emitter_name.startswith(_STRICT_REPORT_CONTRACT_EMITTER_PREFIXES)


def get_declared_finding_kind_definition(
    kind_name: str,
) -> FindingKindDefinition | None:
    """Return the broad declared finding-kind definition catalog entry."""

    return _lookup_declared_finding_kind_definition(kind_name)


def iter_declared_finding_kind_definitions() -> tuple[FindingKindDefinition, ...]:
    """Return the broad declared finding-kind catalog in declaration order."""

    return tuple(_iter_declared_finding_kind_definitions())


def get_supported_report_finding_kind_definition(
    kind_name: str,
) -> FindingKindDefinition | None:
    """Return a report-supported finding-kind definition when available."""

    return _lookup_supported_report_finding_kind_definition(kind_name)


def iter_supported_report_finding_kind_definitions() -> tuple[FindingKindDefinition, ...]:
    """Return the report-supported finding-kind definitions in contract order."""

    return tuple(_iter_supported_report_finding_kind_definitions())


def get_finding_kind_definition(kind_name: str) -> FindingKindDefinition | None:
    """Backward-compatible alias for broad declared finding-kind lookup."""

    return get_declared_finding_kind_definition(kind_name)


def iter_finding_kind_definitions() -> tuple[FindingKindDefinition, ...]:
    """Backward-compatible alias for broad declared finding-kind iteration."""

    return iter_declared_finding_kind_definitions()


class FindingSchemaValidationError(ValueError):
    """Raised when a finding fails the v0.7.1 schema contract."""


def _format_validation_context(
    finding: Mapping[str, object],
    *,
    emitter_name: str,
) -> str:
    """Build a compact error-context suffix for schema validation failures."""

    context_parts = [f"emitter={emitter_name}"]

    skill = str(finding.get("skill", "") or "").strip()
    if skill and skill != emitter_name:
        context_parts.append(f"skill={skill}")

    # Regime boundary: "id" is the canonical JSON key for findings
    # on disk (raw-dict regime); "finding_id" is the canonical field
    # on typed dataclasses (FindingIssueRecord and siblings).
    # This is an explicit raw-dict → typed translation site.
    # The two-regime split is intentional — see cycle-23 Item 3.
    finding_id = str(finding.get("id", "") or "").strip()
    if finding_id:
        context_parts.append(f"id={finding_id}")

    # ``location`` may be a structured object ({"file": ..., "line": ...}),
    # not just a string. Coerce to a compact string before formatting so a
    # dict location renders as its file/line rather than a full dict repr.
    raw_location = finding.get("location", "")
    if isinstance(raw_location, Mapping):
        loc_file = str(raw_location.get("file", "") or "").strip()
        loc_line = str(
            raw_location.get("line", raw_location.get("line_range", "")) or ""
        ).strip()
        location = f"{loc_file}:{loc_line}".strip(":") if (loc_file or loc_line) else ""
    else:
        location = str(raw_location or "").strip()
    if location:
        context_parts.append(f"location={location}")

    return " [" + ", ".join(context_parts) + "]"


def validate_finding_kind_fields(
    finding: Mapping[str, object],
    *,
    emitter_name: str,
) -> FindingKindDefinition:
    """Validate registry-governed finding_kind and subject_key fields.

    This enforces the v0.7.1 registry rules without yet deriving F2
    subject components. Callers are expected to run this before
    persistence and before any F2 signature derivation introduced in
    Task 4 substep 2.
    """

    validation_context = _format_validation_context(
        finding,
        emitter_name=emitter_name,
    )

    finding_kind = str(finding.get("finding_kind", "") or "").strip()
    if not finding_kind:
        raise FindingSchemaValidationError(
            f"finding_kind is required{validation_context}"
        )

    definition = get_finding_kind_definition(finding_kind)
    if definition is None:
        raise FindingSchemaValidationError(
            f"finding_kind '{finding_kind}' is not in the registry{validation_context}"
        )

    raw_subject_key = finding.get("subject_key")
    if raw_subject_key is None:
        subject_key = ""
    elif isinstance(raw_subject_key, str):
        subject_key = raw_subject_key.strip()
    else:
        raise FindingSchemaValidationError(
            f"subject_key must be a string when provided for finding_kind '{finding_kind}'{validation_context}"
        )

    if definition.subject_required == "required" and not subject_key:
        raise FindingSchemaValidationError(
            f"subject_key is required for finding_kind '{finding_kind}'{validation_context}"
        )

    if definition.subject_required == "n/a" and subject_key not in {"", "_na"}:
        raise FindingSchemaValidationError(
            f"subject_key must be omitted or '_na' for finding_kind '{finding_kind}'{validation_context}"
        )

    return definition

# ── Step 1a: Deterministic finding ID ─────────────────────────────────


def _generate_finding_id_v06_legacy(
    module: str,
    skill: str,
    description: str,
    location: str,
) -> str:
    """Generate a v0.6-legacy deterministic finding ID.

    Scheduled for replacement by F2 signature derivation in Task 4
    substep 2 per spec v0.7.1 Section 4.5. The F2 contract excludes
    description from hash input; this function uses ``description[:200]``
    because it predates that contract.

    Returns a string of the form ``F-{hash[:12]}`` where hash is a
    SHA-256 hex digest of the canonical content string. The description
    is truncated to 200 characters so that minor wording changes in long
    descriptions don't produce different IDs for the same legacy root cause.
    """
    canonical = f"{module}|{skill}|{location}|{description[:200]}"
    digest = hashlib.sha256(canonical.encode()).hexdigest()
    return f"F-{digest[:12]}"


# ── Step 1b: Category classification ─────────────────────────────────

_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "encoding": ["encoding", "utf-8", "utf8", "ascii", "codec"],
    "atomicity": ["atomic", "write_text", "non-atomic", "partial write"],
    "concurrency": ["race", "lock", "concurrent", "thread", "async"],
    "validation": ["validation", "sanitiz", "input", "user input"],
    "error_handling": [
        "exception",
        "error handling",
        "silent",
        "swallow",
        "bare except",
    ],
    "path_safety": ["path traversal", "injection", "directory", ".."],
    "resource_mgmt": ["leak", "unclosed", "resource", "connection pool"],
}


def _classify_category(description: str) -> str:
    """Classify a finding description into a category.

    Performs case-insensitive keyword matching against
    ``_CATEGORY_KEYWORDS``.  Returns the first matching category, or
    ``"general"`` if no keywords match.
    """
    lower = description.lower()
    for category, keywords in _CATEGORY_KEYWORDS.items():
        for kw in keywords:
            if kw in lower:
                return category
    return "general"


def unpack_structured_location(finding: Mapping[str, object]) -> dict[str, object]:
    """Lift a structured ``location`` dict into flat finding fields.

    Some emitters (notably ``layer2a-quick-review``) return ``location`` as a
    structured object ``{"file": ..., "line": ..., "function": ...}`` rather
    than a string. When that happens the nested ``file`` / ``function`` /
    ``line`` are copied into the top-level ``file`` / ``function`` /
    ``line_range`` fields (without overwriting an already-populated value), and
    ``location`` is flattened to a ``"file:function"`` string so downstream
    string-based parsing and ID minting behave exactly as they do for string
    locations.

    Additive and idempotent: a non-dict ``location`` is returned unchanged
    (only shallow-copied), so existing string-location findings are untouched.
    """
    raw_location = finding.get("location")
    if not isinstance(raw_location, dict):
        return dict(finding)

    out = dict(finding)
    nested_file = str(raw_location.get("file", "") or "").strip()
    nested_function = str(raw_location.get("function", "") or "").strip()
    nested_line = str(
        raw_location.get("line", raw_location.get("line_range", "")) or ""
    ).strip()

    def _empty(value: object) -> bool:
        return not str(value or "").strip()

    if nested_file and _empty(out.get("file")):
        out["file"] = nested_file
    if nested_function and _empty(out.get("function")):
        out["function"] = nested_function
    if nested_line and _empty(out.get("line_range")):
        out["line_range"] = nested_line

    base = (
        f"{nested_file}:{nested_line}"
        if (nested_file and nested_line)
        else (nested_file or nested_line or "")
    )
    if nested_function:
        flat_location = (
            f"{base} in {nested_function}"
            if base
            else nested_function
        )
    else:
        flat_location = base
    out["location"] = flat_location
    return out


def materialize_emitter_finding(
    finding: Mapping[str, object],
    *,
    emitter_name: str,
    repo_root_canonical: str,
) -> dict[str, object]:
    """Copy a producer finding, attach legacy alias, and mint the F2 ID."""

    raw_location = finding.get("location")
    if isinstance(raw_location, dict):
        structured_file = str(raw_location.get("file", "") or "").strip()
        if not structured_file:
            raise FindingSchemaValidationError(
                "structured location requires a non-empty file"
                + _format_validation_context(finding, emitter_name=emitter_name)
            )

    # Flatten a structured ``location`` dict (e.g. layer2a-quick-review's
    # {file,line,function}) before any string coercion so file/function/line
    # survive into the output. No-op for string locations (cycle 42, Problem A).
    materialized: dict[str, object] = unpack_structured_location(finding)

    finding_kind = str(materialized.get("finding_kind", "") or "").strip()
    if (
        finding_kind
        and _requires_active_report_contract(emitter_name)
        and get_supported_report_finding_kind_definition(finding_kind) is None
    ):
        raise FindingSchemaValidationError(
            f"finding_kind '{finding_kind}' is unsupported by active report contract"
            + _format_validation_context(materialized, emitter_name=emitter_name)
        )

    description = str(materialized.get("description", "") or "")
    location = str(materialized.get("location", "") or "")

    legacy_finding_id = _canonicalize_scope_text(materialized.get("legacy_finding_id", ""))
    if not legacy_finding_id:
        legacy_finding_id = _generate_finding_id_v06_legacy(
            _canonicalize_scope_text(materialized.get("module", "")),
            _canonicalize_scope_text(materialized.get("skill", "")),
            description,
            location,
        )
    materialized["legacy_finding_id"] = legacy_finding_id
    materialized["id"] = generate_finding_id_v2(
        materialized,
        emitter_name=emitter_name,
        repo_root_canonical=repo_root_canonical,
    )
    return materialized


def repair_invalid_finding(
    raw: Mapping[str, object],
    emitter_name: str,
    repo_root_canonical: str = "",
    *,
    invalid_kind_counter: list[int] | None = None,
) -> dict[str, object]:
    """Repair a finding that failed schema validation due to LLM noncompliance.

    The model occasionally emits a ``finding_kind`` that is not in the
    registry (e.g. a ``category`` value placed in the ``finding_kind`` field)
    or omits a required ``subject_key``. Rather than aborting the whole
    assessment, coerce the finding into a valid shape:

            * An unregistered ``finding_kind`` is coerced to
        ``structural_complexity``; the original value is preserved in
        ``raw_finding_kind`` and ``invalid_kind_counter[0]`` is incremented
        when this happens.
      * ``subject_key`` is repaired to satisfy the (possibly coerced) kind's
        ``subject_required`` rule.

    Returns the repaired, materialized finding. Re-raises
    ``FindingSchemaValidationError`` if the finding cannot be repaired even
    after coercion (the caller's outer handler remains the backstop).
    """

    repaired: dict[str, object] = dict(raw)
    original_kind = str(repaired.get("finding_kind", "") or "")

    # Coerce an unregistered finding_kind, preserving the original value.
    if original_kind and get_finding_kind_definition(original_kind) is None:
        repaired["finding_kind"] = "structural_complexity"
        repaired["raw_finding_kind"] = original_kind
        if invalid_kind_counter is not None:
            invalid_kind_counter[0] += 1
        log.warning(
            "LLM emitted unregistered finding_kind %r from %s; "
            "coerced to structural_complexity",
            original_kind,
            emitter_name,
        )

    # Repair subject_key to satisfy the (possibly coerced) kind's rule.
    kind_def = get_finding_kind_definition(
        str(repaired.get("finding_kind", "") or "")
    )
    if kind_def is not None:
        subject_key = str(repaired.get("subject_key", "") or "")
        if kind_def.subject_required == "required" and not subject_key:
            # Derive a placeholder from the finding's own kind + file basename
            # instead of a flat "unknown:unknown", so distinct repaired findings
            # do not all collapse onto the same subject_key.
            placeholder_kind = str(repaired.get("finding_kind", "") or "") or "unknown"
            file_basename = str(repaired.get("file", "") or "").split("/")[-1] or "unknown"
            placeholder_subject = f"{placeholder_kind}:{file_basename}"
            repaired["subject_key"] = placeholder_subject
            log.warning(
                "Missing subject_key for finding_kind %r from %s; "
                "defaulted to %r",
                repaired["finding_kind"],
                emitter_name,
                placeholder_subject,
            )
        elif kind_def.subject_required == "n/a":
            repaired["subject_key"] = "_na"

    return materialize_emitter_finding(
        repaired,
        emitter_name=emitter_name,
        repo_root_canonical=repo_root_canonical,
    )


# ── Step 1c: Structured location parsing ──────────────────────────────

# Matches file paths like  src/utils.py  or  foo/bar.js:123
_FILE_PATH_RE = re.compile(
    r"(?:^|[\s,\(])("
    r"(?:[A-Za-z]:[\\/])?"
    r"(?:[\w.\\/-]+[\\/])?"
    r"[\w.-]+\."
    r"(?:py|jsx|tsx|ts|js|go|rs|java|rb|cpp"
    r"|hpp|cs|php|sh|yaml|yml|json|toml|cfg"
    r"|ini|sql|md|c|h)"
    r")"
)

# Matches  def foo  or  class Bar  or  foo()  or  self.foo()
_FUNCTION_NAME_RE = re.compile(
    r"(?:def\s+(\w+)|class\s+(\w+)|(?:\.)?(\w+)\(\))"
)

# Matches  line 42  |  lines 42-50  |  foo.py:123
_LINE_RANGE_RE = re.compile(
    r"(?:[Ll]ines?\s+(\d+(?:-\d+)?))|(?::(\d+(?:-\d+)?))"
)

_FUNCTION_CONTEXT_RE = re.compile(
    r"\b(?:function|method|class|def)\s*[:=]?\s*\(?([A-Za-z_][\w.]*)\)?",
    re.IGNORECASE,
)

_BRACKETED_IDENTIFIER_RE = re.compile(r"\(([^()]*)\)")

_STRUCTURAL_WORD_RE = re.compile(
    r"\b(?:file|path|function|method|class|def|line|lines|at|in)\b",
    re.IGNORECASE,
)

_STRUCTURAL_BRACKETED_TOKEN_RE = re.compile(
    r"^(?:line(?:s)?(?:\s+\d+(?:-\d+)?)?|method|function|class|def)$",
    re.IGNORECASE,
)

_WINDOWS_DRIVE_PREFIX_RE = re.compile(r"^(?P<drive>[A-Za-z]):(?=/)")

_IDENTIFIER_TOKEN_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")


def _extract_file_path(location: str) -> str:
    """Extract a file path from a location string.

    Returns the first file-like path found, or ``""`` if none.
    """
    if not location:
        return ""
    m = _FILE_PATH_RE.search(location)
    return m.group(1) if m else ""


def _extract_function_name(location: str) -> str:
    """Extract a function or class name from a location string.

    Recognises ``def foo``, ``class Bar``, and ``foo()`` patterns.
    Returns ``""`` if nothing matched.
    """
    if not location:
        return ""
    m = _FUNCTION_NAME_RE.search(location)
    if not m:
        return ""
    # Return the first non-None capture group
    return next((g for g in m.groups() if g is not None), "")


def _extract_line_range(location: str) -> str:
    """Extract a line number or range from a location string.

    Recognises ``line 42``, ``lines 42-50``, ``Line 100``, and
    ``foo.py:123`` patterns.  Returns ``""`` if nothing matched.
    """
    if not location:
        return ""
    m = _LINE_RANGE_RE.search(location)
    if not m:
        return ""
    return next((g for g in m.groups() if g is not None), "")


def _collapse_internal_whitespace(text: str) -> str:
    """Collapse internal whitespace to a single space."""

    return re.sub(r"\s+", " ", text).strip()


def _normalize_windows_drive_prefix(text: str) -> str:
    """Lowercase a leading Windows drive letter when present."""

    match = _WINDOWS_DRIVE_PREFIX_RE.match(text)
    if not match:
        return text
    drive = match.group("drive").lower()
    return drive + text[1:]


def _normalize_repo_root(repo_root_canonical: str) -> str:
    """Normalize the canonical repo root for exact-prefix comparison."""

    root = _collapse_internal_whitespace(str(repo_root_canonical or ""))
    if not root:
        return ""
    root = root.replace("\\", "/")
    root = _normalize_windows_drive_prefix(root)
    root = re.sub(r"/{2,}", "/", root)
    return root.rstrip("/")


def _strip_canonical_repo_root(location: str, repo_root_canonical: str) -> str:
    """Strip the exact canonical repo-root prefix when it is present."""

    normalized_root = _normalize_repo_root(repo_root_canonical)
    normalized_location = _collapse_internal_whitespace(str(location or ""))
    normalized_location = normalized_location.replace("\\", "/")
    normalized_location = _normalize_windows_drive_prefix(normalized_location)
    normalized_location = re.sub(r"/{2,}", "/", normalized_location)

    if not normalized_root:
        return normalized_location

    if normalized_location == normalized_root:
        return ""

    prefix = normalized_root + "/"
    if normalized_location.startswith(prefix):
        return normalized_location[len(prefix):]

    return normalized_location


def _extract_identifier_context(location: str) -> str:
    """Extract the best available function or identifier token."""

    function_name = _extract_function_name(location)
    if function_name:
        return function_name

    match = _FUNCTION_CONTEXT_RE.search(location)
    if match:
        return match.group(1)

    match = _BRACKETED_IDENTIFIER_RE.search(location)
    if match:
        bracketed_text = _collapse_internal_whitespace(match.group(1))
        if (
            bracketed_text
            and _IDENTIFIER_TOKEN_RE.match(bracketed_text)
            and not _STRUCTURAL_BRACKETED_TOKEN_RE.match(bracketed_text)
        ):
            return bracketed_text

    return ""


def _remove_identifier_context(text: str, identifier: str) -> str:
    """Remove already-extracted identifier context from raw location text."""

    if not identifier:
        return text

    escaped = re.escape(identifier)
    patterns = (
        rf"\b(?:function|method|class|def)\s*[:=]?\s*\(?{escaped}\)?(?:\(\))?",
        rf"\b{escaped}\(\)",
        rf"\({escaped}\)",
        rf"(?<![A-Za-z0-9_]){escaped}(?![A-Za-z0-9_])",
    )
    result = text
    for pattern in patterns:
        result = re.sub(pattern, " ", result, flags=re.IGNORECASE)
    return result


def _remove_line_range(text: str, line_range: str) -> str:
    """Remove the extracted line or line-range wrapper from raw text."""

    if not line_range:
        return text

    result = re.sub(
        rf"\b[Ll]ines?\s+{re.escape(line_range)}\b",
        " ",
        text,
    )
    result = result.replace(f":{line_range}", " ")
    return result


def _should_preserve_identifier_case(token: str) -> bool:
    """Return True when *token* looks like an identifier rather than boilerplate."""

    if not token or not _IDENTIFIER_TOKEN_RE.match(token):
        return False

    if any(char.isupper() for char in token):
        return True

    if "_" in token or "." in token:
        return True

    return False


def _normalize_remainder_text(text: str) -> str:
    """Normalize the residual location text after structured extraction."""

    if not text:
        return ""

    result = _STRUCTURAL_WORD_RE.sub(" ", text)
    result = re.sub(r"[\[\]{}()]+", " ", result)
    result = re.sub(r"[,:;]+", " ", result)
    result = re.sub(r"[-]{2,}", "-", result)
    result = _collapse_internal_whitespace(result)
    if not result:
        return ""

    normalized_tokens: list[str] = []
    for token in result.split(" "):
        normalized_tokens.append(
            token if _should_preserve_identifier_case(token) else token.lower()
        )
    return " ".join(normalized_tokens).strip(" -|/")


def _canonicalize_path_segment(file_path: str) -> str:
    """Return the canonical path segment for normalized_location.

    Directory segments are treated as structural and lowercased. The final
    filename stem preserves case when it looks identifier-bearing so
    ``UserService.ts`` and ``userservice.ts`` remain distinct on
    case-sensitive filesystems; the extension is lowercased.
    """

    path = file_path.replace("\\", "/")
    path = _normalize_windows_drive_prefix(path)
    path = re.sub(r"/{2,}", "/", path)
    path = path.lstrip("/")
    if not path:
        return ""

    segments = [segment for segment in path.split("/") if segment]
    if not segments:
        return ""

    canonical_segments: list[str] = []
    last_index = len(segments) - 1
    for index, segment in enumerate(segments):
        if index != last_index:
            canonical_segments.append(segment.lower())
            continue

        stem, dot, extension = segment.rpartition(".")
        if not dot:
            canonical_segments.append(
                segment if _should_preserve_identifier_case(segment) else segment.lower()
            )
            continue

        canonical_stem = (
            stem if _should_preserve_identifier_case(stem) else stem.lower()
        )
        canonical_segments.append(f"{canonical_stem}.{extension.lower()}")

    return "/".join(canonical_segments)


def _canonicalize_line_segment(line_range: str) -> str:
    """Return the canonical line or line-range segment."""

    if not line_range:
        return ""
    return line_range


def normalize_location(location: str, repo_root_canonical: str) -> str:
    """Return the v0.7.1 canonical normalized_location string.

    The implementation applies the spec's ordered cleanup rules and, when it
    can recover structure, emits a compact canonical form in semantic order:
    path -> context -> line/range -> remainder. Empty segments are omitted.
    Structural text is lowercased; extracted identifier tokens retain their
    original case.
    """

    normalized = _collapse_internal_whitespace(str(location or ""))
    if not normalized:
        return ""

    normalized = normalized.replace("\\", "/")
    normalized = _normalize_windows_drive_prefix(normalized)
    normalized = re.sub(r"/{2,}", "/", normalized)
    normalized = _strip_canonical_repo_root(normalized, repo_root_canonical)
    normalized = _collapse_internal_whitespace(normalized)
    if not normalized:
        return ""

    raw_file_path = _extract_file_path(normalized)
    file_path = _strip_canonical_repo_root(raw_file_path, repo_root_canonical)
    identifier = _extract_identifier_context(normalized)
    line_range = _extract_line_range(normalized)

    remainder = normalized
    if raw_file_path:
        path_match = _FILE_PATH_RE.search(remainder)
        if path_match:
            start, end = path_match.span(1)
            remainder = remainder[:start] + " " + remainder[end:]
    remainder = _remove_identifier_context(remainder, identifier)
    remainder = _remove_line_range(remainder, line_range)
    remainder = _normalize_remainder_text(remainder)

    segments: list[str] = []
    if file_path:
        segments.append(_canonicalize_path_segment(file_path))
    if identifier:
        segments.append(identifier)
    if line_range:
        segments.append(_canonicalize_line_segment(line_range))
    if remainder:
        segments.append(remainder)

    if segments:
        return ":".join(segments)

    return _normalize_remainder_text(normalized)


def _canonicalize_scope_text(value: object) -> str:
    """Return a compact canonical string for scope-key components."""

    return _collapse_internal_whitespace(str(value or ""))


def _canonicalize_scope_file(file_path: str, repo_root_canonical: str) -> str:
    """Return the canonical file component used in scope-key tiers 1 and 2."""

    candidate = _strip_canonical_repo_root(file_path, repo_root_canonical)
    if not candidate:
        return ""
    return _canonicalize_path_segment(candidate)


def _resolve_normalized_location(
    finding: Mapping[str, object],
    repo_root_canonical: str,
) -> str:
    """Return the explicit or derived normalized location for a finding."""

    normalized_location = _canonicalize_scope_text(
        finding.get("normalized_location", "")
    )
    if normalized_location:
        return normalized_location

    return normalize_location(
        _canonicalize_scope_text(finding.get("location", "")),
        repo_root_canonical,
    )


def _serialize_scope_key(tier: str, **components: str) -> str:
    """Serialize the resolved structural tier into a deterministic scope key."""

    payload = {"tier": tier, **components}
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


def derive_scope_key(finding: Mapping[str, object]) -> str:
    """Return the resolved structural scope key per spec v0.7.1 §4.4.

    A tier resolves only when every field in that tier is non-empty after
    canonicalization. Merely having a key present in the input dict is not
    sufficient.
    """

    repo_root_canonical = _canonicalize_scope_text(
        finding.get("repo_root_canonical", "")
    )
    module = _canonicalize_scope_text(finding.get("module", ""))
    category = _canonicalize_scope_text(finding.get("category", ""))
    skill = _canonicalize_scope_text(finding.get("skill", ""))

    if not skill:
        raise FindingSchemaValidationError("skill is required for scope_key derivation")

    file_path = _canonicalize_scope_file(
        _canonicalize_scope_text(finding.get("file", "")),
        repo_root_canonical,
    )
    function_name = _canonicalize_scope_text(finding.get("function", ""))
    line_range = _canonicalize_scope_text(finding.get("line_range", ""))
    normalized_location = _resolve_normalized_location(
        finding,
        repo_root_canonical,
    )

    if all((module, file_path, function_name, category, skill)):
        return _serialize_scope_key(
            "tier1",
            module=module,
            file=file_path,
            function=function_name,
            category=category,
            skill=skill,
        )

    if all((module, file_path, line_range, category, skill)):
        return _serialize_scope_key(
            "tier2",
            module=module,
            file=file_path,
            line_range=line_range,
            category=category,
            skill=skill,
        )

    if all((module, normalized_location, category, skill)):
        return _serialize_scope_key(
            "tier3",
            module=module,
            location=normalized_location,
            category=category,
            skill=skill,
        )

    if not normalized_location:
        normalized_location = _UNKNOWN_LOCATION

    # Empty or unparseable locations digest the explicit sentinel rather than
    # the empty string so tier-4 scope derivation stays aligned with anchor
    # derivation. Spec note for v0.7.2: clarify that empty normalized
    # locations canonicalize to `_unknown_location` before tier-4 hashing.
    normalized_location_digest = hashlib.sha256(
        normalized_location.encode()
    ).hexdigest()

    return _serialize_scope_key(
        "tier4",
        normalized_location_digest=normalized_location_digest,
        skill=skill,
    )


def derive_anchor_key(
    finding: Mapping[str, object],
    repo_root_canonical: str,
) -> str:
    """Return the anchor key per spec v0.7.1 §4.5."""

    line_range = _canonicalize_scope_text(finding.get("line_range", ""))
    if line_range:
        return line_range

    normalized_location = _resolve_normalized_location(
        finding,
        repo_root_canonical,
    )

    return normalized_location or _UNKNOWN_LOCATION


def derive_subject_component(
    finding: Mapping[str, object],
    definition: FindingKindDefinition,
) -> str:
    """Return the subject component for F2 composition."""

    if definition.subject_required == "n/a":
        return "_na"

    subject_key = _canonicalize_scope_text(finding.get("subject_key", ""))
    if subject_key:
        return subject_key

    if definition.subject_required == "optional":
        return "_none"

    raise FindingSchemaValidationError(
        f"subject_key is required for finding_kind '{definition.kind_name}'"
    )


def compose_f2_signature(
    finding: Mapping[str, object],
    definition: FindingKindDefinition,
    repo_root_canonical: str,
) -> str:
    """Compose and hash the v2 F2 signature into a stable finding ID."""

    scope_finding = dict(finding)
    if repo_root_canonical and not scope_finding.get("repo_root_canonical"):
        scope_finding["repo_root_canonical"] = repo_root_canonical

    scope_key = derive_scope_key(scope_finding)
    subject_component = derive_subject_component(finding, definition)
    anchor_key = derive_anchor_key(finding, repo_root_canonical)

    signature_v2_input = (
        f"v2|scope={scope_key}|kind={definition.kind_name}"
        f"|subject={subject_component}|anchor={anchor_key}"
    )
    digest = hashlib.sha256(signature_v2_input.encode()).hexdigest()
    return f"F2-{digest[:12]}"


def generate_finding_id_v2(
    finding: Mapping[str, object],
    emitter_name: str,
    repo_root_canonical: str,
) -> str:
    """Validate a finding and return its F2 ID.

    Validation errors are propagated directly; there is no fallback to the
    legacy ID path in v2 generation.
    """

    definition = validate_finding_kind_fields(
        finding,
        emitter_name=emitter_name,
    )
    return compose_f2_signature(
        finding,
        definition,
        repo_root_canonical,
    )


# ── Step 2: Blob Flattening ──────────────────────────────────────────

_BLOB_SEVERITY_MAP: dict[str, str] = {
    "critical": "Critical",
    "p1": "P1",
    "p2": "P2",
    "p3": "P3",
}

# Upper bound on text length passed to ast.literal_eval, to bound the CPU/memory
# cost of parsing deeply nested untrusted literals (CWE-502).
_LITERAL_EVAL_MAX_CHARS = 50_000


def _parse_blob_dict(text: str) -> dict | None:
    """Try to parse *text* as a dict using JSON then ``ast.literal_eval``."""
    text = text.strip()
    if not text.startswith("{"):
        return None
    # JSON first (double-quoted keys)
    try:
        obj = json.loads(text)
        if isinstance(obj, dict):
            return obj
    except (json.JSONDecodeError, ValueError):
        pass
    # Python dict syntax (single-quoted keys). ast.literal_eval can be driven
    # into high CPU/memory by deeply nested literals (CWE-502), so skip it for
    # oversized untrusted text.
    if len(text) > _LITERAL_EVAL_MAX_CHARS:
        return None
    try:
        obj = ast.literal_eval(text)
        if isinstance(obj, dict):
            return obj
    except (ValueError, SyntaxError):
        pass
    return None


def is_blob_finding(description: str) -> bool:
    """Return True if *description* looks like a blob severity dict.

    A blob finding is a JSON/Python dict whose keys overlap with the known
    severity levels (``critical``, ``p1``, ``p2``, ``p3``).
    """
    if not description:
        return False
    obj = _parse_blob_dict(description)
    if obj is None:
        return False
    # Match flatten_blob's case-insensitive key handling: normalise blob keys
    # to lowercase before intersecting with the (lowercase) severity map keys,
    # so a blob like {"P1": [...]} is recognised here just as it is flattened.
    lowered_keys = {str(k).lower() for k in obj.keys()}
    return bool(lowered_keys & set(_BLOB_SEVERITY_MAP.keys()))


def flatten_blob(
    blob_str: str,
    module_name: str,
    skill_name: str,
    repo_root_canonical: str = "",
) -> list[dict]:
    """Flatten a blob severity dict into individual structured findings.

    Each item under a recognised severity key becomes a separate finding
    with proper Schema V2 fields (id, category, file, function, line_range).

    Args:
        blob_str: The raw blob string (JSON or Python dict syntax).
        module_name: Module the finding belongs to.
        skill_name: Skill that produced the finding.

    Returns:
        List of normalised finding dicts.
    """
    obj = _parse_blob_dict(blob_str)
    if obj is None:
        return []

    findings: list[dict] = []
    for key, items in obj.items():
        severity = _BLOB_SEVERITY_MAP.get(key.lower())
        if severity is None:
            continue
        if not isinstance(items, list):
            items = [items]
        for item in items:
            if isinstance(item, dict):
                desc = str(item.get("description", item.get("issue", str(item))))
                # A structured location/file of ``None`` must coerce to the empty
                # string, not the literal ``"None"`` — ``str(None)`` would poison
                # downstream path parsing and F2 anchor derivation.
                location = str(item.get("location") or item.get("file") or "")
                producer_fields = dict(item)
            else:
                desc = str(item)
                location = ""
                producer_fields = {}

            flattened = {
                "module": module_name,
                "skill": skill_name,
                "severity": severity,
                "category": producer_fields.get("category", _classify_category(desc)),
                "location": location,
                "file": producer_fields.get("file", _extract_file_path(location)),
                "function": producer_fields.get("function", _extract_function_name(location)),
                "line_range": producer_fields.get("line_range", _extract_line_range(location)),
                "description": desc,
                "suggested_fix": producer_fields.get("suggested_fix", ""),
                "status": producer_fields.get("status", "open"),
            }
            for identity_field in (
                "finding_kind",
                "subject_key",
                "normalized_location",
                "legacy_finding_id",
            ):
                if identity_field in producer_fields:
                    flattened[identity_field] = producer_fields[identity_field]

            # Isolate each blob item: one malformed entry must not abort the
            # whole call and drop every remaining finding. Log and continue so
            # the surviving entries still flatten (error-signal preservation).
            try:
                findings.append(materialize_emitter_finding(
                    flattened,
                    emitter_name=skill_name,
                    repo_root_canonical=repo_root_canonical,
                ))
            except Exception as e:  # noqa: BLE001 — per-item isolation is the point
                log.warning(
                    "Skipping malformed blob item in module %r (skill %r): %s",
                    module_name,
                    skill_name,
                    e,
                )
                continue

    return findings
