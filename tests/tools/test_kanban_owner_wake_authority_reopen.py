"""Owner-wake recovery may reopen only a contained card's done authority."""
from __future__ import annotations

from argparse import Namespace

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_requeue import owner_wake_authority_reopen_grant
from tests.hermes_cli import test_kanban_containment_lifecycle as containment
from tests.tools import test_kanban_owner_wake_requeue as wake

board = wake.board


def _fixture(conn):
    relay, _consumer, authority, target, spec = containment.setup(conn)
    episode = containment.contain(conn, relay, target, spec)["episode"]
    conn.execute("UPDATE tasks SET status='done', completed_at=1, current_run_id=NULL WHERE id=?", (authority,))
    supervisor = wake._supervisor(conn, kb, [wake._line(f"default/{relay}")])
    kb.claim_task(conn, supervisor)
    supervisor_task = kb.get_task(conn, supervisor)
    assert supervisor_task is not None
    run_id = supervisor_task.current_run_id
    assert run_id is not None
    return relay, authority, supervisor, episode, run_id


def test_owner_wake_reopens_exact_done_authority_via_cli_handler(board, monkeypatch):
    conn = kbc.connect()
    relay, authority, supervisor, episode, run_id = _fixture(conn)
    conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", supervisor)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))

    conn = kbc.connect()
    try:
        assert owner_wake_authority_reopen_grant(
            conn, supervisor, authority, expected_run_id=run_id) == supervisor
    finally:
        conn.close()
    from hermes_cli.kanban import _cmd_reopen
    assert _cmd_reopen(Namespace(task_id=authority, reason="owner-wake recovery", board=None)) == 0

    conn = kbc.connect()
    try:
        authority_task = kb.get_task(conn, authority)
        relay_task = kb.get_task(conn, relay)
        assert authority_task is not None and relay_task is not None
        assert authority_task.status == "ready"
        assert relay_task.containment_episode == episode
        assert [row[0] for row in conn.execute(
            "SELECT kind FROM task_events WHERE task_id=? AND kind='contained'", (relay,)
        )] == ["contained"]
    finally:
        conn.close()


def test_owner_wake_reopen_denies_unrelated_authority_and_non_owner(board, monkeypatch):
    conn = kbc.connect()
    relay, authority, supervisor, _episode, run_id = _fixture(conn)
    other = kb.create_task(conn, title="Unrelated done authority", assignee="default")
    conn.execute("UPDATE tasks SET status='done', completed_at=1 WHERE id=?", (other,))
    conn.commit()
    assert owner_wake_authority_reopen_grant(conn, supervisor, other, expected_run_id=run_id) is None
    assert owner_wake_authority_reopen_grant(
        conn, supervisor, authority, board="other", expected_run_id=run_id) is None
    assert owner_wake_authority_reopen_grant(conn, relay, authority, expected_run_id=run_id) is None
    conn.close()
    monkeypatch.setenv("HERMES_KANBAN_TASK", supervisor)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
