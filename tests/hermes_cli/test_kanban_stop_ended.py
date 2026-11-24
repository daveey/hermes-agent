"""Owning-chat stop-only control for an ended card on a local-executing shadow board (paintbot M5 shape)."""
from __future__ import annotations

import contextvars
import json
import sqlite3
import threading
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_spend_decision as decision
from hermes_cli import kanban_stop_context as ctx
from hermes_cli import kanban_stop_ended as stop
from hermes_cli import kanban_stop_guards as guards
from hermes_cli import kanban_sync_client as ksc
from tests.hermes_cli import kanban_sync_support as support

ME, BOARD, SID = 'hub', 'paintbot-tools', 'sess_owner_1'
INTERACTIVE = staticmethod(lambda _sid: 'desktop')


@pytest.fixture
def shadow(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_routed_sync_config(home, True)
    conn = kbc.connect(home / 'kanban.db')
    ksc.ensure_identity(conn, BOARD, ME)
    sync.set_mode(conn, 'shadow')
    try:
        yield conn
    finally:
        sync.set_mode(conn, 'off')
        conn.close()


def _link(conn, task, session=SID, gateway=ME, released=0):
    from hermes_cli.kanban_chat_links import link_uid
    with kb.write_txn(conn):
        conn.execute("INSERT INTO task_chat_links (uid,task_id,chat_gateway,chat_profile,chat_session,kind,released,created_at) "
                     "VALUES (?,?,?,?,?,'created',?,unixepoch())",
                     (link_uid(task, gateway, 'default', session, 'created'), task, gateway, 'default', session, released))


def ended_card(conn):
    """M5 shape: a source parent, an M6 child, one ended run, now scheduled and unclaimed."""
    source = kb.create_task(conn, title='source', assignee='default')
    target = kb.create_task(conn, title='M5', assignee='default', parents=[source])
    child = kb.create_task(conn, title='M6', assignee='default', parents=[target])
    with kb.write_txn(conn):  # an ended attempt (this file's subject is not claim custody)
        conn.execute("INSERT INTO task_runs (task_id,profile,status,outcome,started_at,ended_at) "
                     "VALUES (?,'default','crashed','crashed',?,?)", (target, int(time.time()) - 60, int(time.time()) - 30))
        conn.execute("UPDATE tasks SET status='scheduled',current_run_id=NULL,claim_lock=NULL,claim_expires=NULL,"
                     "worker_pid=NULL WHERE id=?", (target,))
    assert conn.execute('SELECT uid FROM task_runs WHERE task_id=?', (target,)).fetchone()[0]
    _link(conn, target)
    return source, target, child


def args(conn, target, **over):
    snap = stop.read(conn, board=BOARD, task_id=target)
    base = dict(board=BOARD, task_id=target, expected_digest=snap['expected_digest'], capability=lambda _b: 1,
                reason='Owner stop: safety-refused source; keep M5 incomplete and nonexecuting',
                evidence=['paintbot-tools run324 retain refusal'], escalate_at=int(time.time()) + 86400)
    base.update(over)
    return base


def state(conn, target, source, child):
    return {
        'task': dict(conn.execute('SELECT * FROM tasks WHERE id=?', (target,)).fetchone()),
        'runs': [dict(r) for r in conn.execute('SELECT * FROM task_runs WHERE task_id=? ORDER BY id', (target,))],
        'links': sorted(tuple(r) for r in conn.execute('SELECT parent_id,child_id FROM task_links')),
        'events': conn.execute('SELECT COUNT(*) FROM task_events').fetchone()[0],
        'others': [dict(conn.execute('SELECT * FROM tasks WHERE id=?', (t,)).fetchone()) for t in (source, child)],
    }


def test_snapshot_is_read_only_and_digest_tracks_state(shadow):
    source, target, child = ended_card(shadow)
    before = state(shadow, target, source, child)
    outbox = shadow.execute('SELECT COUNT(*) FROM kanban_sync_outbox').fetchone()[0]
    one = stop.read(shadow, board=BOARD, task_id=target)
    assert one['mode'] == 'snapshot' and one['writes'] == 0
    assert stop.read(shadow, board=BOARD, task_id=target)['expected_digest'] == one['expected_digest']
    assert state(shadow, target, source, child) == before
    assert shadow.execute('SELECT COUNT(*) FROM kanban_sync_outbox').fetchone()[0] == outbox
    kb.add_comment(shadow, target, 'x', 'unrelated comment is still state (event id moves)') if hasattr(kb, 'add_comment') else None
    kb._append_event(shadow, target, 'note', {})
    assert stop.read(shadow, board=BOARD, task_id=target)['expected_digest'] != one['expected_digest']


def test_owner_stops_private_shadow_card_publishes_through_outbox_and_keeps_dependencies(shadow):
    source, target, child = ended_card(shadow)
    before = state(shadow, target, source, child)
    a = args(shadow, target)
    with ctx.bound(SID, gateway=ME, profile='default', source='desktop', source_reader=INTERACTIVE) as token:
        result = stop.apply(shadow, owner=token, **a)
        replay = stop.apply(shadow, owner=token, **a)
    assert replay == {**result, 'replayed': True}
    assert result['protection_scope'] == 'LOCAL-SHADOW-ONLY'
    assert (result['canonical_protected'], result['dependency_satisfied'], result['incomplete'],
            result['terminal_for_run'], result['execution_authority']) == (False, False, True, False, False)
    assert result['request_uid'].startswith('as-') and result['ended_run_uid'] == before['runs'][-1]['uid']
    after = state(shadow, target, source, child)
    assert after['task']['status'] == 'triage' and after['task']['current_run_id'] is None
    assert after['runs'] == before['runs'] and after['links'] == before['links'] and after['others'] == before['others']
    assert decision.execution_restricted(shadow, target)
    assert decision.restricted_floor(shadow, target) is None  # accounting floor unchanged
    assert not decision.execution_restricted(shadow, child) and not decision.execution_restricted(shadow, source)
    # Ordinary ordered outbox carries the genuine event; no fabricated episode/spend/decision rows.
    ops = [json.loads(r[0]) for r in shadow.execute(
        "SELECT fields FROM kanban_sync_outbox WHERE table_name='task_events' ORDER BY rowid")]
    published = [o for o in ops if o.get('kind') == stop.KIND]
    assert len(published) == 1 and json.loads(published[0]['payload'])['protection_scope'] == 'LOCAL-SHADOW-ONLY'
    kinds = {r[0] for r in shadow.execute('SELECT kind FROM task_events WHERE task_id=? AND id>?',
                                          (target, before['task'] and 0))}
    assert not kinds & {'decision_hold_restricted', 'spend_budget_triaged', 'failure_finalized', 'decision_protected_stopped'}
    readback = stop.read(shadow, board=BOARD, task_id=target)
    assert readback['mode'] == 'readback' and readback['receipt']['request_uid'] == result['request_uid']
    assert guards.installed(shadow)


def test_stop_removes_probe_and_clock_backstop_immediately(shadow):
    from hermes_cli import kanban_wake_check as wake
    source, target, child = ended_card(shadow)
    with kb.write_txn(shadow):
        kb._append_event(shadow, target, 'scheduled', {'wake_at': int(time.time()) + 300, 'resume_status': 'ready'})
        wake.set_check(shadow, target, 'false', 120)
    before = state(shadow, target, source, child)
    with ctx.bound(SID, gateway=ME, profile='default', source='desktop', source_reader=INTERACTIVE) as token:
        stop.apply(shadow, owner=token, **args(shadow, target))
    assert wake.get_state(shadow, target) is None
    from hermes_cli.kanban_continuation import _wake_parks
    assert target not in _wake_parks(shadow, int(time.time()) + 600)[0]
    assert state(shadow, target, source, child)['links'] == before['links']
    readback = stop.read(shadow, board=BOARD, task_id=target)
    assert readback['clock_wake_eligible'] is False and readback['wake_check_registered'] is False


def test_stopped_card_cannot_be_claimed_reopened_relinked_or_replaced(shadow):
    source, target, child = ended_card(shadow)
    with ctx.bound(SID, gateway=ME, profile='default', source='desktop', source_reader=INTERACTIVE) as token:
        stop.apply(shadow, owner=token, **args(shadow, target))
    path = shadow.execute('PRAGMA database_list').fetchone()[2]
    raw = sqlite3.connect(path, isolation_level=None)  # an older/other local writer: only the stored triggers apply
    raw.execute('PRAGMA recursive_triggers=OFF')
    assert not kb.claim_task(shadow, target, claimer='default')
    writes = [
        "UPDATE tasks SET status='ready' WHERE id=:t",
        "UPDATE tasks SET id='renamed' WHERE id=:t",
        "INSERT OR REPLACE INTO task_runs (id,task_id,profile,status,started_at) "
        "SELECT id,:s,'default','running',0 FROM task_runs WHERE task_id=:t",
        "UPDATE tasks SET status='done' WHERE id=:t",
        "UPDATE tasks SET status='archived' WHERE id=:t",
        "UPDATE tasks SET claim_lock='x' WHERE id=:t",
        "UPDATE tasks SET body='scope change' WHERE id=:t",
        "DELETE FROM tasks WHERE id=:t",
        "INSERT OR REPLACE INTO tasks (id,title,status,created_at) VALUES (:t,'r','ready',0)",
        "INSERT INTO task_runs (task_id,profile,status,started_at) VALUES (:t,'default','running',0)",
        "UPDATE task_runs SET outcome='completed' WHERE task_id=:t",
        "DELETE FROM task_runs WHERE task_id=:t",
        "DELETE FROM task_links WHERE child_id=:t",
        "DELETE FROM task_links WHERE parent_id=:t",
        "INSERT INTO task_links (parent_id,child_id) VALUES (:t,:s)",
        f"UPDATE task_events SET payload='{{}}' WHERE task_id=:t AND kind='{stop.KIND}'",
        f"DELETE FROM task_events WHERE task_id=:t AND kind='{stop.KIND}'",
        f"INSERT OR REPLACE INTO task_events (id,task_id,kind,payload,created_at) SELECT id,task_id,'note','{{}}',0 "
        f"FROM task_events WHERE task_id=:t AND kind='{stop.KIND}'",
    ]
    for sql in writes:
        before = state(shadow, target, source, child)
        with pytest.raises(sqlite3.IntegrityError, match='administrative_stop_immutable'):
            raw.execute(sql, {'t': target, 's': source})
        assert state(shadow, target, source, child) == before, sql
    raw.close()
    # Unrelated cards keep progressing.
    assert kb.claim_task(shadow, source, claimer='default')


@pytest.mark.parametrize('fault', ['no_token', 'closed', 'other_chat', 'released', 'two_owners', 'machine_source',
                                   'kanban_env', 'foreign_card', 'leased', 'digest', 'ingress', 'primary', 'off',
                                   'deadline', 'conflicting_hold'])
def test_unauthorized_or_unsafe_calls_write_nothing(shadow, monkeypatch, fault):
    source, target, child = ended_card(shadow)
    a = args(shadow, target)
    session, gateway, reader = SID, ME, INTERACTIVE
    if fault == 'other_chat':
        session = 'sess_intruder'
    if fault == 'machine_source':
        reader = staticmethod(lambda _sid: 'kanban')
    if fault == 'released':
        with kb.write_txn(shadow):
            shadow.execute("UPDATE task_chat_links SET released=1 WHERE task_id=?", (target,))
    if fault == 'two_owners':
        _link(shadow, target, session='sess_other')
    if fault == 'kanban_env':
        monkeypatch.setenv('HERMES_KANBAN_TASK', target)
    if fault == 'foreign_card':
        with kb.write_txn(shadow):
            shadow.execute("UPDATE tasks SET created_on='worker-a3' WHERE id=?", (target,))
        a = {**a, 'expected_digest': stop.digest(stop.snapshot(shadow, target))}
    if fault == 'leased':
        with kb.write_txn(shadow):
            shadow.execute("UPDATE tasks SET claim_lock='other:1' WHERE id=?", (target,))
        a = {**a, 'expected_digest': stop.digest(stop.snapshot(shadow, target))}
    if fault == 'digest':
        kb._append_event(shadow, target, 'note', {})
    if fault == 'ingress':
        a['capability'] = lambda _b: 0
    if fault == 'primary':
        sync.set_mode(shadow, 'primary')
    if fault == 'deadline':
        a['escalate_at'] = int(time.time()) + 15 * 86400
    if fault == 'conflicting_hold':
        kb._append_event(shadow, target, 'decision_protected_stopped', {})
        a = {**a, 'expected_digest': stop.digest(stop.snapshot(shadow, target))}
    before = state(shadow, target, source, child)
    outbox = shadow.execute('SELECT COUNT(*) FROM kanban_sync_outbox').fetchone()[0]
    if fault == 'off':
        sync.set_mode(shadow, 'off')
    if fault == 'no_token':  # outside any interactive turn: nothing to hand to apply
        with pytest.raises(stop.StopRefused, match='owner_required'):
            stop.apply(shadow, owner=ctx.current(), **a)
    else:
        with ctx.bound(session, gateway=gateway, profile='default', source='desktop', source_reader=reader) as token:
            if fault == 'closed':
                token.close()
            with pytest.raises(stop.StopRefused):
                stop.apply(shadow, owner=token, **a)
    if fault == 'off':
        sync.set_mode(shadow, 'shadow')
    assert state(shadow, target, source, child) == before
    assert shadow.execute('SELECT COUNT(*) FROM kanban_sync_outbox').fetchone()[0] == outbox
    assert not decision.execution_restricted(shadow, target) or fault == 'conflicting_hold'


def test_replay_with_changed_payload_conflicts_and_deadline_is_never_renewed(shadow):
    _source, target, _child = ended_card(shadow)
    a = args(shadow, target)
    with ctx.bound(SID, gateway=ME, profile='default', source='desktop', source_reader=INTERACTIVE) as token:
        first = stop.apply(shadow, owner=token, **a)
        with pytest.raises(stop.StopRefused, match='stop_ended_conflict'):
            stop.apply(shadow, owner=token, **{**a, 'escalate_at': a['escalate_at'] + 60})
    count = shadow.execute('SELECT COUNT(*) FROM task_events WHERE kind=?', (stop.KIND,)).fetchone()[0]
    assert count == 1 and stop.read(shadow, board=BOARD, task_id=target)['receipt'] == {
        k: v for k, v in first.items() if k not in ('event_uid', 'replayed')}


def test_copied_context_shares_invalidation_and_turn_end_closes(shadow):
    _source, target, _child = ended_card(shadow)
    a = args(shadow, target)
    holder = {}
    with ctx.bound(SID, gateway=ME, profile='default', source='desktop', source_reader=INTERACTIVE) as token:
        copied = contextvars.copy_context()
    assert not token.open
    def run():
        try:
            copied.run(lambda: stop.apply(shadow, owner=ctx.current(), **a))
        except stop.StopRefused as exc:
            holder['err'] = exc.code
    t = threading.Thread(target=run)
    t.start()
    t.join()
    assert holder['err'] == 'stop_ended_owner_required'
    assert kb.get_task(shadow, target).status == 'scheduled'


def test_cancellation_waits_for_inflight_commit_never_inverse(shadow):
    """Turn cancellation arriving mid-commit blocks on the token lock until the commit finishes; after it, refused."""
    _source, target, _child = ended_card(shadow)
    a = args(shadow, target)
    with ctx.bound(SID, gateway=ME, profile='default', source='desktop', source_reader=INTERACTIVE) as token:
        closed = threading.Event()
        closer = threading.Thread(target=lambda: (token.close(), closed.set()))
        seen = {}
        def capability_then_cancel(_board):  # runs inside apply, holding the token lock
            closer.start()
            seen['blocked'] = not closed.wait(0.3)
            return 1
        result = stop.apply(shadow, owner=token, **{**a, 'capability': capability_then_cancel})
        closer.join(5)
        assert seen['blocked'] is True and closed.is_set() and result['ok'] is True
        assert kb.get_task(shadow, target).status == 'triage'
        with pytest.raises(stop.StopRefused, match='owner_required'):
            stop.apply(shadow, owner=token, **a)


def test_existing_primary_off_retain_gate_is_unchanged(shadow, monkeypatch):
    from hermes_cli import kanban_retain
    monkeypatch.setattr(kanban_retain.actors, 'bound_actor', lambda _conn: object())
    with pytest.raises(ValueError, match='retain requires owning primary or genuinely local board'):
        kanban_retain.retain(shadow, board=BOARD, summary='stop', reason='scope_stopped',
                             next_step='owner review', release_condition='none', evidence=['ended'])


def test_token_cannot_cross_live_contexts_or_delegate(shadow):
    from agent.delegation_context import delegated_child_context
    _source, target, _child = ended_card(shadow)
    a = args(shadow, target)
    with ctx.bound(SID, gateway=ME, profile='default', source='desktop', source_reader=INTERACTIVE) as first:
        with ctx.bound(SID, gateway=ME, profile='default', source='desktop', source_reader=INTERACTIVE):
            with pytest.raises(stop.StopRefused, match='owner_required'):
                stop.apply(shadow, owner=first, **a)
        with delegated_child_context('child'):
            with pytest.raises(stop.StopRefused, match='owner_required'):
                stop.apply(shadow, owner=first, **a)


def _tool():
    from tools import kanban_tools
    return next(item for item in kanban_tools._TOOLS if item[0] == 'kanban_stop_ended')


def _local_board(monkeypatch, conn):
    from contextlib import contextmanager
    from tools import kanban_tools

    @contextmanager
    def local(_):
        yield 'default', conn
    monkeypatch.setattr(kanban_tools, '_board', local)


def test_registered_tool_is_chat_only_and_applies_only_through_the_turn_binding(shadow, monkeypatch):
    import toolsets
    from tools import kanban_stop_ended_tools as tool_mod
    from tools import kanban_tools
    from agent.transports import hermes_tools_mcp_server as native
    assert 'kanban_stop_ended' in toolsets.TOOLSETS['kanban']['tools']
    assert 'kanban_stop_ended' in kanban_tools._ORCHESTRATOR_TOOLS  # hidden from dispatcher task workers
    assert 'kanban_stop_ended' not in native.EXPOSED_TOOLS  # no stateless worker transport
    source, target, child = ended_card(shadow)
    _local_board(monkeypatch, shadow)
    monkeypatch.setattr(tool_mod, '_hub_ingress', lambda _b: 1)
    monkeypatch.setattr(ctx, '_local_gateway', lambda: ME)
    monkeypatch.setattr(ctx, '_session_source', lambda _sid: 'desktop')
    handler = _tool()[2]
    snap = json.loads(handler({'task_id': target}))
    assert snap['mode'] == 'snapshot' and snap['writes'] == 0
    a = {'task_id': target, 'expected_digest': snap['expected_digest'], 'reason': 'owner stop',
         'evidence': ['run324 refusal'], 'escalate_at': int(time.time()) + 3600}
    before = state(shadow, target, source, child)
    # No turn bound (worker/cron/tool arguments): refused, zero writes.
    refused = json.loads(handler(a))
    assert refused['ok'] is False and refused['code'] == 'stop_ended_owner_required'
    # A non-interactive turn source binds nothing.
    with ctx.bind_turn(SID, source='kanban', profile='default'):
        assert json.loads(handler(a))['code'] == 'stop_ended_owner_required'
    assert state(shadow, target, source, child) == before
    with ctx.bind_turn(SID, source='desktop', profile='default'):
        done = json.loads(handler(a))
    assert done['ok'] is True and done['protection_scope'] == 'LOCAL-SHADOW-ONLY', done
    back = json.loads(handler({'task_id': target}))
    assert back['mode'] == 'readback' and back['receipt']['request_uid'] == done['request_uid']
    assert kb.get_task(shadow, target).status == 'triage'
