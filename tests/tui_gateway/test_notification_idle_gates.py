"""Idle poller proofs: unchanged readers do no application query. [daveey]"""
import contextlib
import threading

import pytest


@pytest.fixture
def idle_session(tmp_path, monkeypatch):
    from hermes_cli import goals
    from hermes_state import SessionDB
    from tui_gateway import server

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    db = SessionDB(tmp_path / "state.db")
    db.create_session("idle-proof", "tui")
    monkeypatch.setattr(goals, "_get_session_db", lambda: db)
    monkeypatch.setattr(server, "_session_db", lambda session: contextlib.nullcontext(db))
    session = {"session_key": "idle-proof", "history_lock": threading.RLock(), "running": False}
    yield server, db, session
    db.close()


@pytest.mark.parametrize("kind", ["loop", "heartbeat"])
def test_unchanged_schedule_does_not_query(idle_session, monkeypatch, kind):
    server, db, session = idle_session
    fire = getattr(server, f"_maybe_fire_tui_{kind}_tick")
    fire("idle", session)
    reads = []
    def forbidden(*args, **kwargs):
        reads.append(args)
        raise AssertionError("unchanged schedule queried state_meta")
    monkeypatch.setattr(db, "get_meta", forbidden)
    fire("idle", session)
    assert reads == []


@pytest.mark.parametrize("kind", ["loop", "heartbeat"])
def test_schedule_commit_invalidates_empty_proof(idle_session, monkeypatch, kind):
    import time
    from hermes_cli.heartbeat import HeartbeatManager, save_heartbeat
    from hermes_cli.loops import LoopManager, save_loop
    server, _db, session = idle_session
    fire = getattr(server, f"_maybe_fire_tui_{kind}_tick")
    fire("idle", session)
    if kind == "loop":
        mgr = LoopManager(session["session_key"])
        mgr.set("idle proof wake", interval_seconds=60)
        assert mgr.state is not None
        mgr.state.next_due_at = time.time() - 1
        save_loop(session["session_key"], mgr.state)
    else:
        mgr = HeartbeatManager(session["session_key"])
        state = mgr.set("idle proof wake", 60)
        state.created_at = time.time() - 3600
        save_heartbeat(session["session_key"], state)
    sent = []
    monkeypatch.setattr(server, "_notif_gateway_owns_heartbeat", lambda *a: False)
    monkeypatch.setattr(server, "_notif_owner_preflight", lambda *a: True)
    monkeypatch.setattr(server, "_publish_session_control_snapshot", lambda *a: None)
    monkeypatch.setattr(server, "_emit", lambda *a: None)
    monkeypatch.setattr(server, "_notif_run_submit", lambda *a, **kw: sent.append(a[3]) or True)
    fire("idle", session)
    assert len(sent) == 1
    assert "idle proof wake" in sent[0]


def test_unchanged_empty_producer_does_not_query(idle_session, monkeypatch):
    from tui_gateway import producer_admission
    _server, db, session = idle_session
    assert producer_admission.deliver_pending("idle", session) == "none"
    reads = []
    def forbidden(*args, **kwargs):
        reads.append(args)
        raise AssertionError("unchanged producer queried sessions")
    monkeypatch.setattr(db, "get_session", forbidden)
    producer_admission.deliver_pending("idle", session)
    assert reads == []


def test_producer_commit_invalidates_empty_proof(idle_session, monkeypatch):
    import hermes_state_producer_admission as pa
    from tui_gateway import producer_admission
    server, db, session = idle_session
    assert producer_admission.deliver_pending("idle", session) == "none"
    receipt = pa.admit_producer_completion(
        producer='["user-terminal","idle-proof","final"]', profile_home=db.db_path.parent,
        stored_session_id=session["session_key"], text="idle proof completion")
    assert receipt["outcome"] == "admitted"
    sent = []
    monkeypatch.setattr(server, "_kb_owner_refusal", lambda *a: None)
    monkeypatch.setattr(server, "_ensure_active_session_slot", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_notif_submit", lambda *a, **kw: sent.append(a[3]) or True)
    assert producer_admission.deliver_pending("idle", session) == "started"
    assert sent == ["idle proof completion"]


def test_empty_kanban_poll_does_not_bind_provider_scope(idle_session, monkeypatch):
    from hermes_cli import kanban_db_connect
    server, _db, session = idle_session
    kanban_db_connect.connect().close()
    server._notif_poll_kanban("idle", session)
    calls = []
    def forbidden(*args, **kwargs):
        calls.append(args)
        raise AssertionError("idle poll hydrated provider scope")
    monkeypatch.setattr(server, "_session_profile_runtime_scope", forbidden)
    server._notif_poll_kanban("idle", session)
    assert calls == []


def test_board_revision_observes_same_stat_commit(tmp_path, monkeypatch):
    import os
    import sqlite3
    from tui_gateway import server
    path = tmp_path / "board.db"
    writer = sqlite3.connect(path)
    writer.execute("PRAGMA journal_mode=WAL")
    writer.execute("CREATE TABLE sample(value)")
    writer.commit()
    first = server._kb_board_file_sig(path)
    stat = os.stat
    frozen = {str(p): stat(p) for p in (path, str(path) + "-wal")}
    monkeypatch.setattr(os, "stat", lambda p, *a, **kw: frozen.get(str(p)) or stat(p, *a, **kw))
    writer.execute("INSERT INTO sample VALUES (1)")
    writer.commit()
    assert server._kb_board_file_sig(path) != first
    writer.close()


def test_board_quiet_ignores_non_notify_but_not_inflight(tmp_path):
    import sqlite3
    from tui_gateway.notification_idle import board_quiet
    path = tmp_path / "quiet.db"
    with sqlite3.connect(path) as db:
        db.executescript("CREATE TABLE kanban_notify_subs(platform, chat_id, task_id, last_event_id, inflight_from);"
                         "CREATE TABLE task_events(id, task_id, kind);"
                         "INSERT INTO kanban_notify_subs VALUES('tui','idle','task',0,NULL);"
                         "INSERT INTO task_events VALUES(1,'task','comment');")
        assert board_quiet(path, "idle")
        db.execute("UPDATE kanban_notify_subs SET inflight_from=0")
        db.commit()
        assert not board_quiet(path, "idle")
        db.execute("UPDATE kanban_notify_subs SET inflight_from=NULL")
        db.execute("INSERT INTO task_events VALUES(2,'task','completed')")
        db.commit()
        assert not board_quiet(path, "idle")


def test_empty_registry_park_skips_scan(monkeypatch):
    from tools.process_registry import process_registry
    from tui_gateway import server
    calls = []
    def forbidden(owns):
        calls.append(True)
        raise AssertionError("empty registry scanned")
    monkeypatch.setattr(process_registry, "_parked_events", [])
    monkeypatch.setattr(process_registry, "take_parked_events", forbidden)
    assert server._notif_take_parked("idle", {}, process_registry, lambda *a: None) == 0
    assert calls == []
