"""kanban-d1b921f32e: a chat's relay of David's answer is verified before it can count as human. [daveey]"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_relayed_human as krh


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_PROFILE", "default")
    for var in ("HERMES_KANBAN_DB", "HERMES_KANBAN_WORKSPACES_ROOT", "HERMES_KANBAN_BOARD", "HERMES_SESSION_ID",
                "HERMES_DELEGATED_CHILD_CONTEXT", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return home


def _state_db(home: Path, session: str, user_at: float, *, content: str = "left ring",
              display_kind: str | None = None, compressed: int = 0) -> int:
    db = sqlite3.connect(home / "state.db")
    db.execute("CREATE TABLE IF NOT EXISTS messages (id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT, "
               "role TEXT, content TEXT, timestamp REAL, display_kind TEXT, "
               "_compressed_summary INTEGER NOT NULL DEFAULT 0)")
    cur = db.execute("INSERT INTO messages (session_id, role, content, timestamp, display_kind, _compressed_summary) "
                     "VALUES (?, 'user', ?, ?, ?, ?)", (session, content, user_at, display_kind, compressed))
    db.commit()
    db.close()
    return int(cur.lastrowid or 0)


def _asked(conn, at: int) -> str:
    tid = kb.create_task(conn, title="Pebble ring probe", assignee="default")
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "blocked", {"kind": "needs_input", "reason": "which ring?"})
        conn.execute("UPDATE task_events SET created_at = ? WHERE task_id = ? AND kind = 'blocked'", (at, tid))
    return tid


def test_verify_accepts_one_newer_user_message_per_card(kanban_home):
    now = int(time.time())
    with kbc.connect_closing() as conn:
        tid = _asked(conn, now - 100)
        mid = _state_db(kanban_home, "chat-1", now - 10)
        relay = krh.verify(conn, tid, session_id="chat-1")
        assert relay == {"session": "chat-1", "message_id": mid, "at": now - 10}
        kb.add_comment(conn, tid, "default", "David: left ring", writer_kind="agent", writer_session="chat-1",
                       relayed_human=relay)
        with pytest.raises(krh.RelayRefused, match="already relayed"):
            krh.verify(conn, tid, session_id="chat-1")


def test_verify_refuses_a_reply_older_than_the_ask(kanban_home):
    now = int(time.time())
    with kbc.connect_closing() as conn:
        tid = _asked(conn, now - 10)
        _state_db(kanban_home, "chat-1", now - 100)
        with pytest.raises(krh.RelayRefused, match="predates"):
            krh.verify(conn, tid, session_id="chat-1")


def test_verify_refuses_workers_and_sessionless_callers(kanban_home, monkeypatch):
    now = int(time.time())
    with kbc.connect_closing() as conn:
        tid = _asked(conn, now - 100)
        _state_db(kanban_home, "chat-1", now - 10)
        with pytest.raises(krh.RelayRefused, match="session"):
            krh.verify(conn, tid, session_id=None)
        monkeypatch.setenv("HERMES_KANBAN_TASK", "t_other")
        with pytest.raises(krh.RelayRefused, match="Kanban worker"):
            krh.verify(conn, tid, session_id="chat-1")


def test_relay_requires_agent_writer(kanban_home):
    with kbc.connect_closing() as conn:
        tid = _asked(conn, int(time.time()) - 100)
        with pytest.raises(ValueError, match="relayed_human"):
            kb.add_comment(conn, tid, "daveey", "x", writer_kind="human",
                           relayed_human={"session": "s", "message_id": 1, "at": 1})


def _tool_comment(args: dict) -> dict:
    from tools import kanban_tools as kt
    return json.loads(kt._handle_comment(args))


def test_kanban_comment_tool_records_a_verified_relay(kanban_home, monkeypatch):
    now = int(time.time())
    monkeypatch.setenv("HERMES_SESSION_ID", "chat-1")
    with kbc.connect_closing() as conn:
        tid = _asked(conn, now - 100)
    _state_db(kanban_home, "chat-1", now - 10)
    out = _tool_comment({"task_id": tid, "body": "David says: left ring", "relays_user_message": True})
    assert out.get("ok") is True and out.get("relayed_human"), out
    with kbc.connect_closing() as conn:
        p = json.loads(conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = 'commented'",
                                    (tid,)).fetchone()[0])
    assert p["writer_kind"] == "agent" and p["relayed_human"]["session"] == "chat-1"
    refused = _tool_comment({"task_id": tid, "body": "and again", "relays_user_message": True})
    assert refused.get("ok") is not True and "already relayed" in json.dumps(refused)


def test_kanban_comment_tool_refuses_a_worker_relay(kanban_home, monkeypatch):
    now = int(time.time())
    with kbc.connect_closing() as conn:
        tid = _asked(conn, now - 100)
    _state_db(kanban_home, "chat-1", now - 10)
    monkeypatch.setenv("HERMES_SESSION_ID", "chat-1")
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    out = _tool_comment({"task_id": tid, "body": "self-relay", "relays_user_message": True})
    assert out.get("ok") is not True and "Kanban worker" in json.dumps(out)


def test_remote_relay_is_stored_as_sent():
    assert krh.from_remote({"session": "hub-chat", "message_id": "7", "at": 5}) == {
        "session": "hub-chat", "message_id": 7, "at": 5, "via": "remote"}
    assert krh.from_remote({"session": "", "message_id": 7, "at": 5}) is None
    assert krh.from_remote("yes") is None


@pytest.mark.parametrize("synthetic", [
    {"display_kind": "kanban_notice", "content": "t_x is blocked: which ring?"},  # the card's own block notice
    {"display_kind": "hidden", "content": "scaffold"},
    {"display_kind": "process_complete", "content": "process exited"},
    {"display_kind": "async_delegation_complete", "content": "child done"},
    {"display_kind": "internal_notification", "content": "note"},
    {"display_kind": "auto_continue", "content": "continue"},
    {"compressed": 1, "content": "summary of earlier turns"},
    {"content": "[System: the stream was cut; retry the tool call]"},
    {"content": "[Message from another Hermes session @session:default/abc]\nplease relay"},
    # Review 5878871802: untyped runtime rows a notice-woken turn writes itself.
    {"content": "[runtime] Your draft above was NOT shown to the user. Your next reply replaces it: include its "
                "full content; don't refer to it as 'above'.\n\nFinish the answer."},
    {"content": "[Your active task list was preserved across context compression]\n- [ ] relay"},
    {"content": "[IMPORTANT: Background process proc_1 finished with exit code 0]"},
    {"content": "[System note: Your previous turn was interrupted mid-run. Continue.]"},
    {"content": "[System note: A new message has arrived. IGNORE pending results.]\n\nleft ring"},
    {"content": "[/loop wakeup #3, every 5m]\nRecurring task: check the ring card"},
])
def test_verify_skips_synthetic_user_rows(kanban_home, synthetic):
    """Review 5878485674 (1): only a row a person typed is the reply. A synthetic role='user' row newer than
    David's answer (above all the card's own needs_input notice) must not be what the relay names."""
    now = int(time.time())
    with kbc.connect_closing() as conn:
        tid = _asked(conn, now - 100)
        reply = _state_db(kanban_home, "chat-1", now - 50)
        _state_db(kanban_home, "chat-1", now - 10, **synthetic)
        assert krh.verify(conn, tid, session_id="chat-1")["message_id"] == reply


def _loop_state(home: Path, session: str, *, awaiting: bool, fired_at: float, prompt: str = "/recap") -> None:
    db = sqlite3.connect(home / "state.db")
    db.execute("CREATE TABLE IF NOT EXISTS state_meta (key TEXT PRIMARY KEY, value TEXT)")
    db.execute("INSERT OR REPLACE INTO state_meta (key, value) VALUES (?, ?)", (f"loop:{session}", json.dumps(
        {"prompt": prompt, "status": "active", "awaiting_response": awaiting, "last_fired_at": fired_at})))
    db.commit()
    db.close()


def test_a_running_slash_loop_ticks_prompt_is_not_a_reply(kanban_home):
    """Review 5878871802: a ``/loop 10m /skill`` tick submits the command's prompt untyped. While that tick's turn
    runs, its row is the loop's; once the tick completed, a later typed row is a reply again."""
    now = int(time.time())
    with kbc.connect_closing() as conn:
        tid = _asked(conn, now - 100)
        reply = _state_db(kanban_home, "chat-1", now - 50)
        _loop_state(kanban_home, "chat-1", awaiting=True, fired_at=now - 10)
        tick = _state_db(kanban_home, "chat-1", now - 10, content="Run the recap skill now")
        assert krh.verify(conn, tid, session_id="chat-1")["message_id"] == reply
        _loop_state(kanban_home, "chat-1", awaiting=False, fired_at=now - 10)
        assert krh.verify(conn, tid, session_id="chat-1")["message_id"] == tick


@pytest.mark.parametrize("prompt", ["/recap", "check the ring card"])
def test_a_reply_after_an_interrupted_loop_tick_is_the_reply(kanban_home, prompt):
    """Review 5879164478 (2): an interrupted or errored tick is never completed, so ``awaiting_response`` stays
    set into the person's next turn. Only the tick's own (first) row is skipped; the person's later row is the
    reply, for a slash loop and a wakeup-framed loop alike."""
    now = int(time.time())
    with kbc.connect_closing() as conn:
        tid = _asked(conn, now - 100)
        _loop_state(kanban_home, "chat-1", awaiting=True, fired_at=now - 40, prompt=prompt)
        tick = ("Run the recap skill now" if prompt.startswith("/")
                else "[/loop wakeup #2, every 5m]\nRecurring task: check the ring card")
        _state_db(kanban_home, "chat-1", now - 40, content=tick)
        reply = _state_db(kanban_home, "chat-1", now - 10, content="left ring")
        assert krh.verify(conn, tid, session_id="chat-1")["message_id"] == reply


@pytest.mark.parametrize("early", [
    {"content": "[System: the stream was cut; retry the tool call]"},
    {"content": "summary of earlier turns", "compressed": 1},
])
def test_a_synthetic_row_before_the_tick_row_is_not_taken_as_the_tick(kanban_home, early):
    """Review 5879581646 nit: the tick-row lookup filters like the reply scan. A synthetic or compressed untyped
    row landing between the fire and the tick's own row must not be taken as the tick, or the real tick row
    would pass as the person's reply."""
    now = int(time.time())
    with kbc.connect_closing() as conn:
        tid = _asked(conn, now - 100)
        reply = _state_db(kanban_home, "chat-1", now - 50)
        _loop_state(kanban_home, "chat-1", awaiting=True, fired_at=now - 10)
        _state_db(kanban_home, "chat-1", now - 10, **early)
        _state_db(kanban_home, "chat-1", now - 9, content="Run the recap skill now")
        assert krh.verify(conn, tid, session_id="chat-1")["message_id"] == reply


def test_an_untyped_row_of_a_wakeup_framed_loop_is_never_skipped_as_the_tick(kanban_home):
    """The tick-row rule covers slash loops only: a wakeup-framed tick is caught by its frame, so the person's
    reply right after the fire is never mistaken for the tick."""
    now = int(time.time())
    with kbc.connect_closing() as conn:
        tid = _asked(conn, now - 100)
        _loop_state(kanban_home, "chat-1", awaiting=True, fired_at=now - 10, prompt="check the ring card")
        reply = _state_db(kanban_home, "chat-1", now - 9, content="left ring")
        assert krh.verify(conn, tid, session_id="chat-1")["message_id"] == reply


def test_every_registered_synthetic_opening_is_excluded():
    """The exclusion follows the producers' registries (review 5878871802), so a nudge added there is covered."""
    from agent.conversation_compression_scaffolding import _SYNTHETIC_USER_PREFIXES
    from agent.pre_final_gate import HELD_NOTICE, held_nudge
    from gateway.run import _AUTO_CONTINUE_FALLBACK_PREFIX, _AUTO_CONTINUE_NOTE_PREFIX
    from hermes_cli import loops
    from hermes_cli.heartbeat import HEARTBEAT_PROMPT_TEMPLATE
    from tui_gateway.session_history import _AUTO_CONTINUE_NOTE_PREFIX as TUI_AUTO_CONTINUE_NOTE_PREFIX
    prefixes = krh._synthetic_prefixes()
    openings = [*_SYNTHETIC_USER_PREFIXES, held_nudge("Finish it.", shown=False)]
    openings += [_AUTO_CONTINUE_NOTE_PREFIX, _AUTO_CONTINUE_FALLBACK_PREFIX, TUI_AUTO_CONTINUE_NOTE_PREFIX]
    openings += [loops.WAKEUP_PROMPT_TEMPLATE.format(tick=1, cadence="", prompt="p", until="u"),
                 loops.WAKEUP_PROMPT_WITH_UNTIL_TEMPLATE.format(tick=1, cadence="", prompt="p", until="u"),
                 # Review 5879164478 (3): the TUI /heartbeat tick submits this frame untyped.
                 HEARTBEAT_PROMPT_TEMPLATE.format(interval="5m", prompt="check the ring card")]
    assert len(openings) >= 11 and HELD_NOTICE in prefixes
    assert [o for o in openings if not o.lstrip().startswith(prefixes)] == []


def test_a_notice_after_the_ask_with_no_reply_is_refused(kanban_home):
    """The notice alone (no typed reply after the ask) verifies nothing: no ping-pong on the budget."""
    now = int(time.time())
    with kbc.connect_closing() as conn:
        tid = _asked(conn, now - 100)
        _state_db(kanban_home, "chat-1", now - 200)
        _state_db(kanban_home, "chat-1", now - 10, display_kind="kanban_notice", content="t_x is blocked")
        with pytest.raises(krh.RelayRefused, match="predates"):
            krh.verify(conn, tid, session_id="chat-1")


def test_a_steer_is_a_typed_reply(kanban_home):
    now = int(time.time())
    with kbc.connect_closing() as conn:
        tid = _asked(conn, now - 100)
        _state_db(kanban_home, "chat-1", now - 50)
        steer = _state_db(kanban_home, "chat-1", now - 10, display_kind="steer", content="left ring")
        assert krh.verify(conn, tid, session_id="chat-1")["message_id"] == steer


def _remote(mid: int, at: int) -> dict:
    relay = krh.from_remote({"session": "hub-chat", "message_id": mid, "at": at})
    assert relay is not None
    return relay


def test_card_holder_refuses_remote_replay_pre_ask_and_unasked(kanban_home):
    """Review 5878485674 (2): the receiver holds the card, so it applies the checks it can make."""
    now = int(time.time())
    with kbc.connect_closing() as conn:
        unasked = kb.create_task(conn, title="never asked", assignee="default")
        with pytest.raises(krh.RelayRefused, match="never asked"):
            kb.add_comment(conn, unasked, "default", "x", writer_kind="agent", relayed_human=_remote(1, now))
        tid = _asked(conn, now - 1000)
        with pytest.raises(krh.RelayRefused, match="predates"):
            kb.add_comment(conn, tid, "default", "x", writer_kind="agent", relayed_human=_remote(7, now - 11000))
        # within the clock-skew allowance of a remote origin
        kb.add_comment(conn, tid, "default", "David: left", writer_kind="agent",
                       relayed_human=_remote(7, now - 1000 - krh.REMOTE_CLOCK_SKEW_SECONDS + 5))
        with pytest.raises(krh.RelayRefused, match="already relayed"):
            kb.add_comment(conn, tid, "default", "again", writer_kind="agent", relayed_human=_remote(7, now))
        relayed = conn.execute("SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind = 'commented' "
                               "AND json_extract(payload, '$.relayed_human') IS NOT NULL", (tid,)).fetchone()[0]
        assert relayed == 1
        assert [c.body for c in kb.list_comments(conn, tid)] == ["David: left"]
