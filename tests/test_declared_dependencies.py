"""Every third-party module imported by the sentinel package must be declared in
pyproject.toml (core dependencies or an optional extra)."""
from __future__ import annotations

import ast
import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

_IMPORT_TO_DIST = {
    "yaml": "pyyaml",
    "dotenv": "python-dotenv",
    "git": "gitpython",
    "sentence_transformers": "sentence-transformers",
    "torch": "sentence-transformers",  # installed transitively by the workflow extra
}
_NOT_ON_PUBLIC_PYPI: set[str] = set()


def _dist_name(requirement: str) -> str:
    return re.split(r"[<>=!~\[; ]", requirement, maxsplit=1)[0].strip().lower().replace("_", "-")


def _declared() -> set[str]:
    project = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    reqs = list(project.get("dependencies", []))
    for extra in project.get("optional-dependencies", {}).values():
        reqs.extend(extra)
    return {_dist_name(r) for r in reqs}


def _imported_top_level_modules() -> dict[str, str]:
    found: dict[str, str] = {}
    for path in (ROOT / "sentinel").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module]
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if top not in sys.stdlib_module_names and top != "sentinel":
                    found.setdefault(top, path.relative_to(ROOT).as_posix())
    return found


def test_all_third_party_imports_are_declared() -> None:
    declared = _declared()
    missing = {
        module: where
        for module, where in _imported_top_level_modules().items()
        if module not in _NOT_ON_PUBLIC_PYPI
        and _IMPORT_TO_DIST.get(module, module).lower().replace("_", "-") not in declared
    }
    assert not missing, f"Undeclared third-party imports (module -> first file): {missing}"
