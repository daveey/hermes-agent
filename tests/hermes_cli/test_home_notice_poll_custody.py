"""Expected invariants from review6084937752's real sweep probes. [daveey]"""
from contextlib import contextmanager

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as dbc
from hermes_cli import kanban_hold_notices as notices, kanban_spend_decision as decision
from hermes_cli import kanban_ci_progress as ci, kanban_external_watch as watch
from hermes_cli.kanban_external_adapters import github
from tests.hermes_cli import test_home_release_candidates as sup
from tests.hermes_cli import test_home_review_ci_sweep as cisup
from tests.hermes_cli import test_home_probe_writer_custody as custody

board = sup.board


def _snapshot(conn, tid):
    # Lazy empty tables have no task custody bytes; retain every nonempty row.
    return {table: rows for table, rows in custody.snapshot(conn, tid).items() if rows}


def _held(conn, title):
    tid = kb.create_task(conn, title=title, assignee='default')
    with dbc.write_txn(conn):
        conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
        kb._append_event(conn, tid, decision.EVENT, {'reason': 'ordinary budget stop'})
    return tid


def _kinds(conn, tid):
    return [r[0] for r in conn.execute('SELECT kind FROM task_events WHERE task_id=?', (tid,))]


def _freeze(conn, tid, phase):
    if phase == 'ordinary':
        return
    sup.freeze(conn, tid, 'prepared' if phase == 'aborted' else phase)
    if phase == 'aborted':
        from hermes_cli import kanban_home_transfer as home
        with dbc.write_txn(conn):
            home.abort_source(conn, dict(task=tid, source='worker-a3', destination='hub',
                nonce='release-candidates-0001', expected_version=7, phase='aborted', rows=[]))


@pytest.mark.parametrize('phase', ['prepared', 'pending', 'malformed', 'aborted', 'ordinary'])
def test_hold_notice_dispatch_is_quiet_and_recovers(board, caplog, phase):
    from hermes_cli import kanban_db_dispatch as dispatch
    first, neighbour = _held(board, 'first'), _held(board, 'neighbour')
    _freeze(board, first, phase)
    before = _snapshot(board, first)
    for _ in range(3):
        dispatch.dispatch_once(board, spawn_fn=lambda *a, **k: None)
    blocked = phase in ('prepared', 'pending', 'malformed')
    assert (decision.NOTICE in _kinds(board, first)) == (not blocked)
    assert decision.NOTICE in _kinds(board, neighbour)
    assert not [r for r in caplog.records if 'hold notice failed' in r.getMessage()]
    if blocked:
        assert _snapshot(board, first) == before


@pytest.mark.parametrize('phase', ['prepared', 'pending', 'malformed', 'aborted', 'ordinary'])
@pytest.mark.parametrize('job', ['running', 'succeeded'])
def test_ci_returned_poll_skips_frozen_before_io(board, monkeypatch, caplog, phase, job):
    first = cisup._ci_parked(board, 'first', 'o/r/pull/1')
    neighbour = cisup._ci_parked(board, 'neighbour', 'o/r/pull/2')
    _freeze(board, first, phase)
    before = _snapshot(board, first)
    polled = []

    def poll(ref, *a, **kw):
        polled.append(ref)
        return github.Watched({'job': job, 'service': 'ok', 'detail': 'checks',
            'terminal': job == 'succeeded'}, ci.Progress('ok', repo='o/r'), 'github_pr_checks', 'o/r')
    monkeypatch.setattr(github, 'poll', poll)
    for _ in range(3):
        with ci._LOCK:
            ci._LAST_CHECK.clear()
            ci._JITTER.clear()
        ci.sweep(board)
    blocked = phase in ('prepared', 'pending', 'malformed')
    assert ('o/r/pull/1' in polled) == (not blocked)
    assert 'o/r/pull/2' in polled
    assert watch.WATCH_EVENT in _kinds(board, neighbour)
    assert (watch.WATCH_EVENT in _kinds(board, first)) == (not blocked)
    assert not [r for r in caplog.records if 'could not record the poll' in r.getMessage()]
    assert kb.get_task(board, neighbour).status == ('ready' if job == 'succeeded' else 'scheduled')
    if blocked:
        assert _snapshot(board, first) == before


@pytest.mark.parametrize('phase', ['prepared', 'pending', 'malformed', 'aborted'])
@pytest.mark.parametrize('lane', ['notice', 'poll'])
def test_notice_poll_writer_entry_rechecks_custody(board, monkeypatch, caplog, phase, lane):
    first = _held(board, 'first') if lane == 'notice' else cisup._ci_parked(board, 'first', 'o/r/pull/1')
    neighbour = _held(board, 'neighbour') if lane == 'notice' else cisup._ci_parked(board, 'peer', 'o/r/pull/2')
    original = kb.write_txn
    snapshots = []

    @contextmanager
    def writer(conn, *a, **kw):
        if not snapshots:
            _freeze(conn, first, phase)
            snapshots.append(_snapshot(conn, first))
        with original(conn, *a, **kw):
            yield
    monkeypatch.setattr(kb, 'write_txn', writer)
    if lane == 'notice':
        notices.sweep(board)
    else:
        for tid in (first, neighbour):
            watch.apply_poll(board, tid, {'job': 'running', 'service': 'ok'}, now=10000000000,
                             adapter='github_pr_checks', scope='o/r')
    assert snapshots
    event = decision.NOTICE if lane == 'notice' else watch.WATCH_EVENT
    assert event in _kinds(board, neighbour)
    assert (event in _kinds(board, first)) == (phase == 'aborted')
    if phase != 'aborted':
        assert _snapshot(board, first) == snapshots[0]
    assert not [r for r in caplog.records if 'hold notice failed' in r.getMessage()]


def _release(conn, tid, phase):
    if phase == 'prepared':
        from hermes_cli import kanban_home_transfer as home
        with dbc.write_txn(conn):
            home.abort_source(conn, dict(task=tid, source='worker-a3', destination='hub',
                nonce='release-candidates-0001', expected_version=7, phase='aborted', rows=[]))
    else:
        from hermes_cli import kanban_home_generation as generation
        with dbc.write_txn(conn):
            generation.mirror_feed(conn, tid, {'home_generation': 'release-candidates-0001',
                                             'home_transfer_pending': None}, 9)


@pytest.mark.parametrize('phase', ['prepared', 'pending'])
@pytest.mark.parametrize('job', ['running', 'succeeded'])
def test_ci_callback_freeze_then_release_eventual_progress(board, monkeypatch, caplog, phase, job):
    first = cisup._ci_parked(board, 'first', 'o/r/pull/1')
    neighbour = cisup._ci_parked(board, 'peer', 'o/r/pull/2')
    snapshots = []

    def poll(ref, *a, **kw):
        if ref == 'o/r/pull/1' and not snapshots:
            _freeze(board, first, phase)
            snapshots.append(_snapshot(board, first))
        return github.Watched({'job': job, 'service': 'ok', 'terminal': job == 'succeeded'},
                              ci.Progress('ok', repo='o/r'), 'github_pr_checks', 'o/r')
    monkeypatch.setattr(github, 'poll', poll)
    with ci._LOCK:
        ci._LAST_CHECK.clear()
        ci._JITTER.clear()
    ci.sweep(board)
    assert snapshots and _snapshot(board, first) == snapshots[0]
    assert watch.WATCH_EVENT in _kinds(board, neighbour)
    assert not [r for r in caplog.records if 'could not record the poll' in r.getMessage()]
    _release(board, first, phase)
    with ci._LOCK:
        ci._LAST_CHECK.clear()
        ci._JITTER.clear()
    ci.sweep(board)
    assert watch.WATCH_EVENT in _kinds(board, first)
    assert kb.get_task(board, first).status == ('ready' if job == 'succeeded' else 'scheduled')


@pytest.mark.parametrize('phase', ['prepared', 'pending'])
def test_hold_exact_release_repairs_notice_once(board, phase):
    tid = _held(board, 'first')
    _freeze(board, tid, phase)
    notices.sweep(board)
    assert decision.NOTICE not in _kinds(board, tid)
    _release(board, tid, phase)
    notices.sweep(board)
    notices.sweep(board)
    assert _kinds(board, tid).count(decision.NOTICE) == 1


@pytest.mark.parametrize('phase', ['prepared', 'pending'])
def test_ci_selection_race_does_not_consume_healthy_budget(board, monkeypatch, phase):
    first = cisup._ci_parked(board, 'first', 'o/r/pull/1')
    neighbour = cisup._ci_parked(board, 'peer', 'o/r/pull/2')
    original = ci._due

    def due(*a, **kw):
        rows = original(*a, **kw)
        _freeze(board, first, phase)
        return sorted(rows, key=lambda r: r[1] != first)
    monkeypatch.setattr(ci, '_due', due)
    monkeypatch.setattr(ci, 'MAX_CARDS_PER_SWEEP', 1)
    polled = []

    def poll(ref, *a, **kw):
        polled.append(ref)
        return github.Watched({'job': 'running', 'service': 'ok'}, ci.Progress('ok', repo='o/r'),
                              'github_pr_checks', 'o/r')
    monkeypatch.setattr(github, 'poll', poll)
    with ci._LOCK:
        ci._LAST_CHECK.clear()
        ci._JITTER.clear()
    ci.sweep(board)
    assert polled == ['o/r/pull/2']
    assert watch.WATCH_EVENT in _kinds(board, neighbour)


@pytest.mark.parametrize('phase', ['prepared', 'pending'])
def test_frozen_poll_fast_path_takes_no_writer(board, monkeypatch, phase):
    tid = cisup._ci_parked(board, 'first', 'o/r/pull/1')
    _freeze(board, tid, phase)

    def unexpected_writer(*a, **kw):
        raise AssertionError('frozen poll took a writer')
    monkeypatch.setattr(kb, 'write_txn', unexpected_writer)
    assert watch.apply_poll(board, tid, {'job': 'running', 'service': 'ok'}, now=1,
                            adapter='github_pr_checks', scope='o/r') is None
