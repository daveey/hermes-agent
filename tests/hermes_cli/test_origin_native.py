"""Native attestations are local transaction state, not synced provenance."""
import sqlite3

import pytest

from hermes_cli import kanban_origin_native as native


@pytest.fixture
def scope(monkeypatch):
    from hermes_cli import kanban_origin, kanban_execution_observations, kanban_placement_forward
    monkeypatch.setattr(kanban_origin, "gateway_names", lambda: ("macbook", []))
    monkeypatch.setattr(kanban_execution_observations, "board_owner_profile", lambda path: "default")
    monkeypatch.setattr(kanban_placement_forward, "opened_board", lambda conn: "default")
    monkeypatch.setattr(native, "_primary_scope", lambda conn: (
        "macbook", kanban_execution_observations.board_owner_profile(None), "default"))
    return {"origin": {"gateway": "macbook", "profile": "default", "session": "chat-1"}}


def memory_tasks():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY, created_on TEXT, created_at INTEGER)")
    conn.execute("CREATE TABLE kanban_sync_state (board_id TEXT, gateway_id TEXT, mode TEXT)")
    conn.executemany("INSERT INTO tasks VALUES (?, 'macbook', 1)", [("t_root",), ("t_child",)])
    conn.commit()
    return conn


def test_transaction_and_inheritance(scope):
    conn = memory_tasks()
    conn.execute("BEGIN")
    native.record(conn, "t_root", scope, 1)
    native.record(conn, "t_child", {"creator": "t_root"}, 2)
    assert native._receipt(conn, "t_child")["origin"] == scope["origin"]
    assert conn.execute("SELECT inherited_from FROM origin_native_receipts WHERE task_id='t_child'").fetchone()[0] == "t_root"
    conn.rollback()
    assert native._receipt(conn, "t_root") is None


def test_missing_receipt_and_synced_ancestry_cannot_authorize(scope):
    conn = memory_tasks()
    conn.execute("CREATE TABLE task_chat_links (task_id, chat_gateway, chat_profile, chat_session)")
    conn.execute("INSERT INTO task_chat_links VALUES ('t_fake','macbook','default','chat-1')")
    native.record(conn, "t_child", {"creator": "t_fake"}, 2)
    assert native._receipt(conn, "t_child") is None


def test_foreign_parent_namespace_rejected(scope, monkeypatch):
    conn = memory_tasks()
    native.record(conn, "t_root", scope, 1)
    from hermes_cli import kanban_execution_observations
    monkeypatch.setattr(kanban_execution_observations, "board_owner_profile", lambda path: "other")
    native.record(conn, "t_child", {"creator": "t_root"}, 2)
    assert native._receipt(conn, "t_child") is None


@pytest.mark.parametrize("source", ["kanban", "cron", "oneshot", "subagent", None])
def test_capture_rejects_noninteractive(scope, monkeypatch, source):
    from gateway import session_context
    from hermes_cli import kanban_session_provenance, profiles
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(session_context, "get_session_env", lambda name: "chat-1")
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(kanban_session_provenance, "_local_session_sources", lambda sids: {"chat-1": source})
    assert native.capture(None) is None


def test_capture_ambient_interactive_only(scope, monkeypatch):
    from gateway import session_context
    from hermes_cli import kanban_session_provenance, profiles
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    monkeypatch.setattr(session_context, "get_session_env", lambda name: "chat-1")
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(kanban_session_provenance, "_local_session_sources", lambda sids: {"chat-1": "desktop"})
    assert native.capture(None) == scope
    assert native.capture(None, courier=True) is None
    assert native.capture("t_forged") is None
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_worker")
    assert native.capture(None) is None
    assert native.capture("t_other") is None
    assert native.capture("t_worker") == {"creator": "t_worker"}


def test_real_tool_creation_authorizes_native_worker_forward(tmp_path, monkeypatch, capsys):
    from tests.tools import test_kanban_worker_created_chats as fixtures
    from hermes_cli import kanban_origin, kanban_db_connect as kbc
    from hermes_cli.kanban_origin_forward import authorize
    home = fixtures.make_home(tmp_path, monkeypatch)
    from hermes_cli import kanban_db_sync as sync
    with kbc.connect_closing() as conn:
        sync.set_mode(conn, "primary")
    monkeypatch.setattr(kanban_origin, "gateway_names", lambda: ("here", {"here"}))
    monkeypatch.setenv("HERMES_SESSION_ID", fixtures.CHAT)
    task = fixtures.create("tool", monkeypatch, capsys)
    with kbc.connect_closing() as conn:
        binding = native._receipt(conn, task)
    assert binding is not None
    assert binding["origin"] == {"gateway": "here", "profile": "default", "session": fixtures.CHAT}
    kwargs = dict(ledger=home / "missing-ledger.db", origin=binding["origin"], creator=binding["owner"],
                  gateway="here", profile="default", sender="gw:worker-7", run_uid="run-1",
                  lease={"task_id": task, "holder": "worker-7", "run_uid": "run-1", "expires": 200},
                  now_ms=100, session_exists=lambda sid: sid == fixtures.CHAT,
                  native_lookup=lambda origin, creator: native.lookup(origin, creator, gateway="here"))
    assert authorize(**kwargs) == binding
    kwargs["lease"]["holder"] = "worker-8"
    with pytest.raises(ValueError, match="lease_invalid"):
        authorize(**kwargs)
    assert not (home / "missing-ledger.db").exists()
