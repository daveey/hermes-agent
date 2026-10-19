"""A Bot Chat delivery refused because another delivery holds the session is queued, not lost.

Two cron jobs of one profile, both ``deliver: bot-chat``, fired in the same minute: the
second ``hermes chat -c 'Bot Chat' -Q`` child exited 1 with
``hermes-refusal-reason: SESSION_NOT_OWNED`` while the first child held the session lease,
and the result was recorded as a final failure and never posted. The refusal happens at
admission (before the query is read), so the turn never ran and replaying it is safe.
"""
import subprocess

import pytest

from cron import bot_chat_delivery as queue
from cron import scheduler_delivery as delivery
from hermes_state import SessionDB


def _refused(reason):
    return subprocess.CompletedProcess(
        [], 1, "", f"hermes-refusal-reason: {reason}\nBot Chat already has a live owner\n")


@pytest.fixture()
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(db_path=tmp_path / "state.db")
    db.create_session(session_id="chat", source="cli")
    db.set_session_title("chat", "Bot Chat")
    db.close()
    monkeypatch.setattr(delivery.shutil, "which", lambda _: "/bin/hermes")
    return tmp_path


@pytest.mark.parametrize("reason", ["SESSION_NOT_OWNED", "MAX_CONCURRENT_SESSIONS"])
def test_busy_refusal_queues_then_settles_once(home, monkeypatch, reason):
    posted, outcomes = [], [_refused(reason), _refused(reason)]

    def run(argv, env, report_path, timeout):
        with open(argv[argv.index("--query-file") + 1], encoding="utf-8") as fh:
            text = fh.read()
        result = outcomes.pop(0) if outcomes else subprocess.CompletedProcess([], 0, "ok", "")
        if result.returncode == 0:
            posted.append(text)
        return result

    monkeypatch.setattr(delivery, "_run_bot_chat_turn", run)
    job = {"id": "weekly", "name": "weekly research", "execution_id": "exec-2"}

    error = delivery._deliver_to_bot_chat(job, "research output", "")
    receipt = job["_bot_chat_delivery_receipts"]["bot-chat:(own)"]
    assert receipt["status"] == "queued"
    assert "queued" in error and "do not resend" in error
    key = receipt["delivery_id"]
    assert queue.read_pending(key)["status"] == "queued"

    # A drain while the session is still held: refused again, the record goes back to
    # queued (not ambiguous/claimed) so a later drain can still post it.
    queue.drain()
    record = queue.read_pending(key)
    assert record["status"] == "queued" and record.get("busy_attempts") == 1
    assert posted == []

    queue.drain()  # the first delivery released the session
    assert queue.read_pending(key)["status"] == "settled"
    assert len(posted) == 1 and posted[0].endswith("research output")

    # Never posted twice: neither a later drain nor a repeat of the same execution resends.
    queue.drain()
    job.pop("_bot_chat_delivery_receipts")
    assert delivery._deliver_to_bot_chat(job, "research output", "") is None
    assert len(posted) == 1


def test_other_failures_stay_final(home, monkeypatch):
    monkeypatch.setattr(delivery, "_run_bot_chat_turn", lambda *a: subprocess.CompletedProcess(
        [], 1, "", "hermes-refusal-reason: SESSION_COORDINATION_UNAVAILABLE\nregistry unreadable\n"))
    job = {"id": "job", "execution_id": "exec"}
    error = delivery._deliver_to_bot_chat(job, "output", "")
    assert "could not deliver this result" in error
    assert "_bot_chat_delivery_receipts" not in job
    assert not queue._root().is_dir() or not list(queue._root().glob("*.json"))


def test_busy_requeue_is_bounded(home, monkeypatch):
    monkeypatch.setattr(delivery, "_run_bot_chat_turn", lambda *a: _refused("SESSION_NOT_OWNED"))
    monkeypatch.setattr(queue, "MAX_BUSY_ATTEMPTS", 2)
    job = {"id": "job", "execution_id": "exec"}
    delivery._deliver_to_bot_chat(job, "output", "")
    key = job["_bot_chat_delivery_receipts"]["bot-chat:(own)"]["delivery_id"]
    queue.drain()
    assert queue.read_pending(key)["status"] == "queued"
    queue.drain()
    record = queue.read_pending(key)
    assert record["status"] == "ambiguous" and "SESSION_NOT_OWNED" in record["error"]
