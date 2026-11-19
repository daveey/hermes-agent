"""Fresh native power-state reads replace repeated forks, not verdict freshness."""
import subprocess

import pytest

from cron import restart_rerun as rerun
from hermes_cli import kanban_sync_outbox_age as outbox
from hermes_cli import kanban_sync_replica_age as replica


@pytest.mark.parametrize('awake', [False, True])
def test_thirty_rounds_use_fresh_native_state_without_spawning(monkeypatch, awake):
    calls = []
    spawns = []
    monkeypatch.setattr(rerun, '_on_macos', lambda: True)
    def native():
        calls.append(1)
        return awake
    def spawn(*args, **kwargs):
        spawns.append(args)
        raise OSError('unexpected fork')
    monkeypatch.setattr(rerun, '_native_fully_awake', native, raising=False)
    monkeypatch.setattr(rerun.subprocess, 'run', spawn)
    # Exercise the production round entry; no boards means no unrelated DB IO.
    for _ in range(30):
        outbox.after_round([], [])
    assert len(calls) == 30
    assert spawns == []
    assert replica.fully_awake() is awake
    assert rerun.host_fully_awake() is awake


def test_native_verdict_is_not_cached(monkeypatch):
    values = iter([False, True, False])
    monkeypatch.setattr(rerun, '_on_macos', lambda: True)
    monkeypatch.setattr(rerun, '_native_fully_awake', lambda: next(values), raising=False)
    assert [rerun.host_fully_awake() for _ in range(3)] == [False, True, False]


def test_native_error_reads_awake_without_fallback_fork(monkeypatch):
    monkeypatch.setattr(rerun, '_on_macos', lambda: True)
    def broken():
        raise OSError('native read error')
    monkeypatch.setattr(rerun, '_native_fully_awake', broken, raising=False)
    monkeypatch.setattr(rerun.subprocess, 'run', lambda *a, **k: pytest.fail('error must not fork'))
    assert rerun.host_fully_awake()


def test_unavailable_native_retains_subprocess_fallback(monkeypatch):
    monkeypatch.setattr(rerun, '_on_macos', lambda: True)
    monkeypatch.setattr(rerun, '_native_fully_awake', lambda: None, raising=False)
    monkeypatch.setattr(rerun, '_pmset', lambda what: 'UserIsActive 0' if what == 'assertions' else 'AC Power')
    monkeypatch.setattr(rerun, '_lid_closed', lambda: False)
    assert rerun.host_fully_awake()
    monkeypatch.setattr(rerun, '_lid_closed', lambda: True)
    assert not rerun.host_fully_awake()
    def broken(what):
        raise subprocess.SubprocessError('fallback failure')
    monkeypatch.setattr(rerun, '_pmset', broken)
    assert rerun.host_fully_awake()
