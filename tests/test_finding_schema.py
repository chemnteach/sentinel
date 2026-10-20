"""Tests for sentinel.assess.finding_schema and finding_kinds.

Covers Steps 1a–2c of the findings pipeline quality plan:
    1a: Deterministic legacy finding ID generation
    1b: Category classification from description keywords
    1c: Structured location parsing (file path, function name, line range)
    1d: Registry-driven finding_kind / subject_key validation
    1e: Registry integrity and validation behavior coverage
    2c: F2 subject derivation, signature composition, and public ID entry point
"""

from __future__ import annotations

from typing import get_args

import pytest

from sentinel.assess.finding_kinds import (
    FINDING_KIND_REGISTRY,
    SubjectRequirement,
    get_finding_kind_definition as get_declared_finding_kind_definition,
    iter_finding_kind_definitions as iter_declared_finding_kind_definitions,
)
from sentinel.assess.finding_schema import (
    _classify_category,
    _extract_file_path,
    _extract_function_name,
    _extract_line_range,
    _generate_finding_id_v06_legacy,
    compose_f2_signature,
    derive_subject_component,
    FindingSchemaValidationError,
    generate_finding_id_v2,
    get_supported_report_finding_kind_definition,
    materialize_emitter_finding,
    unpack_structured_location,
    repair_invalid_finding,
    validate_finding_kind_fields,
)


def _make_validation_finding(
    *,
    finding_kind: str,
    subject_key: str = "",
    emitter_name: str = "layer2a-quick-review",
    skill: str | None = None,
    finding_id: str = "F-example123456",
    location: str = "handlers/example.py:42",
    description: str = "Example finding for validator coverage",
) -> dict[str, str]:
    """Build a minimal finding payload for validator tests."""

    return {
        "finding_kind": finding_kind,
        "subject_key": subject_key,
        "skill": emitter_name if skill is None else skill,
        "id": finding_id,
        "location": location,
        "description": description,
    }


def _make_f2_finding(
    *,
    finding_kind: str = "logic_bug",
    subject_key: str = "branch:retry",
    module: str = "sentinel.assess.reviewer",
    file: str = "sentinel/assess/reviewer.py",
    function: str = "loadUserProfile",
    line_range: str = "42",
    category: str = "correctness",
    skill: str = "layer2a-quick-review",
    location: str = "sentinel/assess/reviewer.py:42",
) -> dict[str, str]:
    """Build a minimal finding payload for F2 derivation tests."""

    return {
        "finding_kind": finding_kind,
        "subject_key": subject_key,
        "module": module,
        "file": file,
        "function": function,
        "line_range": line_range,
        "category": category,
        "skill": skill,
        "location": location,
    }


# ── Step 1a: Finding ID generation ────────────────────────────────────


class TestGenerateFindingId:
    """_generate_finding_id_v06_legacy produces deterministic, unique IDs."""

    def test_returns_f_prefix(self):
        fid = _generate_finding_id_v06_legacy("mod", "skill", "desc", "loc")
        assert fid.startswith("F-")

    def test_hash_length_is_12(self):
        fid = _generate_finding_id_v06_legacy("mod", "skill", "desc", "loc")
        # F- prefix + 12 hex chars
        assert len(fid) == 14

    def test_stability_same_inputs(self):
        """Same inputs always produce the same ID."""
        a = _generate_finding_id_v06_legacy("utils.py", "quick_review", "missing encoding", "line 42")
        b = _generate_finding_id_v06_legacy("utils.py", "quick_review", "missing encoding", "line 42")
        assert a == b

    def test_uniqueness_different_module(self):
        a = _generate_finding_id_v06_legacy("utils.py", "quick_review", "missing encoding", "line 42")
        b = _generate_finding_id_v06_legacy("helpers.py", "quick_review", "missing encoding", "line 42")
        assert a != b

    def test_uniqueness_different_skill(self):
        a = _generate_finding_id_v06_legacy("utils.py", "quick_review", "missing encoding", "line 42")
        b = _generate_finding_id_v06_legacy("utils.py", "security", "missing encoding", "line 42")
        assert a != b

    def test_uniqueness_different_description(self):
        a = _generate_finding_id_v06_legacy("utils.py", "quick_review", "missing encoding", "line 42")
        b = _generate_finding_id_v06_legacy("utils.py", "quick_review", "race condition", "line 42")
        assert a != b

    def test_uniqueness_different_location(self):
        a = _generate_finding_id_v06_legacy("utils.py", "quick_review", "missing encoding", "line 42")
        b = _generate_finding_id_v06_legacy("utils.py", "quick_review", "missing encoding", "line 99")
        assert a != b

    def test_description_truncated_at_200(self):
        """Long descriptions are truncated to 200 chars for hashing."""
        short_desc = "x" * 200
        long_desc = "x" * 300
        a = _generate_finding_id_v06_legacy("mod", "skill", short_desc, "loc")
        b = _generate_finding_id_v06_legacy("mod", "skill", long_desc, "loc")
        assert a == b

    def test_empty_inputs(self):
        """Empty strings still produce a valid ID."""
        fid = _generate_finding_id_v06_legacy("", "", "", "")
        assert fid.startswith("F-")
        assert len(fid) == 14


# ── Step 1b: Category classification ─────────────────────────────────


class TestClassifyCategory:
    """_classify_category maps keywords in descriptions to categories."""

    @pytest.mark.parametrize(
        "description, expected_category",
        [
            ("Missing utf-8 encoding parameter", "encoding"),
            ("File written without explicit UTF8 codec", "encoding"),
            ("Uses ascii instead of unicode", "encoding"),
            ("Non-atomic write_text call", "atomicity"),
            ("Partial write without atomic rename", "atomicity"),
            ("Race condition in shared state", "concurrency"),
            ("Missing lock on concurrent access", "concurrency"),
            ("Thread safety concern", "concurrency"),
            ("Async handler without awaiting result", "concurrency"),
            ("No input validation on user data", "validation"),
            ("Missing sanitization of form field", "validation"),
            ("Bare except swallows all errors", "error_handling"),
            ("Silent exception handling", "error_handling"),
            ("Error handling missing for network calls", "error_handling"),
            ("Path traversal vulnerability via ..", "path_safety"),
            ("Directory injection risk", "path_safety"),
            ("Unclosed database connection", "resource_mgmt"),
            ("Connection pool leak on error", "resource_mgmt"),
            ("Resource not released in finally", "resource_mgmt"),
        ],
    )
    def test_keyword_classification(self, description: str, expected_category: str):
        assert _classify_category(description) == expected_category

    def test_general_fallback(self):
        """Descriptions matching no keywords return 'general'."""
        assert _classify_category("This is a totally novel issue") == "general"

    def test_empty_description(self):
        assert _classify_category("") == "general"

    def test_case_insensitive(self):
        """Classification is case-insensitive."""
        assert _classify_category("MISSING UTF-8 ENCODING") == "encoding"
        assert _classify_category("Race Condition in module") == "concurrency"


# ── Step 1c: Structured location parsing ──────────────────────────────


class TestExtractFilePath:
    """_extract_file_path finds file paths in location strings."""

    def test_simple_path(self):
        assert _extract_file_path("src/utils.py") == "src/utils.py"

    def test_path_with_surrounding_text(self):
        assert _extract_file_path("In file src/utils.py, line 42") == "src/utils.py"

    def test_nested_path(self):
        assert _extract_file_path("sentinel/assess/reviewer.py:45") == "sentinel/assess/reviewer.py"

    def test_js_extension(self):
        assert _extract_file_path("app/index.js") == "app/index.js"

    def test_tsx_extension(self):
        assert _extract_file_path("components/App.tsx") == "components/App.tsx"

    def test_no_file_path(self):
        assert _extract_file_path("some general location") == ""

    def test_empty_string(self):
        assert _extract_file_path("") == ""

    def test_path_with_line_number_colon(self):
        assert _extract_file_path("foo/bar.py:123") == "foo/bar.py"


class TestUnpackStructuredLocation:
    """Cycle 42 (Problem A): a structured ``location`` dict
    ``{file,line,function}`` (as layer2a-quick-review emits) is lifted into
    flat file/function/line_range fields and flattened to a string."""

    def test_structured_location_lifts_file_function_line(self):
        out = unpack_structured_location(
            {
                "skill": "layer2a-quick-review",
                "location": {
                    "file": "agents/architect.py",
                    "line": "132-159",
                    "function": "review_todo",
                },
            }
        )
        assert out["file"] == "agents/architect.py"
        assert out["function"] == "review_todo"
        assert out["line_range"] == "132-159"
        assert out["location"] == "agents/architect.py:132-159 in review_todo"

    def test_string_location_unchanged(self):
        src = {"skill": "x", "location": "foo.py:bar", "file": "foo.py"}
        out = unpack_structured_location(src)
        assert out["location"] == "foo.py:bar"
        assert out["file"] == "foo.py"

    def test_does_not_overwrite_existing_nonempty_fields(self):
        out = unpack_structured_location(
            {
                "location": {"file": "nested.py", "function": "nf"},
                "file": "explicit.py",
                "function": "ef",
            }
        )
        assert out["file"] == "explicit.py"  # explicit value preserved
        assert out["function"] == "ef"
        # location is still flattened from the nested dict
        assert out["location"] == "nested.py in nf"

    def test_file_only_structured_location(self):
        out = unpack_structured_location({"location": {"file": "only.py"}})
        assert out["file"] == "only.py"
        assert out["location"] == "only.py"

    def test_materialize_extracts_file_from_structured_location(self):
        result = materialize_emitter_finding(
            {
                "skill": "layer2a-quick-review",
                "finding_kind": "logic_bug",
                "severity": "P2",
                "description": "structured location finding",
                "location": {
                    "file": "agents/architect.py",
                    "line": "132-159",
                    "function": "review_todo",
                },
            },
            emitter_name="layer2a-quick-review",
            repo_root_canonical="",
        )
        assert result["file"] == "agents/architect.py"
        assert result["function"] == "review_todo"
        assert result["line_range"] == "132-159"
        assert isinstance(result["location"], str)

    def test_materialize_structured_location_file_changes_identity(self):
        finding_a = materialize_emitter_finding(
            {
                "skill": "layer2a-quick-review",
                "finding_kind": "logic_bug",
                "severity": "P2",
                "description": "same logical issue",
                "location": {
                    "file": "agents/a.py",
                    "line": "10",
                    "function": "scan",
                },
            },
            emitter_name="layer2a-quick-review",
            repo_root_canonical="",
        )

        finding_b = materialize_emitter_finding(
            {
                "skill": "layer2a-quick-review",
                "finding_kind": "logic_bug",
                "severity": "P2",
                "description": "same logical issue",
                "location": {
                    "file": "agents/b.py",
                    "line": "10",
                    "function": "scan",
                },
            },
            emitter_name="layer2a-quick-review",
            repo_root_canonical="",
        )

        assert finding_a["file"] == "agents/a.py"
        assert finding_b["file"] == "agents/b.py"
        assert finding_a["id"] != finding_b["id"]


class TestExtractFunctionName:
    """_extract_function_name finds function/class names in location strings."""

    def test_def_syntax(self):
        assert _extract_function_name("def process_data") == "process_data"

    def test_class_syntax(self):
        assert _extract_function_name("class MyHandler") == "MyHandler"

    def test_call_syntax(self):
        assert _extract_function_name("in process_data()") == "process_data"

    def test_method_call(self):
        assert _extract_function_name("calls self.handle_request()") == "handle_request"

    def test_no_function(self):
        assert _extract_function_name("line 42 in module") == ""

    def test_empty_string(self):
        assert _extract_function_name("") == ""

    def test_def_with_parens(self):
        assert _extract_function_name("def my_func(arg1, arg2)") == "my_func"


class TestExtractLineRange:
    """_extract_line_range finds line references in location strings."""

    def test_single_line(self):
        assert _extract_line_range("line 42") == "42"

    def test_line_range(self):
        assert _extract_line_range("lines 42-50") == "42-50"

    def test_capitalized(self):
        assert _extract_line_range("Line 100") == "100"

    def test_with_colon_separator(self):
        assert _extract_line_range("foo.py:123") == "123"

    def test_no_line_reference(self):
        assert _extract_line_range("in function process_data") == ""

    def test_empty_string(self):
        assert _extract_line_range("") == ""

    def test_lines_with_capital_l(self):
        assert _extract_line_range("Lines 10-20") == "10-20"


# ── Step 1e: Registry integrity and validator behavior ───────────────


class TestFindingKindRegistry:
    """The finding_kind registry is well-formed and stable."""

    def test_registry_defines_required_canonical_kinds(self):
        # Invariant: the system depends on these canonical kinds being
        # present regardless of the total registry size. Adding, removing,
        # or splitting *other* kinds must not break this contract, so this
        # test no longer pins an exact len(FINDING_KIND_REGISTRY).
        required_canonical_kinds = {
            "logic_bug",
            "contract_violation",
            "injection_risk",
            "security_misconfiguration",
            "trust_boundary_gap",
            "concurrency_safety_gap",
            "missing_transaction_boundary",
            "silent_data_loss",
        }
        declared = {entry.kind_name for entry in FINDING_KIND_REGISTRY}
        missing = required_canonical_kinds - declared
        assert not missing, f"registry missing required canonical kinds: {sorted(missing)}"

    def test_registry_has_no_duplicate_or_unresolvable_entries(self):
        # Invariant: registry is non-empty, every kind_name is unique and
        # well-formed, and every entry resolves back to itself via the
        # public lookup. Fails on duplicate/malformed/unresolvable entries
        # without asserting an exact entry count.
        kind_names = [entry.kind_name for entry in FINDING_KIND_REGISTRY]
        assert kind_names, "registry must not be empty"
        assert len(kind_names) == len(set(kind_names)), "duplicate kind_name detected"
        for entry in FINDING_KIND_REGISTRY:
            assert entry.kind_name, "entry with empty kind_name"
            assert get_declared_finding_kind_definition(entry.kind_name) is entry

    def test_all_entries_have_required_shape(self):
        valid_subject_requirements = set(get_args(SubjectRequirement))

        for entry in FINDING_KIND_REGISTRY:
            assert entry.kind_name
            assert entry.description_of_mechanism
            assert entry.subject_required in valid_subject_requirements

    def test_kind_names_are_unique(self):
        kind_names = [entry.kind_name for entry in FINDING_KIND_REGISTRY]
        assert len(kind_names) == len(set(kind_names))

    @pytest.mark.parametrize(
        "entry",
        FINDING_KIND_REGISTRY,
        ids=lambda entry: entry.kind_name,
    )
    def test_lookup_returns_exact_declared_entry(self, entry):
        assert get_declared_finding_kind_definition(entry.kind_name) is entry

    def test_iteration_order_matches_declaration_order(self):
        assert tuple(iter_declared_finding_kind_definitions()) == FINDING_KIND_REGISTRY


class TestValidateFindingKindFields:
    """Registry-field validation enforces the v0.7.1 contract."""

    def test_registered_kind_returns_matching_definition(self):
        finding = _make_validation_finding(
            finding_kind="logic_bug",
            subject_key="",
        )

        definition = validate_finding_kind_fields(
            finding,
            emitter_name="layer2a-quick-review",
        )

        assert definition is get_declared_finding_kind_definition("logic_bug")

    def test_missing_finding_kind_raises_rule_naming_error(self):
        finding = _make_validation_finding(finding_kind="")

        with pytest.raises(FindingSchemaValidationError, match="finding_kind is required") as exc_info:
            validate_finding_kind_fields(
                finding,
                emitter_name="layer2a-quick-review",
            )

        assert "emitter=layer2a-quick-review" in str(exc_info.value)

    def test_unknown_finding_kind_fails_fast(self):
        finding = _make_validation_finding(finding_kind="not_in_registry")

        with pytest.raises(FindingSchemaValidationError, match="is not in the registry") as exc_info:
            validate_finding_kind_fields(
                finding,
                emitter_name="layer2a-quick-review",
            )

        assert "finding_kind 'not_in_registry'" in str(exc_info.value)
        assert "emitter=layer2a-quick-review" in str(exc_info.value)

    def test_required_subject_kind_returns_matching_definition_when_subject_present(self):
        finding = _make_validation_finding(
            finding_kind="contract_violation",
            subject_key="return-contract:get_wafer_state",
        )

        definition = validate_finding_kind_fields(
            finding,
            emitter_name="layer2b-correctness-specialist",
        )

        assert definition is get_declared_finding_kind_definition("contract_violation")

    def test_required_subject_kind_raises_rule_naming_error_when_subject_missing(self):
        finding = _make_validation_finding(
            finding_kind="contract_violation",
            subject_key="",
        )

        with pytest.raises(FindingSchemaValidationError, match="subject_key is required") as exc_info:
            validate_finding_kind_fields(
                finding,
                emitter_name="layer2b-correctness-specialist",
            )

        assert "contract_violation" in str(exc_info.value)
        assert "emitter=layer2b-correctness-specialist" in str(exc_info.value)

    def test_optional_subject_kind_accepts_present_subject(self):
        finding = _make_validation_finding(
            finding_kind="logic_bug",
            subject_key="state-machine:retry-branch",
        )

        definition = validate_finding_kind_fields(
            finding,
            emitter_name="layer2b-correctness-specialist",
        )

        assert definition is get_declared_finding_kind_definition("logic_bug")

    def test_optional_subject_kind_accepts_absent_subject(self):
        finding = _make_validation_finding(
            finding_kind="logic_bug",
            subject_key="",
        )

        definition = validate_finding_kind_fields(
            finding,
            emitter_name="layer2b-correctness-specialist",
        )

        assert definition is get_declared_finding_kind_definition("logic_bug")

    @pytest.mark.parametrize("subject_key", ["", "_na"])
    def test_na_kind_accepts_empty_or_na_subject(self, subject_key: str):
        finding = _make_validation_finding(
            finding_kind="structural_complexity",
            subject_key=subject_key,
        )

        definition = validate_finding_kind_fields(
            finding,
            emitter_name="layer3b-maintainability-specialist",
        )

        assert definition is get_declared_finding_kind_definition("structural_complexity")

    def test_na_kind_raises_rule_naming_error_when_semantic_subject_is_present(self):
        finding = _make_validation_finding(
            finding_kind="structural_complexity",
            subject_key="wafer-state-service",
        )

        with pytest.raises(FindingSchemaValidationError, match="must be omitted or '_na'") as exc_info:
            validate_finding_kind_fields(
                finding,
                emitter_name="layer3b-maintainability-specialist",
            )

        assert "structural_complexity" in str(exc_info.value)
        assert "emitter=layer3b-maintainability-specialist" in str(exc_info.value)

    def test_error_context_deduplicates_skill_when_it_matches_emitter(self):
        finding = _make_validation_finding(
            finding_kind="",
            skill="layer2a-quick-review",
        )

        with pytest.raises(FindingSchemaValidationError) as exc_info:
            validate_finding_kind_fields(
                finding,
                emitter_name="layer2a-quick-review",
            )

        message = str(exc_info.value)
        assert "emitter=layer2a-quick-review" in message
        assert "skill=" not in message

    def test_error_context_includes_skill_when_it_differs_from_emitter(self):
        finding = _make_validation_finding(
            finding_kind="",
            skill="layer2b-correctness-specialist",
        )

        with pytest.raises(FindingSchemaValidationError) as exc_info:
            validate_finding_kind_fields(
                finding,
                emitter_name="reviewer-normalize",
            )

        message = str(exc_info.value)
        assert "emitter=reviewer-normalize" in message
        assert "skill=layer2b-correctness-specialist" in message

    def test_error_context_degrades_gracefully_when_id_and_location_are_missing(self):
        finding = _make_validation_finding(
            finding_kind="",
            finding_id="",
            location="",
        )

        with pytest.raises(FindingSchemaValidationError) as exc_info:
            validate_finding_kind_fields(
                finding,
                emitter_name="layer2a-quick-review",
            )

        message = str(exc_info.value)
        assert "emitter=layer2a-quick-review" in message
        assert "id=" not in message
        assert "location=" not in message


class TestCloseCallKindPairs:
    """Known close-call kind pairs remain registry-valid on both sides."""

    @pytest.mark.parametrize(
        (
            "pair_name",
            "left_finding",
            "right_finding",
        ),
        [
            (
                "logic_bug-vs-contract_violation",
                _make_validation_finding(
                    finding_kind="logic_bug",
                    subject_key="",
                    description="Wrong behavior without a clean contract boundary.",
                ),
                _make_validation_finding(
                    finding_kind="contract_violation",
                    subject_key="return-contract:get_wafer_state",
                    description="Broken explicit return-value contract.",
                ),
            ),
            (
                "trust_boundary_gap-vs-injection_risk",
                _make_validation_finding(
                    finding_kind="trust_boundary_gap",
                    subject_key="api-boundary:tool-config",
                    description="Cross-repo data arrives without explicit trust reduction.",
                ),
                _make_validation_finding(
                    finding_kind="injection_risk",
                    subject_key="sql-query:tool-config",
                    description="The same input reaches an executable sink.",
                ),
            ),
            (
                "configuration_drift-vs-security_misconfiguration",
                _make_validation_finding(
                    finding_kind="configuration_drift",
                    subject_key="env:tool-timeout",
                    description="General env drift without weakening a security control.",
                ),
                _make_validation_finding(
                    finding_kind="security_misconfiguration",
                    subject_key="env:tls-verify",
                    description="A config difference weakens a security control.",
                ),
            ),
            (
                "partial_failure_handling_gap-vs-silent_data_loss",
                _make_validation_finding(
                    finding_kind="partial_failure_handling_gap",
                    subject_key="workflow:publish-sequence",
                    description="A workflow degrades unsafely when one dependency fails.",
                ),
                _make_validation_finding(
                    finding_kind="silent_data_loss",
                    subject_key="write-path:wafer-log",
                    description="The same shape causes durable data loss as the primary impact.",
                ),
            ),
            (
                "structural_complexity-vs-god_class",
                _make_validation_finding(
                    finding_kind="structural_complexity",
                    subject_key="",
                    description="Broad complexity debt without a precise named smell fit.",
                ),
                _make_validation_finding(
                    finding_kind="god_class",
                    subject_key="",
                    description="The same area crosses the threshold into a named smell.",
                ),
            ),
            (
                "change_verification_gap-vs-hidden_dependency",
                _make_validation_finding(
                    finding_kind="change_verification_gap",
                    subject_key="verification-surface:state-store",
                    description="Blast radius is the dominant concern.",
                ),
                _make_validation_finding(
                    finding_kind="hidden_dependency",
                    subject_key="global-state:clock",
                    description="The root mechanism is an implicit dependency.",
                ),
            ),
        ],
        ids=[
            "logic_bug-vs-contract_violation",
            "trust_boundary_gap-vs-injection_risk",
            "configuration_drift-vs-security_misconfiguration",
            "partial_failure_handling_gap-vs-silent_data_loss",
            "structural_complexity-vs-god_class",
            "change_verification_gap-vs-hidden_dependency",
        ],
    )
    def test_both_sides_of_close_call_pair_validate_successfully(
        self,
        pair_name: str,
        left_finding: dict[str, str],
        right_finding: dict[str, str],
    ):
        # Traceability guard: both sides of every close-call pair must name
        # kinds that are present in the *current* active finding_kind
        # registry. This ties the test's expectations directly to the
        # caller-visible contract and fails clearly (rather than via an
        # ambiguous validation error) if a kind is removed or renamed.
        for finding in (left_finding, right_finding):
            kind = finding["finding_kind"]
            assert get_declared_finding_kind_definition(kind) is not None, (
                f"{pair_name}: '{kind}' is not in the active finding_kind registry"
            )

        left_definition = validate_finding_kind_fields(
            left_finding,
            emitter_name="pair-sanity",
        )
        right_definition = validate_finding_kind_fields(
            right_finding,
            emitter_name="pair-sanity",
        )

        assert left_definition.kind_name == left_finding["finding_kind"], pair_name
        assert right_definition.kind_name == right_finding["finding_kind"], pair_name


class TestDeriveSubjectComponent:
    """Subject-component derivation follows the registry subject contract."""

    def test_required_kind_returns_emitter_subject(self):
        finding = _make_f2_finding(
            finding_kind="contract_violation",
            subject_key="return-contract:get_wafer_state",
        )
        definition = get_declared_finding_kind_definition("contract_violation")

        assert definition is not None
        assert derive_subject_component(finding, definition) == "return-contract:get_wafer_state"

    def test_optional_kind_returns_emitter_subject_when_present(self):
        finding = _make_f2_finding(
            finding_kind="logic_bug",
            subject_key="branch:retry",
        )
        definition = get_declared_finding_kind_definition("logic_bug")

        assert definition is not None
        assert derive_subject_component(finding, definition) == "branch:retry"

    def test_optional_kind_returns_none_when_subject_missing(self):
        finding = _make_f2_finding(
            finding_kind="logic_bug",
            subject_key="",
        )
        definition = get_declared_finding_kind_definition("logic_bug")

        assert definition is not None
        assert derive_subject_component(finding, definition) == "_none"

    def test_na_kind_returns_na_even_if_subject_is_locally_present(self):
        finding = _make_f2_finding(
            finding_kind="structural_complexity",
            subject_key="ignored-locally",
        )
        definition = get_declared_finding_kind_definition("structural_complexity")

        assert definition is not None
        assert derive_subject_component(finding, definition) == "_na"


class TestComposeF2Signature:
    """F2 composition is stable and changes on any identity-input delta."""

    repo_root_canonical = "d:/Git/sentinel"

    def test_f2_id_is_stable_across_repeated_calls(self):
        finding = _make_f2_finding()
        definition = get_declared_finding_kind_definition("logic_bug")

        assert definition is not None

        first = compose_f2_signature(finding, definition, self.repo_root_canonical)
        second = compose_f2_signature(finding, definition, self.repo_root_canonical)

        assert first == second

    def test_f2_id_changes_when_scope_key_changes(self):
        finding = _make_f2_finding()
        changed_scope = _make_f2_finding(function="loadRollbackProfile")
        definition = get_declared_finding_kind_definition("logic_bug")

        assert definition is not None

        original = compose_f2_signature(finding, definition, self.repo_root_canonical)
        changed = compose_f2_signature(changed_scope, definition, self.repo_root_canonical)

        assert original != changed

    def test_f2_id_changes_when_finding_kind_changes(self):
        finding = _make_f2_finding(finding_kind="logic_bug")
        changed_kind = _make_f2_finding(finding_kind="feature_envy")
        original_definition = get_declared_finding_kind_definition("logic_bug")
        changed_definition = get_declared_finding_kind_definition("feature_envy")

        assert original_definition is not None
        assert changed_definition is not None

        original = compose_f2_signature(finding, original_definition, self.repo_root_canonical)
        changed = compose_f2_signature(changed_kind, changed_definition, self.repo_root_canonical)

        assert original != changed

    def test_f2_id_changes_when_subject_component_changes(self):
        finding = _make_f2_finding(subject_key="branch:retry")
        changed_subject = _make_f2_finding(subject_key="branch:rollback")
        definition = get_declared_finding_kind_definition("logic_bug")

        assert definition is not None

        original = compose_f2_signature(finding, definition, self.repo_root_canonical)
        changed = compose_f2_signature(changed_subject, definition, self.repo_root_canonical)

        assert original != changed

    def test_f2_id_changes_when_anchor_key_changes(self):
        finding = _make_f2_finding(line_range="42", location="sentinel/assess/reviewer.py:42")
        changed_anchor = _make_f2_finding(line_range="99", location="sentinel/assess/reviewer.py:99")
        definition = get_declared_finding_kind_definition("logic_bug")

        assert definition is not None

        original = compose_f2_signature(finding, definition, self.repo_root_canonical)
        changed = compose_f2_signature(changed_anchor, definition, self.repo_root_canonical)

        assert original != changed

    def test_four_identity_inputs_are_independent(self):
        base_finding = _make_f2_finding(
            finding_kind="logic_bug",
            subject_key="branch:retry",
            function="loadUserProfile",
            line_range="42",
            location="sentinel/assess/reviewer.py:42",
        )
        scope_variant = _make_f2_finding(
            finding_kind="logic_bug",
            subject_key="branch:retry",
            function="loadRollbackProfile",
            line_range="42",
            location="sentinel/assess/reviewer.py:42",
        )
        kind_variant = _make_f2_finding(
            finding_kind="feature_envy",
            subject_key="branch:retry",
            function="loadUserProfile",
            line_range="42",
            location="sentinel/assess/reviewer.py:42",
        )
        subject_variant = _make_f2_finding(
            finding_kind="logic_bug",
            subject_key="branch:rollback",
            function="loadUserProfile",
            line_range="42",
            location="sentinel/assess/reviewer.py:42",
        )
        anchor_variant = _make_f2_finding(
            finding_kind="logic_bug",
            subject_key="branch:retry",
            function="loadUserProfile",
            line_range="99",
            location="sentinel/assess/reviewer.py:99",
        )

        logic_bug_definition = get_declared_finding_kind_definition("logic_bug")
        feature_envy_definition = get_declared_finding_kind_definition("feature_envy")

        assert logic_bug_definition is not None
        assert feature_envy_definition is not None

        derived_ids = {
            compose_f2_signature(base_finding, logic_bug_definition, self.repo_root_canonical),
            compose_f2_signature(scope_variant, logic_bug_definition, self.repo_root_canonical),
            compose_f2_signature(kind_variant, feature_envy_definition, self.repo_root_canonical),
            compose_f2_signature(subject_variant, logic_bug_definition, self.repo_root_canonical),
            compose_f2_signature(anchor_variant, logic_bug_definition, self.repo_root_canonical),
        }

        assert len(derived_ids) == 5


class TestGenerateFindingIdV2:
    """The public F2 entry point validates first and composes deterministic IDs."""

    repo_root_canonical = "d:/Git/sentinel"

    def test_valid_finding_returns_f2_prefixed_id(self):
        finding = _make_f2_finding(
            finding_kind="contract_violation",
            subject_key="return-contract:get_wafer_state",
        )

        finding_id = generate_finding_id_v2(
            finding,
            emitter_name="layer2b-correctness-specialist",
            repo_root_canonical=self.repo_root_canonical,
        )

        assert finding_id.startswith("F2-")

    def test_missing_finding_kind_raises_validation_error(self):
        finding = _make_f2_finding(finding_kind="")

        with pytest.raises(FindingSchemaValidationError, match="finding_kind is required"):
            generate_finding_id_v2(
                finding,
                emitter_name="layer2a-quick-review",
                repo_root_canonical=self.repo_root_canonical,
            )

    def test_na_kind_with_non_empty_subject_raises_before_composition(self):
        finding = _make_f2_finding(
            finding_kind="structural_complexity",
            subject_key="unexpected-subject",
        )

        with pytest.raises(FindingSchemaValidationError, match="must be omitted or '_na'"):
            generate_finding_id_v2(
                finding,
                emitter_name="layer3b-maintainability-specialist",
                repo_root_canonical=self.repo_root_canonical,
            )

    def test_successful_generation_returns_fixed_length_f2_id(self):
        finding = _make_f2_finding(
            finding_kind="contract_violation",
            subject_key="return-contract:get_wafer_state",
        )

        finding_id = generate_finding_id_v2(
            finding,
            emitter_name="layer2b-correctness-specialist",
            repo_root_canonical=self.repo_root_canonical,
        )

        assert finding_id.startswith("F2-")
        assert len(finding_id) == 15

    def test_legacy_id_generation_still_works_after_f2_additions(self):
        legacy_id = _generate_finding_id_v06_legacy(
            "sentinel/assess/reviewer.py",
            "layer2a-quick-review",
            "Example legacy description",
            "sentinel/assess/reviewer.py:42",
        )

        assert legacy_id.startswith("F-")
        assert len(legacy_id) == 14


class TestMaterializeEmitterFinding:
    repo_root_canonical = "d:/Git/sentinel"

    def test_materialize_emitter_finding_uses_producer_fields(self):
        finding = {
            "module": "sentinel.assess.reviewer",
            "skill": "layer2a-quick-review",
            "severity": "P1",
            "category": "error_handling",
            "location": "sentinel/assess/reviewer.py:42",
            "file": "sentinel/assess/reviewer.py",
            "function": "main",
            "line_range": "42",
            "description": "Missing error handling in main(). Additional detail follows.",
            "suggested_fix": "Handle the failure explicitly.",
            "finding_kind": "error_signal_loss",
            "custom_field": "producer-owned",
            "status": "open",
        }

        materialized = materialize_emitter_finding(
            finding,
            emitter_name="layer2a-quick-review",
            repo_root_canonical=self.repo_root_canonical,
        )

        assert materialized["id"].startswith("F2-")
        assert materialized["legacy_finding_id"].startswith("F-")
        assert materialized["finding_kind"] == "error_signal_loss"
        assert materialized["custom_field"] == "producer-owned"
        assert "summary" not in materialized
        assert "subject_key" not in materialized

    def test_materialize_emitter_finding_preserves_existing_legacy_alias(self):
        finding = {
            "module": "sentinel.assess.reviewer",
            "skill": "layer2a-quick-review",
            "severity": "P1",
            "category": "correctness",
            "location": "sentinel/assess/reviewer.py:42",
            "file": "sentinel/assess/reviewer.py",
            "function": "loadUserProfile",
            "line_range": "42",
            "description": "Return contract is violated.",
            "finding_kind": "contract_violation",
            "subject_key": "return-contract:loadUserProfile",
            "legacy_finding_id": "F-existing1234",
            "status": "open",
        }

        materialized = materialize_emitter_finding(
            finding,
            emitter_name="layer2a-quick-review",
            repo_root_canonical=self.repo_root_canonical,
        )

        assert materialized["legacy_finding_id"] == "F-existing1234"
        assert materialized["id"].startswith("F2-")

    def test_materialize_emitter_finding_surfaces_missing_finding_kind(self):
        finding = {
            "module": "scan",
            "skill": "quick-scan-security",
            "severity": "P1",
            "category": "security",
            "location": "d:/Git/sentinel/src/app.py:12",
            "description": "Use of eval() or exec() may allow code injection.",
            "suggested_fix": "Avoid eval/exec.",
            "status": "open",
        }

        with pytest.raises(FindingSchemaValidationError, match="finding_kind is required"):
            materialize_emitter_finding(
                finding,
                emitter_name="quick-scan-security",
                repo_root_canonical=self.repo_root_canonical,
            )

    def test_materialize_emitter_finding_rejects_kind_outside_active_report_contract(self):
        unsupported_kind = next(
            (
                definition
                for definition in iter_declared_finding_kind_definitions()
                if get_supported_report_finding_kind_definition(definition.kind_name) is None
            ),
            None,
        )
        assert unsupported_kind is not None

        finding = {
            "module": "scan",
            "skill": "quick-scan-security",
            "severity": "P1",
            "category": "security",
            "location": "d:/Git/sentinel/src/app.py:12",
            "description": "Finding kind should be rejected by active report contract.",
            "suggested_fix": "Use an active report-contract kind.",
            "status": "open",
            "finding_kind": unsupported_kind.kind_name,
        }
        if unsupported_kind.subject_required == "required":
            finding["subject_key"] = "scan:security"
        elif unsupported_kind.subject_required == "n/a":
            finding["subject_key"] = "_na"

        with pytest.raises(
            FindingSchemaValidationError,
            match="unsupported by active report contract",
        ):
            materialize_emitter_finding(
                finding,
                emitter_name="quick-scan-security",
                repo_root_canonical=self.repo_root_canonical,
            )

    def test_materialize_emitter_finding_rejects_malformed_structured_location(self):
        finding = {
            "module": "sentinel.assess.reviewer",
            "skill": "layer2a-quick-review",
            "severity": "P2",
            "category": "correctness",
            "location": {
                "line": "132-159",
                "function": "review_todo",
            },
            "description": "Structured location omitted the required file value.",
            "suggested_fix": "Include a file path in structured location payloads.",
            "status": "open",
            "finding_kind": "logic_bug",
        }

        with pytest.raises(
            FindingSchemaValidationError,
            match="structured location requires a non-empty file",
        ):
            materialize_emitter_finding(
                finding,
                emitter_name="layer2a-quick-review",
                repo_root_canonical=self.repo_root_canonical,
            )

    def test_materialize_emitter_finding_allows_legacy_string_location_fallback(self):
        finding = {
            "module": "sentinel.assess.reviewer",
            "skill": "layer2a-quick-review",
            "severity": "P2",
            "category": "correctness",
            "location": "sentinel/assess/reviewer.py:132-159 in review_todo",
            "description": "Legacy string location is still supported.",
            "suggested_fix": "Prefer structured locations for new emitters.",
            "status": "open",
            "finding_kind": "logic_bug",
        }

        materialized = materialize_emitter_finding(
            finding,
            emitter_name="layer2a-quick-review",
            repo_root_canonical=self.repo_root_canonical,
        )

        assert materialized["id"].startswith("F2-")


class TestRepairInvalidFinding:
    """Cycle 37: graceful degradation for LLM-noncompliant findings.

    The reviewer lane coerces an out-of-registry ``finding_kind`` (the model
    placing a ``category`` value in the ``finding_kind`` field) to
    ``structural_complexity`` and repairs ``subject_key`` violations rather
    than aborting the whole assessment.
    """

    repo_root_canonical = "d:/Git/sentinel"

    def _base_finding(self, **overrides) -> dict:
        finding = {
            "module": "sentinel.assess.reviewer",
            "skill": "layer2a-quick-review",
            "severity": "P2",
            "category": "correctness",
            "location": "sentinel/assess/reviewer.py:604",
            "description": "main() assumes argv has two elements.",
            "suggested_fix": "Guard against short argv.",
            "finding_kind": "logic_bug",
            "status": "open",
        }
        finding.update(overrides)
        return finding

    def test_repair_invalid_finding_kind_coerces_to_structural(self):
        finding = self._base_finding(
            finding_kind="wrong_assumption",
            subject_key="argv-length",
        )

        repaired = repair_invalid_finding(
            finding,
            emitter_name="layer2a-quick-review",
            repo_root_canonical=self.repo_root_canonical,
        )

        assert repaired["finding_kind"] == "structural_complexity"
        assert repaired["raw_finding_kind"] == "wrong_assumption"
        assert repaired["id"].startswith("F2-")

    def test_repair_invalid_finding_kind_increments_counter(self):
        counter = [0]
        repair_invalid_finding(
            self._base_finding(finding_kind="wrong_assumption"),
            emitter_name="layer2a-quick-review",
            repo_root_canonical=self.repo_root_canonical,
            invalid_kind_counter=counter,
        )
        assert counter[0] == 1

        # A registered kind must not increment the counter.
        repair_invalid_finding(
            self._base_finding(finding_kind="logic_bug"),
            emitter_name="layer2a-quick-review",
            repo_root_canonical=self.repo_root_canonical,
            invalid_kind_counter=counter,
        )
        assert counter[0] == 1

    def test_valid_finding_passes_through_unchanged(self):
        repaired = repair_invalid_finding(
            self._base_finding(finding_kind="logic_bug"),
            emitter_name="layer2a-quick-review",
            repo_root_canonical=self.repo_root_canonical,
        )

        assert repaired["finding_kind"] == "logic_bug"
        assert "raw_finding_kind" not in repaired
        assert repaired["id"].startswith("F2-")

    def test_repair_missing_subject_key_for_required_kind(self):
        # contract_violation requires a subject_key; the LLM omitted it.
        finding = self._base_finding(finding_kind="contract_violation")
        finding.pop("subject_key", None)

        repaired = repair_invalid_finding(
            finding,
            emitter_name="layer2a-quick-review",
            repo_root_canonical=self.repo_root_canonical,
        )

        assert repaired["finding_kind"] == "contract_violation"
        # P1 wave-3: placeholder is derived from finding_kind + file basename
        # instead of a flat "unknown:unknown". This finding has no `file`, so
        # the basename falls back to "unknown".
        assert repaired["subject_key"] == "contract_violation:unknown"
        assert "raw_finding_kind" not in repaired
