"""A duplicate committed during source verification retains its original receipt."""
import pytest
from hermes_cli import kanban_db_connect as kbc
from tests.hermes_cli import test_kanban_card_answer as helpers

kanban_home = helpers.kanban_home


@pytest.mark.parametrize('mode', ['choice', 'archive', 'comment'])
def test_duplicate_commits_during_source_read_returns_winner(kanban_home, mode):
    with kbc.connect_closing() as conn:
        answer = helpers.setup_answer(conn, choices=mode != 'archive')
        if mode == 'archive':
            answer['choice'] = 'archive'
        elif mode == 'comment':
            answer.update(mode='comment', choice='Need context')
        winner = []

        def source(request):
            with kbc.connect_closing() as other:
                winner.append(helpers.apply(other, answer))
            return helpers.verified(request)

        loser = helpers.apply(conn, answer, source)
        assert loser == winner[0]
        assert len(helpers.comments(conn, answer['task'])) == 1
