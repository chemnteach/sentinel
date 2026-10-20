"""Tests-green gate helpers for remediation branches (cycle 56)."""

from __future__ import annotations

import logging
import os
import shlex
import subprocess
import sys
from collections.abc import Sequence

log = logging.getLogger(__name__)

_GOVERN_ENV_VARS = {
    "SENTINEL_STATE_DIR",
    "SENTINEL_REPO_PATH",
    "SENTINEL_BASE_BRANCH",
    "SENTINEL_TEST_COMMAND",
}

# Only these executables (compared by basename, case-insensitive, sans ``.exe``)
# may be launched as a test runner. The current interpreter is always allowed.
_ALLOWED_EXECUTABLES = frozenset({"python", "python3", "py", "pytest"})


def _executable_basename(executable: str) -> str:
    base = os.path.basename(executable).lower()
    if base.endswith(".exe"):
        base = base[:-4]
    return base


def _validate_executable(executable: str) -> None:
    """Reject anything that is not an allow-listed test runner executable."""
    if executable == sys.executable:
        return
    if _executable_basename(executable) not in _ALLOWED_EXECUTABLES:
        raise ValueError(f"executable not permitted by run_tests: {executable!r}")


def _strip_quotes(token: str) -> str:
    if len(token) >= 2 and token[0] == token[-1] and token[0] in ("'", '"'):
        return token[1:-1]
    return token


def _resolve_argv(test_command: str | Sequence[str] | None) -> list[str]:
    """Build a validated argv list for ``subprocess.run(shell=False)``.

    Accepts an explicit argument list, or safely tokenizes a string with
    ``shlex`` (never a shell). Raises ``ValueError`` on malformed input or a
    disallowed executable.
    """
    if test_command is None or (isinstance(test_command, str) and not test_command.strip()):
        return [sys.executable, "-m", "pytest", "--tb=short", "-q"]

    if isinstance(test_command, str):
        # ``posix=False`` preserves Windows path separators (backslashes are not
        # treated as escape chars); quotes are then stripped from each token.
        # On POSIX, backslashes are uncommon in argv[0] so this is also safe.
        posix = os.name != "nt"
        tokens = shlex.split(test_command, posix=posix)
        argv = tokens if posix else [_strip_quotes(t) for t in tokens]
    else:
        argv = [str(a) for a in test_command]

    if not argv:
        raise ValueError("run_tests received an empty test command")

    _validate_executable(argv[0])
    return argv


def run_tests(
    repo_root: str,
    test_command: str | Sequence[str] | None = None,
    timeout: int = 300,
) -> dict:
    """Run the test suite in ``repo_root`` and return real results.

    Returns ``{passed: bool, returncode: int, output: str, summary: str}``.
    ``test_command`` may be an explicit argument list or a string (tokenized
    with ``shlex``, never a shell); it defaults to
    ``<current interpreter> -m pytest --tb=short -q``. The resolved command is
    always executed with ``shell=False`` so shell metacharacters (``;``,
    ``&&``, ``|``, ``$()``, backticks) are never interpreted. Malformed input,
    disallowed executables, timeouts and errors degrade to ``passed=False``
    rather than raising.
    """
    clean_env = {
        k: v for k, v in os.environ.items()
        if k not in _GOVERN_ENV_VARS
    }

    try:
        argv = _resolve_argv(test_command)
        result = subprocess.run(
            argv,
            shell=False,
            cwd=repo_root,
            capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            timeout=timeout,
            env=clean_env,
        )
        output = (result.stdout or "") + (result.stderr or "")
        passed = result.returncode == 0

        summary = ""
        for line in reversed(output.splitlines()):
            if "passed" in line or "failed" in line or "error" in line:
                summary = line.strip()
                break

        log.info(
            "Test run complete: returncode=%d summary=%s", result.returncode, summary
        )
        return {
            "passed": passed,
            "returncode": result.returncode,
            # On failure pytest prints progress dots/percentages FIRST and the
            # actual failure tracebacks + summary LAST. Head-truncation would
            # feed the retry loop mostly dots; return the TAIL on failure so the
            # Coder sees what actually failed.
            "output": output[:6000] if passed else output[-9000:],
            "summary": summary,
        }

    except subprocess.TimeoutExpired:
        log.warning("Test run timed out after %ds", timeout)
        return {
            "passed": False,
            "returncode": -1,
            "output": f"TIMEOUT after {timeout}s",
            "summary": "TIMEOUT",
        }
    except Exception as exc:
        log.warning("Test run failed: %s", exc)
        return {
            "passed": False,
            "returncode": -1,
            "output": str(exc),
            "summary": f"ERROR: {exc}",
        }
