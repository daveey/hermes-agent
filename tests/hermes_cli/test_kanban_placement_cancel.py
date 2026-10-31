"""Durable cancellation of a placed copy: crash gap, send failures, settle. [daveey]

Archiving or manually completing a card that placement forwarded to another gateway must cancel (archive)
the copy there. ``placement_cancel_intent`` is written in the same txn as the terminal flip, and the
dispatcher sweep (:func:`kanban_placement_forward.sweep_unresolved_archives`) replays a forward that never
ran, resends a failed one with backoff, and gives up after a bounded time. A successful settle never cancels.
"""
from __future__ import annotations

import json
import time

import pytest

from tests.hermes_cli.test_kanban_placement import (
    HUB_TID, _archive_requests, _forwarded_card, _forwarder, _unresolved_forwarded_card,
)

pytest_plugins = ["tests.hermes_cli.test_kanban_placement"]  # its ``mac`` fixture (fake hub + board)


class _Crash(BaseException):
    """The process dies right after the terminal txn committed (before the forward recorded anything)."""


def _crash_in(monkeypatch, name):
    from hermes_cli import kanban_placement_forward as kpf
    real = getattr(kpf, name)
    armed = {"on": True}

    def crashing(*a, **kw):
        if armed["on"]:
            armed["on"] = False
            raise _Crash(name)
        return real(*a, **kw)

    monkeypatch.setattr(kpf, name, crashing)


def _kinds(conn, tid):
    return [r[0] for r in conn.execute("SELECT kind FROM task_events WHERE task_id=? ORDER BY id", (tid,))]


def _statuses(conn, tid):
    from hermes_cli import kanban_placement_forward as kpf
    return [e["status"] for e in kpf._archive_events(conn, tid)]


@pytest.mark.parametrize("rc", [0, 3])
def test_crash_after_archive_commit_is_replayed_by_the_sweep_once(mac, monkeypatch, rc):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement_forward as kpf
    with kbc.connect_closing() as conn:
        child = _forwarded_card(mac, monkeypatch, conn)
        _crash_in(monkeypatch, "forward_archive")
        with pytest.raises(_Crash):
            kb.archive_task(conn, child)
    # Restart: a fresh connection sees the committed archive + intent, and no forward.
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, child).status == "archived"
        assert kpf.CANCEL_INTENT in _kinds(conn, child) and _statuses(conn, child) == []
        assert kpf.sweep_unresolved_archives(conn) == []  # inside the grace: the closer may still be forwarding
        assert _archive_requests(mac) == []
        mac["result"]["rc"] = rc
        later = time.time() + kpf.INTENT_GRACE + 1
        assert kpf.sweep_unresolved_archives(conn, now=later) == [child]
        assert kpf.archive_forward_view(conn, child)["status"] == "pending"
        assert kpf.sweep_unresolved_archives(conn, now=later + 3600) == []  # idempotent
    sent = _archive_requests(mac)
    assert len(sent) == 1 and sent[0][1:6] == ["request", "hub", "tasks", f"default/default/{HUB_TID}", "archive"]
    rid = sent[0][sent[0].index("--id") + 1]
    assert mac["hub"].execute("SELECT state FROM sent WHERE id=?", (rid,)).fetchone()[0] == "sent"


def test_archive_without_crash_is_not_resent_by_the_sweep(mac, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement_forward as kpf
    with kbc.connect_closing() as conn:
        child = _forwarded_card(mac, monkeypatch, conn)
        assert kb.archive_task(conn, child)
        assert kpf.sweep_unresolved_archives(conn, now=time.time() + 86400) == []
    assert len(_archive_requests(mac)) == 1


def test_completion_then_archive_sends_one_request(mac, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement_forward as kpf
    with kbc.connect_closing() as conn:
        child = _forwarded_card(mac, monkeypatch, conn)
        kb.unblock_task(conn, child)
        assert kb.complete_task(conn, child, summary="Manual", force=True)
        assert kb.archive_task(conn, child)
        assert _kinds(conn, child).count(kpf.CANCEL_INTENT) == 2
        for dt in (kpf.INTENT_GRACE + 1, 86400, 8 * 86400):
            assert kpf.sweep_unresolved_archives(conn, now=time.time() + dt) == []
        assert _statuses(conn, child) == ["pending"]
    assert len(_archive_requests(mac)) == 1


@pytest.mark.parametrize("rc", [0, 3])
def test_crash_after_manual_completion_with_delayed_create_cancels_the_late_copy(mac, monkeypatch, rc):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement_forward as kpf
    with kbc.connect_closing() as conn:
        child, rid = _unresolved_forwarded_card(mac, monkeypatch, conn)
        assert kb.unblock_task(conn, child)
        _crash_in(monkeypatch, "forward_completion")
        with pytest.raises(_Crash):
            kb.complete_task(conn, child, summary="Manual override", force=True)
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, child).status == "done"
        later = time.time() + kpf.INTENT_GRACE + 1
        # Replay: the target still has no card, so the cancellation is recorded unresolved, not dropped.
        assert kpf.sweep_unresolved_archives(conn, now=later) == []
        assert _statuses(conn, child) == ["unresolved"]
        assert kpf.sweep_unresolved_archives(conn, now=later + 60) == []
        assert _archive_requests(mac) == []
        # The queued create executes late on the target; the next tick cancels that copy.
        mac["hub"].execute("UPDATE sent SET state='done', result=? WHERE id=?",
                           (json.dumps({"ok": True, "task_id": "t_hub0001", "status": "ready"}), rid))
        mac["hub"].commit()
        mac["result"]["rc"] = rc
        assert kpf.sweep_unresolved_archives(conn, now=later + 120) == [child]
        assert kpf.sweep_unresolved_archives(conn, now=later + 180) == []
    sent = _archive_requests(mac)
    assert len(sent) == 1 and sent[0][1:6] == ["request", "hub", "tasks", "default/default/t_hub0001", "archive"]


def test_crash_after_manual_completion_of_a_running_copy_cancels_it(mac, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement_forward as kpf
    from hermes_cli import kanban_wait_replica as replica
    with kbc.connect_closing() as conn:
        child = _forwarded_card(mac, monkeypatch, conn)
        kb.unblock_task(conn, child)
        _crash_in(monkeypatch, "forward_completion")
        with pytest.raises(_Crash):
            kb.complete_task(conn, child, summary="Manual", force=True)
        monkeypatch.setattr(replica, "check", lambda *a, **kw: (1, f"{HUB_TID} running"))
        assert kpf.sweep_unresolved_archives(conn, now=time.time() + kpf.INTENT_GRACE + 1) == [child]
        assert _statuses(conn, child) == ["pending"]
    assert len(_archive_requests(mac)) == 1


@pytest.mark.parametrize("status", ["done", "archived"])
def test_completion_replay_leaves_a_terminal_remote_copy_alone(mac, monkeypatch, status):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement_forward as kpf
    from hermes_cli import kanban_wait_replica as replica
    with kbc.connect_closing() as conn:
        child = _forwarded_card(mac, monkeypatch, conn)
        kb.unblock_task(conn, child)
        _crash_in(monkeypatch, "forward_completion")
        with pytest.raises(_Crash):
            kb.complete_task(conn, child, summary="Manual", force=True)
        monkeypatch.setattr(replica, "check", lambda *a, **kw: (0, f"{HUB_TID} {status}"))
        later = time.time() + kpf.INTENT_GRACE + 1
        assert kpf.sweep_unresolved_archives(conn, now=later) == []
        assert _statuses(conn, child) == ["skipped"]
        assert kpf.sweep_unresolved_archives(conn, now=later + 86400) == []
        assert _statuses(conn, child) == ["skipped"]
    assert mac["calls"] == []


def test_settle_completion_records_no_cancel_intent_and_sweep_never_cancels(mac, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement_forward as kpf
    with kbc.connect_closing() as conn:
        child = _forwarded_card(mac, monkeypatch, conn)
        kb.unblock_task(conn, child)
        assert _forwarder(conn, child, mac, monkeypatch, check=(0, f"{HUB_TID} done")).run() == "completed"
        assert kpf.CANCEL_INTENT not in _kinds(conn, child)
        for dt in (kpf.INTENT_GRACE + 1, 86400, 8 * 86400):
            assert kpf.sweep_unresolved_archives(conn, now=time.time() + dt) == []
        assert _statuses(conn, child) == []
    assert mac["calls"] == []


def test_a_broken_placement_record_never_blocks_the_archive(mac, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement_forward as kpf
    with kbc.connect_closing() as conn:
        child = _forwarded_card(mac, monkeypatch, conn)
        real_state = kpf.state
        calls = {"n": 0}

        def flaky_state(c, tid):
            calls["n"] += 1
            if calls["n"] == 1:  # the in-txn intent read
                raise RuntimeError("corrupt placement record")
            return real_state(c, tid)

        monkeypatch.setattr(kpf, "state", flaky_state)
        assert kb.archive_task(conn, child)
        assert kb.get_task(conn, child).status == "archived"
        assert kpf.CANCEL_INTENT not in _kinds(conn, child)
        assert _statuses(conn, child) == ["pending"]  # the post-commit forward still ran


def test_failed_archive_send_is_retried_with_backoff_until_it_is_queued(mac, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement as kp
    from hermes_cli import kanban_placement_forward as kpf
    with kbc.connect_closing() as conn:
        child = _forwarded_card(mac, monkeypatch, conn)
        mac["result"]["rc"] = 1
        assert kb.archive_task(conn, child)
        t0 = time.time()
        assert _statuses(conn, child) == ["failed"]
        assert kpf.sweep_unresolved_archives(conn, now=t0 + 10) == []  # backoff
        assert len(_archive_requests(mac)) == 1
        # Second attempt: the hub client itself raises (e.g. missing binary): recorded failed, not lost.
        real = kp.hub_runner
        monkeypatch.setattr(kp, "hub_runner", lambda argv: (_ for _ in ()).throw(OSError("no hermes-hub")))
        assert kpf.sweep_unresolved_archives(conn, now=t0 + kpf.FAILED_RETRY_BASE + 1) == []
        events = kpf._archive_events(conn, child)
        assert [e["status"] for e in events] == ["failed", "failed"] and "no hermes-hub" in events[-1]["error"]
        monkeypatch.setattr(kp, "hub_runner", real)
        mac["result"]["rc"] = 0
        # Two failures: the next retry waits 2x the base.
        assert kpf.sweep_unresolved_archives(conn, now=t0 + kpf.FAILED_RETRY_BASE * 2 - 30) == []
        assert kpf.sweep_unresolved_archives(conn, now=t0 + kpf.FAILED_RETRY_BASE * 2 + 30) == [child]
        assert kpf.archive_forward_view(conn, child)["status"] == "pending"
        assert kpf.sweep_unresolved_archives(conn, now=t0 + 86400) == []
    assert len(_archive_requests(mac)) == 2


def test_failed_retry_backoff_is_capped(mac, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement_forward as kpf
    with kbc.connect_closing() as conn:
        child = _forwarded_card(mac, monkeypatch, conn)
        mac["result"]["rc"] = 1
        assert kb.archive_task(conn, child)
        for _ in range(9):  # 10 failures: uncapped backoff would be 300 * 2**9 s (~43 h)
            kpf.sweep_unresolved_archives(conn, now=time.time() + kpf.FAILED_RETRY_MAX + 1)
        assert _statuses(conn, child).count("failed") == 10
        mac["result"]["rc"] = 0
        assert kpf.sweep_unresolved_archives(conn, now=time.time() + kpf.FAILED_RETRY_MAX - 60) == []
        assert kpf.sweep_unresolved_archives(conn, now=time.time() + kpf.FAILED_RETRY_MAX + 1) == [child]


def test_completion_send_exception_is_recorded_failed_and_retried(mac, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement as kp
    from hermes_cli import kanban_placement_forward as kpf
    with kbc.connect_closing() as conn:
        child, rid = _unresolved_forwarded_card(mac, monkeypatch, conn)
        kb.unblock_task(conn, child)
        real = kp.remote_task_id
        monkeypatch.setattr(kp, "remote_task_id", lambda r: (_ for _ in ()).throw(OSError("replica unreadable")))
        assert kb.complete_task(conn, child, summary="Manual", force=True)
        assert kb.get_task(conn, child).status == "done"
        events = kpf._archive_events(conn, child)
        assert [e["status"] for e in events] == ["failed"] and "replica unreadable" in events[0]["error"]
        monkeypatch.setattr(kp, "remote_task_id", real)
        mac["hub"].execute("UPDATE sent SET state='done', result=? WHERE id=?",
                           (json.dumps({"ok": True, "task_id": "t_hub0001", "status": "ready"}), rid))
        mac["hub"].commit()
        assert kpf.sweep_unresolved_archives(conn, now=time.time() + kpf.FAILED_RETRY_BASE + 1) == [child]
    assert len(_archive_requests(mac)) == 1


def test_failed_archive_send_tries_once_more_then_gives_up_after_seven_days(mac, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement_forward as kpf
    with kbc.connect_closing() as conn:
        child = _forwarded_card(mac, monkeypatch, conn)
        mac["result"]["rc"] = 1
        assert kb.archive_task(conn, child)
        assert kpf.sweep_unresolved_archives(conn, now=time.time() + kpf.ARCHIVE_SWEEP_GIVE_UP + 60) == []
        assert _statuses(conn, child) == ["failed", "failed", "expired"]
        assert any("kept failing" in c.body for c in kb.list_comments(conn, child))
        kpf.sweep_unresolved_archives(conn, now=time.time() + 30 * 86400)
        assert len(_statuses(conn, child)) == 3
    assert len(_archive_requests(mac)) == 2


def test_legacy_failed_without_intent_gives_up_on_its_first_event_clock(mac, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement_forward as kpf
    with kbc.connect_closing() as conn:
        child = _forwarded_card(mac, monkeypatch, conn)
        mac["result"]["rc"] = 1
        assert kb.archive_task(conn, child)
        conn.execute("DELETE FROM task_events WHERE task_id=? AND kind=?", (child, kpf.CANCEL_INTENT))
        conn.execute("UPDATE task_events SET created_at=? WHERE task_id=? AND kind=?",
                     (int(time.time() - 8 * 86400), child, kpf.ARCHIVE_FORWARDED))
        conn.commit()
        kpf._record_failure(conn, child, OSError("still down"))  # a retry that failed just now
        # A last-event clock would never expire while retries keep failing; the first event's does.
        assert kpf.sweep_unresolved_archives(conn) == []
        assert _statuses(conn, child) == ["failed", "failed", "failed", "expired"]


def test_crash_gap_after_a_long_outage_still_sends_before_giving_up(mac, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement_forward as kpf
    with kbc.connect_closing() as conn:
        child = _forwarded_card(mac, monkeypatch, conn)
        _crash_in(monkeypatch, "forward_archive")
        with pytest.raises(_Crash):
            kb.archive_task(conn, child)
        far = time.time() + kpf.ARCHIVE_SWEEP_GIVE_UP + 60
        assert kpf.sweep_unresolved_archives(conn, now=far) == [child]
        assert _statuses(conn, child) == ["pending"]
    assert len(_archive_requests(mac)) == 1


def test_crash_gap_gives_up_after_seven_days_when_every_try_fails(mac, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement_forward as kpf
    with kbc.connect_closing() as conn:
        child = _forwarded_card(mac, monkeypatch, conn)
        _crash_in(monkeypatch, "forward_archive")
        with pytest.raises(_Crash):
            kb.archive_task(conn, child)
        mac["result"]["rc"] = 1
        far = time.time() + kpf.ARCHIVE_SWEEP_GIVE_UP + 60
        assert kpf.sweep_unresolved_archives(conn, now=far) == []
        assert _statuses(conn, child) == ["failed"]
        assert kpf.sweep_unresolved_archives(conn, now=far + 1) == []
        assert _statuses(conn, child) == ["failed", "failed", "expired"]
        assert kpf.sweep_unresolved_archives(conn, now=far + 30 * 86400) == []
        assert len(_statuses(conn, child)) == 3


def test_unresolved_archive_whose_late_send_fails_is_not_reported_sent(mac, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement_forward as kpf
    with kbc.connect_closing() as conn:
        child, rid = _unresolved_forwarded_card(mac, monkeypatch, conn)
        assert kb.archive_task(conn, child)
        assert _statuses(conn, child) == ["unresolved"]
        mac["hub"].execute("UPDATE sent SET state='done', result=? WHERE id=?",
                           (json.dumps({"ok": True, "task_id": "t_hub0001", "status": "ready"}), rid))
        mac["hub"].commit()
        mac["result"]["rc"] = 1
        t0 = time.time()
        assert kpf.sweep_unresolved_archives(conn, now=t0) == []  # failed: not reported sent
        assert _statuses(conn, child) == ["unresolved", "failed"]
        mac["result"]["rc"] = 0
        assert kpf.sweep_unresolved_archives(conn, now=t0 + kpf.FAILED_RETRY_BASE + 1) == [child]
        assert _statuses(conn, child) == ["unresolved", "failed", "pending"]
    assert len(_archive_requests(mac)) == 2
