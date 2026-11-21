"""Recorded UI decisions are human answers, never transcript or spend grants."""
import pytest

from hermes_cli import kanban_card_answer as ca
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli import test_kanban_card_answer as helpers

kanban_home = helpers.kanban_home


def test_truthful_non_transcript_provenance_and_accounting(kanban_home):
    from hermes_cli import kanban_no_progress as progress
    from hermes_cli import kanban_spend_budget as spend
    with kbc.connect_closing() as conn:
        req = helpers.setup_answer(conn)
        helpers.apply(conn, req)
        note = [e for e in kb.list_events(conn, req['task']) if e.kind == 'commented'][-1]
        assert note.payload['writer_kind'] == 'agent'
        assert 'relayed_human' not in note.payload
        assert note.payload['recorded_decision'] == {**req, 'originGateway': 'origin'}
        # Isolate the comment's accounting from the simultaneous transition.
        with kb.write_txn(conn):
            conn.execute("UPDATE task_events SET created_at=2000000000 WHERE task_id=? AND kind='commented' AND id="
                         "(SELECT MAX(id) FROM task_events WHERE task_id=? AND kind='commented')",
                         (req['task'], req['task']))
        assert progress.progress_marker(conn, req['task'], before=2_000_000_001) == 2_000_000_000
        assert spend._window_start(conn, req['task'], 2_000_000_001) < 2_000_000_000


def test_spend_hold_survives_verified_answer(kanban_home):
    with kbc.connect_closing() as conn:
        req = helpers.setup_answer(conn)
        with kb.write_txn(conn):
            kb._append_event(conn, req['task'], 'spend_budget_triaged', {'reason': 'independent budget hold'})
        before = helpers.comments(conn, req['task'])
        with pytest.raises(ca.AnswerRefused, match='ask_changed_or_held'):
            helpers.apply(conn, req)
        assert helpers.comments(conn, req['task']) == before
        assert kb.get_task(conn, req['task']).human_request


@pytest.mark.parametrize('archive', [False, True])
def test_containment_marker_blocks_release(kanban_home, archive):
    with kbc.connect_closing() as conn:
        req = helpers.setup_answer(conn, choices=not archive)
        if archive:
            req['choice'] = 'archive'
        with kb.write_txn(conn):
            cols = {r[1] for r in conn.execute('PRAGMA table_info(tasks)')}
            if 'containment_episode' not in cols:
                conn.execute('ALTER TABLE tasks ADD COLUMN containment_episode TEXT')
            conn.execute("UPDATE tasks SET containment_episode='independent-episode' WHERE id=?", (req['task'],))
        before = helpers.comments(conn, req['task'])
        with pytest.raises(ca.AnswerRefused, match='ask_changed_or_held'):
            helpers.apply(conn, req)
        assert helpers.comments(conn, req['task']) == before
        assert kb.get_task(conn, req['task']).status == 'blocked'


def test_provenance_rejects_impersonation(kanban_home):
    with kbc.connect_closing() as conn:
        req = helpers.setup_answer(conn)
        p = {**req, 'originGateway': 'origin'}
        for kwargs in ({'writer_kind': 'human'}, {'writer_kind': 'agent', 'relayed_human': {'message_id': 123}}):
            with pytest.raises(ValueError, match='agent relay'):
                kb.add_comment(conn, req['task'], 'user-steps', 'answer', recorded_decision=p, **kwargs)
        with pytest.raises(ca.AnswerRefused, match='invalid_decision_provenance'):
            kb.add_comment(conn, req['task'], 'user-steps', 'answer', writer_kind='agent',
                           recorded_decision={'operationId': 'forged'})
