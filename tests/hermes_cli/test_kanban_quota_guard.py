"""A recovered quota wall must never become a human block. [daveey]

Regression tests for 2026-10-23: six cards sat ``blocked`` / ``kind=capability`` for ~13 h with
"Held in ready by the respawn guard (blocker_auth) for 30 min without running. Last error: pid N
exited rate-limited (quota wall)". The rate-limit stamp outlived its ``rate_limited`` run (the
latest run was ``changes_requested`` / ``blocked``), so the guard read it as ``blocker_auth`` and
the hold cap escalated it to a human kind.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_dispatch_quota as kbq

QUOTA_ERR = "pid 82594 exited rate-limited (quota wall) — requeued without counting a failure"
LEGACY_BLOCK = ("Held in ready by the respawn guard (blocker_auth) for 30 min without running. "
                f"Last error: {QUOTA_ERR} Fix the cause (credentials, quota, PR binding), then unblock.")


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in ("HERMES_KANBAN_TASK", "HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD",
                "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_DELEGATED_CHILD_CONTEXT",
                "HERMES_KANBAN_RATE_LIMIT_COOLDOWN_SECONDS"):
        monkeypatch.delenv(key, raising=False)
    kb.init_db()
    return home


def _ended_with_error(conn, tid, outcome, error, ended_at):
    """A run that ended with ``outcome`` while a stale ``error`` stamp stays on the task."""
    kb.claim_task(conn, tid)
    run_id = kb.get_task(conn, tid).current_run_id
    conn.execute("UPDATE task_runs SET outcome=?, status=?, ended_at=? WHERE id=?",
                 (outcome, outcome, ended_at, run_id))
    conn.execute("UPDATE tasks SET status='ready', current_run_id=NULL, claim_lock=NULL, "
                 "claim_expires=NULL, worker_pid=NULL, last_failure_error=? WHERE id=?", (error, tid))
    conn.commit()


def _err(conn, tid):
    return conn.execute("SELECT last_failure_error FROM tasks WHERE id=?", (tid,)).fetchone()[0]


@pytest.mark.parametrize("outcome", ["blocked", "changes_requested"])
def test_stale_quota_stamp_is_not_blocker_auth(kanban_home, monkeypatch, outcome):
    now = 5_000_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="quota stamp", assignee="a")
        _ended_with_error(conn, tid, outcome, QUOTA_ERR, now - 10)
        # Inside the cooldown: the quota cooldown path, never blocker_auth.
        assert kbd.check_respawn_guard(conn, tid) == "rate_limit_cooldown"
        assert _err(conn, tid) == QUOTA_ERR
        # Cooldown over: retry, and the stamp is gone so it cannot re-trap the card.
        monkeypatch.setattr(kb.time, "time", lambda: now + 400)
        assert kbd.check_respawn_guard(conn, tid) is None
        assert _err(conn, tid) is None


@pytest.mark.parametrize("text", ["429 Too Many Requests", "provider quota exceeded",
                                  "hit the rate limit", "exited rate-limited (quota wall)"])
def test_quota_text_never_blocker_auth_with_no_runs(kanban_home, text):
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="quota", assignee="a")
        conn.execute("UPDATE tasks SET last_failure_error=? WHERE id=?", (text, tid))
        conn.commit()
        assert kbd.check_respawn_guard(conn, tid) != "blocker_auth"


def test_rate_limited_outcome_clears_stamp_after_cooldown(kanban_home, monkeypatch):
    now = 5_000_000
    monkeypatch.setattr(kb.time, "time", lambda: now + 400)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="rl", assignee="a")
        _ended_with_error(conn, tid, "rate_limited", QUOTA_ERR, now)
        assert kbd.check_respawn_guard(conn, tid) is None
        assert _err(conn, tid) is None


def test_real_auth_error_still_blocker_auth_and_escalates(kanban_home, monkeypatch, all_assignees_spawnable):
    monkeypatch.setenv("HERMES_KANBAN_GUARD_HOLD_CAP_SECONDS", "600")
    spawned = []
    now = 5_000_000
    # Freeze before lifecycle writes: a slow claim must not land after the first guard.
    monkeypatch.setattr(kb.time, "time", lambda: now)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="auth", assignee="a")
        from tests.hermes_cli.kanban_guard_helpers import configure_eligible_profile
        configure_eligible_profile(conn, tid, kanban_home, "a")
        _ended_with_error(conn, tid, "changes_requested", "401 authentication error", now - 5)
        assert kbd.check_respawn_guard(conn, tid) == "blocker_auth"
        kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: spawned.append(a) or 1)
        monkeypatch.setattr(kb.time, "time", lambda: now + 700)
        kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: spawned.append(a) or 1)
        task = kb.get_task(conn, tid)
        assert task.status == "triage"
        assert task.block_kind == "transient"
        assert not spawned


def test_auth_wins_over_quota_words():
    assert not kbq.is_quota_error("429 then 401 unauthorized")
    assert kbq.is_quota_error(QUOTA_ERR)
    assert not kbq.is_quota_error("invalid api key")


@pytest.mark.parametrize("outcome", ["blocked", "changes_requested"])
def test_quota_hold_never_escalates_to_human_block(kanban_home, monkeypatch, all_assignees_spawnable, outcome):
    monkeypatch.setenv("HERMES_KANBAN_GUARD_HOLD_CAP_SECONDS", "600")
    spawned = []
    now = 5_000_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="quota hold", assignee="a")
        from tests.hermes_cli.kanban_guard_helpers import configure_eligible_profile
        configure_eligible_profile(conn, tid, kanban_home, "a")
        _ended_with_error(conn, tid, outcome, QUOTA_ERR, now - 5)
        for dt in (0, 300, 700, 1300):
            monkeypatch.setattr(kb.time, "time", lambda dt=dt: now + dt)
            res = kbd.dispatch_once(conn, spawn_fn=lambda *a, **k: spawned.append(a) or 1)
            assert tid not in res.auto_blocked
            task = kb.get_task(conn, tid)
            assert task.status != "blocked"
            assert task.block_kind not in ("capability", "needs_input")
        assert spawned, "the card retries once the cooldown has elapsed"


def test_hold_cap_requeues_quota_error_instead_of_blocking(kanban_home, monkeypatch):
    """The hold-cap path itself, reached for a quota-flavored error: re-queue, never a human kind."""
    monkeypatch.setenv("HERMES_KANBAN_GUARD_HOLD_CAP_SECONDS", "600")
    now = 5_000_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="capped", assignee="a")
        conn.execute("UPDATE tasks SET last_failure_error=? WHERE id=?", (QUOTA_ERR, tid))
        conn.commit()
        monkeypatch.setattr(kb.time, "time", lambda: now)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "respawn_guarded", {"reason": "blocker_auth"})
        monkeypatch.setattr(kb.time, "time", lambda: now + 3600)
        for reason in ("blocker_auth", "rate_limit_cooldown"):
            assert kbd._block_if_guard_hold_capped(conn, tid, reason) is False
        task = kb.get_task(conn, tid)
        assert task.status == "ready"
        assert _err(conn, tid) is None


def _legacy_blocked(conn, monkeypatch, at, reason=LEGACY_BLOCK):
    monkeypatch.setattr(kb.time, "time", lambda: at)
    tid = kb.create_task(conn, title="legacy held", assignee="nobody-runs-this")
    conn.execute("UPDATE tasks SET last_failure_error=? WHERE id=?", (QUOTA_ERR, tid))
    conn.commit()
    assert kb.block_task(conn, tid, reason=reason, kind="capability")
    assert kb.get_task(conn, tid).status == "blocked"
    return tid


def _complete_other(conn, monkeypatch, at):
    monkeypatch.setattr(kb.time, "time", lambda: at)
    other = kb.create_task(conn, title="other", assignee="b")
    kb.claim_task(conn, other)
    assert kb.complete_task(conn, other, result="ok")
    return other


def test_existing_quota_hold_block_released_after_other_completion(kanban_home, monkeypatch):
    now = 5_000_000
    with kbc.connect() as conn:
        tid = _legacy_blocked(conn, monkeypatch, now)
        # Nothing completed since the block: stays blocked.
        assert kbq.release_recovered_quota_blocks(conn) == []
        _complete_other(conn, monkeypatch, now + 60)
        monkeypatch.setattr(kb.time, "time", lambda: now + 120)
        res = kbd.dispatch_once(conn, dry_run=True)
        assert tid in res.quota_block_released
        assert kb.get_task(conn, tid).status == "ready"
        bodies = [r[0] for r in conn.execute("SELECT body FROM task_comments WHERE task_id=?", (tid,))]
        assert any(b.startswith(kbq.RELEASE_NOTE_MARKER) and "quota" in b for b in bodies)


def test_completion_before_block_is_not_proof(kanban_home, monkeypatch):
    now = 5_000_000
    with kbc.connect() as conn:
        _complete_other(conn, monkeypatch, now - 60)
        tid = _legacy_blocked(conn, monkeypatch, now)
        monkeypatch.setattr(kb.time, "time", lambda: now + 120)
        assert kbq.release_recovered_quota_blocks(conn) == []
        assert kb.get_task(conn, tid).status == "blocked"


def test_auth_hold_block_is_not_released(kanban_home, monkeypatch):
    now = 5_000_000
    with kbc.connect() as conn:
        reason = ("Held in ready by the respawn guard (blocker_auth) for 30 min without running. "
                  "Last error: 401 authentication error Fix the cause (credentials, quota, PR binding), "
                  "then unblock.")
        tid = _legacy_blocked(conn, monkeypatch, now, reason=reason)
        _complete_other(conn, monkeypatch, now + 60)
        monkeypatch.setattr(kb.time, "time", lambda: now + 120)
        assert kbq.release_recovered_quota_blocks(conn) == []
        task = kb.get_task(conn, tid)
        assert task.status == "blocked" and task.block_kind == "capability"
        ev = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='blocked'", (tid,)).fetchone()
        assert json.loads(ev[0])["kind"] == "capability"


def test_hold_clock_survives_non_dispatcher_comments(kanban_home, monkeypatch):
    """A comment from an orchestrator/human must not restart the hold clock (t_d5d83916)."""
    monkeypatch.setenv("HERMES_KANBAN_GUARD_HOLD_CAP_LONG_SECONDS", "3600")
    now = 5_000_000
    monkeypatch.setattr(kb.time, "time", lambda: now)
    with kbc.connect() as conn:
        tid = kb.create_task(conn, title="pr held", assignee="a")
        monkeypatch.setattr(kb.time, "time", lambda: now)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "respawn_guarded", {"reason": "active_pr"})
        monkeypatch.setattr(kb.time, "time", lambda: now + 1800)
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "commented", {"author": "default", "len": 10})
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "respawn_guarded", {"reason": "active_pr"})
        from hermes_cli.kanban_db_dispatch_guard_notice import guard_hold_started_at
        assert guard_hold_started_at(conn, tid) == now
        monkeypatch.setattr(kb.time, "time", lambda: now + 3700)
        assert kbd._block_if_guard_hold_capped(conn, tid, "active_pr") is True
        assert kb.get_task(conn, tid).status != "ready"
