"""A bare ``python -m unittest`` run must not write test failures into a live Hermes home.

pytest isolates HERMES_HOME in tests/conftest.py before any test module is imported;
unittest never loads conftest. A unittest run that inherits the operator's home sends the
fixtures' deliberate failures (e.g. the fake EACCES in test_code_kernel_remote) through
hermes_logging into that home's errors.log, where the coach self-heal lane files them as
real defects (agent-dada3769d1).

Every case uses a disposable HOME, so the "production" root here is ``<tmp>/.hermes``,
never the real one.
"""
if not __name__.startswith("tests."): raise ImportError("Use scripts/run_tests.sh; unittest discovery bypasses test isolation")  # unittest-discovery guard
import os
from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
CANARY = "canary: untouched\n"
NOISY_TEST = (
    "tests.tools.test_code_kernel_remote.TestSharedHostLockdown."
    "test_spawn_locks_down_dirs_and_hides_token"
)
WARNING = "remote kernel spawn failed"


def _env(tmp_path, hermes_home):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("HERMES_", "PYTEST_"))}
    env["HOME"] = str(tmp_path)
    env["TMPDIR"] = str(tmp_path / "tmp")
    (tmp_path / "tmp").mkdir(exist_ok=True)
    if hermes_home is not None:
        env["HERMES_HOME"] = str(hermes_home)
    return env


def _canary_home(tmp_path):
    prod = tmp_path / ".hermes"
    (prod / "logs").mkdir(parents=True)
    (prod / "profiles" / "work" / "logs").mkdir(parents=True)
    for home in (prod, prod / "profiles" / "work"):
        (home / "logs" / "errors.log").write_text(CANARY)
        (home / "config.yaml").write_text(CANARY)
    return prod


def _snapshot(prod):
    return {str(p.relative_to(prod)): p.read_bytes()
            for p in sorted(prod.rglob("*")) if p.is_file()}


def _run(env, args):
    return subprocess.run(
        [sys.executable, "-m", "unittest", *args], cwd=ROOT, env=env,
        capture_output=True, text=True, timeout=180, check=False,
    )


UNSAFE = {
    "unset": lambda prod: None,
    "production_root": lambda prod: prod,
    "production_profile": lambda prod: prod / "profiles" / "work",
}
# Importing a module that imports hermes_cli.main attaches errors.log to the ambient home,
# as broad discovery did in the incident; -k then runs only the fixture that warns.
NAMED = ["-k", NOISY_TEST.rsplit(".", 1)[1],
         "tests.hermes_cli.test_agent_env_advertisement", NOISY_TEST.rsplit(".", 2)[0]]
ENTRY = {
    "named": NAMED,
    "discover_below_tests": ["discover", "-s", "tests/tools", "-t", ".",
                             "-p", "test_code_kernel_remote.py"],
}


@pytest.mark.parametrize("entry", sorted(ENTRY))
@pytest.mark.parametrize("home", sorted(UNSAFE))
def test_unittest_against_production_home_refuses_before_writes(tmp_path, home, entry):
    prod = _canary_home(tmp_path)
    before = _snapshot(prod)
    result = _run(_env(tmp_path, UNSAFE[home](prod)), ENTRY[entry])
    out = result.stdout + result.stderr
    assert result.returncode != 0, out
    assert "HERMES_HOME=$(mktemp -d -p /tmp)" in out, out
    assert _snapshot(prod) == before


def test_unittest_with_isolated_home_runs_and_keeps_warning(tmp_path):
    prod = _canary_home(tmp_path)
    before = _snapshot(prod)
    iso = tmp_path / "isolated-home"
    iso.mkdir()
    result = _run(_env(tmp_path, iso), NAMED)
    out = result.stdout + result.stderr
    assert result.returncode == 0, out
    assert "Ran 1 test" in out
    # The genuine warning still reaches the (isolated) home's errors.log.
    assert WARNING in (iso / "logs" / "errors.log").read_text()
    assert _snapshot(prod) == before


def test_non_unittest_importers_of_tests_package_still_work(tmp_path):
    # evals/ and apps/desktop/e2e import helpers from ``tests`` outside any runner.
    _canary_home(tmp_path)
    result = subprocess.run(
        [sys.executable, "-c", "import tests; print('ok')"], cwd=ROOT,
        env=_env(tmp_path, None), capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_production_logging_still_records_warnings(tmp_path):
    # Control: the guard must not suppress real warnings in a non-test process.
    prod = _canary_home(tmp_path)
    code = (
        "import logging, hermes_logging; hermes_logging.setup_logging(); "
        "logging.getLogger('tools.code_kernel_remote').warning('real warning probe')"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], cwd=ROOT, env=_env(tmp_path, prod),
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "real warning probe" in (prod / "logs" / "errors.log").read_text()
