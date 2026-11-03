"""Autouse offline-transport fixtures load in a pytest-only interpreter.

The bootstrap-installer lane runs tests/scripts/test_fresh_source_install.py
with only pytest installed and drives Hermes in subprocesses, so the
in-process httpx/requests fences must stand down instead of erroring setup.
"""
if not __name__.startswith("tests."): raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")  # unittest-discovery guard
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
_HIDE_TRANSPORTS = (
    "import sys\n"
    "sys.modules['httpx'] = None\n"
    "sys.modules['requests'] = None\n"
    "import pytest\n"
    "sys.exit(pytest.main(['-q', '-p', 'no:cacheprovider', sys.argv[1]]))\n"
)


def test_probe_runs_under_every_autouse_fixture():
    """Collected by the child run below; trivially green in the normal suite."""


def test_autouse_fixtures_skip_absent_transport_libraries():
    node = f"{Path(__file__).relative_to(REPO).as_posix()}::test_probe_runs_under_every_autouse_fixture"
    result = subprocess.run(
        [sys.executable, "-c", _HIDE_TRANSPORTS, node],
        cwd=REPO, capture_output=True, text=True, timeout=120, check=False,
    )
    assert result.returncode == 0, result.stdout[-4000:] + result.stderr[-2000:]
    assert "1 passed" in result.stdout
