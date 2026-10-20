"""
tests/test_reviewer_triggers.py

Tests for language-aware specialist trigger logic in reviewer.py.

Verifies that the concurrency specialist is NOT triggered for JS/TS modules
where async/await is a standard language feature, while still being triggered
for Python and other languages.
"""

from __future__ import annotations

import pytest

from sentinel.assess.reviewer import (
    _is_async_native_module,
    _determine_specialists,
)
from sentinel.skills.loader import Skills


# ---------------------------------------------------------------------------
# _is_async_native_module unit tests
# ---------------------------------------------------------------------------


class TestIsAsyncNativeModule:
    """Tests for the _is_async_native_module helper."""

    def test_concurrency_not_triggered_for_js_module(self) -> None:
        """JS/TS module should be detected as async-native."""
        module = {
            "name": "frontend/components",
            "files": [
                "src/App.tsx",
                "src/hooks/useAuth.ts",
                "src/components/Button.jsx",
                "src/index.js",
            ],
        }
        assert _is_async_native_module(module) is True

    def test_concurrency_triggered_for_python_module(self) -> None:
        """Python module should NOT be detected as async-native."""
        module = {
            "name": "backend/api",
            "files": [
                "api/views.py",
                "api/models.py",
                "api/serializers.py",
            ],
        }
        assert _is_async_native_module(module) is False

    def test_mixed_module_majority_rules(self) -> None:
        """>50% JS/TS files means async-native; <=50% does not."""
        # 3 JS/TS out of 5 = 60% → async-native
        module_mostly_js = {
            "name": "mixed-mostly-js",
            "files": [
                "index.ts",
                "utils.js",
                "component.tsx",
                "config.py",
                "setup.cfg",
            ],
        }
        assert _is_async_native_module(module_mostly_js) is True

        # 2 JS/TS out of 5 = 40% → NOT async-native
        module_mostly_py = {
            "name": "mixed-mostly-py",
            "files": [
                "index.ts",
                "utils.js",
                "views.py",
                "models.py",
                "tests.py",
            ],
        }
        assert _is_async_native_module(module_mostly_py) is False

    def test_async_native_check_empty_files(self) -> None:
        """Empty file list should return False (not crash)."""
        assert _is_async_native_module({"name": "empty", "files": []}) is False
        assert _is_async_native_module({"name": "missing"}) is False


# ---------------------------------------------------------------------------
# Integration: _determine_specialists with language awareness
# ---------------------------------------------------------------------------


class TestDetermineSpecialistsLanguageAware:
    """Tests that _determine_specialists respects async-native modules."""

    def test_async_keyword_skips_concurrency_for_js_module(self) -> None:
        """When findings mention 'async', concurrency should NOT run for JS modules."""
        js_module = {
            "name": "frontend",
            "files": ["App.tsx", "hooks.ts", "index.js"],
        }
        findings = [
            {"description": "Uses async/await pattern for data fetching"},
        ]

        specialists = _determine_specialists(
            findings=findings,
            module_content="const data = await fetch('/api')",
            recommended_skills=[],
            module=js_module,
        )

        assert Skills.CONCURRENCY not in specialists

    def test_async_keyword_triggers_concurrency_for_python_module(self) -> None:
        """When findings mention 'async', concurrency SHOULD run for Python modules."""
        py_module = {
            "name": "backend",
            "files": ["server.py", "handlers.py"],
        }
        findings = [
            {"description": "Uses async event loop with shared state"},
        ]

        specialists = _determine_specialists(
            findings=findings,
            module_content="async def handle(request):",
            recommended_skills=[],
            module=py_module,
        )

        assert Skills.CONCURRENCY in specialists

    def test_race_condition_still_triggers_concurrency_for_js(self) -> None:
        """'race condition' should trigger concurrency even for JS modules.

        Only the 'async' and 'concurrency' keywords are suppressed for
        async-native modules. Explicit concurrency concerns like 'race condition'
        should still trigger the specialist.
        """
        js_module = {
            "name": "frontend",
            "files": ["worker.ts", "shared.ts", "index.ts"],
        }
        findings = [
            {"description": "Potential race condition in shared state updates"},
        ]

        specialists = _determine_specialists(
            findings=findings,
            module_content="// web worker shared state",
            recommended_skills=[],
            module=js_module,
        )

        assert Skills.CONCURRENCY in specialists

    def test_full_coverage_mode_runs_all_specialists_without_findings(self) -> None:
        """Full coverage mode should always schedule every specialist skill."""
        js_module = {
            "name": "frontend",
            "files": ["App.tsx", "hooks.ts", "index.js"],
        }

        specialists = _determine_specialists(
            findings=[],
            module_content="const data = await fetch('/api')",
            recommended_skills=[],
            module=js_module,
            enforce_full_specialist_coverage=True,
        )

        expected = {
            Skills.CORRECTNESS,
            Skills.SECURITY,
            Skills.DATA_INTEGRITY,
            Skills.PERFORMANCE,
            Skills.MAINTAINABILITY,
            Skills.TESTABILITY,
            Skills.ERROR_HANDLING,
            Skills.CONCURRENCY,
            Skills.CODE_SMELL,
            Skills.REFACTORING_ADVISOR,
        }
        assert expected.issubset(set(specialists))

    def test_alarm_keyword_triggers_alarm_integrity(self) -> None:
        """A finding mentioning 'alarm' routes the layer2k specialist."""
        module = {"name": "controller", "files": ["AcmeEquipmentManager.cs"]}
        findings = [
            {"description": "standardEquipment_AlarmReported handles only one alarm id"},
        ]
        specialists = _determine_specialists(
            findings=findings,
            module_content="switch (e.ECSAlarmReport.AlarmData.AlarmId)",
            recommended_skills=[],
            module=module,
        )
        assert Skills.ALARM_INTEGRITY in specialists

    def test_non_alarm_module_does_not_trigger_alarm_integrity(self) -> None:
        """The alarm specialist stays off when no WIP-stall signal is present."""
        module = {"name": "utils", "files": ["math.py"]}
        findings = [{"description": "off-by-one in loop bound"}]
        specialists = _determine_specialists(
            findings=findings,
            module_content="for i in range(n): pass",
            recommended_skills=[],
            module=module,
        )
        assert Skills.ALARM_INTEGRITY not in specialists

    def test_full_coverage_includes_alarm_integrity(self) -> None:
        """Full specialist coverage always schedules the alarm specialist."""
        specialists = _determine_specialists(
            findings=[],
            module_content="class C {}",
            recommended_skills=[],
            module={"name": "m", "files": ["a.cs"]},
            enforce_full_specialist_coverage=True,
        )
        assert Skills.ALARM_INTEGRITY in specialists

    def test_no_alarms_disables_specialist_when_triggered(self) -> None:
        """enable_alarm_specialist=False (--no-alarms) drops layer2k even when a
        finding mentions 'alarm'."""
        specialists = _determine_specialists(
            findings=[{"description": "handles one alarm id"}],
            module_content="switch (e.AlarmId)",
            recommended_skills=[],
            module={"name": "controller", "files": ["Eq.cs"]},
            enable_alarm_specialist=False,
        )
        assert Skills.ALARM_INTEGRITY not in specialists

    def test_no_alarms_disables_specialist_under_full_coverage(self) -> None:
        """--no-alarms wins even over full specialist coverage."""
        specialists = _determine_specialists(
            findings=[],
            module_content="class C {}",
            recommended_skills=[],
            module={"name": "m", "files": ["a.cs"]},
            enforce_full_specialist_coverage=True,
            enable_alarm_specialist=False,
        )
        assert Skills.ALARM_INTEGRITY not in specialists


class TestDetermineSpecialistsReliabilityFirst:
    """Tests for the reliability-first pilot review profile."""

    def test_reliability_first_runs_core_skills_without_findings(self) -> None:
        specialists = _determine_specialists(
            findings=[],
            module_content="def save_data():\n    return True\n",
            review_profile="reliability-first",
        )

        assert specialists == [
            Skills.CORRECTNESS,
            Skills.DATA_INTEGRITY,
            Skills.ERROR_HANDLING,
            Skills.OPERATIONAL_RISK,
        ]

    def test_reliability_first_filters_dropped_skills(self) -> None:
        findings = [
            {
                "description": (
                    "performance issue with complexity, test coverage gaps, "
                    "maintainability concerns, and code smell that needs refactor"
                )
            }
        ]

        specialists = _determine_specialists(
            findings=findings,
            module_content="def save_data():\n    return True\n",
            recommended_skills=[
                Skills.PERFORMANCE,
                Skills.TESTABILITY,
                Skills.MAINTAINABILITY,
                Skills.CODE_SMELL,
                Skills.REFACTORING_ADVISOR,
                Skills.SECURITY,
            ],
            review_profile="reliability-first",
        )

        assert Skills.CORRECTNESS in specialists
        assert Skills.DATA_INTEGRITY in specialists
        assert Skills.ERROR_HANDLING in specialists
        assert Skills.SECURITY in specialists
        assert Skills.PERFORMANCE not in specialists
        assert Skills.TESTABILITY not in specialists
        assert Skills.MAINTAINABILITY not in specialists
        assert Skills.CODE_SMELL not in specialists
        assert Skills.REFACTORING_ADVISOR not in specialists

    def test_reliability_first_adds_security_for_boundary_module(self) -> None:
        module = {
            "name": "config",
            "files": ["Config/App.config", "Config/Service.xml"],
        }

        specialists = _determine_specialists(
            findings=[],
            module_content="<configuration><service endpoint='https://example' /></configuration>",
            module=module,
            review_profile="reliability-first",
        )

        assert Skills.SECURITY in specialists
