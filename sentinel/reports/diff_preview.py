"""Illustrative diff preview for the Decision Brief (Part A′).

Under ``--preview-diffs``, one LLM call per remediation *bundle* sketches an
**illustrative** unified diff showing roughly what the fix might look like, given
only the cited source. The result is embedded (collapsed) in the static brief so
it works offline on a shared drive — there is no backend at view time.

The diff is NOT the governed change. Phase 2 regenerates the real edit from
scratch under TDD / Architect / QA gates; this preview only helps a reviewer
gauge precise impact at triage time. It is labelled as such wherever it renders
and is deliberately kept out of the exported decisions JSON.

This mirrors ``consequence_synth.py``: cache-first, only-uncached calls, atomic
cache writes, graceful degradation (any failure -> no diff, brief still renders).
Two differences that matter:
  * Source is **neutralized here** (``_strip_invisible`` + ``_neutralize_code_fences``)
    before it ever reaches a prompt — the brief's ``_source`` is NOT prompt-safe
    (``reports/source_reader.read_finding_source`` does not neutralize).
  * The cache key hashes both the source AND the proposed-change prose, because
    the prose (Stage 5b output) is the instruction being illustrated and can
    change independently of the source.
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sys
from pathlib import Path

from sentinel.orchestrator.source_retrieval import (
    _neutralize_code_fences,
    _strip_invisible,
)

log = logging.getLogger(__name__)

_CACHE_FILENAME = "diff_preview_cache.json"
_MAX_SOURCE_CHARS = 16000
_MAX_DIFF_CHARS = 4000

DISCLAIMER = (
    "Illustrative preview generated at report time — NOT the governed Phase 2 "
    "change. Phase 2 regenerates the real edit under TDD/Architect/QA review. "
    "Approve or defer based on the finding and acceptance criteria, not this diff."
)


def _sanitize_source(text: str) -> str:
    """Strip invisible codepoints and defuse code fences in source before it is
    injected into a prompt. Mandatory: the LLM gateway rejects invisible chars
    (retryable:false) and a raw ``` run in customer source can break out of the
    prompt fence. The brief's ``_source`` has had neither treatment."""
    return _neutralize_code_fences(_strip_invisible(text or ""))


def _input_hash(source: str, proposed_changes: list[str]) -> str:
    """Stable hash of the two inputs that determine the diff: the (sanitized)
    source and the proposed-change prose. Changing either invalidates the cache."""
    h = hashlib.sha256()
    h.update(_sanitize_source(source).encode("utf-8"))
    h.update(b"\x00")
    h.update("\n".join(str(c) for c in (proposed_changes or [])).encode("utf-8"))
    return h.hexdigest()


def load_cache(state_dir: str) -> dict[str, dict]:
    """Load cached diffs -> ``{unit_id: {"input_hash": ..., "diff": ...}}``.

    Returns empty dict if the cache is absent or unreadable.
    """
    path = Path(state_dir) / _CACHE_FILENAME
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        units = data.get("units")
        return units if isinstance(units, dict) else {}
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to load diff-preview cache: %s", exc)
        return {}


def save_cache(state_dir: str, cache: dict[str, dict]) -> bool:
    """Persist the diff cache atomically (temp-file + rename). Returns True on
    success; a write failure is logged and swallowed so rendering never aborts."""
    path = Path(state_dir) / _CACHE_FILENAME
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(
            json.dumps({"units": cache}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(path)
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("Failed to save diff-preview cache: %s", exc)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass
        return False


def _build_prompt(unit: dict) -> str:
    """Build the illustrative-diff prompt for one unit (inline, not a skill file —
    this is a report-time helper, not a governed pipeline stage)."""
    title = str(unit.get("title", "") or "")
    files = [str(x) for x in (unit.get("affected_files") or [])]
    changes = [str(c) for c in (unit.get("proposed_changes") or [])]
    source = _sanitize_source(str(unit.get("source", "") or ""))[:_MAX_SOURCE_CHARS]

    files_line = ", ".join(files) if files else "(not specified)"
    changes_block = "\n".join(f"  - {c}" for c in changes) or "  - (none provided)"

    return (
        "You are a senior engineer sketching a concrete ILLUSTRATIVE edit that "
        "would address the proposed changes below, using ONLY the cited source.\n\n"
        "This is a preview to help a reviewer gauge impact — it is illustrative, "
        "not the final change, and will not be applied (a later gated pass "
        "re-derives the real fix under tests). Prefer to SHOW a plausible concrete "
        "edit rather than withhold one: pick the single most representative change "
        "and sketch it. Ground every line in the cited source — edit only the "
        "file(s) shown, and do NOT invent files, APIs, types, methods, or "
        "identifiers that are not present in the cited source. The cited source "
        "may be code OR configuration / markup / data (XML, JSON, .config, .xml, "
        ".vbs, etc.) — sketch a concrete edit in whatever form the source takes "
        "(e.g. removing a plaintext credential or secret line, changing an "
        "insecure port, replacing an FTP endpoint with SFTP, tightening a "
        "permission value). Respond with exactly NO_DIFF only if there is "
        "genuinely no line in the cited source you could change to address the "
        "proposed changes.\n\n"
        f"UNIT: {title}\n"
        f"AFFECTED FILES: {files_line}\n"
        f"PROPOSED CHANGES:\n{changes_block}\n\n"
        "CITED SOURCE:\n"
        "-----\n"
        f"{source}\n"
        "-----\n\n"
        "Respond with a single minimal unified diff (--- / +++ / @@ hunks) and "
        "nothing else — no prose, no markdown fences, no explanation. Keep it to "
        "the few lines that actually change."
    )


# A removed line shorter than this (whitespace stripped) is structural (``}``,
# ``{``, ``return;``) — too common to ground meaningfully, so grounding skips it.
_MIN_GROUNDED_LEN = 4


def _removed_lines_grounded(diff: str, source: str) -> bool:
    """True when every substantive removed (``-``) line in ``diff`` actually
    appears in ``source`` (whitespace-insensitive).

    Guards against a misleading "current code": the illustrative diff must delete
    lines that exist at the finding's cited location, not a plausible-looking line
    the model lifted from a different method (a wide source window can span
    several) or invented outright. File headers (``--- ``/``+++ ``) and trivial
    structural lines are ignored. No source -> not enforced."""
    if not source:
        return True
    src_norm = re.sub(r"\s+", "", source)
    for line in str(diff).splitlines():
        if line.startswith("--- ") or line.startswith("+++ "):
            continue  # file headers, not code
        if not line.startswith("-"):
            continue
        removed = re.sub(r"\s+", "", line[1:])
        if len(removed) < _MIN_GROUNDED_LEN:
            continue  # trivial/structural line
        if removed not in src_norm:
            return False
    return True


def _parse_response(
    text: str, affected_files: list[str], source: str | None = None,
) -> str | None:
    """Validate a model diff response. Returns the cleaned diff, or None when the
    model declined (NO_DIFF), the output is empty, it references none of the
    affected files, or its removed lines aren't grounded in ``source`` (a guard
    against a misleading "current code" — see ``_removed_lines_grounded``)."""
    if text is None:
        return None
    cleaned = str(text).strip()
    if not cleaned or cleaned.upper().startswith("NO_DIFF"):
        return None

    # Strip a leading/trailing ```diff fence if the model added one anyway.
    if cleaned.startswith("```"):
        lines = cleaned.splitlines()
        if lines and lines[-1].strip() == "```":
            lines = lines[1:-1]
        else:
            lines = lines[1:]
        cleaned = "\n".join(lines).strip()
    if not cleaned:
        return None

    # A real diff should carry hunk/file markers. Reject free-form prose that
    # slipped past the "diff only" instruction.
    if not any(marker in cleaned for marker in ("@@", "--- ", "+++ ", "\n+", "\n-")):
        return None

    # Guard against hallucinated edits: the diff must mention at least one of the
    # unit's affected files (by basename) when we know them.
    basenames = [Path(f).name for f in (affected_files or []) if f]
    if basenames and not any(b and b in cleaned for b in basenames):
        return None

    # The removed ("current code") lines must exist at the cited source.
    if not _removed_lines_grounded(cleaned, _sanitize_source(source or "")):
        return None

    return cleaned[:_MAX_DIFF_CHARS]


def generate_diff(unit: dict, client) -> str | None:
    """Call LLM to sketch one illustrative diff. Returns the diff string, or
    None on any failure / decline (caller renders no diff for the unit)."""
    if client is None:
        return None
    prompt = _build_prompt(unit)
    try:
        resp = client.call_model(
            system_prompt=(
                "You produce a single minimal unified diff and nothing else. "
                "No markdown fences. No prose. Output NO_DIFF if you cannot."
            ),
            user_message=prompt,
        )
        text = resp.content if hasattr(resp, "content") else resp
        return _parse_response(
            str(text), unit.get("affected_files") or [], unit.get("source"),
        )
    except Exception as exc:  # noqa: BLE001
        log.warning(
            "Diff preview failed for unit %s: %s", unit.get("id", "?"), exc
        )
        return None


def generate_all(units: list[dict], state_dir: str, client) -> dict[str, str]:
    """Generate illustrative diffs for all units -> ``{unit_id: diff}``.

    Cache-first: only calls LLM for units whose (source + prose) hash is not
    already cached; saves the updated cache after. Units that decline or fail are
    left out of the result (renderer shows no diff). Progress to stderr.
    """
    cache = load_cache(state_dir)
    result: dict[str, str] = {}
    new_calls = 0
    total = len(units)

    hits = 0
    for unit in units:
        uid = str(unit.get("id", "") or "")
        if uid and uid in cache and cache[uid].get("input_hash") == _input_hash(
            str(unit.get("source", "") or ""), unit.get("proposed_changes") or []
        ):
            hits += 1
    print(
        f"Diff preview: {hits} cached, {total - hits} to generate ({total} units)",
        file=sys.stderr,
    )

    done = 0
    for unit in units:
        uid = str(unit.get("id", "") or "")
        if not uid:
            continue
        ihash = _input_hash(
            str(unit.get("source", "") or ""), unit.get("proposed_changes") or []
        )
        cached = cache.get(uid)
        if cached and cached.get("input_hash") == ihash and cached.get("diff"):
            result[uid] = cached["diff"]
            done += 1
            continue

        diff = generate_diff(unit, client)
        if diff:
            cache[uid] = {"input_hash": ihash, "diff": diff}
            result[uid] = diff
            new_calls += 1
        # Decline/failure: leave uncached so a later run can retry.

        done += 1
        if done % 25 == 0 or done == total:
            print(
                f"  {done}/{total} units processed ({new_calls} new LLM calls)",
                file=sys.stderr,
            )

    if new_calls > 0:
        if save_cache(state_dir, cache):
            print(
                f"Diff cache saved: {len(cache)} entries -> "
                f"{state_dir}/{_CACHE_FILENAME}",
                file=sys.stderr,
            )
        else:
            print(
                f"Diff cache save FAILED -> {state_dir}/{_CACHE_FILENAME} "
                "(see log); continuing with in-memory results",
                file=sys.stderr,
            )

    return result
