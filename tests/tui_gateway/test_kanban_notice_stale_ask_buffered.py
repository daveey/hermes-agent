"""A block notice buffered in the tui poller's ``_kanban_pending`` re-reads its card when the batch is delivered.

The legacy tui_gateway path renders a notice when it is claimed, then holds it in ``_kanban_pending`` until the
chat's running turn ends. PR #1260 refreshed a stale "Next action for the user" ask only on the gateway admission
path (``refresh_payload``), so a notice buffered here behind a busy turn still asked the user to decide a card
that had since finished (review N3 of t_01a1ce7ed2addb09). [daveey]
"""

import contextlib
import threading

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as kbn
from hermes_state import SessionDB
from tui_gateway import server

KEY = "20261112_101500_ab12cd"
ASK = "Next action for the user"
REQUEST = dict(action="Reply 'accept offline coverage' or 'require Docker proof'. Unblock = apply it; "
                      "archive = drop the remaining acceptance work.",
               why_human="Accepting offline coverage changes the acceptance criterion.", interaction="reply",
               scope="Only the standalone Docker integration acceptance.", evidence="No Docker endpoint on either host.")


def _session(running: bool) -> dict:
    return {"session_key": KEY, "history_lock": threading.RLock(), "running": running}


@pytest.fixture
def gate(monkeypatch):
    state = {"submitted": []}

    def _submit(rid, sid, session, text, **kwargs):
        state["submitted"].append(text)
        with session["history_lock"]:
            session["running"] = False
        return True

    monkeypatch.setattr(server, "_run_prompt_submit", _submit)
    monkeypatch.setattr(server, "_emit", lambda *a, **k: None)
    return state


@pytest.fixture
def sdb(tmp_path, monkeypatch):
    d = SessionDB(tmp_path / "state.db")
    d.create_session(KEY, "desktop")
    d.append_message(KEY, "user", "run the drill")

    @contextlib.contextmanager
    def _opener(session):
        yield d

    monkeypatch.setattr(server, "_session_db", _opener)
    return d


def _blocked_card() -> str:
    with contextlib.closing(kbc.connect()) as conn:
        tid = kb.create_task(conn, title="state CI fixture", assignee="default")
        kbn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id=KEY)
        assert kb.block_task(conn, tid, kind="needs_input", reason="Choose coverage", human_request=dict(REQUEST))
        return tid


def _finish(tid: str) -> None:
    with contextlib.closing(kbc.connect()) as conn:
        assert kb.unblock_task(conn, tid)
        assert kb.complete_task(conn, tid, result="fixed")


def _buffer_then_deliver(gate, sdb, then):
    """Claim the block notice while a turn runs, run ``then``, end the turn; return (buffered, row, submitted)."""
    tid = _blocked_card()
    busy = _session(running=True)
    server._notif_poll_kanban_scoped("ui", busy)
    assert gate["submitted"] == [], "the chat is busy: the notice waits in the buffer"
    pending = list(busy["_kanban_pending"])
    buffered = next(t for t in pending if ASK in t)
    assert f"Open task: @task:default/{tid}" in buffered
    attrs = {k: getattr(buffered, k, None) for k in ("_kb_keys", "_kb_blocked", "_kb_remote")}
    then(tid)
    # Only the already-buffered notice is delivered here: later events are not claimed while backing off.
    busy["_kanban_refused_until"] = float("inf")
    with busy["history_lock"]:
        busy["running"] = False
    server._notif_poll_kanban_scoped("ui", busy)
    assert len(gate["submitted"]) == 1, "the buffered batch runs as one turn once the chat is idle"
    assert {k: getattr(buffered, k, None) for k in attrs} == attrs, "the buffered text keeps its identity"
    rows = [m for m in sdb.get_messages(KEY) if m.get("display_kind") == "kanban_notice"]
    assert len(rows) == 1
    return "\n".join(pending), rows[0]["content"], gate["submitted"][0]


def test_buffered_block_notice_for_a_finished_card_drops_the_ask(gate, sdb):
    original, row, submitted = _buffer_then_deliver(gate, sdb, _finish)
    assert ASK not in submitted, "the card finished while the notice waited: no decision is left to ask for"
    assert "Why only the user" not in submitted
    assert "Since then: unblocked" in submitted and "completed" in submitted and "No action needed." in submitted
    assert "Scope: Only the standalone Docker integration acceptance." in submitted
    assert row == submitted, "the transcript row and the turn carry the same refreshed text"
    assert original.split(ASK)[0] in submitted, "the notice itself stays"


def test_buffered_block_notice_for_a_still_blocked_card_is_byte_identical(gate, sdb):
    original, row, submitted = _buffer_then_deliver(gate, sdb, lambda tid: None)
    assert submitted == original
    assert row == original
    assert f"{ASK}: {REQUEST['action']}" in submitted
