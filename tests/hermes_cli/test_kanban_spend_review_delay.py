"""Five-minute fallback review configuration; safety stops never enter it."""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_spend_reviewer as reviewer
from tests.hermes_cli import test_kanban_spend_budget as helpers

kanban_home = helpers.kanban_home


@pytest.mark.parametrize("value, expected", [
    (None, 300), (0, 0), (60, 60), (900, 900), (-1, 300), (True, 300), ("60", 300),
])
def test_review_delay_config(value, expected):
    cfg = {"kanban": {"spend_review_after_seconds": value}}
    assert reviewer.review_after_seconds(cfg) == expected


@pytest.mark.parametrize("delay", [0, 60, 300, 900])
def test_backlog_uses_configured_delay_at_exact_boundary(kanban_home, spend_reviewer, monkeypatch, delay):
    from hermes_cli import config
    from tests.hermes_cli import test_kanban_spend_reviewer as cases
    monkeypatch.setattr(config, "load_config", lambda: {"kanban": {"spend_review_after_seconds": delay}})
    with kbc.connect_closing() as conn:
        tid, episode = cases._routed_with_reviewer_off(conn, monkeypatch)
        noticed = reviewer._notice_at(conn, tid, episode)
        assert noticed is not None
        assert reviewer.queue_backlog(conn, now=noticed + delay - 1, cfg=(True, 2)) == []
        assert reviewer.queue_backlog(conn, now=noticed + delay, cfg=(True, 2)) == [tid]
        assert reviewer.queue_backlog(conn, now=noticed + delay, cfg=(True, 2)) == []


def test_review_delay_default():
    assert reviewer.BACKLOG_AFTER == 300
    assert reviewer.review_after_seconds({}) == 300


@pytest.mark.parametrize("cause", ["safety_refusal", "unknown_stop"])
def test_safety_and_unknown_causes_never_queue_or_grant(kanban_home, spend_reviewer, cause):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="Held stop", assignee="default")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
            kb._append_event(conn, tid, "spend_budget_triaged", {"cause": cause, "reason": "Keep held"})
        episode = reviewer._current_episode(conn, tid)
        assert episode is not None
        assert not reviewer.eligible(conn, tid, episode)
        with kb.write_txn(conn):
            assert not reviewer.queue(conn, tid, episode, backlog=True)
        assert reviewer.state(conn, tid, episode) is None
        assert reviewer.grant(conn, tid, episode, 10, "not authorized", "no evidence") is not None
        assert kb.get_task(conn, tid).status == "triage"
