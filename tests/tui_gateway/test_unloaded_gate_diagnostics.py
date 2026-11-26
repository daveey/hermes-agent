"""Fallback counters follow the first actual conservative gate return."""
import contextlib
import json
import sqlite3
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_origin, kanban_reader_retention, loop_accounting as la
from tui_gateway import kanban_remote_notices as remote, kanban_replica_observer
from tui_gateway import kanban_unloaded_gates as gates


@pytest.fixture
def gate(tmp_path, monkeypatch, record_property):
    ledger = tmp_path / 'ledger.db'
    conn = sqlite3.connect(ledger)
    conn.execute('CREATE TABLE notices(session, ref, state, holder_pid, holder_start, delivered)')
    conn.commit()
    controls = dict(token=1, me=False, alive=True, liveness=[], cards=[], fingerprint=True)
    monkeypatch.setattr(kanban_origin, '_hub_db', lambda: tmp_path / 'shadow.db')
    monkeypatch.setattr(kanban_origin, 'gateway_names', lambda: ('gw',))
    stamp = SimpleNamespace(ident='source', generation=1, signature=1)
    monkeypatch.setattr(kanban_replica_observer, 'fingerprint', lambda path: stamp if controls['fingerprint'] else None)
    monkeypatch.setattr(remote, '_woken', set())
    monkeypatch.setattr(remote, 'ledger_path', lambda: ledger)
    monkeypatch.setattr(kanban_reader_retention, 'reader', lambda path: contextlib.closing(sqlite3.connect(path)))
    monkeypatch.setattr(gates, '_revision', lambda conn: 1)
    monkeypatch.setattr(remote, '_holder_is_me', lambda pid: controls['me'])
    def alive(pid, start):
        controls['liveness'].append((pid, start))
        return controls['alive']
    monkeypatch.setattr(remote, '_holder_alive', alive)
    monkeypatch.setattr(gates, '_replica_token', lambda *a: controls['token'])
    monkeypatch.setattr(remote, '_sweep_snapshot', lambda *a: (controls['cards'], {}))
    gates.reset_for_tests()
    la.reset()
    yield controls, conn
    record_property('counter_snapshot', json.dumps(la.counters(), sort_keys=True))
    conn.close()
    gates.reset_for_tests()
    la.reset()


def reasons():
    prefix = 'unloaded.diag.remote_notices.fallback.'
    return {k[len(prefix):]: v['runs'] for k, v in la.counters().items() if k.startswith(prefix)}


@pytest.mark.parametrize('branch', ['woken', 'self_holder', 'dead_holder', 'held_with_report', 'no_token',
                                  'other_pending', 'probe_error', 'admission_bypassed', 'unknown'])
def test_actual_fallback_reasons_conserve(gate, monkeypatch, branch):
    controls, conn = gate
    if branch == 'woken':
        remote._woken.add('chat')
    if branch in ('self_holder', 'dead_holder', 'held_with_report'):
        conn.execute("INSERT INTO notices VALUES ('chat','gw/card','done',123,1,0)")
        conn.commit()
        controls.update(me=branch == 'self_holder', alive=branch != 'dead_holder')
    if branch == 'no_token':
        controls['token'] = None
    if branch == 'other_pending':
        controls['fingerprint'] = False
    if branch == 'probe_error':
        def fail(*a):
            raise OSError('probe failed')
        monkeypatch.setattr(gates, '_replica_token', fail)
    if branch == 'unknown':
        monkeypatch.setattr(gates, 'probe', lambda *a, **k: gates.Inputs())
    calls = []
    with gates.scheduling(SimpleNamespace(retry=lambda: None, due_at=lambda deadline: None)):
        assert gates.run('remote_notices', lambda: calls.append('work') or [], live=set(),
                         every_store=branch != 'admission_bypassed') == []
    assert calls == ['work']
    assert reasons() == {branch: 1}
    assert sum(reasons().values()) == la.counters()['unloaded.remote_notices']['fallbacks']
    if branch == 'other_pending':
        assert la.counters()['unloaded.diag.remote_notices.pending_detail.identity_unknown']['runs'] == 1
    if branch == 'self_holder':
        assert controls['liveness'] == []
    if branch in ('dead_holder', 'held_with_report'):
        assert controls['liveness'] == [(123, 1)]


def test_nonpending_change_and_skip_are_not_fallbacks(gate):
    calls = []
    for _ in range(2):
        gates.run('remote_notices', lambda: calls.append('work') or [], live=set())
    assert calls == ['work']
    assert reasons() == {}
    row = la.counters()['unloaded.remote_notices']
    assert (row['runs'], row['fallbacks'], row['changes'], row['skips']) == (2, 0, 1, 1)


@pytest.mark.parametrize('detail', ['snapshot_changed', 'no_ledger', 'unsettled', 'chat_reactivated'])
def test_other_pending_details_follow_real_downstream_decisions(gate, monkeypatch, tmp_path, detail):
    controls, _conn = gate
    controls['cards'] = [('owner', 'card', {}, [])]
    monkeypatch.setattr(remote, '_notice_state', lambda *a: 'done')
    if detail == 'snapshot_changed':
        from tui_gateway import kanban_replica_observer
        stamps = iter([SimpleNamespace(ident=(1,), generation=0, signature=()), None])
        monkeypatch.setattr(kanban_replica_observer, 'fingerprint', lambda *a: next(stamps))
    if detail == 'no_ledger':
        monkeypatch.setattr(remote, 'ledger_path', lambda: tmp_path / 'absent')
    if detail == 'unsettled':
        monkeypatch.setattr(gates, '_cards_settled', lambda *a, **k: False)
    if detail == 'chat_reactivated':
        def settled(*a, parked, **k):
            parked.add(('default', 'chat'))
            return True
        monkeypatch.setattr(gates, '_cards_settled', settled)
        gates.run('remote_notices', list, live=set())
        la.reset()
        monkeypatch.setattr(gates, '_chat_inert', lambda *a: False)
    gates.run('remote_notices', list, live=set())
    assert reasons() == {'other_pending': 1}
    assert la.counters()['unloaded.diag.remote_notices.pending_detail.' + detail]['runs'] == 1
