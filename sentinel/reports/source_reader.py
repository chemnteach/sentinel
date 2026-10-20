from __future__ import annotations

import re
from pathlib import Path

# Multi-hump CamelCase identifiers (e.g. GetAllObjectAttributes, AreAllWafersComplete)
# — reliable construct names to search for; excludes single-word words like
# "Configure"/"Replace" that appear in prose and would over-match.
_CONSTRUCT_IDENT_RE = re.compile(r"\b[A-Z][a-z0-9]*[A-Z][A-Za-z0-9]{2,}\b")


def _contained_path(root: Path, file_rel: str) -> Path | None:
    """Resolve ``root / file_rel`` and return it only when it stays inside
    ``root`` — rejecting absolute paths and ``..`` traversal that would escape
    the repo (CWE-22). Returns None on escape or an unresolvable path.

    ``finding["file"]`` can be attacker-influenced when assessing an untrusted
    repository, so render-time file access must be constrained to the repo root.
    """
    try:
        root_resolved = root.resolve()
        candidate = (root / file_rel).resolve()
    except (OSError, ValueError):
        return None
    if candidate != root_resolved and not candidate.is_relative_to(root_resolved):
        return None
    return candidate


def read_finding_source(
    finding: dict,
    repo_root: str,
    context_lines: int = 5,
    max_lines: int = 60,
) -> str | None:
    """Extract relevant source lines for
    a finding.

    Tries finding["file"] + line_range
    first. Falls back to searching for
    the function named in location.
    Returns up to ``max_lines`` lines with a
    header comment, or None if the file cannot
    be read. ``context_lines`` sets how many
    lines above/below the cited range are
    included — widen it (with a larger
    ``max_lines``) when a finding's cited line
    number may be imprecise and the relevant
    code sits nearby (e.g. the diff previewer).
    """
    file_rel = str(
        finding.get("file", "") or "")
    line_range = str(
        finding.get("line_range", "") or "")
    location = str(
        finding.get("location", "") or "")

    # Resolve relative file path
    if not file_rel:
        if ":" in location:
            file_rel = location.split(
                ":")[0].strip()

    if not file_rel:
        return None

    root = Path(repo_root)
    # Only accept candidates that resolve INSIDE repo_root — an absolute or
    # ../-traversal file path must not read files outside it. (FIX-CRITICAL
    # run-019: path-traversal read)
    source_path: Path | None = None
    for rel in (file_rel, f"sentinel/{file_rel}"):
        c = _contained_path(root, rel)
        if c is not None and c.is_file():
            source_path = c
            break

    if source_path is None:
        return None

    try:
        raw = source_path.read_text(
            encoding="utf-8",
            errors="replace",
        )
        lines = raw.splitlines()
    except Exception:
        return None

    # Parse line range if available
    start_line: int | None = None
    end_line: int | None = None
    if line_range:
        try:
            # Atomic: only commit both bounds on a full parse, so a malformed
            # range like "42-" (start parses, end does not) falls back to the
            # function-name search instead of leaving end_line None -> TypeError.
            parts = line_range.split("-")
            _s = int(parts[0].strip()) - 1
            _e = int(parts[-1].strip())
            start_line, end_line = _s, _e
        except (ValueError, IndexError):
            pass

    # Use the line range only when it actually falls inside the file. Models
    # frequently emit line numbers past EOF (e.g. a 317-line file cited at
    # "592-651"); in that case — or with no/again malformed range — fall back
    # to locating the function/class named in `location`, which is reliable
    # when line numbers are not.
    relevant: list[str] | None = None
    header = ""
    if (
        start_line is not None
        and end_line is not None
        and start_line < len(lines)
    ):
        ctx_start = max(0, start_line - context_lines)
        ctx_end = min(len(lines), end_line + context_lines)
        window = lines[ctx_start:ctx_end]
        if window:
            relevant = window
            header = f"# {source_path.name} lines {ctx_start + 1}–{ctx_end}"

    if relevant is None:
        # Find function/class by name. Tolerate a trailing ", lines N-M" or
        # other tokens after the symbol, and strip any class prefix.
        func_name = ""
        if ":" in location:
            tail = location.split(":")[-1].strip()
            tail = tail.split(",")[0].strip()   # drop ", lines N-M"
            tail = tail.split(" ")[0].strip()   # first token only
            func_name = tail.split(".")[-1] if "." in tail else tail

        start: int | None = None
        if func_name:
            for i, line in enumerate(lines):
                stripped = line.strip()
                if (
                    stripped.startswith(f"def {func_name}")
                    or stripped.startswith(f"async def {func_name}")
                    or stripped.startswith(f"class {func_name}")
                ):
                    start = i
                    break

        if start is not None:
            relevant = lines[start:start + max_lines]
            header = f"# {source_path.name} line {start + 1}"
        else:
            relevant = lines[:min(40, max_lines)]
            header = f"# {source_path.name}"

    # Cap at max_lines
    relevant = relevant[:max_lines]
    if not relevant:
        return None

    return header + "\n" + "\n".join(relevant)


def read_symbol_source(
    finding: dict,
    repo_root: str,
    hint_text: str = "",
    context_lines: int = 60,
    max_lines: int = 140,
) -> str | None:
    """Locate the named construct a finding/change references and return a
    line-numbered window around its first occurrence in the file.

    Complements ``read_finding_source`` for the diff previewer: when a finding's
    cited line number is imprecise, the relevant method/element can sit far from
    the cited window. The ``read_finding_source`` fallback only searches for
    Python ``def``/``class``; this one is language-agnostic (a plain identifier
    search) so it also finds C# methods / XML elements / VBS routines. Candidate
    symbols come from ``finding['function']``, the tail of ``finding['location']``,
    and multi-hump CamelCase identifiers in ``hint_text`` (the proposed changes).
    Returns None when no candidate symbol resolves in the file.
    """
    file_rel = str(finding.get("file", "") or "")
    location = str(finding.get("location", "") or "")
    if not file_rel and ":" in location:
        file_rel = location.split(":")[0].strip()
    if not file_rel:
        return None

    root = Path(repo_root)
    source_path: Path | None = None
    for rel in (file_rel, f"sentinel/{file_rel}"):
        c = _contained_path(root, rel)
        if c is not None and c.is_file():
            source_path = c
            break
    if source_path is None:
        return None
    try:
        lines = source_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception:
        return None

    # Candidate construct names, best signal first.
    syms: list[str] = []
    seen: set[str] = set()

    def _add(tok: str) -> None:
        tok = tok.strip()
        if len(tok) >= 4 and tok not in seen:
            seen.add(tok)
            syms.append(tok)

    fn = str(finding.get("function", "") or "").strip()
    if fn:
        _add(fn)
    if ":" in location:
        for tok in re.findall(r"[A-Za-z_][A-Za-z0-9_]{3,}", location.split(":", 1)[-1]):
            _add(tok)
    for tok in _CONSTRUCT_IDENT_RE.findall(hint_text or ""):
        _add(tok)

    if not syms:
        return None

    # First line that mentions any candidate symbol (search top-priority syms first).
    best: int | None = None
    for s in syms[:8]:
        for i, l in enumerate(lines):
            if s in l:
                best = i
                break
        if best is not None:
            matched = s
            break
    if best is None:
        return None

    start = max(0, best - context_lines)
    end = min(len(lines), best + context_lines)
    window = lines[start:end][:max_lines]
    if not window:
        return None
    return (
        f"# {source_path.name} near '{matched}' (lines {start + 1}-{start + len(window)})\n"
        + "\n".join(window)
    )


def resolve_start_line(
    finding: dict,
    repo_root: str,
) -> int | None:
    """Find the start line of the function
    named in finding["location"] within
    the source file.

    Used to back-fill line numbers for
    findings that have file + function
    but no line_range.

    Returns 1-based line number or None.
    """
    file_rel = str(
        finding.get("file", "") or "")
    location = str(
        finding.get("location", "") or "")
    line_range = str(
        finding.get("line_range", "") or "")

    # If line_range already present,
    # no need to resolve
    if line_range:
        return None

    if not file_rel:
        if ":" in location:
            file_rel = location.split(
                ":")[0].strip()
    if not file_rel:
        return None

    root = Path(repo_root).resolve()
    candidates = [
        root / file_rel,
        root / "sentinel" / file_rel,
    ]
    source_path: Path | None = None
    for c in candidates:
        resolved = c.resolve()
        if resolved.is_relative_to(root) and resolved.exists():
            source_path = resolved
            break
    if source_path is None:
        return None

    func_name = ""
    if ":" in location:
        func_name = location.split(
            ":")[-1].strip()
        if "." in func_name:
            func_name = func_name.split(
                ".")[-1]

    if not func_name:
        return None

    try:
        lines = source_path.read_text(
            encoding="utf-8",
            errors="replace",
        ).splitlines()
    except Exception:
        return None

    for i, line in enumerate(lines):
        stripped = line.strip()
        if (
            stripped.startswith(
                f"def {func_name}")
            or stripped.startswith(
                f"async def {func_name}")
            or stripped.startswith(
                f"class {func_name}")
        ):
            return i + 1  # 1-based

    return None


def vscode_link(
    finding: dict,
    repo_root: str,
) -> str | None:
    """Build a VS Code deep-link URI for
    a finding. Includes line number when
    line_range is available.

    Returns None if no file can be
    resolved.
    """
    file_rel = str(
        finding.get("file", "") or "")
    line_range = str(
        finding.get("line_range", "") or "")
    location = str(
        finding.get("location", "") or "")

    if not file_rel:
        if ":" in location:
            file_rel = location.split(
                ":")[0].strip()

    if not file_rel:
        return None

    root = Path(repo_root)
    # Reject a file path that escapes the repo root (absolute or ../ traversal)
    # rather than emitting a link that points outside it. (FIX-CRITICAL run-019:
    # unvalidated vscode URI)
    contained = _contained_path(root, file_rel)
    if contained is None:
        return None
    candidates = [
        root / file_rel,
        root / "sentinel" / file_rel,
    ]

    resolved: Path | None = None
    for c in candidates:
        if c.exists():
            resolved = c
            break

    if resolved is None:
        # Best-guess path — already verified to be within the repo root.
        resolved = contained

    # Normalize to forward slashes
    uri = "vscode://file/" + str(
        resolved).replace("\\", "/")

    # Add line number if available
    if line_range:
        try:
            start = int(
                line_range.split(
                    "-")[0].strip())
            uri += f":{start}"
        except (ValueError, IndexError):
            pass
    else:
        # Resolve from function name
        resolved_line = resolve_start_line(
            finding, repo_root)
        if resolved_line:
            uri += f":{resolved_line}"

    return uri


def github_link(
    finding: dict,
    base_url: str,
    ref: str,
    path_prefix: str = "",
) -> str | None:
    """Build a commit-pinned GitHub permalink for a finding.

    Returns ``<base_url>/blob/<ref>/<path_prefix>/<file>#L<line>`` (line included
    when one is available) so a recipient can open the code in the browser
    without a local checkout — pinned to ``ref`` (a commit SHA) so the ``#L``
    anchors stay accurate as the repo changes after assessment.

    ``file`` is taken relative to the assessed root. When the assessment scanned
    a *subtree* of the repo (e.g. ``Contoso.Line.Tool/``), the finding paths are
    subtree-relative and ``path_prefix`` supplies the missing repo-root-relative
    prefix so the URL resolves; leave it empty when finding paths are already
    repo-root-relative. Pure string-builder: no filesystem access — the line
    number is read from ``line_range`` or, as a fallback, ``finding["_resolved_line"]``.

    Returns None when ``base_url``, ``ref``, or the file path is missing —
    callers treat a None link as "no GitHub permalink for this finding".
    """
    if not base_url or not ref:
        return None

    file_rel = str(finding.get("file", "") or "")
    line_range = str(finding.get("line_range", "") or "")
    location = str(finding.get("location", "") or "")

    if not file_rel and ":" in location:
        file_rel = location.split(":")[0].strip()
    if not file_rel:
        return None

    # Normalize to a repo-relative POSIX path (no leading slash, no backslashes),
    # prepending the subtree prefix when finding paths are subtree-relative.
    path = file_rel.replace("\\", "/").lstrip("/")
    prefix = str(path_prefix or "").replace("\\", "/").strip("/")
    if prefix:
        path = f"{prefix}/{path}"
    url = f"{base_url.rstrip('/')}/blob/{ref}/{path}"

    line: int | None = None
    if line_range:
        try:
            line = int(line_range.split("-")[0].strip())
        except (ValueError, IndexError):
            line = None
    if line is None:
        resolved = finding.get("_resolved_line")
        if isinstance(resolved, int):
            line = resolved
    if line:
        url += f"#L{line}"

    return url
