"""Unittest entry paths must not clobber an inherited Hermes config.

Run the historical writer only against a disposable sentinel home, never real state.
"""
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SENTINEL = "model: sentinel\nterminal:\n  backend: local\n"


def _run(tmp_path, args):
    home = tmp_path / "inherited-home"
    home.mkdir()
    config = home / "config.yaml"
    config.write_text(SENTINEL)
    env = dict(os.environ, HERMES_HOME=str(home), HOME=str(tmp_path))
    result = subprocess.run(
        [sys.executable, "-m", "unittest", *args], cwd=ROOT, env=env,
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
