"""Bounded caching, custody races and actual ready-lane dispatch for idle-green PRs."""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_pr_idle_green as idle
from hermes_cli import kanban_pr_idle_checks as checks
from hermes_cli import kanban_pr_acceptance as acceptance
from hermes_cli import kanban_db_dispatch_pr_state as prs
from tests.hermes_cli import test_kanban_active_pr_idle_green as helpers

kanban_home = helpers.kanban_home
green = helpers.green
pytestmark = pytest.mark.usefixtures("kanban_home")


def test_green_owner_reaches_real_dispatch(green, all_assignees_spawnable):
    with kbc.connect() as conn:
        tid = helpers.published(conn)
        result = dispatch.dispatch_once(conn, spawn_fn=lambda *a, **kw: 987654)
        assert any(row[0] == tid for row in result.spawned)
        assert kb.get_task(conn, tid).status == "running"


def test_hold_cap_uses_existing_visible_supervisor_escalation(green, monkeypatch):
    from hermes_cli import kanban_db_dispatch_guard_notice as notice
    green[0]["ok"] = False
    with kbc.connect() as conn:
        tid = helpers.published(conn)
        now = int(kb.time.time())
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "respawn_guarded", {"reason": "active_pr"})
        monkeypatch.setattr(kb.time, "time", lambda: now + 2099)
        assert not dispatch._block_if_guard_hold_capped(conn, tid, "active_pr")
        assert notice.guard_hold_started_at(conn, tid) == now
        monkeypatch.setattr(kb.time, "time", lambda: now + 2100)
        assert dispatch._block_if_guard_hold_capped(conn, tid, "active_pr")
        assert kb.get_task(conn, tid).status == "triage"
        event = conn.execute("SELECT payload FROM task_events WHERE task_id=? "
                             "AND kind='supervisor_escalated'", (tid,)).fetchone()
        assert event and 'active_pr' in event[0]


def test_pending_cache_refreshes_after_five_minutes(green, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(checks.time, "monotonic", lambda: clock[0])
    green[0]["ok"] = False
    with kbc.connect() as conn:
        tid = helpers.published(conn)
        assert dispatch.check_respawn_guard(conn, tid) == "active_pr"
        green[0]["ok"] = True
        clock[0] += 299
        assert dispatch.check_respawn_guard(conn, tid) == "active_pr"
        assert len(green[1]) == 1
        clock[0] += 1
        assert dispatch.check_respawn_guard(conn, tid) is None
        assert len(green[1]) == 2


@pytest.mark.parametrize("race", ["claim", "watch", "contract", "assignee", "head"])
def test_ci_collection_revalidates_custody_and_head(green, monkeypatch, race):
    with kbc.connect() as conn:
        tid = helpers.published(conn)
        def collect(*args, **kwargs):
            updates = {
                "claim": ("UPDATE tasks SET status='running' WHERE id=?", (tid,)),
                "contract": ("UPDATE tasks SET completion_contract='local-only' WHERE id=?", (tid,)),
                "assignee": ("UPDATE tasks SET assignee='b' WHERE id=?", (tid,)),
            }
            with kb.write_txn(conn):
                if race in updates:
                    conn.execute(*updates[race])
                elif race == "watch":
                    from hermes_cli.kanban_wake_check import set_check
                    set_check(conn, tid, "true", 120)
                else:
                    monkeypatch.setattr(prs, "pr_head", lambda *a, **kw: "b" * 40)
            return green[0]
        monkeypatch.setattr(acceptance, "collect_acceptance", collect)
        assert dispatch.check_respawn_guard(conn, tid) == "active_pr"


@pytest.mark.parametrize("states", [{helpers.URL: None}, {helpers.URL: "OPEN", helpers.URL + "0": "OPEN"}])
def test_unknown_or_additional_holding_pr_is_not_discharged(green, states):
    with kbc.connect() as conn:
        tid = helpers.published(conn)
        assert not idle.releases(conn, tid, states)
        assert len(green[1]) == (0 if None in states.values() else 2)


def test_missing_head_is_not_green(green, monkeypatch):
    monkeypatch.setattr(prs, "pr_head", lambda *a, **kw: None)
    with kbc.connect() as conn:
        assert dispatch.check_respawn_guard(conn, helpers.published(conn)) == "active_pr"
        assert green[1] == []


@pytest.mark.parametrize("contract", ["malformed", helpers.URL + "0"])
def test_invalid_or_uncovered_contract_stays_held(green, contract):
    with kbc.connect() as conn:
        tid = helpers.published(conn)
        conn.execute("UPDATE tasks SET completion_contract=? WHERE id=?", (contract, tid))
        conn.commit()
        assert not idle.releases(conn, tid, {helpers.URL: "OPEN"})
        assert green[1] == []


def test_disappearing_task_identity_is_not_admitted(green, monkeypatch):
    with kbc.connect() as conn:
        tid = helpers.published(conn)
        monkeypatch.setattr(idle, "_identity", lambda *a: None)
        assert not idle.releases(conn, tid, {helpers.URL: "OPEN"})
