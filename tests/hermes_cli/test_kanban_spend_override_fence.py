"""Spend-stop decisions compare the last read with the committed budget, not the shared profile."""
from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_spend_budget as budget
from hermes_cli.kanban_release_comments import UnreadComments
from tests.hermes_cli import test_kanban_spend_budget as helpers

kanban_home = helpers.kanban_home


def _stop(conn, tid):
    with kb.write_txn(conn):
        kb._append_event(conn, tid, budget.EVENT, {"reason": "bounded stop"})
        conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
    return helpers._events(conn, tid, budget.EVENT)[-1]


def _card(conn):
    tid = kb.create_task(conn, title="One spend decision", assignee="default")
    return tid, _stop(conn, tid)


def _write(conn, tid, runs=3, **kw):
    return budget.set_override(conn, tid, runs=runs, by="default", reason="bounded rescope", **kw)


@pytest.mark.parametrize('acknowledge', [False, True], ids=['no-read', 'same-read-watermark'])
def test_independent_writers_cannot_stack_decisions(kanban_home, acknowledge):
    with kbc.connect_closing() as conn:
        tid, _ = _card(conn)
        if acknowledge:
            _write(conn, tid, 1)
        seen = kb.list_comments(conn, tid)[-1].id if acknowledge else None
    barrier = threading.Barrier(2)

    def decide(runs):
        with kbc.connect_closing() as conn:
            barrier.wait(timeout=10)
            try:
                _write(conn, tid, runs, **({'seen': seen} if acknowledge else {}))
                return "applied"
            except UnreadComments as exc:
                return exc

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(decide, (3, 17)))
    assert results.count("applied") == 1, results
    refusal = next(r for r in results if isinstance(r, UnreadComments))
    with kbc.connect_closing() as conn:
        assert len(helpers._events(conn, tid, budget.OVERRIDE_EVENT)) == 1 + acknowledge
        comments = kb.list_comments(conn, tid)
        assert len(comments) == 1 + acknowledge
        assert refusal.comments[0]["id"] == comments[-1].id
        assert f"--seen {comments[-1].id}" in str(refusal)
        assert "spend-budget" in refusal.rerun


def test_single_decider_receipt_and_deliberate_reconciliation(kanban_home):
    with kbc.connect_closing() as conn:
        tid, stop = _card(conn)
        receipt = _write(conn, tid)
        assert receipt["episode"] == (stop.uid or str(stop.id))
        comment = kb.list_comments(conn, tid)[-1]
        _write(conn, tid, 2, seen=comment.id)
        assert budget.card_override(conn, tid)["runs"] == 2
        with pytest.raises(UnreadComments):
            _write(conn, tid, 17, seen=comment.id)
        assert len(helpers._events(conn, tid, budget.OVERRIDE_EVENT)) == 2


def test_delayed_decision_after_promotion_is_still_stale(kanban_home):
    with kbc.connect_closing() as conn:
        tid, _ = _card(conn)
        _write(conn, tid)
        assert kb.promote_task(conn, tid, actor="operator", reason="bounded")[0]
        with pytest.raises(UnreadComments):
            _write(conn, tid, 17)
        assert budget.card_override(conn, tid)["runs"] == 3


def test_new_episode_and_ordinary_updates_are_not_old_decisions(kanban_home):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="ordinary", assignee="default")
        _write(conn, tid)
        _write(conn, tid, 4)
        _stop(conn, tid)
        _write(conn, tid, 2)
        assert kb.promote_task(conn, tid, actor="operator", reason="bounded")[0]
        _stop(conn, tid)
        _write(conn, tid, 1)
        assert budget.card_override(conn, tid)["runs"] == 1


def test_cli_refuses_stale_budget_and_accepts_readback(kanban_home):
    from hermes_cli import kanban as cli

    with kbc.connect_closing() as conn:
        tid, _ = _card(conn)
    command = f"spend-budget {tid} --runs 3 --reason bounded"
    assert "spend budget" in cli.run_slash(command)
    refused = cli.run_slash(command)
    assert "unread_comments" in refused
    with kbc.connect_closing() as conn:
        cid = kb.list_comments(conn, tid)[-1].id
    assert "spend budget" in cli.run_slash(f"{command} --seen {cid}")


@pytest.mark.parametrize('missing', ['comment', 'reference'])
def test_missing_evidence_cannot_be_acknowledged(kanban_home, missing):
    with kbc.connect_closing() as conn:
        tid, _ = _card(conn)
        _write(conn, tid)
        with kb.write_txn(conn):
            if missing == 'comment':
                conn.execute('DELETE FROM task_comments WHERE task_id=?', (tid,))
            else:
                conn.execute("UPDATE task_events SET payload=json_remove(payload,'$.decision_comment') "
                             "WHERE task_id=? AND kind=?", (tid, budget.OVERRIDE_EVENT))
        with pytest.raises(UnreadComments, match='unavailable') as refused:
            _write(conn, tid, 17, seen=10**9)
        assert '--seen' not in refused.value.rerun
        assert len(helpers._events(conn, tid, budget.OVERRIDE_EVENT)) == 1


def test_legacy_event_before_comment_and_aged_hold(kanban_home):
    with kbc.connect_closing() as conn:
        tid, _ = _card(conn)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, budget.OVERRIDE_EVENT, {'runs': 3})
            cid = kb.add_comment(conn, tid, budget.AUTHOR, 'Legacy decision', system=True)
            conn.execute('UPDATE task_events SET created_at=created_at-7200 WHERE task_id=?', (tid,))
        with pytest.raises(UnreadComments) as refused:
            _write(conn, tid, 17, board='other-board')
        assert f'--board other-board spend-budget {tid} --seen {cid}' in refused.value.rerun
        _write(conn, tid, 2, seen=cid)


@pytest.mark.parametrize('synced', [False, True], ids=['local', 'hub-synced'])
def test_legacy_reviewer_decision_accepts_comment_tool_watermark(kanban_home, monkeypatch, synced):
    from hermes_cli import kanban as cli
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_sync_client as client
    from tests.hermes_cli import kanban_sync_support as support
    from tools import kanban_tools as kt

    monkeypatch.delenv('HERMES_KANBAN_TASK', raising=False)
    if synced:
        support.write_sync_config(kanban_home, True)
    with kbc.connect_closing() as conn:
        if synced:
            sync.install_or_remove(conn, str(kanban_home / 'kanban.db'))
            client.ensure_identity(conn, 'default', 'fixture')
        tid, stop = _card(conn)
        episode = stop.uid or str(stop.id)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, budget.OVERRIDE_EVENT,
                             {'runs': 2, 'by': 'kanban-spend-reviewer', 'episode': episode})
            decision_id = kb.add_comment(conn, tid, 'kanban-spend-reviewer',
                                         f'Spend reviewer granted 2 more run(s) in a fresh window (stop {episode}): bounded',
                                         system=True)
    out = json.loads(kt._handle_comment({'task_id': tid, 'body': 'Creator approves bounded Phase B'}))
    assert out['ok'], out
    seen = out['comment_id']
    command = f'spend-budget {tid} --runs 6 --reason bounded'
    refused = cli.run_slash(command)
    assert f'--seen {decision_id}' in refused
    assert 'unavailable' not in refused
    assert 'spend budget' in cli.run_slash(f'{command} --seen {seen}')


@pytest.mark.parametrize('producer', ['kanban-spend-reviewer', 'default'])
def test_legacy_decision_does_not_borrow_another_producers_comment(kanban_home, producer):
    with kbc.connect_closing() as conn:
        tid, _ = _card(conn)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, budget.OVERRIDE_EVENT, {'runs': 2, 'by': producer})
            wrong_author = 'kanban-spend-budget' if producer == 'kanban-spend-reviewer' else 'kanban-spend-reviewer'
            cid = kb.add_comment(conn, tid, wrong_author, 'Unrelated decision', system=True)
        with pytest.raises(UnreadComments, match='unavailable'):
            _write(conn, tid, seen=cid)
        assert len(helpers._events(conn, tid, budget.OVERRIDE_EVENT)) == 1


def _legacy_preceding_decision(conn, tid, producer='default'):
    """Replay set_override's comment-before-event transaction without modern references."""
    _write(conn, tid)
    with kb.write_txn(conn):
        event = conn.execute('SELECT id,payload FROM task_events WHERE task_id=? AND kind=?',
                             (tid, budget.OVERRIDE_EVENT)).fetchone()
        payload = json.loads(event['payload'])
        payload.pop('episode')
        payload.pop('decision_comment')
        payload['by'] = producer
        conn.execute('UPDATE task_events SET payload=? WHERE id=?', (json.dumps(payload), event['id']))
        cid = kb.list_comments(conn, tid)[-1].id
        if producer == 'kanban-spend-reviewer':
            conn.execute('UPDATE task_comments SET author=? WHERE id=?', (producer, cid))
            conn.execute("UPDATE task_events SET payload=json_set(payload,'$.author',?) "
                         "WHERE task_id=? AND kind='commented'", (producer, tid))
        # Pin the real legacy ordering even across a wall-clock second boundary.
        conn.execute('UPDATE task_events SET created_at=2000000000 WHERE task_id=?', (tid,))
    return cid, event['id']


@pytest.mark.parametrize('producer', ['default', 'kanban-spend-reviewer'])
def test_legacy_preceding_comment_release_seen(kanban_home, monkeypatch, capsys, producer):
    from tests.hermes_cli import test_kanban_release as releases

    monkeypatch.setattr('hermes_cli.kanban_comment_writer.cli_comment_writer', lambda **kw: releases.HUMAN)
    with kbc.connect_closing() as conn:
        tid, _ = _card(conn)
        cid, eid = _legacy_preceding_decision(conn, tid, producer)
        previous = conn.execute('SELECT * FROM task_events WHERE task_id=? AND id<? ORDER BY id DESC LIMIT 1',
                                (tid, eid)).fetchone()
        assert previous['kind'] == 'commented' and previous['created_at'] == 2000000000
        before = releases.written(conn, tid)
    from hermes_cli import kanban as cli
    refused = cli.run_slash(f'spend-budget {tid} --runs 4 --reason bounded')
    assert 'unread_comments' in refused and f'--seen {cid}' in refused
    with kbc.connect_closing() as conn:
        assert releases.written(conn, tid) == before
    assert releases.argv(tid, '--seen', str(cid), '--json') == 0
    assert json.loads(capsys.readouterr().out.strip().splitlines()[-1])['outcome'] == 'released'
    with kbc.connect_closing() as conn:
        assert releases.status(conn, tid) == 'ready'


@pytest.mark.parametrize('invalid', ['missing-row', 'missing-event', 'wrong-author', 'older-second',
                                      'intervening-event', 'other-task'])
def test_legacy_preceding_comment_stays_fail_closed(kanban_home, monkeypatch, capsys, invalid):
    from tests.hermes_cli import test_kanban_release as releases

    monkeypatch.setattr('hermes_cli.kanban_comment_writer.cli_comment_writer', lambda **kw: releases.HUMAN)
    with kbc.connect_closing() as conn:
        tid, _ = _card(conn)
        other = kb.create_task(conn, title='Other task', assignee='default')
        cid, eid = _legacy_preceding_decision(conn, tid)
        with kb.write_txn(conn):
            sql, params = {
                'missing-row': ('DELETE FROM task_comments WHERE id=?', (cid,)),
                'missing-event': ("DELETE FROM task_events WHERE task_id=? AND kind='commented'", (tid,)),
                'wrong-author': ("UPDATE task_events SET payload=json_set(payload,'$.author','kanban-spend-reviewer') "
                                 "WHERE task_id=? AND kind='commented'", (tid,)),
                'older-second': ("UPDATE task_events SET created_at=created_at-1 WHERE task_id=? AND kind='commented'", (tid,)),
                'intervening-event': ("UPDATE task_events SET kind='unrelated' WHERE id=?", (eid,)),
                'other-task': ("UPDATE task_events SET task_id=? WHERE task_id=? AND kind='commented'", (other, tid)),
            }[invalid]
            conn.execute(sql, params)
            if invalid == 'intervening-event':
                kb._append_event(conn, tid, budget.OVERRIDE_EVENT, {'runs': 3, 'by': 'default'})
                conn.execute('UPDATE task_events SET created_at=2000000000 WHERE task_id=?', (tid,))
        before = conn.total_changes
        with pytest.raises(UnreadComments, match='unavailable'):
            _write(conn, tid, seen=cid)
        assert conn.total_changes == before
        sets = len(helpers._events(conn, tid, budget.OVERRIDE_EVENT))
    assert releases.argv(tid, '--seen', str(cid), '--json') == 1
    refused = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
    assert refused['refusal'] == 'budget_decision_unread' and 'unavailable' in refused['message']
    with kbc.connect_closing() as conn:
        assert len(helpers._events(conn, tid, budget.OVERRIDE_EVENT)) == sets
        assert releases.status(conn, tid) == 'triage'


def test_portable_comment_reference_ignores_foreign_integer(kanban_home):
    from hermes_cli import kanban_db_sync as sync
    from hermes_cli import kanban_sync_client as client
    from tests.hermes_cli import kanban_sync_support as support

    support.write_sync_config(kanban_home, True)
    with kbc.connect_closing() as conn:
        sync.install_or_remove(conn, str(kanban_home / 'kanban.db'))
        client.ensure_identity(conn, 'default', 'fixture')
        tid, _ = _card(conn)
        _write(conn, tid)
        cid = kb.list_comments(conn, tid)[-1].id
        with kb.write_txn(conn):
            # Model a synced receiver: local row ID differs from the producer's.
            conn.execute("UPDATE task_comments SET uid='portable-decision' WHERE id=?", (cid,))
            event = conn.execute('SELECT id,payload FROM task_events WHERE task_id=? AND kind=?',
                                 (tid, budget.OVERRIDE_EVENT)).fetchone()
            data = json.loads(event['payload'])
            data['decision_comment'].update(comment_id=999999, comment_uid='portable-decision')
            conn.execute('UPDATE task_events SET payload=? WHERE id=?', (json.dumps(data), event['id']))
        with pytest.raises(UnreadComments) as refused:
            _write(conn, tid, 17, seen=cid-1)
        assert refused.value.comments[0]['id'] == cid
        _write(conn, tid, 2, seen=cid)
