"""The notifier's per-board skip gate keeps every recovery path (card H1: review R4, C10).

A board is skipped only when its activity revision, the subscription bookkeeping digest and what this gateway
serves are unchanged since the last full pass, that pass left nothing pending, and nothing is due (in-flight
claim, hourly GC, the 60 s floor). Each test below fails against a gate that drops the guard it names.
"""
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gateway import kanban_tui_owner
from gateway import kanban_watchers_notifier as watcher
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as notify
from hermes_cli import loop_accounting as la


@pytest.fixture
def board(tmp_path, monkeypatch):
    path = tmp_path / 'board.db'
    monkeypatch.setenv('HERMES_KANBAN_DB', str(path))
    conn = kbc.connect(path)
    try:
        yield conn
    finally:
        conn.close()


@pytest.fixture
def opens(monkeypatch):
    """Counts the writable opens: a skipped pass never opens the board."""
    spy = Mock(wraps=kbc.connect)
    monkeypatch.setattr(kbc, 'connect', spy)
    return spy


def _runner(*platforms):
    return SimpleNamespace(_owns_kanban_dispatcher_lock=lambda: True,
                           adapters={p: object() for p in platforms}, _profile_adapters={},
                           config=SimpleNamespace(multiplex_profiles=False))


def _tick(runner, *, gc_due=False):
    """One notifier tick on the pinned board: a fresh collector, as the watcher builds each tick."""
    collector = watcher._Collector(runner, kb, notifier_profile='default', gc_due=gc_due, gc_retention_days=30)
    with kb.pin_first_board_resolution():
        skipped = collector.collect_board('default') is watcher._SKIPPED
    return skipped, collector.deliveries


def _event(conn, task, kind='completed'):
    with kbc.write_txn(conn):
        kb._append_event(conn, task, kind, {})
    return conn.execute('SELECT MAX(id) FROM task_events WHERE task_id = ?', (task,)).fetchone()[0]


@pytest.fixture
def routed(monkeypatch):
    monkeypatch.setattr(watcher, '_adapter_for_subscription', lambda *a: object())


def test_unchanged_board_is_skipped_until_its_key_moves(board, opens, routed):
    task = kb.create_task(board, title='idle', assignee='default')
    notify.add_notify_sub(board, task_id=task, platform='telegram', chat_id='chat')
    runner = _runner('telegram')
    assert _tick(runner) == (False, [])
    assert opens.call_count == 1
    for _ in range(3):
        assert _tick(runner) == (True, [])
    assert opens.call_count == 1  # skipped passes never open the board
    # A lifecycle event its claim never takes moves the revision once, lists nothing, and the gate closes again.
    _event(board, task, 'spawned')
    assert _tick(runner) == (False, [])
    assert _tick(runner) == (True, [])
    _event(board, task, 'completed')
    skipped, deliveries = _tick(runner)
    assert not skipped and [ev.kind for ev in deliveries[0]['events']] == ['completed']


def test_bookkeeping_only_write_moves_the_key(board, opens, routed):
    task = kb.create_task(board, title='pinged', assignee='default')
    notify.add_notify_sub(board, task_id=task, platform='telegram', chat_id='chat')
    runner = _runner('telegram')
    _tick(runner)
    assert _tick(runner)[0]
    rev = board.execute('SELECT revision FROM kanban_activity_revision').fetchone()[0]
    notify.record_notify_ping(board, task_id=task, platform='telegram', chat_id='chat', event_id=10**6)
    assert board.execute('SELECT revision FROM kanban_activity_revision').fetchone()[0] == rev  # bookkeeping
    assert not _tick(runner)[0]


@pytest.mark.parametrize('exc', [RuntimeError('symlink loop'), OSError('gone')])
def test_probe_failure_falls_back_to_the_writable_open(board, opens, routed, monkeypatch, exc):
    """A probe error (Path.resolve raises RuntimeError on a symlink loop before 3.13) must not abort the tick:
    the board runs its full pass, as before the gate."""
    task = kb.create_task(board, title='probe', assignee='default')
    notify.add_notify_sub(board, task_id=task, platform='telegram', chat_id='chat')
    _event(board, task, 'completed')

    def boom(**_kw):
        raise exc
    monkeypatch.setattr(notify, 'notify_gate_probe', boom)
    skipped, deliveries = _tick(_runner('telegram'))
    assert not skipped and [ev.kind for ev in deliveries[0]['events']] == ['completed']


@pytest.mark.parametrize('why', ['holder_died', 'claim_went_stale'])
def test_orphaned_claim_on_idle_board_is_redelivered_within_one_tick(board, opens, routed, monkeypatch, why):
    task = kb.create_task(board, title='held', assignee='default')
    notify.add_notify_sub(board, task_id=task, platform='telegram', chat_id='chat')
    runner = _runner('telegram')
    assert _tick(runner) == (False, [])
    assert _tick(runner)[0]  # idle board, gate closed
    before = notify.list_notify_subs(board)[0]['last_event_id']
    last = _event(board, task)
    with kbc.write_txn(board):  # another live process claimed it and has not acked
        board.execute('UPDATE kanban_notify_subs SET inflight_from=?, last_event_id=?, inflight_pid=12345, '
                      'inflight_start=NULL, inflight_at=?', (before, last, int(time.time()) - 100))
    alive = {'v': True}
    monkeypatch.setattr(notify, '_holder_alive', lambda row: alive['v'])
    monkeypatch.setattr(notify, '_holder_is_me', lambda row: False)
    for _ in range(2):  # live, fresh claim: nothing to deliver, and nothing changes on the board
        assert _tick(runner) == (False, [])
    if why == 'holder_died':
        alive['v'] = False
    else:
        monkeypatch.setattr(notify, 'NOTIFY_CLAIM_STALE_S', 90)  # the claim is now older than the stale bound
    skipped, deliveries = _tick(runner)
    assert not skipped
    assert [ev.kind for ev in deliveries[0]['events']] == ['completed']


def test_foreign_orphan_is_recovered_on_an_idle_board(board, opens, monkeypatch):
    """A claim on a platform this gateway does not list (a serve's tui row) is still rewound by its recovery."""
    task = kb.create_task(board, title='tui held', assignee='default')
    notify.add_notify_sub(board, task_id=task, platform='tui', chat_id='session')
    runner = _runner('telegram')  # no LOCAL adapter: tui rows are never listed here
    before = notify.list_notify_subs(board)[0]['last_event_id']
    last = _event(board, task)
    with kbc.write_txn(board):
        board.execute('UPDATE kanban_notify_subs SET inflight_from=?, last_event_id=?, inflight_pid=12345, '
                      'inflight_start=NULL, inflight_at=?', (before, last, int(time.time())))
    alive = {'v': True}
    monkeypatch.setattr(notify, '_holder_alive', lambda row: alive['v'])
    monkeypatch.setattr(notify, '_holder_is_me', lambda row: False)
    for _ in range(2):
        assert _tick(runner) == (False, [])
    alive['v'] = False
    assert _tick(runner) == (False, [])
    row = notify.list_notify_subs(board)[0]
    assert row['inflight_from'] is None and row['last_event_id'] == before


def test_adapter_reconnect_delivers_without_a_new_event(board, opens, routed):
    task = kb.create_task(board, title='waiting', assignee='default')
    notify.add_notify_sub(board, task_id=task, platform='telegram', chat_id='chat')
    _event(board, task)
    runner = _runner('slack')  # telegram is down: its row is not even listed
    assert _tick(runner) == (False, [])
    assert _tick(runner) == (True, [])
    runner.adapters['telegram'] = object()
    skipped, deliveries = _tick(runner)
    assert not skipped and [ev.kind for ev in deliveries[0]['events']] == ['completed']


def test_adapter_object_swap_reruns_the_board(board, opens, routed):
    task = kb.create_task(board, title='swap', assignee='default')
    notify.add_notify_sub(board, task_id=task, platform='telegram', chat_id='chat')
    runner = _runner('telegram')
    _tick(runner)
    assert _tick(runner)[0]
    runner.adapters['telegram'] = object()
    assert not _tick(runner)[0]


def test_anchor_appears_without_a_new_event(board, opens, monkeypatch):
    task = kb.create_task(board, title='unanchored', assignee='default')
    notify.add_notify_sub(board, task_id=task, platform='telegram', chat_id='chat')
    _event(board, task)
    anchor = {'v': None}
    monkeypatch.setattr(watcher, '_adapter_for_subscription', lambda *a: anchor['v'])
    runner = _runner('telegram')
    for _ in range(3):  # C10: the pending row left unclaimed keeps its board running
        assert _tick(runner) == (False, [])
    anchor['v'] = object()
    skipped, deliveries = _tick(runner)
    assert not skipped and [ev.kind for ev in deliveries[0]['events']] == ['completed']


def test_tui_owner_appears_without_a_new_event(board, opens, monkeypatch):
    task = kb.create_task(board, title='tui chat', assignee='default')
    notify.add_notify_sub(board, task_id=task, platform='tui', chat_id='session')
    _event(board, task)
    owned = {'v': False}

    def claim(collector, conn, slug, sub):
        return {'sub': sub, 'events': ['completed'], 'tui_owner': True} if owned['v'] else None
    monkeypatch.setattr(kanban_tui_owner, 'claim', claim)
    runner = _runner('local')
    for _ in range(3):
        assert _tick(runner) == (False, [])
    owned['v'] = True
    skipped, deliveries = _tick(runner)
    assert not skipped and deliveries[0]['tui_owner']


def test_hourly_gc_still_runs_on_an_idle_board(board, opens, routed, monkeypatch):
    task = kb.create_task(board, title='gc', assignee='default')
    notify.add_notify_sub(board, task_id=task, platform='telegram', chat_id='chat')
    purge = Mock(return_value=0)
    monkeypatch.setattr(notify, 'purge_stale_done_notify_subs', purge)
    runner = _runner('telegram')
    _tick(runner)
    assert _tick(runner)[0]
    assert not _tick(runner, gc_due=True)[0]
    assert purge.call_count == 1


def test_full_pass_floor(board, opens, routed, monkeypatch):
    task = kb.create_task(board, title='floor', assignee='default')
    notify.add_notify_sub(board, task_id=task, platform='telegram', chat_id='chat')
    runner = _runner('telegram')
    _tick(runner)
    assert _tick(runner)[0]
    monkeypatch.setattr(watcher, '_GATE_FLOOR_S', 0.0)
    assert not _tick(runner)[0]


def test_collect_counts_skips_in_loop_accounting(board, routed):
    task = kb.create_task(board, title='acct', assignee='default')
    notify.add_notify_sub(board, task_id=task, platform='telegram', chat_id='chat')
    runner = _runner('telegram')
    name = 'notifier.collect_board/default'
    start = la.counters().get(name, {}).get('skips', 0)
    for _ in range(3):
        watcher._notifier_collect(runner, kb, notifier_profile='default', gc_due=False, gc_retention_days=30)
    assert la.counters()[name]['skips'] - start == 2


def test_pending_kinds_lists_only_rows_with_a_claimable_event(board):
    task = kb.create_task(board, title='kinds', assignee='default')
    for platform in ('telegram', 'tui'):
        notify.add_notify_sub(board, task_id=task, platform=platform, chat_id='chat')
    kinds = {'': ('completed',), 'tui': ('status',)}

    def listed(**kw):
        return sorted(s['platform'] for s in notify.list_notify_subs(board, pending_only=True, **kw))
    _event(board, task, 'spawned')
    assert listed() == ['telegram', 'tui']  # unchanged default: any unseen event
    assert listed(pending_kinds=kinds) == []
    _event(board, task, 'status')
    assert listed(pending_kinds=kinds) == ['tui']
    _event(board, task, 'completed')
    assert listed(pending_kinds=kinds) == ['telegram', 'tui']
    assert listed(pending_kinds={'': ()}) == []
    with kbc.write_txn(board):
        board.execute("UPDATE kanban_notify_subs SET last_event_id = 1000000000 WHERE platform = 'telegram'")
        board.execute("UPDATE kanban_notify_subs SET last_event_id = 1000000000, inflight_from = 1, "
                      "inflight_pid = 1 WHERE platform = 'tui'")
    assert listed(pending_kinds=kinds) == ['tui']  # in flight stays listed for recovery


def test_gate_probe_reads_owned_count_inflight_and_key(board):
    from pathlib import Path
    path = Path(board.execute('PRAGMA database_list').fetchone()[2])
    task = kb.create_task(board, title='probe', assignee='default')
    notify.add_notify_sub(board, task_id=task, platform='telegram', chat_id='chat', notifier_profile='other')
    count, inflight, key = notify.notify_gate_probe(db_path=path, notifier_profiles={'default'})
    assert (count, inflight) == (0, 0) and key is not None
    assert notify.notify_gate_probe(db_path=path, notifier_profiles={'other'})[:2] == (1, 0)
    assert notify.notify_gate_probe(db_path=path.with_name('missing.db')) == (0, 0, None)
