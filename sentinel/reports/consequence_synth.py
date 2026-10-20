"""LLM consequence personalization for the Decision Brief (cycle 53).

Under ``--synthesize``, one LLM call per finding rewrites the static
``consequences.yaml`` best/likely/worst template into a description specific to
the actual code (using ``read_finding_source`` output as context). Results are
cached by finding_id under ``<state-dir>/consequence_cache.json`` (domain-keyed;
switching domains invalidates it). Any failure falls back to the template, so
the brief always renders.
"""
from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger(__name__)

_CACHE_FILENAME = "consequence_cache.json"


def load_cache(state_dir: str, domain: str) -> dict[str, dict]:
    """Load cached personalized consequences from state_dir.

    Returns empty dict if cache does not exist or domain does not match. The
    cache is domain-specific — switching domains invalidates it.
    """
    path = Path(state_dir) / _CACHE_FILENAME
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("domain") != domain:
            log.info(
                "Consequence cache domain mismatch (%s vs %s) — starting fresh",
                data.get("domain"), domain,
            )
            return {}
        return data.get("findings", {})
    except Exception as exc:
        log.warning("Failed to load consequence cache: %s", exc)
        return {}


def save_cache(state_dir: str, domain: str, cache: dict[str, dict]) -> bool:
    """Persist the consequence cache to state_dir atomically (temp-file + rename).

    Returns True on success, False if the write failed (logged but swallowed so
    synthesis never aborts on a cache-write error). Callers that report cache
    status must check this rather than assume success.
    """
    path = Path(state_dir) / _CACHE_FILENAME
    tmp = path.with_suffix(path.suffix + ".tmp")
    try:
        tmp.write_text(
            json.dumps({"domain": domain, "findings": cache}, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
        tmp.replace(path)
        return True
    except Exception as exc:
        log.warning("Failed to save consequence cache: %s", exc)
        try:
            tmp.unlink(missing_ok=True)
        except Exception:
            pass
        return False


def _build_prompt(finding: dict, template: dict, domain: str, source_code: str | None) -> str:
    """Build the LLM prompt for consequence personalization."""
    kind = finding.get("finding_kind", "")
    file_ = finding.get("file", "")
    location = finding.get("location", "")
    description = str(finding.get("description", "") or "")[:500]

    domain_template = template.get(domain, {})
    best = domain_template.get("best_case", "")
    likely = domain_template.get("likely", "")
    worst = domain_template.get("worst_case", "")

    source_section = ""
    if source_code:
        source_section = "\nRELEVANT CODE:\n```python\n" + source_code[:2000] + "\n```\n"

    return (
        "You are a code review assistant helping a code owner understand the "
        "consequences of not fixing a specific issue.\n\n"
        "Use the consequence template below as a guide, but rewrite each level to "
        "be specific to this exact code. Be concrete — name the actual function, "
        "the actual data, the actual system behavior. 2 sentences max per level. "
        "Do not use generic phrases like 'could lead to issues'.\n\n"
        "FINDING:\n"
        f"  Kind: {kind}\n"
        f"  File: {file_}\n"
        f"  Location: {location}\n"
        f"  Description: {description}\n"
        f"{source_section}\n"
        f"CONSEQUENCE TEMPLATE ({domain} domain):\n"
        f"  Best case: {best}\n"
        f"  Likely: {likely}\n"
        f"  Worst case: {worst}\n\n"
        "Respond with JSON only, no markdown, no explanation:\n"
        '{"best_case": "...", "likely": "...", "worst_case": "..."}'
    )


def _parse_response(text: str) -> dict | None:
    """Parse LLM JSON response, stripping markdown fences. Returns None if
    parsing fails, a required key is missing, or any value is empty."""
    text = text.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        text = "\n".join(
            lines[1:-1] if lines and lines[-1].strip() == "```" else lines[1:]
        ).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None

    required = {"best_case", "likely", "worst_case"}
    if not isinstance(parsed, dict) or not required.issubset(parsed.keys()):
        return None
    for k in required:
        if not str(parsed.get(k, "")).strip():
            return None

    return {
        "best_case": str(parsed["best_case"])[:400],
        "likely": str(parsed["likely"])[:400],
        "worst_case": str(parsed["worst_case"])[:400],
    }


def personalize_consequence(
    finding: dict,
    template: dict,
    domain: str,
    source_code: str | None,
    client,
) -> dict | None:
    """Call LLM to personalize the consequence template for one finding.

    Returns a dict with best_case/likely/worst_case, or None on any failure
    (caller falls back to the template). Uses the real ModelClient API —
    ``call_model(system_prompt, user_message) -> ModelResponse`` — and reads
    ``.content`` (tolerating a plain-string return for test doubles).
    """
    if client is None:
        return None

    domain_template = template.get(domain, {})
    if not domain_template:
        return None

    prompt = _build_prompt(finding, template, domain, source_code)

    try:
        resp = client.call_model(
            system_prompt=(
                "You are a code review assistant. Respond with JSON only. "
                "No markdown. No explanation."
            ),
            user_message=prompt,
        )
        text = resp.content if hasattr(resp, "content") else resp
        return _parse_response(str(text))
    except Exception as exc:
        log.warning(
            "Consequence synthesis failed for %s: %s", finding.get("id", "?"), exc
        )
        return None


def synthesize_all(
    findings: list[dict],
    consequences: dict,
    domain: str,
    source_cache: dict[str, str | None],
    state_dir: str,
    client,
) -> dict[str, dict]:
    """Personalize consequences for all findings → ``{finding_id: consequence}``.

    Loads the existing cache first; only calls LLM for uncached findings;
    saves the updated cache after all calls. Progress goes to stderr.
    """
    import sys

    cache = load_cache(state_dir, domain)
    hits = sum(1 for f in findings if f.get("id") in cache)
    print(
        f"Consequence synthesis: {hits} cached, {len(findings) - hits} to generate "
        f"({len(findings)} total)",
        file=sys.stderr,
    )

    total = len(findings)
    done = 0
    new_calls = 0

    for finding in findings:
        fid = finding.get("id", "")
        if not fid:
            continue
        if fid in cache:
            done += 1
            continue

        kind = finding.get("finding_kind", "")
        template = consequences.get(kind, {})
        src = source_cache.get(fid)

        result = personalize_consequence(finding, template, domain, src, client)
        if result is not None:
            cache[fid] = result
            new_calls += 1
        # None: leave uncached — renderer falls back to template.

        done += 1
        if done % 50 == 0 or done == total:
            print(
                f"  {done}/{total} findings processed ({new_calls} new LLM calls)",
                file=sys.stderr,
            )

    if new_calls > 0:
        if save_cache(state_dir, domain, cache):
            print(
                f"Cache saved: {len(cache)} entries -> {state_dir}/{_CACHE_FILENAME}",
                file=sys.stderr,
            )
        else:
            print(
                f"Cache save FAILED -> {state_dir}/{_CACHE_FILENAME} "
                "(see log); continuing with in-memory results",
                file=sys.stderr,
            )

    return cache
