"""Real CLI and unchanged deployed monitor exercise refresh-to-audit exclusion.

The monitor fixture is byte-identical to the deployed module at diagnosis:
SHA256 f031ad086270daf6222a54a1c974b5a9e8acb440c207675b75af1b0bc0f5ccbc.
Only its subprocess transport is adapted to the configured SQLite/HTTP fixtures.
"""
from contextlib import contextmanager, redirect_stdout
from importlib.machinery import SourceFileLoader
import importlib.util
import io
import json
from pathlib import Path
import threading
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_sync_client as client
from hermes_cli import kanban_sync_refusal_audit as refusal
from hermes_cli.kanban_sync_guard import board_sync_lock, SyncRoundBusy
from tests.hermes_cli import test_kanban_history_residue_cli as fixtures

configured = fixtures.configured
stores = fixtures.stores


def _monitor():
    path = Path(__file__).parents[1] / 'fixtures' / 'kanban_drift_monitor.py.txt'
    spec = importlib.util.spec_from_loader('unchanged_drift_monitor', SourceFileLoader('unchanged_drift_monitor', str(path)))
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_busy_exhaustion_is_unknown_not_clean_or_monitor_drift(configured, monkeypatch, tmp_path, capsys):
    from hermes_cli import kanban_sync_diagnostic as diagnostic
    paths, _hub, _calls, _state, _connect = configured
    waits = []
    monkeypatch.setattr(diagnostic, '_wait', lambda seconds: waits.append(seconds))
    with board_sync_lock(paths[0]):
        assert fixtures.cli.cmd_sync(fixtures.args('diff', '--json')) == 1
    report = json.loads(capsys.readouterr().out)
    assert report['clean'] is False and report['unavailable'] is True
    assert report['refresh_skipped'] and len(waits) == diagnostic.MAX_ATTEMPTS - 1
    monitor = _monitor()
    tools = []
    consumer = monitor.Monitor(tmp_path / 'busy-monitor.json', lambda *a: tools.append(a))
    for _ in range(2):
        result = consumer.sample('fixture/default', report)
        assert result['streak'] == 0 and result['unavailable_since'] is not None
    assert tools == []


@pytest.mark.parametrize('mode', ['refusal', 'history', 'mixed'])
@pytest.mark.parametrize('fault', [None, 'field', 'unproven', 'race'])
def test_competing_sync_cannot_take_refresh_to_audit_gap(configured, monkeypatch, tmp_path, mode, fault):
    paths, hub, calls, _state, connect = configured
    fixtures.board.observe_board('default', 'op-original', 'Keep history')
    live = connect(paths[0])
    row = dict(live.execute('SELECT * FROM kanban_sync_outbox').fetchone())
    if mode == 'mixed':
        row.update(op_id='server-op', row_uid='server-event')
        live.execute(f"INSERT INTO kanban_sync_outbox ({','.join(row)}) VALUES ({','.join('?' for _ in row)})",
                     tuple(row.values()))
    reason, _, detail = row['last_error'].partition(': ')
    proof = dict(version=1, board='default', gateway='home', execution_authority=False,
                 historical_identity_certified=False, receipt=dict(client_op_id=row['op_id'],
                 status='rejected', reason=reason, detail=detail or None))
    def receipt(*a):
        if fault == 'race':
            connect(paths[0]).execute('UPDATE kanban_sync_outbox SET attempts=attempts+1')
        return None if mode == 'history' or fault == 'unproven' else proof
    monkeypatch.setattr(refusal, '_receipt', receipt)
    if fault == 'field':
        live.execute("UPDATE tasks SET title='real drift'")
    if fault == 'unproven':
        live.execute("UPDATE kanban_sync_outbox SET last_error='unknown: not audited'")
    before = list(live.iterdump())
    calls.clear()  # remaining paths must be GET-only; no historical-intent replay

    # Transport is simulated; normal flock, SQLite, CLI and audit cuts are real.
    @contextmanager
    def pulled(*a, **kw):
        assert kw.get('diagnostic') is True
        yield None, client.Pulled(), False
    monkeypatch.setattr(client, '_pulled_replica', pulled)
    attempts, threads, errors = [], [], []
    release = threading.Event()
    ready = threading.Event()
    def locked_round(*a, **kw):
        attempts.append('acquired')
        ready.set()
        assert release.wait(10), 'test did not release competing sync'
    monkeypatch.setattr(client, '_sync_board_locked', locked_round)
    def compete():
        ready.clear()
        def sync_round():
            try:
                client.sync_board('default', paths[0], hub, 'home')
            except SyncRoundBusy:
                attempts.append('blocked')
                ready.set()
            except BaseException as exc:
                errors.append(exc)
                ready.set()
        thread = threading.Thread(target=sync_round)
        threads.append(thread)
        thread.start()
        assert ready.wait(5), 'competing sync did not reach lock boundary'
    original_diff = client.diff
    original_changes = hub.changes
    def diff(*a, **kw):
        compete()  # after pull and immediately before ordinary diff / refusal audit
        return original_diff(*a, **kw)
    def changes(*a, **kw):
        compete()  # both audits' empty-feed boundaries, including history after refusal
        return original_changes(*a, **kw)
    monkeypatch.setattr(client, 'diff', diff)
    monkeypatch.setattr(hub, 'changes', changes)
    monitor = _monitor()
    def run(argv, **kw):
        assert argv == ['fixture-cli', 'sync', 'diff', '--json']
        output = io.StringIO()
        with redirect_stdout(output):
            rc = fixtures.cli.cmd_sync(fixtures.args('diff', '--json'))
        return SimpleNamespace(returncode=rc, stdout=output.getvalue(), stderr='')
    monkeypatch.setattr(monitor.subprocess, 'run', run)
    tools = []
    consumer = monitor.Monitor(tmp_path / 'monitor.json', lambda *a: tools.append(a))
    try:
        report = monitor.collect(['fixture-cli', 'sync', 'diff', '--json'])
        assert report['clean'] is (fault is None)
        if fault is None:
            assert not report.get('unavailable') and not report.get('refresh_skipped')
            if mode != 'history':
                assert report['audited_server_refusals'] == [row['op_id']]
            if mode != 'refusal':
                assert report['audited_history_rejections'] == ['op-original']
            for _ in range(2):
                assert consumer.sample('fixture/default', report)['streak'] == 0
            assert tools == []
        assert attempts and set(attempts) == {'blocked'}
        assert all(c[0] == 'GET' for c in calls)
        if fault != 'race':
            assert list(live.iterdump()) == before
    finally:
        release.set()
        for thread in threads:
            thread.join(5)
            assert not thread.is_alive()
    assert errors == []
    with board_sync_lock(paths[0]):
        pass  # caller completion releases the normal fence
