"""Tests for ``sentinel.assess.subject_key_normalizer``.

Covers the four mandated G7 normalization steps plus the
idempotency, token-preservation, and empty-input contracts. Spec
source: ``docs/substep_4d/group_4d_3/batch_findings_2026-05-28.md``
§4.
"""

from __future__ import annotations

import pytest

from sentinel.assess.subject_key_normalizer import canonicalize_subject_key


class TestCanonicalizeSubjectKey:
    """Behavioral contract for ``canonicalize_subject_key``."""

    def test_idempotent_on_already_normalized_value(self) -> None:
        """``f(f(x)) == f(x)`` for an already-normalized value."""
        normalized = canonicalize_subject_key("external_call:foo/bar.py")
        assert normalized == "external_call:foo/bar.py"
        assert canonicalize_subject_key(normalized) == normalized

    def test_lowercases_body(self) -> None:
        """Body is lowercased; token preserved verbatim."""
        assert (
            canonicalize_subject_key("External_Call:Foo")
            == "External_Call:foo"
        )

    def test_normalizes_backslash_to_forward_slash(self) -> None:
        """Backslashes in body become forward slashes (then prefix strip)."""
        # "path:src\\db.py" -> backslash->slash -> "path:src/db.py"
        # -> strip "src/" -> "path:db.py" -> lowercase no-op
        assert (
            canonicalize_subject_key("path:src\\db.py")
            == "path:db.py"
        )

    def test_collapses_repeated_slashes(self) -> None:
        """Runs of ``/`` collapse to a single ``/``."""
        assert (
            canonicalize_subject_key("path:foo//bar")
            == "path:foo/bar"
        )

    @pytest.mark.parametrize(
        "prefix",
        ["src/", "lib/", "app/", "test/", "tests/", "spec/"],
    )
    def test_strips_leading_path_prefix(self, prefix: str) -> None:
        """Each of the 6 fixed prefixes is stripped when leading."""
        body = f"{prefix}nested/file.py"
        result = canonicalize_subject_key(f"ns:{body}")
        assert result == "ns:nested/file.py"

    def test_greedy_strips_all_leading_prefixes(self) -> None:
        """``src/test/foo`` strips both ``src/`` and ``test/`` → ``foo``.

        Greedy stripping is required for idempotency on nested
        prefixes: single-strip would leave ``src/test/foo`` and
        ``test/foo`` as distinct fixed points.
        """
        assert canonicalize_subject_key("ns:src/test/foo") == "ns:foo"
        # Verify idempotent on the result.
        once = canonicalize_subject_key("ns:src/test/foo")
        twice = canonicalize_subject_key(once)
        assert once == twice

    def test_dotted_namespace_body_preserved(self) -> None:
        """Dotted-stem bodies lowercase but are not split or stripped."""
        # Spec language "strip namespace qualifiers" is satisfied by the
        # 4 mandated mechanical steps. Dotted-stem aggressive stripping
        # is intentionally out of scope for this cycle.
        assert (
            canonicalize_subject_key(
                "transaction_boundary:My.Namespace.Class"
            )
            == "transaction_boundary:my.namespace.class"
        )

    def test_token_preserved_verbatim(self) -> None:
        """Token case is NOT touched; body is normalized."""
        assert (
            canonicalize_subject_key("External_Call:src/Foo")
            == "External_Call:foo"
        )

    def test_empty_input_returns_empty(self) -> None:
        """Empty and whitespace-only inputs return ``""``."""
        assert canonicalize_subject_key("") == ""
        assert canonicalize_subject_key("   ") == ""

    def test_no_colon_treated_as_body(self) -> None:
        """Input without ``:`` is normalized as a bare body."""
        assert (
            canonicalize_subject_key("WaferStateService")
            == "waferstateservice"
        )
