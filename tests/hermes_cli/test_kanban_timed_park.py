"""Timed parks: a block that waits on a clock is ``scheduled``, never a human block.

Regression for cards that parked on a timer with ``kanban_block`` (goal-mode cards
may not ``kanban_schedule``): a parent-less ``dependency`` was re-kinded to
``needs_input``, so the Desktop sidebar painted the chat red. [daveey]
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from hermes_cli import kanban_continuation as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_session_activity as ksa
from hermes_cli import kanban_timed_park as ktp


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    ksa._reset_cache_for_tests()
    yield home
    ksa._reset_cache_for_tests()


def _running(conn, session_id: str | None = None) -> str:
    tid = kb.create_task(conn, title="t", assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready', session_id=? WHERE id=?", (session_id, tid))
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    return tid


def _last(conn, tid, kind):
    return [e for e in kb.list_events(conn, tid) if e.kind == kind][-1].payload


def test_parentless_dependency_block_parks_scheduled_not_needs_input(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        before = int(time.time())
        assert kb.block_task(conn, tid, reason="waiting for tonight's 04:30 run; cron wakes it", kind="dependency", clock_only=True)
        task = kb.get_task(conn, tid)
        assert task.status == "scheduled"
        assert task.block_kind is None
        assert not [e for e in kb.list_events(conn, tid) if e.kind == "blocked"]
        park = _last(conn, tid, "scheduled")
        assert park["timed_park"] and park["requested_kind"] == "dependency"
        assert before + ktp.DEFAULT_WAKE_SECONDS <= park["wake_at"] <= int(time.time()) + ktp.DEFAULT_WAKE_SECONDS
        assert kb.latest_run(conn, tid).outcome == "scheduled"


def test_scheduled_until_marker_sets_the_wake_and_the_dispatcher_wakes_it(kanban_home: Path) -> None:
    wake = int(time.time()) + 3 * 3600
    stamp = datetime.fromtimestamp(wake, tz=UTC).isoformat().replace("+00:00", "Z")
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        assert kb.block_task(conn, tid, reason=f"SCHEDULED_UNTIL={stamp} — rate-limit cooldown ends", kind="needs_input", clock_only=True)
        assert kb.get_task(conn, tid).status == "scheduled"
        assert _last(conn, tid, "scheduled")["wake_at"] == wake
        assert kc.wake_due_continuations(conn, now=wake - 1) == []
        assert kc.wake_due_continuations(conn, now=wake) == [tid]
        assert kb.get_task(conn, tid).status == "ready"


def test_time_kind_parks_and_unblock_still_wakes_early(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        assert kb.block_task(conn, tid, reason="re-measure tomorrow", kind="time", clock_only=True)
        assert kb.get_task(conn, tid).status == "scheduled"
        assert kb.unblock_task(conn, tid)
        assert kb.get_task(conn, tid).status == "ready"


def test_kanban_schedule_marker_becomes_a_real_timer(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        assert kb.schedule_task(conn, tid, reason="SCHEDULED_UNTIL=2000-01-01T00:00:00Z")
        due = _last(conn, tid, "scheduled")["wake_at"]
        assert kc.wake_due_continuations(conn, now=due) == [tid]
        # No marker -> the default wait, never "forever" (a deleted cron stranded cards). [daveey]
        tid2 = _running(conn)
        assert kb.schedule_task(conn, tid2, reason="rate-limit cooldown", clock_only=True)
        assert _last(conn, tid2, "scheduled")["wake_at"] >= int(time.time()) + ktp.DEFAULT_WAKE_SECONDS - 5
        assert kc.wake_due_continuations(conn, now=int(time.time()) + 3600) == []
        assert kc.wake_due_continuations(conn, now=int(time.time()) + 10**7) == [tid2]


def test_needs_input_without_marker_still_blocks_for_a_human(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        assert kb.block_task(conn, tid, reason="1) tap Approve on the iPhone", kind="needs_input")
        assert (kb.get_task(conn, tid).status, kb.get_task(conn, tid).block_kind) == ("blocked", "needs_input")


def test_shared_blocker_dependency_parks_with_a_wake(kanban_home: Path) -> None:
    """A parent-less dependency behind a shared blocker used to land sticky needs_input with no
    wake (nothing probed it); it now parks scheduled on the shared probe cadence. [daveey]"""
    from hermes_cli.kanban_continuation import pending_wake

    with kbc.connect_closing() as conn:
        tid = _running(conn)
        assert kb.block_task(conn, tid, reason="CI billing", kind="dependency", blocker_id="gh-billing")
        assert kb.get_task(conn, tid).status == "scheduled"
        assert (pending_wake(conn, tid) or {}).get("blocker_id") == "gh-billing"


def test_kanban_block_tool_time_kind_on_goal_mode_card(kanban_home: Path, monkeypatch) -> None:
    from tools import kanban_tools as kt

    with kbc.connect_closing() as conn:
        tid = _running(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET goal_mode = 1 WHERE id = ?", (tid,))
        run_id = kb.get_task(conn, tid).current_run_id
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    out = json.loads(kt._handle_block({"reason": "SCHEDULED_UNTIL=2099-01-01T00:00:00Z nightly run", "kind": "time",
                                       "clock_only": True}))
    assert out.get("status") == "scheduled", out
    assert out.get("wake_at")


def _sidebar_counts(home: Path):
    db = home / "kanban.db"
    with sqlite3.connect(db):
        pass
    ksa._reset_cache_for_tests()
    return ksa.active_task_counts(), ksa.blocked_task_counts()


def test_sidebar_counts_only_human_blocks_as_blocked(kanban_home: Path, monkeypatch) -> None:
    """Only evidenced human requests paint red; operational blocks remain active."""
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(kanban_home))
    with kbc.connect_closing() as conn:
        human = _running(conn, "s_human")
        kb.block_task(conn, human, reason="1) tap Approve", kind="needs_input", human_request={
            "action": "Tap Approve on the iPhone sign-in prompt",
            "why_human": "Only the owner can confirm the prompt on their phone",
            "interaction": "physical",
            "scope": "Pending account sign-in on the owner's iPhone",
            "evidence": "Sign-in reports a pending device confirmation",
        })
        timed = _running(conn, "s_timed")
        kb.block_task(conn, timed, reason="nightly run; cron wakes", kind="dependency", clock_only=True)
        flaky = _running(conn, "s_flaky")
        kb.block_task(conn, flaky, reason="429s", kind="transient")
        untyped = _running(conn, "s_untyped")
        kb.block_task(conn, untyped, reason="??")
    active, blocked = _sidebar_counts(kanban_home)
    assert blocked == {"s_human": 1}
    assert active.get("s_untyped") == 1
    assert active.get("s_timed") == 1 and active.get("s_flaky") == 1


def test_untyped_blocked_card_classified_as_time_parks_scheduled_not_blocked(kanban_home: Path) -> None:
    """[daveey] A card created blocked (or parked untyped by the breaker) that is then blocked with
    kind='time' is a wait on a clock: it must leave ``blocked`` for ``scheduled`` (classify-in-place used to
    stamp block_kind and keep it ``blocked``, which the Desktop showed under For you with an unblock button)."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="tracker", assignee=None, initial_status="blocked", lint_blocked=False, session_id="s1")
        assert kb.get_task(conn, tid).status == "blocked" and kb.get_task(conn, tid).block_kind is None
        assert kb.block_task(conn, tid, reason="External-event wait while the remote graph executes", kind="time",
                             wake_check="test -f /example/ready", check_every=120)
        task = kb.get_task(conn, tid)
        assert task.status == "scheduled" and task.block_kind is None
        park = _last(conn, tid, "scheduled")
        assert park["timed_park"] and park["requested_kind"] == "time"
        assert park["wake_check"] == "test -f /example/ready" and park["check_every"] == 120
    ksa._reset_cache_for_tests()
    assert ksa.blocked_task_counts().get("s1", 0) == 0
    assert ksa.active_task_counts().get("s1") == 1  # still unfinished


def test_untyped_blocked_card_classified_as_needs_input_stays_a_human_block(kanban_home: Path) -> None:
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="ask", assignee=None, initial_status="blocked", lint_blocked=False, session_id="s2")
        assert kb.block_task(conn, tid, reason="Pick option A or B", kind="needs_input")
        task = kb.get_task(conn, tid)
        assert task.status == "blocked" and task.block_kind == "needs_input"
