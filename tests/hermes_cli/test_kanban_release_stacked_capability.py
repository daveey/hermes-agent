"""Explicit historical capability recovery must not broaden a current scope release."""
import json

import pytest

from gateway.session_context import scoped_current_session_id
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_release as release
from hermes_cli import kanban_retain as retain
from hermes_cli.kanban_actionability_sources import describe
from tests.hermes_cli import test_kanban_release as helpers
from tests.hermes_cli import test_kanban_scope_renewal as scope

board = scope.board


def stacked(board, monkeypatch, cause='capability_unavailable', multiple=False, human_note=False):
    tid, _, session = scope.support.bind(board, monkeypatch)
    with kb.write_txn(board):
        kb._append_event(board, tid, 'spend_budget_triaged', {'cause': cause})
        old = dict(board.execute("SELECT * FROM task_events WHERE task_id=? AND kind='spend_budget_triaged'",
                                 (tid,)).fetchone())
        if multiple:
            kb._append_event(board, tid, 'spend_budget_triaged', {'cause': 'capability_unavailable'})
        kb._append_event(board, tid, 'promoted_manual', {'reason': 'legacy promotion without release receipt'})
    if human_note:
        kb.add_comment(board, tid, 'user', 'Preserve the independent acceptance gates.', writer_kind='human')
    with scoped_current_session_id(session):
        result = retain.retain(board, board='default', **scope.ARGS)
    for name in ('HERMES_KANBAN_TASK', 'HERMES_KANBAN_WORKER_TASK', 'HERMES_SESSION_ID'):
        monkeypatch.delenv(name, raising=False)
    return tid, scope.renewal.key(old), result['episode_uid']


def receipts(board, tid):
    return [json.loads(row[0]) for row in board.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind='stop_released' ORDER BY id", (tid,))]


def test_explicit_stacked_release_clears_each_exact_episode(board, monkeypatch):
    tid, old, current = stacked(board, monkeypatch)
    out = helpers.run(board, tid, capability_episodes=[old])
    assert out['outcome'] == 'released', out
    assert [r['episode'] for r in receipts(board, tid)] == [old, current]
    assert out['capability_episodes'] == [old]
    assert 'private_hold' not in describe(board, tid)['violations']
    assert not scope.decision.execution_restricted(board, tid)
    before = helpers.written(board, tid)
    assert helpers.run(board, tid, capability_episodes=[old])['outcome'] == 'already_released'
    assert helpers.written(board, tid) == before


def test_plain_release_never_infers_historical_capability_permission(board, monkeypatch):
    tid, old, current = stacked(board, monkeypatch)
    assert helpers.run(board, tid)['outcome'] == 'released'
    assert [r['episode'] for r in receipts(board, tid)] == [current]
    assert old != current
    assert 'private_hold' in describe(board, tid)['violations']


@pytest.mark.parametrize('status', ['ready', 'running'])
def test_already_released_cannot_silently_acknowledge_new_selection(board, monkeypatch, status):
    tid, old, _ = stacked(board, monkeypatch)
    assert helpers.run(board, tid)['outcome'] == 'released'
    board.execute('UPDATE tasks SET status=? WHERE id=?', (status, tid))
    board.commit()
    before = helpers.written(board, tid)
    out = helpers.run(board, tid, capability_episodes=[old])
    assert out['refusal'] == 'capability_selection_not_released', out
    assert helpers.written(board, tid) == before


@pytest.mark.parametrize('selection', ['same', 'subset', 'superset', 'empty'])
def test_resume_does_not_expand_recorded_selection(board, monkeypatch, selection):
    tid, old, _ = stacked(board, monkeypatch, multiple=True)
    parent = kb.create_task(board, title='parent', assignee='default')
    kb.link_tasks(board, parent, tid)
    extra = str(board.execute("SELECT id FROM task_events WHERE task_id=? AND kind='spend_budget_triaged' "
                              "AND json_extract(payload,'$.cause')='capability_unavailable' AND id!=?",
                              (tid, old)).fetchone()[0])
    # This unsynced legacy fixture mixes retain's v7 terminal uid with uid-less events.
    # Give the new decision a later second, as real published synced history already does by v7 time.
    after = board.execute('SELECT max(created_at)+1 FROM task_events WHERE task_id=?', (tid,)).fetchone()[0]
    monkeypatch.setattr('time.time', lambda: after)
    place = release._place
    monkeypatch.setattr(release, '_place', lambda *a: ('refused', 'injected placement failure'))
    out = helpers.run(board, tid, capability_episodes=[old, extra] if selection == 'subset' else [old])
    assert out['refusal'] == 'dependency_wait_refused', out
    before = receipts(board, tid)
    recorded = release._release_event(board, tid)
    assert recorded is not None and release._untouched_since(board, tid, recorded)
    monkeypatch.setattr(release, '_place', place)
    choices = {'same': [old], 'subset': [old], 'superset': [old, extra], 'empty': []}
    out = helpers.run(board, tid, capability_episodes=choices[selection])
    if selection == 'superset':
        assert out['refusal'] == 'capability_selection_not_released', out
        assert helpers.status(board, tid) == 'triage'
    else:
        assert out.get('resumed') and out['outcome'] == 'waiting_on_parents', out
        if choices[selection]:
            assert out['capability_episodes'] == choices[selection]
    assert receipts(board, tid) == before


@pytest.mark.parametrize('cause', ['scope_stopped', 'decision_pending', None])
def test_noncapability_selection_refused_without_writes(board, monkeypatch, cause):
    tid, old, _ = stacked(board, monkeypatch, cause)
    before = helpers.written(board, tid)
    out = helpers.run(board, tid, capability_episodes=[old])
    assert out['refusal'] == 'invalid_capability_episode', out
    assert helpers.written(board, tid) == before
    assert helpers.status(board, tid) == 'triage'


def test_foreign_or_missing_episode_does_not_write(board, monkeypatch):
    tid, _, _ = stacked(board, monkeypatch)
    other = helpers.spend_stopped(board)
    foreign = scope.renewal.key(dict(board.execute(
        "SELECT * FROM task_events WHERE task_id=? AND kind='spend_budget_triaged'", (other,)).fetchone()))
    for bad in (foreign, 'missing', '', 42):
        before = helpers.written(board, tid)
        out = helpers.run(board, tid, capability_episodes=[bad])
        assert out['refusal'] == 'invalid_capability_episode', out
        assert helpers.written(board, tid) == before


def test_selection_is_exact_and_deduplicated(board, monkeypatch):
    tid, old, current = stacked(board, monkeypatch, multiple=True)
    extra = scope.renewal.key(dict(board.execute(
        "SELECT * FROM task_events WHERE task_id=? AND kind='spend_budget_triaged' AND id NOT IN (?,?)",
        (tid, old, current)).fetchone()))
    # Select only the original capability stop; the other remains held.
    out = helpers.run(board, tid, capability_episodes=[old, old])
    assert out['outcome'] == 'released', out
    assert [r['episode'] for r in receipts(board, tid)].count(old) == 1
    assert extra not in [r['episode'] for r in receipts(board, tid)]
    assert current != extra
    assert 'private_hold' in describe(board, tid)['violations']


@pytest.mark.parametrize('mode', ['unrelated', 'own_run', 'worker'])
def test_selection_does_not_grant_release_authority(board, monkeypatch, mode):
    tid, old, _ = stacked(board, monkeypatch)
    session = board.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='owner_admitted'",
                            (tid,)).fetchone()
    own = json.loads(session[0])['session_id']
    caller = own if mode == 'own_run' else scope.CHAT
    env = {'HERMES_SESSION_ID': caller}
    if mode == 'worker':
        env['HERMES_KANBAN_TASK'] = 't_another_worker'
    before = helpers.written(board, tid)
    out = release.release(board, tid, writer=('agent', caller, 'fixture'), env=env, ancestry=[env],
                          capability_episodes=[old])
    assert out['outcome'] == 'refused', out
    assert helpers.written(board, tid) == before


@pytest.mark.parametrize('hold', ['safety', 'containment'])
def test_independent_holds_dominate_selection(board, monkeypatch, hold):
    tid, old, current = stacked(board, monkeypatch)
    if hold == 'safety':
        kb._append_event(board, tid, 'decision_hold_restricted', {'cause': 'safety_refusal', 'episode': current})
        board.commit()
    else:
        monkeypatch.setattr('hermes_cli.kanban_containment_guard.pending', lambda *a, **kw: True)
    before = helpers.written(board, tid)
    assert helpers.run(board, tid, capability_episodes=[old])['outcome'] == 'refused'
    assert helpers.written(board, tid) == before


def test_selected_row_race_is_revalidated_before_any_receipts(board, monkeypatch):
    tid, old, _ = stacked(board, monkeypatch)
    original = release._decide

    def race(conn, state):
        with kb.write_txn(conn):
            conn.execute("UPDATE task_events SET payload=? WHERE task_id=? AND id=?",
                         (json.dumps({'cause': 'capability_unavailable', 'reason': 'changed'}), tid, old))
        return original(conn, state)

    monkeypatch.setattr(release, '_decide', race)
    out = helpers.run(board, tid, capability_episodes=[old])
    assert out['refusal'] == 'promote_refused', out
    assert not receipts(board, tid)
    assert helpers.status(board, tid) == 'triage'


def test_receipt_failure_rolls_back_every_episode_and_promotion(board, monkeypatch):
    import sqlite3
    tid, old, _ = stacked(board, monkeypatch)
    original = kb._append_event
    count = 0

    def fail(conn, task, kind, *args, **kwargs):
        nonlocal count
        if kind == 'stop_released':
            count += 1
            if count == 2:
                raise sqlite3.OperationalError('injected second receipt failure')
        return original(conn, task, kind, *args, **kwargs)

    monkeypatch.setattr(kb, '_append_event', fail)
    with pytest.raises(sqlite3.OperationalError, match='injected'):
        helpers.run(board, tid, capability_episodes=[old])
    assert count == 2
    assert not receipts(board, tid)
    assert helpers.status(board, tid) == 'triage'


def test_real_cli_carries_selection_and_refuses_bulk(board, monkeypatch, capsys):
    tid, old, current = stacked(board, monkeypatch)
    monkeypatch.setattr('hermes_cli.kanban_comment_writer.cli_comment_writer', lambda **kw: helpers.HUMAN)
    before = helpers.written(board, tid)
    assert helpers.argv(tid, '--ids', 't_other', '--capability-episode', old, '--json') == 2
    assert helpers.written(board, tid) == before
    capsys.readouterr()
    assert helpers.argv(tid, '--capability-episode', old, '--json') == 0
    out = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert out['capability_episodes'] == [old]
    assert [r['episode'] for r in receipts(board, tid)] == [old, current]


def test_owner_reads_historical_human_note_before_explicit_release(board, monkeypatch):
    tid, old, _ = stacked(board, monkeypatch, human_note=True)
    scope.link(board, tid)
    env = {'HERMES_SESSION_ID': scope.CHAT}
    kwargs = dict(writer=scope.AGENT, env=env, ancestry=[env], capability_episodes=[old])
    out = release.release(board, tid, **kwargs)
    assert out['refusal'] == 'budget_decision_unread', out
    assert 'Preserve the independent acceptance gates.' in out['message']
    assert f'--capability-episode {old}' in out['message']
    assert not receipts(board, tid)
    out = release.release(board, tid, seen=max(out['unread']), **kwargs)
    assert out['outcome'] == 'released', out
    assert 'private_hold' not in describe(board, tid)['violations']


@pytest.mark.parametrize('shape', ['null', 'scalar', 'null-item', 'bool-item', 'over-bound'])
def test_malformed_selection_container_refuses_without_writes(board, monkeypatch, shape):
    tid, old, _ = stacked(board, monkeypatch)
    values = {'null': None, 'scalar': old, 'null-item': [None], 'bool-item': [True], 'over-bound': [old] * 33}[shape]
    before = helpers.written(board, tid)
    assert helpers.run(board, tid, capability_episodes=values)['refusal'] == 'invalid_capability_episode'
    assert helpers.written(board, tid) == before


def test_synced_selection_requires_portable_uid_not_local_id(board, monkeypatch):
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_release_capability as capability
    tid, old, _ = stacked(board, monkeypatch)
    sync.apply_sync_schema_steps(board)
    board.commit()

    board.execute("INSERT INTO kanban_sync_state (board_id, gateway_id, hlc_last, mode) "
                  "VALUES ('fixture-board', 'home', '', 'primary')")
    board.commit()
    assert sync.sync_state(board) is not None
    # Published history has portable identities; schema installation alone does not backfill them.
    published_uid = sync.uuid7()
    board.execute('UPDATE task_events SET uid=? WHERE id=?', (published_uid, old))
    board.commit()
    uid = board.execute('SELECT uid FROM task_events WHERE id=?', (old,)).fetchone()[0]
    assert uid and uid != old
    assert capability.selected(board, tid, [uid])[0]['uid'] == uid
    with pytest.raises(release._Refusal, match='not an exact capability'):
        capability.selected(board, tid, [old])
    board.execute('UPDATE task_events SET uid=NULL WHERE id=?', (old,))
    board.commit()
    with pytest.raises(release._Refusal, match='not an exact capability'):
        capability.selected(board, tid, [old])


def test_safety_superseded_capability_is_never_selected(board, monkeypatch):
    from hermes_cli import kanban_release_capability as capability
    tid, old, _ = stacked(board, monkeypatch)
    board.execute('UPDATE task_events SET payload=? WHERE id=?',
                  (json.dumps({'cause': 'capability_unavailable', 'superseded_by_safety_stop': True}), old))
    board.commit()
    with pytest.raises(release._Refusal, match='not an exact capability'):
        capability.selected(board, tid, [old])


def test_current_capability_selection_is_not_written_twice(board):
    tid = helpers.spend_stopped(board)
    row = dict(board.execute("SELECT * FROM task_events WHERE task_id=? AND kind='spend_budget_triaged'",
                             (tid,)).fetchone())
    episode = scope.renewal.key(row)
    board.execute('UPDATE task_events SET payload=? WHERE id=?',
                  (json.dumps({'cause': 'capability_unavailable'}), row['id']))
    board.commit()
    assert helpers.run(board, tid, capability_episodes=[episode])['outcome'] == 'released'
    assert [r['episode'] for r in receipts(board, tid)] == [episode]


def test_synced_home_records_portable_historical_receipt(board):
    from hermes_cli import kanban_db_sync as sync

    sync.apply_sync_schema_steps(board)
    board.commit()
    home = 'fixture-home'

    board.execute('INSERT INTO kanban_sync_state (board_id,gateway_id,hlc_last,mode) VALUES (?,?,?,?)',
                  ('fixture-board', home, '', 'primary'))
    board.commit()
    sync.install_triggers(board)
    board.commit()
    tid = helpers.spend_stopped(board)
    board.execute('UPDATE tasks SET created_on=? WHERE id=?', (home, tid))
    board.commit()
    row = dict(board.execute("SELECT * FROM task_events WHERE task_id=? AND kind='spend_budget_triaged'", (tid,)).fetchone())
    old = row['uid']
    assert old
    board.execute('UPDATE task_events SET payload=? WHERE id=?',
                  (json.dumps({'cause': 'capability_unavailable'}), row['id']))
    board.commit()
    kb._append_event(board, tid, 'spend_budget_triaged', {'reason': 'current ordinary budget stop'})
    board.commit()
    assert helpers.run(board, tid, capability_episodes=[old])['outcome'] == 'released'
    historical = [r for r in receipts(board, tid) if r['episode'] == old]
    assert len(historical) == 1 and historical[0]['mandate_comment_uid']
    from hermes_cli.kanban_actionability_sources import Reader, _private_reason
    reader = Reader(board)
    assert _private_reason(board, reader.task(tid), reader) is None
    assert 'private_hold' not in describe(board, tid)['violations']
    before = helpers.written(board, tid)
    assert helpers.run(board, tid, capability_episodes=[old])['outcome'] == 'already_released'
    assert helpers.written(board, tid) == before
