"""A ready card's wait starts when its last run ended, and a clock hold says when it ends. [daveey]

hermes-hub 2026-11-10: t_01a1a1dea9c64d6a ran 80 min (run 163883, claimed 21:05:48Z) until a gateway restart
requeued it (``owner_restart_lost`` 22:26:32Z). The same tick posted ``chat_wait_unclaimed`` "ready 80 min with no
worker" (``wait_event_id ready@1794344748``) and the ``held_in_ready`` diagnostic said "Ready 82m": both measured the
wait from the run's ``claimed`` event, because ``owner_restart_lost`` (and the run's end) were not ready-entry
floors. The card was in its 120 s ``owner_restart_cooldown`` and the dispatcher claimed it 38 s after the cooldown
ended. The same misreport followed run 163575 earlier that day. These tests pin: the run's interval never counts as
ready time (events-only and runs paths, chat notice and diagnostic), a genuine 31-min unclaimed wait after the run
still alerts, and an active clock hold is shown with its expiry.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kn
from hermes_cli import kanban_diagnostics as kd

_ENV = ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_CLAIM_LOCK", "HERMES_KANBAN_HOME", "HERMES_DELEGATED_CHILD_CONTEXT",
        "HERMES_KANBAN_WORKER_TASK", "HERMES_KANBAN_OWNER_RESTART_COOLDOWN_SECONDS")


@pytest.fixture
def board(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    for key in _ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _restart_lost_card(conn, *, claimed_ago: int, lost_ago: int, record_lost_event: bool = True) -> str:
    """A subscribed card claimed ``claimed_ago`` s ago whose run was lost to a gateway restart ``lost_ago`` s ago."""
    now = int(time.time())
    tid = kb.create_task(conn, title="retention ladder", assignee="default", initial_status="running")
    kn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id="origin-chat", delivery_metadata={"reason": "create"})
    conn.execute("UPDATE tasks SET created_at=? WHERE id=?", (now - claimed_ago - 600, tid))
    conn.execute("UPDATE task_events SET created_at=? WHERE task_id=?", (now - claimed_ago - 600, tid))
    meta = {"exit_code": 76, "owner_restart": True, "retry_status": "ready"}
    conn.execute(
        "INSERT INTO task_runs (task_id, status, started_at, ended_at, outcome, metadata) "
        "VALUES (?, 'reclaimed', ?, ?, 'reclaimed', ?)", (tid, now - claimed_ago, now - lost_ago, json.dumps(meta)))
    conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'claimed', ?, ?)",
                 (tid, json.dumps({"lock": "hub:1", "run_id": 1}), now - claimed_ago))
    conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, 'spawned', ?, ?)",
                 (tid, json.dumps({"pid": 1}), now - claimed_ago))
    if record_lost_event:
        conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES "
                     "(?, 'owner_restart_lost', ?, ?)", (tid, json.dumps(meta), now - lost_ago))
    conn.execute("UPDATE tasks SET status='ready', claim_lock=NULL WHERE id=?", (tid,))
    conn.commit()
    return tid


def _notices(conn, tid):
    from hermes_cli import kanban_chat_wait as cw
    return [json.loads(p) for (p,) in conn.execute(
        "SELECT payload FROM task_events WHERE task_id=? AND kind=? ORDER BY id", (tid, cw.NOTICE_KIND))]


@pytest.mark.parametrize("record_lost_event", [True, False], ids=["owner_restart_lost_event", "run_end_only"])
def test_restart_requeue_after_a_long_run_is_not_a_stale_ready_wait(board, record_lost_event):
    """Regression: 80 min of running before the restart must not read as 80 min ready with no worker."""
    from hermes_cli import kanban_chat_wait as cw
    from hermes_cli import kanban_parent_unclaimed as pu
    conn = kbc.connect()
    try:
        tid = _restart_lost_card(conn, claimed_ago=80 * 60, lost_ago=1, record_lost_event=record_lost_event)
        assert cw.sweep(conn) == 0
        assert _notices(conn, tid) == []
        assert int(time.time()) - pu.ready_since(conn, tid) < 60
        assert pu.ready_hint(conn, kb.get_task(conn, tid)) is None
    finally:
        conn.close()


def test_genuine_unclaimed_wait_after_the_run_still_alerts_with_its_own_age(board):
    """Control: 31 min unclaimed after the restart is a real wait; the notice counts from the run's end."""
    from hermes_cli import kanban_chat_wait as cw
    conn = kbc.connect()
    try:
        tid = _restart_lost_card(conn, claimed_ago=111 * 60, lost_ago=31 * 60)
        assert cw.sweep(conn) == 1
        (notice,) = _notices(conn, tid)
        assert notice["ready_minutes"] == 31
        assert "hold_until" not in notice  # the 120 s cooldown ended long ago
        assert cw.sweep(conn) == 0
    finally:
        conn.close()


def test_active_clock_hold_is_named_with_its_expiry_in_the_notice(board, monkeypatch):
    """A genuine wait still alerts during a clock hold, and says which hold and when it ends."""
    from hermes_cli import kanban_chat_wait as cw
    monkeypatch.setenv("HERMES_KANBAN_OWNER_RESTART_COOLDOWN_SECONDS", str(3600))
    conn = kbc.connect()
    try:
        tid = _restart_lost_card(conn, claimed_ago=100 * 60, lost_ago=31 * 60)
        now = int(time.time())
        assert cw.sweep(conn) == 1
        (notice,) = _notices(conn, tid)
        assert notice["hold_reason"] == "owner_restart_cooldown"
        assert abs(notice["hold_until"] - (now - 31 * 60 + 3600)) <= 2
        text = cw.notice_text(notice)
        assert "owner_restart_cooldown" in text and "until" in text
    finally:
        conn.close()


# --- held_in_ready / stranded_in_ready diagnostics (pure) ---------------------------------------------------------


def _ev(kind, ts, **payload):
    return {"kind": kind, "created_at": ts, "payload": payload or None}


def _run(ended_at, *, owner_restart=True):
    return {"id": 1, "outcome": "reclaimed", "ended_at": ended_at, "metadata": json.dumps({"owner_restart": owner_restart})}


def test_held_diagnostic_excludes_the_prior_run_and_shows_the_active_cooldown_expiry():
    now = 1_794_349_600
    lost = now - 8
    events = [_ev("created", now - 3 * 3600), _ev("claimed", now - 80 * 60), _ev("spawned", now - 80 * 60),
              _ev("owner_restart_lost", lost, owner_restart=True),
              _ev("respawn_guarded", lost + 1, reason="owner_restart_cooldown")]
    task = {"id": "t_x", "assignee": "default", "status": "ready", "claim_lock": None, "created_at": now - 3 * 3600,
            "consecutive_failures": 0}
    diags = kd.compute_task_diagnostics(task, events, [_run(lost)], now=now)
    assert [d for d in diags if d.kind in ("held_in_ready", "stranded_in_ready")] == []
    # Same history seen 40 min later: a genuine hold age, counted from the run's end, with the guard reason and
    # its (now expired) clock in the data.
    later = lost + 40 * 60
    (held,) = [d for d in kd.compute_task_diagnostics(task, events, [_run(lost)], now=later)
               if d.kind == "held_in_ready"]
    assert held.data["ready_since"] == lost
    assert held.data["guard_reason"] == "owner_restart_cooldown"
    assert held.data["hold_until"] == lost + 120
    assert "owner_restart_cooldown" in held.title and "40m" in held.title


def test_held_diagnostic_title_says_until_while_the_clock_hold_is_active():
    now = 1_794_349_600
    lost = now - 31 * 60
    events = [_ev("created", now - 3 * 3600), _ev("claimed", now - 2 * 3600),
              _ev("owner_restart_lost", lost), _ev("respawn_guarded", lost + 1, reason="owner_restart_cooldown")]
    task = {"id": "t_x", "assignee": "default", "status": "ready", "claim_lock": None, "created_at": now - 3 * 3600}
    cfg = {"owner_restart_cooldown_seconds": 3600}
    (held,) = [d for d in kd.compute_task_diagnostics(task, events, [_run(lost)], now=now, config=cfg)
               if d.kind == "held_in_ready"]
    assert held.data["hold_until"] == lost + 3600
    assert "until" in held.title


def test_stranded_diagnostic_floors_on_the_last_run_end_but_still_fires_for_a_genuine_wait():
    now = 1_794_349_600
    task = {"id": "t_x", "assignee": "default", "status": "ready", "claim_lock": None, "created_at": now - 5 * 3600,
            "consecutive_failures": 0}
    events = [_ev("created", now - 5 * 3600), _ev("claimed", now - 4 * 3600)]
    # The run ended 2 min ago (no requeue event recorded): not stranded.
    assert [d for d in kd.compute_task_diagnostics(task, events, [_run(now - 120, owner_restart=False)], now=now)
            if d.kind == "stranded_in_ready"] == []
    # The run ended 45 min ago: stranded, aged from the run's end.
    (stranded,) = [d for d in kd.compute_task_diagnostics(task, events, [_run(now - 45 * 60, owner_restart=False)],
                                                          now=now) if d.kind == "stranded_in_ready"]
    assert stranded.data["ready_since"] == now - 45 * 60



def test_a_failed_hold_lookup_drops_the_hold_never_the_notice(board, monkeypatch):
    from hermes_cli import kanban_chat_wait as cw
    from hermes_cli import kanban_db_dispatch_clock as clock
    calls = []

    def _boom(*args, **_k):
        calls.append(args)
        raise RuntimeError("clock lookup broke")

    monkeypatch.setattr(clock, "guard_hold_until", _boom)
    conn = kbc.connect()
    try:
        tid = _restart_lost_card(conn, claimed_ago=100 * 60, lost_ago=31 * 60)
        assert cw.sweep(conn) == 1
        assert calls and calls[0][2] == "owner_restart_cooldown"  # the failing lookup was reached
        (notice,) = _notices(conn, tid)
        assert "hold_until" not in notice and notice["ready_minutes"] == 31
    finally:
        conn.close()


def test_a_guard_from_an_earlier_ready_episode_does_not_mask_the_current_hold(board, monkeypatch):
    """An old non-clock guard reason (before this episode's run ended) must not hide the owner-restart hold."""
    from hermes_cli import kanban_chat_wait as cw
    monkeypatch.setenv("HERMES_KANBAN_OWNER_RESTART_COOLDOWN_SECONDS", str(3600))
    conn = kbc.connect()
    try:
        tid = _restart_lost_card(conn, claimed_ago=100 * 60, lost_ago=31 * 60)
        conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES "
                     "(?, 'respawn_guarded', ?, ?)", (tid, json.dumps({"reason": "blocker_auth"}),
                                                       int(time.time()) - 3 * 3600))
        conn.commit()
        assert cw.sweep(conn) == 1
        (notice,) = _notices(conn, tid)
        assert notice["hold_reason"] == "owner_restart_cooldown"
    finally:
        conn.close()


def test_guard_clock_ignores_a_guard_before_the_episode_floor():
    now = 1_794_349_600
    lost = now - 40 * 60
    events = [_ev("created", now - 6 * 3600), _ev("respawn_guarded", now - 5 * 3600, reason="rate_limit_cooldown")]
    cfg = {"rate_limit_cooldown_seconds": 600}
    # positive control: without the floor the old guard and its run-end clock are reported
    assert kd._guard_clock(events, [_run(lost)], cfg) == ("rate_limit_cooldown", lost + 600)
    assert kd._guard_clock(events, [_run(lost)], cfg, since=lost) == (None, None)
