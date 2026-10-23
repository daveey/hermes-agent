"""The delegate-child env marker is bound to the agent process that fenced the lineage. [daveey]

Root cause of the 2026-10-21 Desktop fence (Hermes.app pid 59331): HERMES_DELEGATED_CHILD_CONTEXT
is inherited by every descendant, including an app a worker reopened with ``open`` (ppid 1), so the
whole Desktop believed it was a delegate child. Scrubbing at launch sites kept missing one (a
detached BabyBear carried it the next day). The marker now only counts in a process that still
descends from its owner; anything that left the lineage heals itself.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from agent import delegation_context as dc

REPO = Path(__file__).resolve().parents[2]

PROBE = (
    "import json,os,sys;sys.path.insert(0,%r);"
    "from agent import delegation_context as dc;"
    "fenced=dc.is_delegated_child_process_context();"
    "print(json.dumps({'fenced':fenced,'marker':os.environ.get(dc.DELEGATED_CHILD_ENV_MARKER),"
    "'owner':os.environ.get(dc.DELEGATED_CHILD_OWNER_ENV)}))" % str(REPO)
)


def _probe(env):
    out = subprocess.run([sys.executable, "-c", PROBE], env=env, capture_output=True, text=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


@pytest.fixture
def worker_env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_abc12345")
    monkeypatch.delenv(dc.DELEGATED_CHILD_ENV_MARKER, raising=False)
    monkeypatch.delenv(dc.DELEGATED_CHILD_OWNER_ENV, raising=False)
    dc._live_cache.clear()
    return dc.delegated_child_subprocess_env(dict(os.environ))


def test_spawned_descendant_is_still_fenced(worker_env):
    assert worker_env[dc.DELEGATED_CHILD_OWNER_ENV].startswith(f"{os.getpid()}:")
    assert _probe(worker_env)["fenced"] is True


def test_process_outside_the_lineage_is_not_fenced_and_heals(worker_env):
    # A process that does not descend from the owner (what `open` / a detached launch produces):
    # owner = an unrelated live process.
    sibling = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"])
    try:
        fp = dc._start_fingerprint(sibling.pid)
        env = dict(worker_env, **{dc.DELEGATED_CHILD_OWNER_ENV: f"{sibling.pid}:{fp}"})
        got = _probe(env)
    finally:
        sibling.kill()
        sibling.wait()
    assert got == {"fenced": False, "marker": None, "owner": None}


def test_dead_owner_unfences(worker_env):
    gone = subprocess.Popen([sys.executable, "-c", "pass"])
    gone.wait()
    env = dict(worker_env, **{dc.DELEGATED_CHILD_OWNER_ENV: f"{gone.pid}:1"})
    assert _probe(env)["fenced"] is False


def test_unbound_legacy_marker_still_fences(worker_env):
    env = dict(worker_env)
    env.pop(dc.DELEGATED_CHILD_OWNER_ENV)
    assert _probe(env)["fenced"] is True


def test_inherited_owner_is_kept_through_nested_spawns(worker_env, monkeypatch):
    monkeypatch.setenv(dc.DELEGATED_CHILD_ENV_MARKER, worker_env[dc.DELEGATED_CHILD_ENV_MARKER])
    monkeypatch.setenv(dc.DELEGATED_CHILD_OWNER_ENV, worker_env[dc.DELEGATED_CHILD_OWNER_ENV])
    again = dc.scrub_kanban_env(dict(os.environ))
    assert again[dc.DELEGATED_CHILD_OWNER_ENV] == worker_env[dc.DELEGATED_CHILD_OWNER_ENV]


def test_owner_key_is_agent_scope():
    assert dc.is_agent_scope_env_key(dc.DELEGATED_CHILD_OWNER_ENV)
