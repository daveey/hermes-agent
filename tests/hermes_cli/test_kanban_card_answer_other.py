"""Record-only courier answers never imply a card transition."""
import pytest

from hermes_cli import kanban_card_answer as ca
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect_closing
from tests.hermes_cli.test_kanban_card_answer import apply, comments, setup_answer
from tests.hermes_cli import test_kanban_awaiting as helpers

kanban_home = helpers.kanban_home


@pytest.mark.parametrize('text', ['plan-a', 'archive', 'Different instructions'])
def test_informational_answer_is_atomic_idempotent_and_keeps_human_ask(kanban_home, text):
    with connect_closing(kanban_home / 'kanban.db') as conn:
        answer = dict(setup_answer(conn), mode='comment', choice=text)
        original = kb.get_task(conn, answer['task'])
        receipt = apply(conn, answer)
        assert receipt == apply(conn, answer)
        current = kb.get_task(conn, answer['task'])
        assert current.status == original.status == 'blocked'
        assert current.human_request == original.human_request
        assert len(comments(conn, answer['task'])) == 1
        assert receipt['mode'] == 'comment' and receipt['choice'] == text
        assert not any(e.kind in ('unblocked', 'archived') for e in kb.list_events(conn, answer['task']))
        assert sum(e.kind == 'card_answer_recorded' for e in kb.list_events(conn, answer['task'])) == 1
        with pytest.raises(ca.AnswerRefused, match='operation_conflict'):
            apply(conn, {k: v for k, v in answer.items() if k != 'mode'})


def test_informational_answer_refuses_changed_generation_without_comment(kanban_home):
    with connect_closing(kanban_home / 'kanban.db') as conn:
        answer = dict(setup_answer(conn), mode='comment', choice='free text')
        kb._append_event(conn, answer['task'], 'blocked', {'reason': 'fresh ask'})
        conn.commit()
        with pytest.raises(ca.AnswerRefused, match='stale_ask'):
            apply(conn, answer)
        assert comments(conn, answer['task']) == []
