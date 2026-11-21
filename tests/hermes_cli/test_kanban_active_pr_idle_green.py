"""Idle bound PRs resume after green CI without an operator handoff."""
import time
from concurrent.futures import Future

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli import kanban_db_dispatch_pr_state as prs
from hermes_cli import kanban_pr_acceptance as acceptance
from hermes_cli import kanban_pr_idle_checks as checks
from tests.hermes_cli.kanban_guard_helpers import worker_comment
from tests.hermes_cli.test_kanban_park_invariants import kanban_home

__all__ = ["kanban_home"]
pytestmark = pytest.mark.usefixtures("kanban_home")
URL = "https://github.com/example/project/pull/19"
SHA = "a" * 40


@pytest.fixture
def green(monkeypatch):
    prs.clear_cache()
    class ImmediatePool:
        def submit(self, fn, *args):
            future = Future()
            future.set_result(fn(*args))
            return future
    monkeypatch.setattr(checks, "_pool", ImmediatePool())
    monkeypatch.setattr(prs, "pr_state", lambda url: "OPEN")
    monkeypatch.setattr(prs, "pr_head", lambda url, **kw: SHA)
    monkeypatch.setattr(checks, "_fresh_head_matches", lambda url, a, head: prs.pr_head(url) == head)
    receipt = {"ok": True, "pr_state": "OPEN", "head_sha": SHA,
               "pr_url": URL, "checks": [{"name": "required", "conclusion": "success"}]}
    calls = []
    def collect(*args, **kwargs):
        calls.append((args, kwargs))
        return dict(receipt)
    monkeypatch.setattr(acceptance, "collect_acceptance", collect)
    yield receipt, calls
    prs.clear_cache()


def published(conn):
    tid = kb.create_task(conn, title="finish published work", assignee="a", completion_contract=URL)
    kb.claim_task(conn, tid)
    worker_comment(conn, tid, "a", f"Opened {URL}")
    # A lost publisher, not an orderly park/handoff: ordinary active_pr reproducer.
    conn.execute("UPDATE task_runs SET status='crashed', outcome='crashed', ended_at=? WHERE task_id=?",
                 (int(time.time()), tid))
    conn.execute("UPDATE tasks SET status='ready', current_run_id=NULL, claim_lock=NULL, "
                 "claim_expires=NULL, worker_pid=NULL WHERE id=?", (tid,))
    conn.commit()
    return tid


def test_idle_bound_green_released_on_next_guard_tick(green):
    with kbc.connect() as conn:
        tid = published(conn)
        assert dispatch.check_respawn_guard(conn, tid) is None
        assert len(green[1]) == 1


@pytest.mark.parametrize("change", [
    {"ok": False, "classification": "failing"},
    {"ok": False, "classification": "pending"},
    {"ok": False, "classification": "infra"},
    {"head_sha": "b" * 40}, {"pr_url": URL + "0"}, {"checks": []}, {"pr_state": "CLOSED"},
])
def test_missing_or_non_green_exact_head_stays_held(green, change):
    green[0].update(change)
    with kbc.connect() as conn:
        assert dispatch.check_respawn_guard(conn, published(conn)) == "active_pr"


def test_active_run_stays_held_without_ci_lookup(green):
    with kbc.connect() as conn:
        tid = published(conn)
        conn.execute("UPDATE task_runs SET status='running', outcome=NULL, ended_at=NULL WHERE task_id=?", (tid,))
        conn.commit()
        assert dispatch.check_respawn_guard(conn, tid) == "active_pr"
        assert green[1] == []


def test_live_wake_watch_stays_held_without_ci_lookup(green):
    from hermes_cli import kanban_wake_check as wake
    with kbc.connect() as conn:
        tid = published(conn)
        with kb.write_txn(conn):
            wake.set_check(conn, tid, "true", 120)
        assert dispatch.check_respawn_guard(conn, tid) == "active_pr"
        assert green[1] == []


def test_local_only_publication_resumes_existing_pr_after_green(green):
    with kbc.connect() as conn:
        tid = published(conn)
        conn.execute("UPDATE tasks SET completion_contract='local-only' WHERE id=?", (tid,))
        conn.commit()
        assert dispatch.check_respawn_guard(conn, tid) is None
        assert len(green[1]) == 1


def test_active_pr_cap_is_at_most_thirty_five_minutes(monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_GUARD_HOLD_CAP_LONG_SECONDS", "21600")
    monkeypatch.setenv("HERMES_KANBAN_GUARD_HOLD_CAP_SECONDS", "7200")
    assert dispatch.guard_hold_cap_seconds("active_pr") == 2100
