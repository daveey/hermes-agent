"""A blocked passive PM worker must not hold a CLI command hostage."""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from hermes_cli import venv_sync

BUDGET = 4.0  # seconds: the probe deadline this test sets


@pytest.mark.platforms("posix")
@pytest.mark.parametrize("stage", ["preparation", "response"])
def test_command_starts_with_blocked_pm_worker(tmp_path, stage):
    root = tmp_path / "checkout"
    (root / ".git").mkdir(parents=True)
    (root / "pyproject.toml").write_text("[project]\nname='fixture'\n")
    (root / "install-stamp.json").write_text('{"updateMechanism":"self"}')
    source = Path(__file__).resolve().parents[2]
    script = f'''
import sys, os, json, threading, time
from pathlib import Path
sys.path.insert(0, {str(source)!r})
from hermes_cli import venv_sync
import pm
# Block real PM client dispatch, wherever it runs: either before its worker starts, or
# on its response. The same block goes into this process (where an unbounded launcher
# asks PM directly) and into the bounded probe's worker, when there is one.
client_block = """
import pm.client, threading
if {stage!r} == 'preparation':
    def prepare():
        Path({str(tmp_path / 'probe-entered')!r}).write_text('preparation')
        threading.Event().wait()
    pm.client.runtime_environment = prepare
else:
    pm.client.runtime_environment = lambda: dict(os.environ)
    pm.client._worker_command = lambda *a: [sys.executable, '-c', {('from pathlib import Path; import os, threading; Path(' + repr(str(tmp_path / 'probe-entered')) + ').write_text(str(os.getpid())); threading.Event().wait()')!r}]
"""
# The runtime diagnostic's slow part (plugin-union inputs, YAML, PM drift) sits in
# pm.activate; block it inside the probe worker only, where the diagnostic runs.
activate_block = """
import pm
def _blocked_activate(*a, **k):
    Path({str(tmp_path / 'runtime-entered')!r}).write_text(str(os.getpid()))
    threading.Event().wait()
pm.activate = _blocked_activate
"""
exec(client_block)
try:
    from hermes_cli import launch_probe
except ImportError:  # a launcher with no bounded probe: only the in-process block applies
    launch_probe = None
else:
    launch_probe._WORKER = launch_probe._WORKER.replace(
        'try:\\n', client_block + activate_block + '\\ntry:\\n', 1)
    # A probe budget generous enough that even a loaded runner's cold worker reaches the
    # injected block: the claim is "bounded by the budget", not "fast on a quiet host".
    launch_probe.DEFAULT_TIMEOUT = {BUDGET!r}
venv_sync.publish_launchers = lambda *a, **k: (_ for _ in ()).throw(AssertionError('published'))
pm.sync_venv = lambda *a, **k: (_ for _ in ()).throw(AssertionError('synced'))
root = Path({str(root)!r})
pending = venv_sync.arm_completion(root)
started = time.monotonic()
venv_sync.prepare_launch(root, ['kanban', 'list'])
prepared = time.monotonic()
assert pending.is_file(), 'timeout cleared completion debt'
# Prove the runtime diagnostic is bounded on its own too (a launch with no verdict,
# e.g. one whose prepare_launch was skipped), not only deferred after an unknown launch.
getattr(venv_sync, '_unknown_roots', set()).clear()
venv_sync.check_runtime(root)
print(json.dumps({{'prepare': prepared - started, 'runtime': time.monotonic() - prepared}}), flush=True)
'''
    env = dict(os.environ, HERMES_HOME=str(tmp_path / "home"))
    env.pop("HERMES_DISABLE_LAZY_INSTALLS", None)
    # Unbounded code never returns: the outer timeout only turns that hang into a failure.
    result = subprocess.run([sys.executable, "-I", "-c", script], env=env,
                            capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr
    timings = json.loads(result.stdout)
    from hermes_cli.launch_probe import _REAP_SECONDS
    allowance = BUDGET + _REAP_SECONDS + 2.0  # deadline + process-group reap + scheduling
    assert timings["prepare"] < allowance, timings
    assert timings["runtime"] < allowance, timings
    assert (tmp_path / "probe-entered").is_file(), "fixture never reached the blocked PM boundary"
    assert (tmp_path / "runtime-entered").is_file(), "runtime diagnostic never reached its blocked activation"
    import psutil
    from tests.waiting import wait_until
    blocked = [int((tmp_path / "runtime-entered").read_text())]
    if stage == "response":
        blocked.append(int((tmp_path / "probe-entered").read_text()))
    for pid in blocked:
        wait_until(lambda pid=pid: not psutil.pid_exists(pid) or psutil.Process(pid).status() == psutil.STATUS_ZOMBIE,
                   timeout=3, what="blocked PM descendant terminated")


def test_runtime_opt_out_does_not_probe(tmp_path, monkeypatch):
    import pm
    root = tmp_path / "checkout"
    (root / ".git").mkdir(parents=True)
    (root / "install-stamp.json").write_text(json.dumps({"updateMechanism": "self"}))
    monkeypatch.setenv("HERMES_DISABLE_LAZY_INSTALLS", "1")
    activated = []
    monkeypatch.setattr(pm, "activate", lambda **kw: activated.append(kw))
    assert venv_sync.check_runtime(root) is None
    assert activated == [{"allow_incomplete": True}]


def test_runtime_check_reuses_this_launchs_current_verdict(tmp_path, monkeypatch):
    """After prepare_launch found the checkout current, dispatch checks only the store's tools."""
    import pm
    from hermes_cli import launch_probe
    root = tmp_path / "checkout"
    (root / ".git").mkdir(parents=True)
    (root / "install-stamp.json").write_text(json.dumps({"updateMechanism": "self"}))
    monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)
    monkeypatch.setattr(launch_probe, "probe", lambda *a, **k: pytest.fail("probed a known-current checkout"))
    activated = []
    monkeypatch.setattr(pm, "activate", lambda **kw: activated.append(kw) or ["git: missing"])
    monkeypatch.setattr(venv_sync, "_current_roots", {root.resolve()})
    problem = venv_sync.check_runtime(root)
    assert activated == [{"allow_incomplete": True}]
    assert problem is not None and "git: missing" in problem


def test_runtime_check_defers_after_this_launchs_probe_timed_out(tmp_path, monkeypatch):
    """One deadline per launch: an unknown currency verdict is not followed by a second wait."""
    import pm
    from hermes_cli import launch_probe
    root = tmp_path / "checkout"
    (root / ".git").mkdir(parents=True)
    (root / "install-stamp.json").write_text(json.dumps({"updateMechanism": "self"}))
    monkeypatch.delenv("HERMES_DISABLE_LAZY_INSTALLS", raising=False)
    monkeypatch.setattr(launch_probe, "probe", lambda *a, **k: pytest.fail("waited out a second probe"))
    activated = []
    monkeypatch.setattr(pm, "activate", lambda **kw: activated.append(kw) or ["git: missing"])
    monkeypatch.setattr(venv_sync, "_unknown_roots", {root.resolve()})
    assert venv_sync.check_runtime(root) is None
    assert activated == [{"allow_incomplete": True}], "bundled tools must stay on PATH"


@pytest.mark.platforms("posix")
def test_exec_launch_hands_the_verdict_to_the_execd_self(tmp_path):
    """Real execv: the managed interpreter starts as the same pid with this launch's verdict."""
    root = (tmp_path / "checkout").resolve()
    source = Path(__file__).resolve().parents[2]
    child = ("import os, json; print(json.dumps([os.getpid(), "
             f"os.environ.get({venv_sync.LAUNCH_HANDOFF_ENV!r})]), flush=True)")
    script = f'''
import os, sys
sys.path.insert(0, {str(source)!r})
from pathlib import Path
from hermes_cli import venv_sync
venv_sync._handoff = ("current", Path({str(root)!r}))
print(os.getpid(), flush=True)
venv_sync.exec_launch(Path(sys.executable), [sys.executable, "-I", "-c", {child!r}])
'''
    env = {k: v for k, v in os.environ.items() if k != venv_sync.LAUNCH_HANDOFF_ENV}
    result = subprocess.run([sys.executable, "-I", "-c", script], env=env,
                            capture_output=True, text=True, timeout=60, check=True)
    parent_pid, received = result.stdout.splitlines()
    child_pid, value = json.loads(received)
    assert child_pid == int(parent_pid), "execv must keep the pid the handoff is bound to"
    assert value == f"current:{parent_pid}:{root}"


def test_failed_exec_withdraws_the_verdict(tmp_path, monkeypatch):
    """A launch whose exec fails keeps running here: no subprocess or later re-exec may
    inherit a verdict this process did not hand off."""
    root = (tmp_path / "checkout").resolve()
    monkeypatch.delenv(venv_sync.LAUNCH_HANDOFF_ENV, raising=False)
    monkeypatch.setattr(venv_sync, "_handoff", ("unknown", root))
    seen = []

    def failing_exec(path, argv):
        seen.append(os.environ.get(venv_sync.LAUNCH_HANDOFF_ENV))
        raise OSError("exec format error")

    monkeypatch.setattr(os, "execv", failing_exec)
    with pytest.raises(OSError):
        venv_sync.exec_launch(Path(sys.executable), [sys.executable])
    assert seen == [f"unknown:{os.getpid()}:{root}"]
    assert venv_sync.LAUNCH_HANDOFF_ENV not in os.environ


def test_unknown_verdict_with_owed_update_says_so(tmp_path, monkeypatch, capsys):
    """A deferred completion is never silent: the operator learns it is still owed."""
    root = (tmp_path / "checkout").resolve()
    (root / ".git").mkdir(parents=True)
    monkeypatch.setattr(venv_sync, "_unknown_roots", set())
    venv_sync._unknown_launch_target(root)
    assert capsys.readouterr().err == "", "no debt, nothing to report"
    pending = venv_sync.arm_completion(root)
    venv_sync._unknown_launch_target(root)
    assert "source update is still unfinished" in capsys.readouterr().err
    assert pending.is_file(), "an unknown verdict must not clear the debt"
    venv_sync._unknown_launch_target(root, announce=False)  # the exec'd child of that launch
    assert capsys.readouterr().err == "", "said once per launch, not again after the handoff"


@pytest.mark.platforms("posix")
@pytest.mark.parametrize(("command", "bounded"), [
    (["kanban", "list"], True),
    (["update", "--check"], True),
    (["update"], False),
])
def test_explicit_update_waits_for_the_full_answer(tmp_path, monkeypatch, command, bounded):
    """`hermes update` is the remedy deferred launches name: it never takes the bounded
    probe's unknown. Report-only update forms and every other command do."""
    import pm
    from hermes_cli import launch_probe
    root = (tmp_path / "checkout").resolve()
    calls = []
    monkeypatch.setattr(launch_probe, "probe", lambda *a, **k: calls.append("probe"))
    monkeypatch.setattr(pm, "venv_is_current", lambda **k: calls.append("full") or True)
    assert venv_sync._launch_currency(root, command) is (None if bounded else True)
    assert calls == (["probe"] if bounded else ["full"])
