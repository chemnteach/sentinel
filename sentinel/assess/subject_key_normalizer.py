"""G7 ``subject_key`` canonical normalization utility.

Spec source: ``docs/substep_4d/group_4d_3/batch_findings_2026-05-28.md``
§4 ("G7 ``subject_key`` Canonical Normalization — Three-Emitter
Convergent Signal"). That batch identified three independent emitter
cycles (layer3d, layer3e, layer2d) producing G7 cross-regen Jaccard
scores below threshold because the raw-string comparison could not see
through surface-form variance (namespace qualifiers, path prefixes,
anchor stems). Forward application: implement canonical
``subject_key`` normalization as a pre-Jaccard / pre-bucketing step.

The four mandated steps (applied to body only):
  a. replace ``\\`` with ``/``
  b. collapse consecutive ``//`` to a single ``/``
c. greedily strip leading path prefixes from the fixed list
         below (repeats until no listed prefix matches the leading
         segment; required for idempotency on nested prefixes)
  d. lowercase the result

Token (the ``<namespace>:`` prefix before the first colon) is
preserved verbatim. Tokens are registry-controlled identifiers
(``external_call:``, ``transaction_boundary:``, …) and must not be
mutated by normalization; only the body is consumer-supplied free
text subject to surface-form variance.

This module is policy, not data. It deliberately lives outside
``subject_key_registry`` to preserve that module's data-only
invariant.
"""

from __future__ import annotations

from typing import Final

__all__ = ["canonicalize_subject_key"]


_PATH_PREFIX_STRIP: Final[tuple[str, ...]] = (
    "src/",
    "lib/",
    "app/",
    "test/",
    "tests/",
    "spec/",
)


def canonicalize_subject_key(subject_key: str) -> str:
    """G7 normalization: canonicalize body only; token preserved verbatim.

    Steps applied to the body (substring after the first ``:``):
      a. replace ``\\`` with ``/``
      b. collapse runs of ``/`` to a single ``/``
      c. greedily strip leading path prefixes from the fixed list
         (``src/``, ``lib/``, ``app/``, ``test/``, ``tests/``, ``spec/``);
         repeats until no listed prefix matches the leading segment
      d. lowercase

    If the input has no ``:``, the entire string is treated as body and
    the result is the normalized body (no leading ``:``). Empty or
    whitespace-only input returns ``""``.

    The function is idempotent: ``f(f(x)) == f(x)`` for all inputs.
    Greedy prefix stripping is required for that property — single-
    strip would leave ``src/test/foo`` and ``test/foo`` as distinct
    fixed points.
    """
    if not subject_key or not subject_key.strip():
        return ""

    idx = subject_key.find(":")
    if idx < 0:
        token, body = "", subject_key
    else:
        token, body = subject_key[: idx + 1], subject_key[idx + 1 :]

    body = body.replace("\\", "/")
    while "//" in body:
        body = body.replace("//", "/")
    changed = True
    while changed:
        changed = False
        for prefix in _PATH_PREFIX_STRIP:
            if body.startswith(prefix):
                body = body[len(prefix) :]
                changed = True
                break
    body = body.lower()

    return f"{token}{body}" if token else body
