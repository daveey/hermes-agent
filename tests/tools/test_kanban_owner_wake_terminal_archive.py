"""A listed terminal containment archive cannot authorize an authority reopen."""
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.tools import test_kanban_owner_wake_authority_reopen as recovery

board = recovery.board


def test_owner_wake_does_not_reopen_authority_for_terminal_archived_target(board, monkeypatch):
    slug, relay, consumer, authority, _, _, episode = recovery._fixture(monkeypatch)
    with kbc.connect_closing(db_path=kb.board_own_db_path(slug)) as conn:
        assert kb.archive_task(conn, relay)
        assert kb.get_task(conn, relay).status == "archived"
    result = recovery._call(slug, relay, authority)
    assert result.get("ok") is not True, result
    with kbc.connect_closing(db_path=kb.board_own_db_path(slug)) as conn:
        assert kb.get_task(conn, authority).status == "done"
        assert kb.get_task(conn, relay).status == "archived"
        assert kb.get_task(conn, relay).containment_episode == episode
        assert kb.get_task(conn, consumer).status == "todo"
