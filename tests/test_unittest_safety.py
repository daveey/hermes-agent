"""Unittest entry paths must not clobber an inherited Hermes config.

Run the historical writer only against a disposable sentinel home, never real state.
"""
if not __name__.startswith("tests."): raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")  # unittest-discovery guard
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SENTINEL = "model: sentinel\nterminal:\n  backend: local\n"


def _run(tmp_path, args, cwd=ROOT):
    home = tmp_path / "inherited-home"
    home.mkdir()
    config = home / "config.yaml"
    config.write_text(SENTINEL)
    env = dict(os.environ, HERMES_HOME=str(home), HOME=str(tmp_path))
    result = subprocess.run(
        [sys.executable, "-m", "unittest", *args], cwd=cwd, env=env,
        capture_output=True, text=True, timeout=120, check=False,
    )
    return result, config


def test_explicit_image_unittest_preserves_inherited_home(tmp_path):
    result, config = _run(tmp_path, [
        "tests.tools.test_image_generate_schema.TestDynamicParamGating."
        "test_krea_3_advertises_edit_wording_and_its_own_controls", "-v",
    ])
    assert result.returncode == 0, result.stdout + result.stderr
    assert "Ran 1 test" in result.stderr
    assert config.read_text() == SENTINEL


@pytest.mark.parametrize("start", [".", "tests"])
def test_repository_unittest_discovery_refuses_before_test_writes(tmp_path, start):
    # Restrict RED to the historical writer, not thousands of unrelated tests.
    result, config = _run(tmp_path, [
        "discover", "-s", start, "-t", ".", "-p", "test_image_generate_schema.py",
    ])
    assert result.returncode != 0, result.stdout + result.stderr
    assert "scripts/run_tests.sh" in result.stderr
    assert config.read_text() == SENTINEL


REFUSAL = "Use scripts/run_tests.sh; unittest discovery bypasses test isolation"
PATTERN = "test_image_generate_schema.py"


@pytest.mark.parametrize(
    "form",
    [
        ("root-no-t", ROOT, ["discover", "-s", "tests", "-p", PATTERN]),
        ("inside-tests", ROOT / "tests", ["discover", "-p", PATTERN]),
        ("inside-tests-bare", ROOT / "tests", ["-q"]),
    ],
    ids=lambda f: f[0],
)
def test_uncovered_discovery_forms_are_refused(tmp_path, form):
    _, cwd, args = form
    result, config = _run(tmp_path, args, cwd=cwd)
    assert result.returncode != 0, result.stdout + result.stderr
    assert REFUSAL in result.stdout + result.stderr
    assert config.read_text() == SENTINEL


def test_bare_unittest_at_repo_root_is_refused(tmp_path):
    result, config = _run(tmp_path, ["-q"])
    assert result.returncode != 0, result.stdout + result.stderr
    assert "scripts/run_tests.sh" in result.stderr
    assert config.read_text() == SENTINEL


def _package_dirs():
    tests = ROOT / "tests"
    return sorted(
        p.parent for p in tests.rglob("__init__.py") if "__pycache__" not in p.parts
    )


def test_every_test_package_refuses_discovery():
    missing = [
        str(d.relative_to(ROOT))
        for d in _package_dirs()
        if "def load_tests(loader, tests, pattern)" not in (d / "__init__.py").read_text()
        or REFUSAL not in (d / "__init__.py").read_text()
    ]
    assert not missing, f"packages without the discovery-refusing load_tests: {missing}"


def test_every_top_level_test_module_has_import_guard():
    missing = [
        p.name
        for p in sorted((ROOT / "tests").glob("test*.py"))
        if "# unittest-discovery guard" not in p.read_text()
        or REFUSAL not in p.read_text()
    ]
    assert not missing, f"top-level test modules without the guard line: {missing}"
