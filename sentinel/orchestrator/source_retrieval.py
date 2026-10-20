from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

_CONTEXT_LINES = 20
_MAX_WHOLE_FILE_LINES = 300
_FUNCTION_WINDOW_LINES = 80

# Zero-width / invisible code points that trip the LLM gateway's
# ASCII-smuggling (prompt-injection) filter, which rejects the entire call with
# retryable:false. Customer source saved as UTF-8-with-BOM embeds U+FEFF, which
# would otherwise blank out the verification call for that file; the rest are
# zero-width / bidi / smuggling vectors. Stripping them also defends against
# genuine ASCII-smuggling hidden in untrusted source. Built via chr() so this
# file stays pure ASCII — no literal invisibles of the very kind we strip.
_INVISIBLE_CODEPOINTS = (
    0x00AD,                     # soft hyphen
    *range(0x200B, 0x2010),     # zero-width space/joiner/non-joiner, LRM, RLM
    0x2028, 0x2029,             # line / paragraph separators
    *range(0x202A, 0x202F),     # bidi embedding / override
    *range(0x2060, 0x2065),     # word joiner + invisible math operators
    0xFEFF,                     # BOM / zero-width no-break space
)
_INVISIBLE_CHARS_RE = re.compile(
    "[" + "".join(chr(c) for c in _INVISIBLE_CODEPOINTS) + "]"
)


def _strip_invisible(text: str) -> str:
    """Remove zero-width / invisible control characters from source before it is
    injected into a model prompt. See ``_INVISIBLE_CHARS_RE``."""
    return _INVISIBLE_CHARS_RE.sub("", text)


def _normalize_path(file_field: str,
                    repo_root: Path) -> Path | None:
    """Resolve a finding file path to an absolute
    path under repo_root.

    Findings use subtree-relative paths
    (e.g. assess/reviewer.py). The scanned subtree
    is sentinel/ under the repo root.
    """
    if not file_field:
        return None
    candidate = repo_root / "sentinel" / file_field
    if candidate.exists():
        return candidate
    # Try without the sentinel/ prefix in case
    # the finding already has it
    candidate2 = repo_root / file_field
    if candidate2.exists():
        return candidate2
    return None


def get_source_for_finding(
    finding: dict,
    repo_root: str | Path,
) -> str:
    """Return the relevant source snippet for a
    finding, for injection into the verifiability
    gate context.

    Uses line_range when available (pinpoint mode);
    falls back to whole-file (capped at
    _MAX_WHOLE_FILE_LINES lines).
    """
    repo_root = Path(repo_root)
    file_field = finding.get("file", "")
    line_range = finding.get("line_range", "")

    path = _normalize_path(file_field, repo_root)
    if path is None:
        log.warning(
            "source_retrieval: cannot locate %r "
            "under %s",
            file_field, repo_root,
        )
        return f"# Source not found: {file_field}"

    try:
        lines = _strip_invisible(
            path.read_text(encoding="utf-8", errors="replace")
        ).splitlines()
    except OSError as exc:
        log.warning(
            "source_retrieval: cannot read %s: %s",
            path, exc,
        )
        return f"# Cannot read {file_field}: {exc}"

    # Pinpoint mode: line_range = "N" or "N-M"
    if line_range:
        try:
            parts = line_range.split("-")
            start = max(0, int(parts[0]) - 1
                        - _CONTEXT_LINES)
            end = (int(parts[-1]) + _CONTEXT_LINES
                   if len(parts) > 1
                   else int(parts[0]) + _CONTEXT_LINES)
            end = min(len(lines), end)
            snippet = lines[start:end]
            if snippet:
                return (
                    f"# {file_field} "
                    f"(lines {start+1}-{end})\n"
                    + "\n".join(
                        f"{start+i+1:4d}  {l}"
                        for i, l in enumerate(snippet)
                    )
                )
            # Empty slice = stale line_range beyond EOF
            # Fall through to function-aware mode
        except (ValueError, IndexError):
            pass  # fall through to whole-file

    # Function-aware mode: locate the cited
    # function by name
    function_name = finding.get("function", "")
    if function_name:
        import re as _re
        pattern = _re.compile(
            rf"^\s*(async\s+)?def\s+{_re.escape(function_name)}\s*[\(:]",
            _re.MULTILINE,
        )
        for i, line in enumerate(lines):
            if pattern.match(line):
                end = min(len(lines),
                          i + _FUNCTION_WINDOW_LINES)
                snippet = lines[i:end]
                return (
                    f"# {file_field} — "
                    f"def {function_name} "
                    f"(lines {i+1}-{end})\n"
                    + "\n".join(
                        f"{i+j+1:4d}  {l}"
                        for j, l in enumerate(snippet)
                    )
                )
        log.warning(
            "source_retrieval: function %r not "
            "found in %s — falling back to "
            "whole-file",
            function_name, file_field,
        )

    # Whole-file mode (capped)
    capped = lines[:_MAX_WHOLE_FILE_LINES]
    suffix = (
        f"\n# ... ({len(lines) - _MAX_WHOLE_FILE_LINES}"
        " more lines not shown)"
        if len(lines) > _MAX_WHOLE_FILE_LINES
        else ""
    )
    return (
        f"# {file_field} (first "
        f"{len(capped)} lines)\n"
        + "\n".join(
            f"{i+1:4d}  {l}"
            for i, l in enumerate(capped)
        )
        + suffix
    )


_CODE_FENCE_RE = re.compile(r"`{3,}")


def _neutralize_code_fences(text: str) -> str:
    """Break any run of three-or-more backticks by inserting a single space
    between them, so repository content embedded inside a ```python fence can
    never terminate that fence and inject into the surrounding prompt. A space
    (not a zero-width character) is used deliberately: an invisible separator
    would itself trip the LLM gateway's ASCII-smuggling filter and reject the
    whole call. The backtick glyphs are preserved (content stays readable); only
    the literal consecutive run is broken."""
    return _CODE_FENCE_RE.sub(
        lambda m: " ".join("`" * len(m.group())), text
    )


def build_verifiability_context(
    findings: list[dict],
    repo_root: str | Path,
) -> str:
    """Build the context string for the
    verifiability gate skill.

    Returns a formatted string with each finding's
    claim + cited source, ready for injection.
    """
    sections = []
    for f in findings:
        fid = f.get("id", "unknown")
        claim = (
            f"ID: {fid}\n"
            f"finding_kind: {f.get('finding_kind','')}\n"
            f"subject_key: {f.get('subject_key','')}\n"
            f"description: {f.get('description','')}\n"
        )
        # Neutralize any ``` run in repository content so an attacker-controlled
        # source file cannot close the ```python fence early and inject
        # instructions into the verifiability-gate prompt. (FIX-CRITICAL
        # run-019: fenced-block prompt injection)
        source = _neutralize_code_fences(get_source_for_finding(f, repo_root))
        sections.append(
            f"## Finding {fid}\n\n"
            f"### Claim\n{claim}\n"
            f"### Cited Source\n```python\n"
            f"{source}\n```\n"
        )
    return "\n---\n".join(sections)
