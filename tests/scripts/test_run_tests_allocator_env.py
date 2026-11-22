"""``scripts/run_tests.sh`` forwards an explicit ``MALLOC_ARENA_MAX`` opt-in, and nothing else.

Regression for the allocator opt-in (address-space-bounded tests need a small arena count): an
exported ``MALLOC_ARENA_MAX`` must reach the pytest child exactly, an unset one must stay unset,
and a malformed one must be refused with status 2 BEFORE any interpreter, setup or test child
starts. The hermetic ``env -i`` must keep dropping credentials either way.

The chain under test is the real one, in an offline checkout copy (tests/pm/run_tests_harness_support.py):
``run_tests.sh`` -> ``run-in-hermes-env`` -> ``_activation.sh`` -> ``python -m pm.environments`` ->
``env -i`` runner -> pytest child. Only provisioning is a fixture; it proves nothing about a
locked install. A canary file outside the repository (so no repo conftest applies) records what
its own process saw.
"""

from __future__ import annotations

import os
import sys
import subprocess
from pathlib import Path

import pytest

from tests.pm.run_tests_harness_support import (
    SECRETS,
    composed_env,
    count_lines,
    explicit_python,
    observed_records,
    own_observation,
    provision,
    runner_checkout,
    runner_command,
    write_canary,
)

pytestmark = pytest.mark.platforms("linux")  # resource receipts use /proc and cgroup v2

MESSAGE = "MALLOC_ARENA_MAX must be a canonical decimal integer from 1 to 1024"
VALID = [None, "2", "1", "1024"]
INVALID = {
    "empty": "",
    "zero": "0",
    "negative": "-2",
    "whitespace": " ",
    "leading-zero": "02",
    "over-limit": "1025",
    "overflow": "9" * 40,
    "command-substitution": "$(touch {marker})",
    "list-injection": "1; touch {marker}",
    "env-injection": "1 HERMES_TEST_SECRET_SENTINEL=leaked",
    "newline": "1\n",
}
ROOT_SKEW_NS = 7 * 10**9


@pytest.fixture(autouse=True)
def _host_policy_is_not_worker_mode():
    """The runner reads the ACCOUNT's worker policy (not HOME-relative); worker mode ignores the
    private slot directory this fixture relies on."""
    from hermes_cli.worker_compute_capacity import worker_enabled

    if worker_enabled():
        pytest.skip("host worker_resources policy is enabled: the runner would ignore the fixture slot dir")


class Chain:
    """One offline checkout plus how to drive its runner."""

    def __init__(self, tmp_path: Path, *, stamp_skew_ns: int = 0) -> None:
        self.tmp = tmp_path
        self.root, self.env, self.store = runner_checkout(tmp_path, stamp_skew_ns=stamp_skew_ns)
        self.observed = tmp_path / "observed" / "child.jsonl"
        self.case = write_canary(tmp_path / "canary", self.observed)
        self.marker = tmp_path / "injected"
        self.setup_calls = self.root / "setup-calls.jsonl"
        self.bootstrap_log = tmp_path / "bootstrap-calls.log"
        self.explicit_log = tmp_path / "explicit-python-calls.log"

    def run(self, malloc: str | None, env: dict | None = None) -> subprocess.CompletedProcess:
        env = dict(self.env if env is None else env)
        env.pop("MALLOC_ARENA_MAX", None)
        if malloc is not None:
            env["MALLOC_ARENA_MAX"] = malloc.format(marker=self.marker)
        return subprocess.run(runner_command(self.root, self.case), cwd=self.tmp, env=env,
                              capture_output=True, text=True, timeout=300, check=False)

    def explicit_env(self) -> dict:
        return {**self.env, "HERMES_PYTHON": str(explicit_python(self.tmp))}

    def current_env(self) -> dict:
        """A caller already activated: PM's real composed environment for this checkout."""
        provision(self.root, self.env)
        self.setup_calls.unlink()
        env = {**self.env, **composed_env(self.root, self.env)}
        self.bootstrap_log.unlink(missing_ok=True)  # composing used the bootstrap; the run must not
        return env


def _assert_child(chain: Chain, run: subprocess.CompletedProcess, malloc: str | None) -> dict:
    report = run.stdout + run.stderr
    assert run.returncode == 0, report
    (record,) = observed_records(chain.observed)  # retries are off: exactly one execution
    env = record["env"]
    if malloc is None:
        assert "MALLOC_ARENA_MAX" not in env, env
    else:
        assert env.get("MALLOC_ARENA_MAX") == malloc, env.get("MALLOC_ARENA_MAX")
    for name in SECRETS:
        assert name not in env, f"{name} leaked into the hermetic child"
    assert not chain.marker.exists()
    # The run is serial and keeps the memory limits it inherited.
    assert record["rlimit_as"] == own_observation()["rlimit_as"]
    assert record["cgroup_memory_max"] == own_observation()["cgroup_memory_max"]
    assert Path(record["executable"]).resolve() == Path(sys.executable).resolve()
    # The private slot directory, not the host's, took the (single) file's slot.
    slots = {path.name for path in (chain.tmp / "host-slots").iterdir()}
    assert slots and slots <= {"0.lock", "1.lock"}, slots
    assert env.get("HERMES_TEST_SLOT_HELD") == "1"
    return record


@pytest.mark.parametrize("malloc", VALID, ids=lambda v: "absent" if v is None else v)
def test_explicit_hermes_python_forwards_the_allocator_opt_in(tmp_path, malloc):
    chain = Chain(tmp_path)
    run = chain.run(malloc, chain.explicit_env())
    _assert_child(chain, run, malloc)
    assert "not activated — using HERMES_PYTHON" in run.stdout
    assert count_lines(chain.setup_calls) == 0
    assert count_lines(chain.bootstrap_log) == 0
    assert count_lines(chain.explicit_log) >= 1  # the explicit interpreter did the work


@pytest.mark.parametrize("malloc", VALID, ids=lambda v: "absent" if v is None else v)
def test_current_activation_runs_without_setup(tmp_path, malloc):
    chain = Chain(tmp_path)
    run = chain.run(malloc, chain.current_env())
    _assert_child(chain, run, malloc)
    assert "re-running under run-in-hermes-env" not in run.stderr
    assert count_lines(chain.setup_calls) == 0
    assert count_lines(chain.bootstrap_log) == 0


@pytest.mark.parametrize("malloc", [None, "1024"], ids=["absent", "1024"])
def test_missing_activation_reexecs_once_through_setup(tmp_path, malloc):
    chain = Chain(tmp_path)
    run = chain.run(malloc)
    _assert_child(chain, run, malloc)
    assert run.stderr.count("re-running under run-in-hermes-env") == 1, run.stderr
    assert count_lines(chain.setup_calls) == 1


@pytest.mark.parametrize("skew_ns", [ROOT_SKEW_NS, -ROOT_SKEW_NS], ids=["input-newer", "input-older"])
def test_stale_activation_resyncs_once_and_restamps(tmp_path, skew_ns):
    chain = Chain(tmp_path)
    env = chain.current_env()
    lock = chain.root / "pm" / "lock.json"
    moved = lock.stat().st_mtime_ns + skew_ns
    os.utime(lock, ns=(moved, moved))
    run = chain.run("2", env)
    _assert_child(chain, run, "2")
    assert run.stderr.count("re-running under run-in-hermes-env") == 1, run.stderr
    assert count_lines(chain.setup_calls) == 1
    stamp = Path(env["__HERMES_ACTIVATED"]).parent / "inputs" / "pm" / "lock.json"
    assert stamp.stat().st_mtime_ns == lock.stat().st_mtime_ns == moved


def test_refresh_that_leaves_the_environment_stale_is_refused(tmp_path):
    """Setup stamps a mtime the input does not have: the re-exec must stop, not loop or run tests."""
    chain = Chain(tmp_path, stamp_skew_ns=10**9)
    run = chain.run("2")
    report = run.stdout + run.stderr
    assert run.returncode == 1, report
    assert "run-in-hermes-env did not produce a current environment" in run.stderr, report
    assert count_lines(chain.setup_calls) == 1
    assert observed_records(chain.observed) == []


@pytest.mark.parametrize("name", INVALID)
@pytest.mark.parametrize("mode", ["missing-activation", "explicit-python"])
def test_invalid_allocator_value_is_refused_before_any_child(tmp_path, mode, name):
    chain = Chain(tmp_path)
    env = chain.explicit_env() if mode == "explicit-python" else None
    run = chain.run(INVALID[name], env)
    report = run.stdout + run.stderr
    assert run.returncode == 2, report
    assert MESSAGE in run.stderr, report
    assert "▶" not in run.stdout, report  # no stage of the runner started
    assert count_lines(chain.setup_calls) == 0, "setup ran"
    assert count_lines(chain.bootstrap_log) == 0, "the bootstrap interpreter ran"
    assert count_lines(chain.explicit_log) == 0, "HERMES_PYTHON ran"
    assert observed_records(chain.observed) == [], "a test child ran"
    assert not chain.marker.exists(), "the value was evaluated by a shell"
    assert "leaked" not in report
