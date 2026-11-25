"""Recheck reviewer authority at queue and decision time, not only before the model call."""
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_spend_reviewer as reviewer
from tests.hermes_cli import test_kanban_spend_reviewer as helpers

kanban_home = helpers.kanban_home
inline = helpers.inline


def test_human_comment_prevents_backlog_queue(kanban_home, spend_reviewer, monkeypatch):
    monkeypatch.setattr(reviewer, "settings", lambda cfg=None: (False, 2))
    with kbc.connect_closing() as conn:
        tid, episode = helpers._stopped(conn)
        kb.add_comment(conn, tid, "owner", "Leave this held until I decide.", writer_kind="human")
        monkeypatch.setattr(reviewer, "settings", lambda cfg=None: (True, 2))
        assert not reviewer.eligible(conn, tid, episode)
        assert not reviewer.queue(conn, tid, episode, backlog=True)
        assert reviewer.state(conn, tid, episode) is None


def test_cap_rechecked_after_judge(kanban_home, spend_reviewer, inline, monkeypatch):
    with kbc.connect_closing() as conn:
        tid, episode = helpers._stopped(conn)

        def judge(ctx):
            monkeypatch.setattr(reviewer, "settings", lambda cfg=None: (True, 0))
            return helpers._grant()

        monkeypatch.setattr(reviewer, "judge", judge)
        reviewer.start_due(conn)
        assert kb.get_task(conn, tid).status == "triage"
        assert reviewer.state(conn, tid, episode) == reviewer.ESCALATED


def test_foreign_copy_rechecked_after_judge(kanban_home, spend_reviewer, inline, monkeypatch):
    from hermes_cli import kanban_wake_check
    with kbc.connect_closing() as conn:
        tid, episode = helpers._stopped(conn)

        def judge(ctx):
            monkeypatch.setattr(kanban_wake_check, "foreign_copy", lambda conn, task_id: True)
            return helpers._grant()

        monkeypatch.setattr(reviewer, "judge", judge)
        reviewer.start_due(conn)
        assert kb.get_task(conn, tid).status == "triage"
        assert reviewer.state(conn, tid, episode) == reviewer.ESCALATED
