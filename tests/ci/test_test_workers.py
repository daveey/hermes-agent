"""scripts/ci/test_workers.py: test workers come from the CI slice budget, not nproc (t_94f2c8ba)."""
from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

_PATH = Path(__file__).resolve().parents[2] / "scripts" / "ci" / "test_workers.py"
_spec = importlib.util.spec_from_file_location("test_workers_mod", _PATH)
tw = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(tw)


def test_budget_divides_the_slice_across_concurrent_shards():
    n, why = tw.workers({"HERMES_CI_SLICE_CPUS": "10", "HERMES_CI_CONCURRENT_SHARDS": "3"}, nproc=16)
    assert n == 5  # 10/3 = 3.33 CPUs x 1.5
    assert "10 CPUs / 3 concurrent shards" in why


def test_nproc_does_not_change_a_budgeted_count():
    env = {"HERMES_CI_SLICE_CPUS": "10", "HERMES_CI_CONCURRENT_SHARDS": "3"}
    assert tw.workers(env, nproc=3)[0] == tw.workers(env, nproc=64)[0]


def test_runner_quota_caps_the_per_shard_share():
    env = {"HERMES_CI_SLICE_CPUS": "10", "HERMES_CI_CONCURRENT_SHARDS": "1", "HERMES_CI_RUNNER_CPUS": "4"}
    assert tw.workers(env, nproc=16)[0] == 6


def test_floor_of_two_workers_and_explicit_override_wins():
    assert tw.workers({"HERMES_CI_SLICE_CPUS": "2", "HERMES_CI_CONCURRENT_SHARDS": "8"}, nproc=16)[0] == 2
    env = {"HERMES_TEST_WORKERS": "7", "HERMES_CI_SLICE_CPUS": "10", "HERMES_CI_CONCURRENT_SHARDS": "3"}
    assert tw.workers(env, nproc=16) == (7, "HERMES_TEST_WORKERS")


def test_without_a_slice_budget_it_keeps_the_old_two_per_cpu():
    assert tw.workers({}, nproc=3)[0] == 6


def test_bad_budget_values_fail_loudly():
    with pytest.raises(SystemExit):
        tw.workers({"HERMES_CI_SLICE_CPUS": "ten", "HERMES_CI_CONCURRENT_SHARDS": "3"}, nproc=4)
    with pytest.raises(SystemExit):
        tw.workers({"HERMES_CI_SLICE_CPUS": "10", "HERMES_CI_CONCURRENT_SHARDS": "0"}, nproc=4)


def test_cli_prints_only_the_number_on_stdout():
    proc = subprocess.run(
        [sys.executable, str(_PATH)],
        env={"HERMES_CI_SLICE_CPUS": "10", "HERMES_CI_CONCURRENT_SHARDS": "3", "PATH": "/usr/bin:/bin"},
        capture_output=True, text=True, timeout=60, check=True,
    )
    assert proc.stdout.strip() == "5"
    assert "slice 10 CPUs" in proc.stderr
