"""Shared broker waitpid custody survives detach mode; no zombie and no timeout takeover."""
import subprocess
from types import SimpleNamespace

import pytest

from hermes_cli import serve_producer_broker as shared
from hermes_cli import serve_producer_runtime as runtime


@pytest.mark.parametrize('spawned', [False, True])
def test_retire_reaps_only_its_spawned_owner(tmp_path, monkeypatch, spawned):
    relay = runtime.ProjectionRelay({}, home=tmp_path, runtime=tmp_path / 'runtime', generation='test')
    relay._owner = {'owner': 'fixture'}
    monkeypatch.setattr(shared, 'owner_alive', lambda owner: False)
    calls = []
    if spawned:
        relay._spawned = SimpleNamespace(wait=lambda timeout: calls.append(timeout))
    assert relay.retire(timeout=1)
    assert len(calls) == int(spawned)
    assert all(0 <= value <= 1 for value in calls)


def test_live_spawned_owner_timeout_is_not_success_or_kill(tmp_path, monkeypatch):
    relay = runtime.ProjectionRelay({}, home=tmp_path, runtime=tmp_path / 'runtime', generation='test')
    relay._owner = {'owner': 'fixture'}
    monkeypatch.setattr(shared, 'owner_alive', lambda owner: False)
    def still_alive(timeout):
        raise subprocess.TimeoutExpired('owned-broker', timeout)
    relay._spawned = SimpleNamespace(wait=still_alive)
    assert relay.retire(timeout=0) is False
