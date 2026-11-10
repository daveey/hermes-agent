"""Local transport receipt IDs do not establish machine creation provenance."""
import pytest

from hermes_cli import kanban_chat_links as links
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as notify
from hermes_state import SessionDB
from tests.tools.test_kanban_worker_created_chats import make_home, WORKER

CHAT = "local-" + "cd" * 32


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = make_home(tmp_path, monkeypatch)
    monkeypatch.setattr("hermes_cli.kanban_origin.gateway_names", lambda: ("here", {"here", "away"}))
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "default")
    db = SessionDB(db_path=home / "state.db")
    db.create_session(CHAT, source="desktop")
    db._conn.execute("UPDATE sessions SET source='local', created_source='desktop' WHERE id=?", (CHAT,))
    db._conn.commit()
    db.close()
    notify._source_cache.clear()
    return home


def test_desktop_hash_is_not_headless(home):
    assert not notify.is_headless_session(CHAT)
    assert not notify.is_headless_session(f"here/default/{CHAT}")
    assert not notify.is_machine_session("here", CHAT)
    assert notify.is_headless_session(WORKER)
    assert notify.is_headless_session("local-" + "ef" * 32)


@pytest.mark.parametrize("ref", [f"away/default/{CHAT}", f"here/research/{CHAT}"])
def test_local_row_cannot_prove_foreign_provenance(home, ref):
    assert notify.is_headless_session(ref)
    assert notify.is_machine_session(ref.split('/')[0], ref)


@pytest.mark.parametrize("via", ["origin", "shepherd"])
def test_ledger_keeps_desktop_origin_and_shepherd(home, monkeypatch, via):
    monkeypatch.setattr("hermes_cli.kanban_session_activity._remote_links", lambda: [
        {"id": "remote-card", "board": "default", "origin_gateway": "here", "origin_profile": "default",
         "session": CHAT, "via": via, "status": "ready"}])
    rows = list(links._ledger_links("default", "here").values())
    assert len(rows) == 1
    assert rows[0]["chat_session"] == CHAT and rows[0]["released"] == 0


def test_ledger_profile_is_not_ambient_profile(home, monkeypatch):
    monkeypatch.setattr("hermes_cli.kanban_session_activity._remote_links", lambda: [
        {"id": "remote-card", "board": "default", "origin_gateway": "here", "origin_profile": "research",
         "session": CHAT, "via": "origin", "status": "ready"}])
    assert links._ledger_links("default", "here") == {}


def test_desktop_subscription_inherits_and_reconciles(home):
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="Desktop parent", session_id=CHAT)
        notify.add_notify_sub(conn, task_id=parent, platform="tui", chat_id=CHAT,
                              delivery_metadata={"reason": "create"})
        assert [s["chat_id"] for s in notify.list_notify_subs(conn, parent)] == [CHAT]
        child = kb.create_task(conn, title="child", parents=[parent])
        assert [s["chat_id"] for s in notify.list_notify_subs(conn, child)] == [CHAT]
        links.reconcile(conn, "default", "here", ledger=False)
        assert conn.execute("SELECT 1 FROM task_chat_links WHERE task_id=? AND chat_session=? AND released=0",
                            (parent, CHAT)).fetchone()


def test_notice_uses_supplied_store_provenance(home):
    from tui_gateway.notice_fanout import worker_transcript_reason, WORKER_TRANSCRIPT
    db = SessionDB(db_path=home / "state.db", read_only=True)
    try:
        assert worker_transcript_reason(db, CHAT) is None
        assert worker_transcript_reason(db, WORKER) == WORKER_TRANSCRIPT
    finally:
        db.close()


def test_other_profile_reconcile_and_inheritance_retain_desktop(home, monkeypatch):
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="Desktop owner", session_id=CHAT)
        notify.add_notify_sub(conn, task_id=parent, platform="tui", chat_id=CHAT,
                              delivery_metadata={"reason": "create"}, notifier_profile="default")
        links.reconcile(conn, "default", "here", ledger=False)
        other = home / "profiles" / "research"
        other.mkdir(parents=True)
        monkeypatch.setenv("HERMES_HOME", str(other))
        monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "research")
        # The same board is reconciled and a child is filed by another local profile.
        assert not notify.is_headless_session(f"here/default/{CHAT}")
        result = links.reconcile(conn, "default", "here", ledger=False)
        assert result["retired"] == 0
        assert conn.execute("SELECT 1 FROM task_chat_links WHERE task_id=? AND released=0", (parent,)).fetchone()
        child = kb.create_task(conn, title="other-profile child", parents=[parent])
        assert [s["chat_id"] for s in notify.list_notify_subs(conn, child)] == [CHAT]


@pytest.mark.parametrize("created", ["kanban", "cron", "oneshot", "subagent", "unknown", "local", None])
def test_local_transport_requires_interactive_provenance(home, created):
    db = SessionDB(db_path=home / "state.db")
    try:
        db._conn.execute("UPDATE sessions SET created_source=? WHERE id=?", (created, CHAT))
        db._conn.commit()
    finally:
        db.close()
    notify._source_cache.clear()
    assert notify.is_headless_session(CHAT)
    assert notify.is_machine_session("here", CHAT)


def test_explicit_profile_reads_only_that_profile(home):
    other = home / "profiles" / "research"
    other.mkdir(parents=True)
    db = SessionDB(db_path=other / "state.db")
    db.create_session(CHAT, source="kanban")
    db.close()
    assert notify.is_headless_session(f"here/research/{CHAT}")
    assert not notify.is_headless_session(f"here/default/{CHAT}")
    # Unqualified colliding data cannot establish human proof.
    assert notify.is_headless_session(CHAT)


def test_qualified_local_desktop_subscription_and_origin(home):
    from hermes_cli import kanban_creator_chats
    with kbc.connect_closing() as conn:
        parent = kb.create_task(conn, title="qualified parent")
        notify.add_notify_sub(conn, task_id=parent, platform="hub", chat_id=f"here/default/{CHAT}",
                              delivery_metadata={"reason": "origin"})
        child = kb.create_task(conn, title="qualified child", parents=[parent])
        assert [s["chat_id"] for s in notify.list_notify_subs(conn, child)] == [f"here/default/{CHAT}"]
        kanban_creator_chats.initialize_ownership(conn, child, None, [], 1, f"@session:default@here/{CHAT}")
        assert conn.execute("SELECT 1 FROM task_chat_links WHERE task_id=? AND chat_session=? AND released=0",
                            (child, CHAT)).fetchone()


def test_notice_does_not_use_ambient_profile(home):
    from tui_gateway.notice_fanout import worker_transcript_reason, WORKER_TRANSCRIPT
    other = home / "profiles" / "research"
    other.mkdir(parents=True)
    db = SessionDB(db_path=other / "state.db")
    db.create_session(CHAT, source="kanban")
    try:
        assert not notify.is_headless_session(f"here/default/{CHAT}")
        assert worker_transcript_reason(db, CHAT) == WORKER_TRANSCRIPT
    finally:
        db.close()
