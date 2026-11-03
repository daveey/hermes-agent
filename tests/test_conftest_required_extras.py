"""HERMES_TEST_REQUIRE_EXTRAS: a listed optional extra fails instead of skipping (tests/_fixtures/required_extras.py)."""
from __future__ import annotations

if not __name__.startswith("tests."): raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")  # unittest-discovery guard

import os
import shutil
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from tests._fixtures import required_extras

_MISSING = "hermes_required_extras_probe_absent"
_REPO_ROOT = Path(__file__).resolve().parents[1]


def _call_expecting_error(fn, modname):
    """Call *fn*; a skip here is the bug under test, so report it as a failure, not a skip."""
    try:
        fn(modname)
    except pytest.skip.Exception as exc:
        pytest.fail(f"{modname} was skipped, not failed: {exc}")


def _real_importorskip():
    return getattr(pytest.importorskip, "__wrapped__", pytest.importorskip)


def test_listed_missing_module_raises_and_names_it():
    wrapped = required_extras.strict_importorskip(_real_importorskip(), frozenset({_MISSING}))
    with pytest.raises(ImportError, match=_MISSING) as excinfo:
        _call_expecting_error(wrapped, f"{_MISSING}.test_utils")
    assert required_extras.ENV_VAR in str(excinfo.value)


def test_unlisted_missing_module_still_skips():
    wrapped = required_extras.strict_importorskip(_real_importorskip(), frozenset({"keyring"}))
    with pytest.raises(pytest.skip.Exception):
        wrapped(_MISSING, reason="optional extra not installed")


def test_listed_present_module_is_returned():
    wrapped = required_extras.strict_importorskip(_real_importorskip(), frozenset({"json"}))
    import json

    assert wrapped("json") is json


def test_unset_leaves_importorskip_untouched(monkeypatch):
    sentinel = _real_importorskip()
    monkeypatch.setattr(pytest, "importorskip", sentinel)
    assert required_extras.install({}) is False
    assert required_extras.install({required_extras.ENV_VAR: " , "}) is False
    assert pytest.importorskip is sentinel
    with pytest.raises(pytest.skip.Exception):
        pytest.importorskip(_MISSING)


def test_install_matches_top_level_names_and_does_not_stack(monkeypatch):
    monkeypatch.setattr(pytest, "importorskip", _real_importorskip())
    env = {required_extras.ENV_VAR: f"keyring, {_MISSING}.sub"}
    assert required_extras.install(env) is True
    assert required_extras.install(env) is True  # re-install wraps the original, not the wrapper
    assert getattr(pytest.importorskip, "__wrapped__", None) is _real_importorskip()
    with pytest.raises(ImportError, match=_MISSING):
        _call_expecting_error(pytest.importorskip, f"{_MISSING}.other")
    with pytest.raises(pytest.skip.Exception):
        pytest.importorskip("hermes_required_extras_unlisted_absent")


@pytest.mark.parametrize(
    ("env_value", "expect_error"),
    [(None, False), ("other_extra", False), (_MISSING, True)],
    ids=["unset-skips", "unlisted-skips", "listed-fails"],
)
def test_collection_through_conftest(tmp_path, env_value, expect_error):
    """End to end: a module-level importorskip under tests/ collects as skip or error via conftest."""
    probe_dir = _REPO_ROOT / "tests" / f"_required_extras_probe_{os.getpid()}_{tmp_path.name}"
    probe_dir.mkdir()
    try:
        probe = probe_dir / "test_probe.py"
        probe.write_text(
            textwrap.dedent(
                f"""
                import pytest
                mod = pytest.importorskip("{_MISSING}", reason="optional extra not installed")
                def test_never():
                    pass
                """
            ),
            encoding="utf-8",
        )
        env = {k: v for k, v in os.environ.items() if k != required_extras.ENV_VAR}
        if env_value is not None:
            env[required_extras.ENV_VAR] = env_value
        result = subprocess.run(
            [sys.executable, "-m", "pytest", "--collect-only", "-q", "-p", "no:cacheprovider", str(probe)],
            cwd=_REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=300,
            check=False,
        )
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)
    output = result.stdout + result.stderr
    if expect_error:
        assert result.returncode != 0, output
        assert _MISSING in output and required_extras.ENV_VAR in output, output
    else:
        assert result.returncode in (0, 5), output  # 5 = nothing collected (module skipped)
        assert "ERROR" not in output and "error" not in output.splitlines()[-1], output
