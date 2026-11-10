"""Local Desktop provenance survives ledger reconciliation into replica API/count views."""
import pytest

from hermes_cli import kanban_chat_links as links
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_notify as notify
from hermes_state import SessionDB
from tests.hermes_cli import test_kanban_chat_links_parity as parity

CHAT = "local-" + "cd" * 32
env = parity.env


@pytest.mark.parametrize("via", ["origin", "shepherd"])
def test_desktop_ledger_link_reaches_task_list_and_active_count(env, monkeypatch, via):
    monkeypatch.setattr("hermes_cli.kanban_origin.gateway_names", lambda: (parity.ME, {parity.ME, parity.HUB}))
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "default")
    db = SessionDB(db_path=env["home"] / "state.db")
    db.create_session(CHAT, source="desktop")
    assert db._conn is not None
    db._conn.execute("UPDATE sessions SET source='local', created_source='desktop' WHERE id=?", (CHAT,))
    db._conn.commit()
    db.close()
    notify._source_cache.clear()
    env["ledger"].append({"id": "t_remote", "board": "default", "gateway": parity.HUB, "profile": "default",
                          "origin_gateway": parity.ME, "origin_profile": "default", "session": CHAT,
                          "via": via, "status": "ready", "unfinished": True})
    with kbc.connect_closing(env["home"] / "kanban.db") as conn:
        links.reconcile(conn, "default", parity.ME)
        rows = [tuple(r) for r in conn.execute(
            "SELECT task_id, chat_session, kind, released FROM task_chat_links WHERE task_id='t_remote'")]
        assert rows == [("t_remote", CHAT, "created" if via == "origin" else "shepherd", 0)]
        assert conn.execute("SELECT 1 FROM kanban_sync_outbox WHERE table_name='task_chat_links'").fetchone()
    # Read back the actual reconciled link shape through the shared replica consumer.
    parity.replica(env, [("t_remote", "ready", parity.HUB)], rows)
    annotated, tasks = parity.assert_parity(CHAT, expect=1)
    assert annotated["kanban_active"] == 1
    assert [(t["id"], t["status"], t["source"]) for t in tasks] == [("t_remote", "ready", "replica")]
