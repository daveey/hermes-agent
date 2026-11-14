"""Edge cases of the shared-blocker check lane: cohort tokens, home choice, future receipts."""
from __future__ import annotations

from datetime import UTC, datetime

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_shared_blocker_checks as checks
from hermes_cli import kanban_shared_blockers as blockers
from tests.hermes_cli import test_kanban_shared_blockers as helpers

kanban_home = helpers.kanban_home
_blocked_behind = helpers._blocked_behind
WALL = "test-wall"
GH = "github-rest-quota"
CFG = {"kanban": {"shared_blocker_checks": {WALL: "unused"}}}
T0 = 1800000000


@pytest.fixture
def clock(monkeypatch):
    now = [T0]
    monkeypatch.setattr(blockers.time, "time", lambda: now[0])
    monkeypatch.setattr(checks.time, "time", lambda: now[0])
    return now


def _events(conn, tid, kind=checks.CHECK_EVENT):
    return [e for e in kb.list_events(conn, tid) if e.kind == kind]


def _iso(epoch):
    return datetime.fromtimestamp(epoch, tz=UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_older_text_stamped_assertion_does_not_join_newest_cohort(kanban_home, monkeypatch, clock):
    """SQLite sorts a TEXT ended_at after every INTEGER one, so a legacy ISO-stamped
    assertion arrives last in the scan even when it is older. It must not join the
    newest cohort: that would change the token and revoke a verified success."""
    with kbc.connect_closing() as conn:
        _blocked_behind(conn, "newest assertion", blocker_id=WALL)
        clock[0] += 30
        monkeypatch.setattr(checks, "_run_check", lambda c: (0, ""))
        assert checks.run_due_checks(conn, cfg=CFG) == [WALL]
        token = checks._assertion_tokens(conn)[WALL]
        assert not blockers.blocker_states(conn)[WALL].is_open

        older = _blocked_behind(conn, "older legacy assertion", blocker_id=WALL)
        with kb.write_txn(conn):
            conn.execute("UPDATE task_runs SET started_at=?, ended_at=? WHERE task_id=?",
                         (_iso(T0 - 200), _iso(T0 - 100), older))
        assert conn.execute("SELECT typeof(ended_at) FROM task_runs WHERE task_id=?",
                            (older,)).fetchone()[0] == "text"
        assert checks._assertion_tokens(conn)[WALL] == token
        assert not blockers.blocker_states(conn)[WALL].is_open


def _own_gateway(conn, monkeypatch):
    """Make this board a synced home (``own_gateway``) whose cards carry ``created_on`` (foreign when not ours)."""
    from hermes_cli import kanban_wake_check as wake

    if "created_on" not in {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}:
        conn.execute("ALTER TABLE tasks ADD COLUMN created_on TEXT")
    monkeypatch.setattr(wake, "own_gateway", lambda _conn: "home-gw")


def _created_on(conn, tid, gateway):
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET created_on = ? WHERE id = ?", (gateway, tid))


def test_check_evidence_lands_on_home_card_when_a_foreign_card_sorts_first(kanban_home, monkeypatch, clock):
    with kbc.connect_closing() as conn:
        first = _blocked_behind(conn, "a", blocker_id=WALL)
        second = _blocked_behind(conn, "b", blocker_id=WALL)
        foreign, home = sorted([first, second])
        _own_gateway(conn, monkeypatch)
        _created_on(conn, foreign, "worker-gw")
        monkeypatch.setattr(checks, "_run_check", lambda c: (0, "recovered"))
        clock[0] += 30
        assert checks.run_due_checks(conn, cfg=CFG) == [WALL]
        assert not _events(conn, foreign)
        assert [e.payload["rc"] for e in _events(conn, home)] == [0]
        assert not blockers.blocker_states(conn)[WALL].is_open


def test_home_turning_foreign_during_check_writes_no_evidence(kanban_home, monkeypatch, clock):
    with kbc.connect_closing() as conn:
        tid = _blocked_behind(conn, "home", blocker_id=WALL)
        _own_gateway(conn, monkeypatch)

        def handed_over(_command):
            _created_on(conn, tid, "worker-gw")  # ownership moved to another gateway while the shell ran
            return 0, ""

        monkeypatch.setattr(checks, "_run_check", handed_over)
        clock[0] += 30
        assert checks.run_due_checks(conn, cfg=CFG) == []
        assert not _events(conn, tid)
        assert not _events(conn, tid, checks.ATTEMPT_EVENT)
        assert blockers.blocker_states(conn)[WALL].is_open


def test_fence_ignores_future_dated_receipt(kanban_home, clock):
    """A receipt stamped after the evaluation clock (a skewed remote, or a replay at an
    earlier now) is not recovery evidence: it must not fence a current assertion."""
    with kbc.connect_closing() as conn:
        first = _blocked_behind(conn, "quota", blocker_id=GH)
        old_token = checks._assertion_tokens(conn)[GH]
        with kb.write_txn(conn):
            kb._append_event(conn, first, checks.CHECK_EVENT, {
                "blocker_id": GH, "command": "unused", "rc": 0, "output": "",
                "started_at": T0 + 1000, "checked_at": T0 + 1000, "checked_at_ms": (T0 + 1000) * 1000,
                "assertion_id": old_token, "fails": 0, "active_runs": []})
        clock[0] += 30
        _blocked_behind(conn, "new outage", blocker_id=GH)
        state = blockers.blocker_states(conn)[GH]
        assert state.is_open
        assert state.cleared_at == 0
        # Once the clock reaches the receipt it is ordinary historical recovery again.
        assert not blockers.blocker_states(conn, now=T0 + 1000)[GH].is_open
