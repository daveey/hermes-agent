"""Restarts (session-outcome contract, lane A2): the backend boot sweep continues a chat a restart interrupted
even when no Desktop reopens it (on by default, existing freshness window), and the orphan reap tells a parent
chat when its subagent was killed mid-work. [daveey]"""

from __future__ import annotations

import json
import time

import pytest

import hermes_state_outcome as so
from hermes_state import SessionDB
from tui_gateway import server
from tui_gateway import startup_resume as sr

DEAD_PID = 2 ** 22 - 3  # never a live process


def _marker(home, key, age):
    path = home / "desktop" / "interrupted_turns.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.loads(path.read_text()) if path.exists() else {}
    data[key] = {"prompt": "finish the migration", "started_at": time.time() - age, "attempts": 0,
                 "auto_continue": True, "writer_pid": DEAD_PID}
    path.write_text(json.dumps(data))


def test_boot_continues_a_fresh_unopened_chat_and_not_a_stale_one(monkeypatch):
    from hermes_cli import config
    from hermes_constants import get_hermes_home
    home = get_hermes_home()
    _marker(home, "fresh-chat", age=60)
    _marker(home, "stale-chat", age=3 * 3600)  # past the default 15 min window
    # Default config apart from the settle delay: resume_on_startup is not set anywhere.
    monkeypatch.setattr(config, "load_config", lambda: {"desktop": {"auto_continue": {
        "resume_on_startup_delay_seconds": 0}}})
    import threading
    stop = threading.Event()
    resumed = []
    def resume(rid, params):
        resumed.append(params["session_id"])
        stop.set()
        return {"result": {"auto_continue": {}}}
    monkeypatch.setitem(server._methods, "session.resume", resume)

    thread = sr.start_startup_resume_sweep(stop=stop)

    assert thread is not None, "the boot sweep must run without resume_on_startup in config"
    thread.join(10)
    assert resumed == ["fresh-chat"]


@pytest.fixture
def db(tmp_path):
    so._reset_hooks_for_tests()
    handle = SessionDB(db_path=tmp_path / "state.db")
    yield handle
    handle.close()
    so._reset_hooks_for_tests()


def _age(db, sid, seconds):
    then = time.time() - seconds
    db._conn.execute("UPDATE sessions SET started_at = ? WHERE id = ?", (then, sid))
    db._conn.execute("UPDATE messages SET timestamp = ? WHERE session_id = ?", (then, sid))
    db._conn.commit()


@pytest.mark.parametrize("last_role, told", [("tool", True), ("assistant", False)])
def test_a_reaped_subagent_mid_work_tells_its_parent(monkeypatch, db, last_role, told):
    db.create_session("parent", source="desktop")
    db.append_message("parent", role="user", content="audit the three repos")
    db.append_message("parent", role="assistant", content="Delegated the audit to a subagent.")
    db.create_session("child", source="subagent", parent_session_id="parent")
    db._conn.execute("UPDATE sessions SET title = 'repo auditor' WHERE id = 'child'")
    db._conn.commit()
    db.append_message("child", role="user", content="audit repo A")
    if last_role == "tool":
        db.append_message("child", role="assistant", content="", tool_calls=[
            {"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}])
        db.append_message("child", role="tool", content="ok", tool_call_id="c1", tool_name="terminal")
    else:
        db.append_message("child", role="assistant", content="Repo A is clean.")
    _age(db, "child", 8 * 3600)
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_SESSION_TTL_S", 6 * 3600)

    assert "child" in server._sweep_orphaned_session_rows()

    notices = [m for m in db.get_messages("parent") if m.get("display_kind") == "kanban_notice"]
    if not told:
        assert notices == []
        return
    [notice] = notices
    assert "subagent repo auditor was stopped by a restart before finishing" in notice["content"]
    stored = so.get_outcome("parent", db=db)
    assert (stored["kind"], stored["reason"]) == ("unresolved", "subagent_killed")


def _killed_child_of(db, parent="parent", child="child"):
    """A parent chat whose delegate child a restart killed mid-tool, aged past the reap TTL."""
    db.create_session(parent, source="desktop")
    db.append_message(parent, role="user", content="audit the three repos")
    db.append_message(parent, role="assistant", content="Delegated the audit to a subagent.")
    db.create_session(child, source="subagent", parent_session_id=parent)
    db._conn.execute("UPDATE sessions SET title = ? WHERE id = ?", ("repo auditor" if child == "child" else f"repo auditor {child}", child))
    db._conn.commit()
    db.append_message(child, role="user", content="audit repo A")
    db.append_message(child, role="assistant", content="", tool_calls=[
        {"id": "c1", "type": "function", "function": {"name": "terminal", "arguments": "{}"}}])
    db.append_message(child, role="tool", content="ok", tool_call_id="c1", tool_name="terminal")
    _age(db, child, 8 * 3600)


def _notices(db, sid="parent"):
    return [m for m in db.get_messages(sid) if m.get("display_kind") == "kanban_notice"]


def test_a_busy_parent_gets_the_killed_subagent_notice_once_its_turn_ends(monkeypatch, db, caplog):
    """desktop-ac1b365935: a parent mid-turn refused the reap's notice; it was logged as a warning with a
    traceback and dropped for good. Now the refusal is a quiet "parent busy, will retry" and the sweeper's retry
    delivers it once the turn ends, exactly once."""
    import logging
    from tui_gateway import reap_notices as rn
    _killed_child_of(db)
    holder = f"pid={__import__('os').getpid()}:turn-in-progress"
    assert db.try_acquire_session_turn_lease("parent", holder)  # the parent chat is mid-turn
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_SESSION_TTL_S", 6 * 3600)

    with caplog.at_level(logging.INFO, logger="tui_gateway.reap_notices"):
        assert "child" in server._sweep_orphaned_session_rows()
    assert _notices(db) == []
    reap_lines = [r for r in caplog.records if r.name == "tui_gateway.reap_notices"]
    assert not [r for r in reap_lines if r.levelno >= logging.WARNING], "an expected busy parent is not a failure"
    assert any("parent busy, will retry" in r.getMessage() and r.exc_info is None for r in reap_lines)

    with caplog.at_level(logging.INFO, logger="tui_gateway.reap_notices"):
        assert rn.retry_pending_killed_notices(db) == []  # still busy: skipped quietly, nothing written
    assert _notices(db) == []
    assert not [r for r in caplog.records if r.name == "tui_gateway.reap_notices" and r.levelno >= logging.WARNING]

    db.release_session_turn_lease("parent", holder)
    assert rn.retry_pending_killed_notices(db) == ["parent"]
    [notice] = _notices(db)
    assert "subagent repo auditor was stopped by a restart before finishing" in notice["content"]
    stored = so.get_outcome("parent", db=db)
    assert (stored["kind"], stored["reason"]) == ("unresolved", "subagent_killed")
    assert rn.retry_pending_killed_notices(db) == []  # delivered: a second pass writes nothing
    assert len(_notices(db)) == 1


def test_a_parent_busy_past_the_overdue_window_logs_one_warning(monkeypatch, db, caplog):
    import logging
    from tui_gateway import reap_notices as rn
    rn._reset_for_tests()
    _killed_child_of(db)
    holder = f"pid={__import__('os').getpid()}:turn-in-progress"
    assert db.try_acquire_session_turn_lease("parent", holder)
    monkeypatch.setattr(server, "_get_db", lambda: db)
    monkeypatch.setattr(server, "_SESSION_TTL_S", 6 * 3600)
    assert "child" in server._sweep_orphaned_session_rows()
    db._conn.execute("UPDATE sessions SET ended_at = ? WHERE id = 'child'", (time.time() - rn.OVERDUE_S - 5,))
    db._conn.commit()

    with caplog.at_level(logging.INFO, logger="tui_gateway.reap_notices"):
        rn.retry_pending_killed_notices(db)
        rn.retry_pending_killed_notices(db)
    warnings = [r for r in caplog.records if r.name == "tui_gateway.reap_notices" and r.levelno >= logging.WARNING]
    assert len(warnings) == 1 and "overdue" in warnings[0].getMessage() and "child" in warnings[0].getMessage()


def test_an_unexpected_notice_failure_still_warns_with_a_traceback(monkeypatch, db, caplog):
    import logging
    from tui_gateway import reap_notices as rn
    _killed_child_of(db)

    def boom(*_a, **_k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(db, "append_kanban_notice", boom)
    monkeypatch.setattr(db, "record_kanban_notice", boom)
    with caplog.at_level(logging.INFO, logger="tui_gateway.reap_notices"):
        assert rn.notify_parents_of_killed_subagents(db, ["child"]) == []
    [warning] = [r for r in caplog.records if r.name == "tui_gateway.reap_notices" and r.levelno >= logging.WARNING]
    assert warning.exc_info is not None and "could not tell parent" in warning.getMessage()


def test_the_retry_ignores_old_reaps_and_children_that_finished(db, caplog):
    import logging
    from tui_gateway import reap_notices as rn
    with caplog.at_level(logging.INFO, logger="tui_gateway.reap_notices"):
        assert rn.retry_pending_killed_notices(None) == []  # no state.db in this process: nothing to do, no noise
    assert not caplog.records
    _killed_child_of(db)
    db._conn.execute("UPDATE sessions SET ended_at = ?, end_reason = 'startup_orphan_reap' WHERE id = 'child'",
                     (time.time() - 25 * 3600,))  # reaped more than a day ago: out of the retry window
    _killed_child_of(db, parent="p2", child="c2")
    db._conn.execute("UPDATE sessions SET ended_at = ?, end_reason = 'agent_close' WHERE id = 'c2'", (time.time(),))
    db._conn.commit()
    assert rn.retry_pending_killed_notices(db) == []
    assert _notices(db) == [] and _notices(db, "p2") == []


def test_the_kanban_unloaded_sweeper_pass_runs_the_retry_before_the_guarantee(monkeypatch, db):
    import threading
    from tui_gateway import kanban_unloaded as ku
    from tui_gateway import notice_fanout, notice_guarantee
    _killed_child_of(db)
    db._conn.execute("UPDATE sessions SET ended_at = ?, end_reason = 'startup_orphan_reap' WHERE id = 'child'",
                     (time.time() - 60,))  # reaped a minute ago; its inline notice was refused
    db._conn.commit()
    monkeypatch.setattr(server, "_get_db", lambda: db)
    stop = threading.Event()
    seen_at_guarantee = []
    for name in ("HERMES_KANBAN_UNLOADED_FIRST_S", "HERMES_KANBAN_UNLOADED_SWEEP_S"):
        monkeypatch.setenv(name, "0" if name.endswith("FIRST_S") else "1")
    monkeypatch.setattr(ku, "sweep_remote_once", lambda *a, **k: [])
    monkeypatch.setattr(ku, "sweep_once", lambda *a, **k: [])
    monkeypatch.setattr(ku, "live_session_ids", lambda: set())
    monkeypatch.setattr(notice_fanout, "card_change_pass", lambda: None)
    monkeypatch.setattr(notice_guarantee, "guarantee_pass",
                        lambda: seen_at_guarantee.append(len(_notices(db))) or stop.set())
    ku._loop(stop)
    assert seen_at_guarantee == [1]
