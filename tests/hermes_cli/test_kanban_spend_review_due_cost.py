"""The per-tick due scan must not re-read spend reviews that already ended. [daveey]"""
import json
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_spend_decision as decision
from hermes_cli import kanban_spend_reviewer as reviewer
from hermes_cli import kanban_spend_review_request as journal
from tests.hermes_cli import test_kanban_spend_caused_review as helpers

kanban_home = helpers.kanban_home
TERMINAL = [reviewer.SETTLED, reviewer.ESCALATED, reviewer.GRANTED]


def _ended(conn, terminal, *, status="done"):
    """A source card whose frozen request went QUEUED -> STARTED -> terminal, then left triage."""
    tid, ep = helpers.stop(conn)
    journal.freeze(conn, "b", "gw", {"task": tid, "episode": ep, "event_uid": "u" + tid, "verdict": "release"})
    with kb.write_txn(conn):
        kb._append_event(conn, tid, reviewer.QUEUED, {"episode": ep, "backlog": True})
        kb._append_event(conn, tid, reviewer.STARTED, {"episode": ep, "attempt": 1, "deadline": 1})
        if terminal:
            kb._append_event(conn, tid, terminal, {"episode": ep})
        conn.execute("UPDATE tasks SET status=? WHERE id=?", (status, tid))
    return tid, ep


def _count_reads(monkeypatch):
    seen = {"stop": 0, "events": 0}
    real_stop, real_events = decision.strict_episode, reviewer._review_events
    def stop(conn, task_id):
        seen["stop"] += 1
        return real_stop(conn, task_id)
    def events(conn, task_id, episode):
        seen["events"] += 1
        return real_events(conn, task_id, episode)
    monkeypatch.setattr(decision, "strict_episode", stop)
    monkeypatch.setattr(reviewer, "_review_events", events)
    return seen


@pytest.mark.parametrize("terminal", TERMINAL)
def test_ended_requests_cost_no_per_request_reads(kanban_home, monkeypatch, terminal):
    with kbc.connect_closing() as conn:
        keys = [_ended(conn, terminal) for _ in range(12)]
        assert reviewer._due(conn, 10**10, set()) == []  # the one pass that notices each review ended
        seen = _count_reads(monkeypatch)
        statements = []
        conn.set_trace_callback(statements.append)
        assert reviewer._due(conn, 10**10, set()) == []
        conn.set_trace_callback(None)
        assert seen == {"stop": 0, "events": 0}
        assert len(statements) <= 4, statements  # a constant number, whatever the journal holds
        assert journal.open_keys(conn) == []
        # History is kept byte for byte: only the marker changed.
        assert len(journal.requests(conn)) == 12
        assert all(journal.get(conn, tid, ep)["wire"]["event_uid"] == "u" + tid for tid, ep in keys)


def test_statement_count_does_not_grow_with_ended_history(kanban_home):
    def statements(conn):
        out = []
        conn.set_trace_callback(out.append)
        reviewer._due(conn, 10**10, set())
        conn.set_trace_callback(None)
        return len(out)
    with kbc.connect_closing() as conn:
        for _ in range(3):
            _ended(conn, reviewer.SETTLED)
        reviewer._due(conn, 10**10, set())
        few = statements(conn)
        for _ in range(30):
            _ended(conn, reviewer.SETTLED)
        reviewer._due(conn, 10**10, set())
        assert statements(conn) == few


@pytest.mark.parametrize("status", ["done", "archived", "ready"])
def test_unconfirmed_request_on_ended_card_is_still_due(kanban_home, status):
    with kbc.connect_closing() as conn:
        for _ in range(5):
            _ended(conn, reviewer.SETTLED)
        tid, ep = _ended(conn, None, status=status)
        for _ in range(3):  # stays due on every tick until its review ends; ended neighbours never hide it
            assert reviewer._due(conn, 10**10, set()) == [(tid, ep, 1)]
        assert journal.open_keys(conn) == [(tid, ep)]


def test_unconfirmed_request_still_reaches_reconcile_bound(kanban_home, monkeypatch):
    monkeypatch.setattr(reviewer, "settings", lambda: (True, 2))
    with kbc.connect_closing() as conn:
        _ended(conn, reviewer.SETTLED)
        tid, ep = _ended(conn, None)
        with kb.write_txn(conn):
            for attempt in range(2, journal.MAX_RECONCILE_ATTEMPTS + 1):
                kb._append_event(conn, tid, reviewer.STARTED, {"episode": ep, "attempt": attempt, "deadline": 1})
        assert reviewer._due(conn, 10**10, set()) == [(tid, ep, journal.MAX_RECONCILE_ATTEMPTS)]
        assert reviewer.start_due(conn, now=10**10) == []
        assert reviewer.state(conn, tid, ep) == reviewer.ESCALATED
        assert reviewer._due(conn, 10**10, set()) == []
        assert journal.open_keys(conn) == []


def test_requeued_triage_stop_is_due_after_its_request_closed(kanban_home):
    with kbc.connect_closing() as conn:
        tid, ep = _ended(conn, reviewer.ESCALATED, status="triage")
        assert reviewer._due(conn, 10**10, set()) == []
        assert journal.open_keys(conn) == []
        with kb.write_txn(conn):
            kb._append_event(conn, tid, reviewer.QUEUED, {"episode": ep})
        assert reviewer._due(conn, 10**10, set()) == [(tid, ep, 1)]


def test_close_failure_keeps_request_open_and_scan_working(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        tid, ep = _ended(conn, reviewer.SETTLED)
        def broken(conn, keys):
            raise sqlite3.OperationalError("database is locked")
        monkeypatch.setattr(journal, "close", broken)
        assert reviewer._due(conn, 10**10, set()) == []
        assert journal.open_keys(conn) == [(tid, ep)]


def test_table_from_before_the_marker_is_read_then_marked(kanban_home):
    with kbc.connect_closing() as conn:
        tid, ep = helpers.stop(conn)
        with kb.write_txn(conn):
            conn.execute(f"CREATE TABLE {journal.TABLE} (task TEXT NOT NULL, episode TEXT NOT NULL, "
                         "request TEXT NOT NULL, PRIMARY KEY(task,episode))")
            conn.execute(f"INSERT INTO {journal.TABLE} VALUES (?,?,?)",
                         (tid, ep, json.dumps({"board": "b", "gateway": "gw",
                                               "wire": {"task": tid, "episode": ep, "event_uid": "u"}})))
            kb._append_event(conn, tid, reviewer.QUEUED, {"episode": ep})
            kb._append_event(conn, tid, reviewer.SETTLED, {"episode": ep})
            conn.execute("UPDATE tasks SET status='done' WHERE id=?", (tid,))
        assert journal.open_keys(conn) == [(tid, ep)]
        assert reviewer._due(conn, 10**10, set()) == []
        assert journal.open_keys(conn) == []
        other, ep2 = _ended(conn, None)  # freeze still works on the migrated table
        assert journal.open_keys(conn) == [(other, ep2)]
