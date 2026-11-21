"""Request-bound observer refuses stale identities and never consumes Board auth."""
import base64
import json
from pathlib import Path


import pytest

from hermes_cli import worker_drain_observer as observer
from hermes_cli import worker_drain_census as census
from hermes_cli import worker_drain_fence as fence


@pytest.fixture
def bound(monkeypatch):
    ident = fence.Identity('i-12345678', 'boot', 'epoch', 1000)
    latch = {'phase': 'sealed', 'generation': 3, 'attempt': 'attempt'}
    monkeypatch.setattr(observer.os, 'geteuid', lambda: 0)
    monkeypatch.setattr(fence, 'load_activation', lambda: {'uid': 1000})
    monkeypatch.setattr(fence, 'current_identity', lambda act: ident)
    monkeypatch.setattr(fence, 'census_root', lambda: (Path('/fixture'), {0, 1000}))
    monkeypatch.setattr(fence, 'read_latch', lambda root, who: dict(latch))
    monkeypatch.setattr(census, 'observe_all', lambda: {k: census.Observation(census.ZERO) for k in census.ADAPTERS})
    return observer._binding(ident, latch), latch


def test_receipt_echoes_exact_request_hash_and_adapter_manifest(bound):
    req, _ = bound
    rec = observer.observe_bound(req)
    assert rec['request_hash'] == fence.request_hash(req)
    assert rec['binding'] == req
    assert rec['complete'] is True and rec['count'] == 0
    assert set(rec['adapters']) == set(census.ADAPTERS)


@pytest.mark.parametrize('field,value', [('boot', 'old'), ('iid', 'other'), ('node_epoch', 'old'),
                                       ('generation', 2), ('phase', 'draining'), ('attempt', 'old')])
def test_stale_request_refused(bound, field, value):
    req, _ = bound
    req[field] = value
    with pytest.raises(fence.DrainRefused):
        observer.observe_bound(req)


def test_unknown_never_complete(bound, monkeypatch):
    req, _ = bound
    monkeypatch.setattr(census, 'observe_all', lambda: {k: census.Observation(census.UNKNOWN) for k in census.ADAPTERS})
    rec = observer.observe_bound(req)
    assert rec['count'] is None and not rec['complete']


def test_transition_during_observation_refuses(bound, monkeypatch):
    req, latch = bound
    def observe():
        latch['generation'] += 1
        return {k: census.Observation(census.ZERO) for k in census.ADAPTERS}
    monkeypatch.setattr(census, 'observe_all', observe)
    with pytest.raises(fence.DrainRefused):
        observer.observe_bound(req)


def test_fixed_main_is_only_scope_that_excludes_self(bound, monkeypatch, capsys):
    req, _ = bound
    monkeypatch.setattr(observer, '__name__', '__main__')
    def observe():
        assert census._fixed_observer_pid == observer.os.getpid()
        return {k: census.Observation(census.ZERO) for k in census.ADAPTERS}
    monkeypatch.setattr(census, 'observe_all', observe)
    blob = base64.b64encode(json.dumps(req).encode()).decode()
    assert observer.main([blob]) == 0
    assert json.loads(capsys.readouterr().out)['complete']
    assert census._fixed_observer_pid is None


def test_root_results_reads_activated_account_not_root_home(tmp_path, monkeypatch):
    import sqlite3
    root = tmp_path / 'worker'
    root.mkdir()
    db = root / 'kanban.db'
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE kanban_sync_outbox (id INTEGER)')
        conn.execute('INSERT INTO kanban_sync_outbox VALUES (1)')
    monkeypatch.setattr(fence, 'census_root', lambda: (root, {0, 1000}))
    assert census.observe_results().state == census.POSITIVE
    db.unlink()
    import hashlib
    records = root / 'worker-resources' / 'boards'
    records.mkdir(parents=True)
    (records / (hashlib.sha256(str(db).encode()).hexdigest() + '.json')).write_text(json.dumps(str(db)))
    assert census.observe_results().state == census.UNKNOWN


def test_ordinary_caller_cannot_exempt_its_own_live_process(tmp_path, monkeypatch):
    from tests.hermes_cli.test_worker_drain_census_manifest import _proc
    _proc(tmp_path, 10)
    monkeypatch.setattr(census.os, 'getpid', lambda: 10)
    monkeypatch.setattr(census, 'baseline_is_live', lambda base: True)
    base = census.Baseline('boot', 'iid', 'epoch', frozenset())
    assert census.observe_processes(str(tmp_path), base).state == census.POSITIVE
    monkeypatch.setattr(census, '_fixed_observer_pid', 10)
    assert census.observe_processes(str(tmp_path), base).state == census.ZERO


def test_imported_main_cannot_exclude_its_daemon(bound, capsys):
    req, _ = bound
    blob = base64.b64encode(json.dumps(req).encode()).decode()
    assert observer.main([blob]) == 75
    assert 'fixed module' in json.loads(capsys.readouterr().out)['reason']
    assert census._fixed_observer_pid is None


def test_actual_module_entry_refuses_without_credentials():
    import subprocess
    import sys
    from tools.environments.local import build_subprocess_env
    env = build_subprocess_env(scrub_secrets=True)
    # Invalid request proves the fixed entry executes its real parser and emits
    # a refusal without consulting activation, Board or a credential provider.
    result = subprocess.run([sys.executable, '-m', 'hermes_cli.worker_drain_observer', 'e30='],
                            capture_output=True, text=True, encoding='utf-8', timeout=10,
                            env=env, check=False)
    assert result.returncode == 75
    assert json.loads(result.stdout)['count'] is None
    assert not json.loads(result.stdout)['complete']
    assert 'Traceback' not in result.stderr


def test_unprivileged_observer_refuses_before_any_scan(bound, monkeypatch):
    req, _ = bound
    monkeypatch.setattr(observer.os, 'geteuid', lambda: 1000)
    with pytest.raises(fence.DrainRefused):
        observer.observe_bound(req)
