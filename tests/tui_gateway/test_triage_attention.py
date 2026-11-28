"""Exercise current-episode clocks through real board readers and canonical publication."""
import contextlib
import json
import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from hermes_cli import kanban_session_activity as activity
from hermes_cli import kanban_triage_attention as attention
from tui_gateway import notice_fanout as fanout
from tui_gateway import notice_triage_clock as clocks
from tests.tui_gateway import test_session_outcome_notices as helpers

db = helpers.db


STOPPED = {'disposition': {'reason': 'scope_stopped'}}  # what kanban_retained_status reads as a retained stop


def hold(tid, entered, payload=None):
    with contextlib.closing(kbc.connect()) as conn:
        # This fixture creates through the real writer, then installs an older test
        # episode. Its creation must precede that episode in origin chronology too.
        conn.execute("UPDATE task_events SET created_at=? WHERE task_id=? AND kind='created' AND created_at>=?",
                     (entered - 1, tid, entered))
        conn.execute("UPDATE tasks SET status='triage',block_kind='decision_pending' WHERE id=?", (tid,))
        conn.execute("INSERT INTO task_events(task_id,kind,created_at,payload) VALUES (?,'decision_retained',?,?)",
                     (tid, entered, json.dumps(payload or {})))
        conn.commit()
    activity._reset_cache_for_tests()


@pytest.mark.parametrize('age,red', [(599, False), (600, False), (601, True), (6 * 86400, True)])
def test_board_to_canonical_status_and_detached_count(db, monkeypatch, age, red):
    now = 1_800_000_000
    tid = helpers._card(done=False)
    hold(tid, now - age)
    monkeypatch.setattr(fanout.time, 'time', lambda: now)
    from hermes_state_outcome import refresh_outcome
    outcome = refresh_outcome(helpers.CHAT, 'card_change', db=db)
    assert (outcome['status']['presentation']['stripe'] == 'stuck') is red
    assert outcome['kind'] != 'decision'
    selected = activity.session_tasks([helpers.CHAT])
    card = next(c for c in selected if c['id'] == tid)
    assert card['triage_entered_at'] == now - age
    markers = attention.marker(card) | {(card['key'], 'active', True)}
    row = {}
    attention.project(row, markers, now=now)
    assert ('kanban_triage_attention' in row) is red
    assert 'needs_you' not in row


@pytest.mark.parametrize('imported', [False, True], ids=['local_order', 'rehomed_arrival_order'])
@pytest.mark.parametrize('payload', [None, STOPPED], ids=['held', 'retained_stop'])
def test_natural_threshold_refreshes_existing_canonical_without_card_mutation(db, monkeypatch, payload, imported):
    now = [1_800_000_000.]
    monkeypatch.setattr(fanout.time, 'time', lambda: now[0])
    tid = helpers._card(done=False)
    hold(tid, now[0] - 599, payload)
    if imported:
        # Prior-home rows can arrive after the destination already has the hold.
        # Real reader/adapter/fanout must not mistake arrival IDs for episode order.
        with contextlib.closing(kbc.connect()) as conn:
            conn.execute("INSERT INTO task_events(task_id,kind,created_at,payload) VALUES (?,'spawned',?,'{}')",
                         (tid, now[0] - 620))
            conn.commit()
        activity._reset_cache_for_tests()
    opener = helpers._opener(db)
    helpers.so.refresh_outcome(helpers.CHAT, 'card_change', db=db)
    first = fanout.card_change_pass(session_db=opener)
    assert first.clock_deadline is not None
    with contextlib.closing(kbc.connect()) as conn:
        top = conn.execute('SELECT MAX(id) FROM task_events').fetchone()[0]
    boundary = helpers.so.get_outcome(helpers.CHAT, db=db)
    assert boundary['status']['presentation']['stripe'] != 'stuck'
    now[0] += 1
    assert not fanout.card_change_pass(session_db=opener)
    # A clock pass must not reacquire task history.
    now[0] += 1
    from hermes_cli.kanban_reader_retention import reader
    with monkeypatch.context() as patch:
        patch.setattr(attention, 'annotate', lambda *a, **k: pytest.fail('unexpected history reread'))
        with reader(kb.kanban_db_path()) as conn:
            aged, due = clocks.local(conn, str(kb.kanban_db_path().resolve()), top, now=now[0])
    assert aged == {tid} and due is None
    second = fanout.card_change_pass(session_db=opener)
    assert [r['task_id'] for r in second] == [tid]
    result = helpers.so.get_outcome(helpers.CHAT, db=db)
    assert result['status']['presentation']['stripe'] == 'stuck'
    assert result['status']['kind'] != 'decision'
    assert result['status']['revision'] > boundary['status']['revision']
    item = next(i for i in result['status']['on'] if i['id'] == 'card:' + tid)
    if payload:  # a real stopped scope keeps its retained identity while the local clock turns it red
        assert boundary['status']['reason'] == 'linked_work_retained'
        assert (item['state'], item['triage_overdue']) == ('retained', True)
        assert item['retained_stop']['reason'] == 'scope_stopped'
        assert (result['status']['kind'], result['status']['reason']) == ('unresolved', 'linked_work_retained')
        assert result['status']['presentation']['reason_codes'] == ['linked_work_retained', 'triage_overdue']
    else:
        assert item['state'] == 'triage_overdue'
    with contextlib.closing(kbc.connect()) as conn:
        assert conn.execute('SELECT MAX(id) FROM task_events').fetchone()[0] == top
    assert not fanout.card_change_pass(session_db=opener)


@pytest.mark.parametrize('retained', [{}, {'retained_stop': {'reason': 'scope_stopped'}}])
def test_remote_projection_changes_with_clock_and_clears_after_exit(retained):
    from types import SimpleNamespace
    card = {'id': 't_same', 'key': 'remote/default/work/t_same', 'status': 'triage',
            'session': 'chat', 'triage_entered_at': 1000, **retained}
    source = SimpleNamespace(LISTED_STATUSES=('triage', 'ready'), _remote_cards=lambda *a, **k: [card])
    from unittest.mock import patch
    with patch.object(fanout.time, 'time', return_value=1600):
        old, _ = fanout._remote_projection(source)
        assert clocks.remote_due() > 1600
    with patch.object(fanout.time, 'time', return_value=1601):
        aged, _ = fanout._remote_projection(source)
    assert old['chat'] == frozenset({(card['key'], 'triage')})
    assert aged['chat'] == frozenset({(card['key'], 'triage_overdue')})
    card['status'] = 'ready'
    with patch.object(fanout.time, 'time', return_value=1602):
        cleared, _ = fanout._remote_projection(source)
    assert cleared['chat'] == frozenset({(card['key'], 'ready')})


def test_same_status_reentry_is_semantic_in_both_remote_memo_paths(db):
    tid = helpers._card(done=False)
    hold(tid, 1000)
    with contextlib.closing(kbc.connect()) as conn:
        from hermes_cli.kanban_db_sync import apply_sync_schema_steps
        apply_sync_schema_steps(conn)
        first = fanout._task_prints(conn, [tid])
        conn.execute("INSERT INTO task_events(task_id,kind,created_at,payload) VALUES (?,'unblocked',1600,'{}')", (tid,))
        conn.execute("INSERT INTO task_events(task_id,kind,created_at,payload) VALUES (?,'decision_retained',1601,'{}')", (tid,))
        conn.commit()
        second = fanout._task_prints(conn, [tid])
        assert first != second
        assert second[tid][0][-1] == 1601
    row = {'key': 'owner/default/board/' + tid, 'status': 'triage', 'triage_entered_at': 1000}
    before = fanout._ledger_content_key([row])
    row['triage_entered_at'] = 1601
    assert fanout._ledger_content_key([row]) != before


def test_keep_parked_quiets_the_real_canonical_stripe_and_session_count_for_a_day(db, monkeypatch):
    now = [1_800_000_000.]
    monkeypatch.setattr(fanout.time, 'time', lambda: now[0])
    tid = helpers._card(done=False)
    hold(tid, now[0] - 7200)
    with contextlib.closing(kbc.connect()) as conn:
        from hermes_cli.kanban_db_sync import apply_sync_schema_steps
        apply_sync_schema_steps(conn)
        before = fanout._task_prints(conn, [tid])
        conn.execute("INSERT INTO task_events(task_id,kind,created_at,payload) VALUES (?,?,?,'{}')",
                     (tid, attention.PARK_ACK, int(now[0]) - 60))
        conn.commit()
        assert fanout._task_prints(conn, [tid]) != before  # the remote memo sees the acknowledgement
    activity._reset_cache_for_tests()
    from hermes_state_outcome import refresh_outcome
    outcome = refresh_outcome(helpers.CHAT, 'card_change', db=db)
    assert outcome['status']['presentation']['stripe'] != 'stuck'
    card = next(c for c in activity.session_tasks([helpers.CHAT]) if c['id'] == tid)
    assert card['triage_entered_at'] == now[0] - 7200
    assert card['triage_quiet_until'] == int(now[0]) - 60 + attention.PARK_QUIET_S
    markers = attention.marker(card) | {(card['key'], 'active', True)}
    row = {}
    attention.project(row, markers, now=now[0])
    assert 'kanban_triage_attention' not in row
    now[0] += attention.PARK_QUIET_S
    activity._reset_cache_for_tests()
    assert refresh_outcome(helpers.CHAT, 'card_change', db=db)['status']['presentation']['stripe'] == 'stuck'
    attention.project(row, markers, now=now[0])
    assert row['kanban_triage_attention']['refs'] == [card['key']]
