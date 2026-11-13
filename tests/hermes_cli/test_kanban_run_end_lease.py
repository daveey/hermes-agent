"""A run the holder ends itself frees its Board lease while this host's push loop is down. [daveey]

t_01a1d05e8be13a6a (David-MBP 2026-11-13): with sync down on SchemaAhead, two runs that parked
normally kept their Board lease (only the pushed ``scheduled`` row frees it, and it never left the
outbox); at expiry the Board reaped each as a crash (``stale_lease=macbook``) and the second tripped
the retry breaker. The Board service is a stub (kanban_sync_support.FakeLeaseClient).
"""

from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from tests.hermes_cli import kanban_sync_support as support


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    return support.kanban_home(tmp_path, monkeypatch)


@pytest.fixture
def lease_client():
    yield from support.lease_client()


@pytest.fixture
def synced_conn(kanban_home):
    yield from support.synced_conn(kanban_home)


@pytest.fixture
def primary(synced_conn):
    sync.set_mode(synced_conn, "primary")
    return synced_conn


def _claimed(conn, lease_client, title="parks"):
    tid = kb.create_task(conn, title=title, assignee="worker")
    assert kb.claim_task(conn, tid) is not None
    run_uid = lease_client.calls[-1][1]["run_uid"]
    assert lease_client.calls[-1][0] == "acquire"
    return tid, run_uid


def _stall_outbox(conn):
    """The push loop is down: everything queued so far has sat in the outbox for ten minutes."""
    conn.execute("UPDATE kanban_sync_outbox SET created_at = created_at - 600000")
    conn.commit()


def _releases(lease_client):
    return [req for kind, req in lease_client.calls if kind == "release"]


def test_schedule_releases_the_run_lease_while_the_outbox_is_stalled(primary, lease_client):
    tid, run_uid = _claimed(primary, lease_client)
    _stall_outbox(primary)
    assert kb.schedule_task(primary, tid, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z", clock_only=True)
    [req] = _releases(lease_client)
    assert req["task_uid"] == tid and req["run_uid"] == run_uid


@pytest.mark.parametrize("end", ["block", "complete", "review", "continue"])
def test_block_complete_review_and_continue_release_the_run_lease(primary, lease_client, end):
    tid, run_uid = _claimed(primary, lease_client)
    _stall_outbox(primary)
    if end == "block":
        assert kb.block_task(primary, tid, reason="needs an answer", kind="time", clock_only=True)
    elif end == "complete":
        assert kb.complete_task(primary, tid, summary="done")
    elif end == "review":
        assert kb.request_review(primary, tid, summary="please review")
    else:
        from hermes_cli import kanban_continuation
        assert kanban_continuation.continue_task(primary, tid, checkpoint="halfway")["ok"]
    assert [r["run_uid"] for r in _releases(lease_client)] == [run_uid]


@pytest.mark.parametrize("error", [sync.LeaseUnavailable("connection refused"), RuntimeError("bad answer")])
def test_a_failed_release_leaves_the_park_intact(primary, lease_client, monkeypatch, error):
    tid, run_uid = _claimed(primary, lease_client)
    _stall_outbox(primary)

    def release(**kw):
        lease_client.calls.append(("release", kw))
        raise error

    monkeypatch.setattr(lease_client, "release", release)
    assert kb.schedule_task(primary, tid, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z", clock_only=True)
    assert [r["run_uid"] for r in _releases(lease_client)] == [run_uid]
    task = kb.get_task(primary, tid)
    assert task.status == "scheduled"
    run = primary.execute("SELECT outcome, ended_at FROM task_runs WHERE uid = ?", (run_uid,)).fetchone()
    assert run["outcome"] == "scheduled" and run["ended_at"] is not None


def test_a_draining_outbox_leaves_the_release_to_the_row_push(primary, lease_client):
    """Board releaseTask turns a card it still sees running back to ready: on a healthy host the pushed
    row ends the lease itself, so a direct release would only open a window for another machine's claim."""
    tid, _run_uid = _claimed(primary, lease_client)
    assert kb.schedule_task(primary, tid, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z", clock_only=True)
    assert _releases(lease_client) == []


def test_a_crash_end_does_not_send_a_neutral_release(primary, lease_client):
    """Crash accounting releases with its own outcome; a neutral release sent first would hide the charge."""
    tid, _run_uid = _claimed(primary, lease_client)
    _stall_outbox(primary)
    with kb.write_txn(primary):
        kb._end_run(primary, tid, outcome="crashed")
    assert _releases(lease_client) == []


def test_a_rolled_back_end_keeps_the_lease(primary, lease_client):
    tid, _run_uid = _claimed(primary, lease_client)
    _stall_outbox(primary)
    with pytest.raises(RuntimeError):
        with kb.write_txn(primary):
            run_id = kb._end_or_synthesize_run(primary, tid, outcome="scheduled", status="scheduled",
                                               synthesize=False)
            assert run_id is not None
            raise RuntimeError("abort")
    assert _releases(lease_client) == []
    # The next txn on the same connection does not inherit the dropped queue.
    with kb.write_txn(primary):
        primary.execute("SELECT 1")
    assert _releases(lease_client) == []
