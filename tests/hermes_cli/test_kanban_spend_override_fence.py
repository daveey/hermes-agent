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
        with pytest.raises(UnreadComments, match='unavailable'):
            _write(conn, tid, 17, seen=10**9)
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
