"""Withdrawal transport is an explicit owner event, not an archived status."""
from contextlib import closing

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli.kanban_origin_ledger import observe_owner_task
from tui_gateway import kanban_remote_notices as remote


@pytest.mark.parametrize("pending", [True, False])
def test_owner_publishes_only_new_explicit_withdrawal(pending):
    with closing(kbc.connect()) as conn:
        tid = kb.create_task(conn, title="Decision", assignee="default")
        if pending:
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status='blocked',human_request=? WHERE id=?",
                             ('{"action":"private request contents"}', tid))
        assert kb.archive_task(conn, tid)
        task = dict(id=tid, status="archived", gateway="owner", profile="build", board="default",
                    origin_bindings=[], sessions=[])
        conn.execute("BEGIN")
        published = observe_owner_task(conn, task, now=0)
        event = conn.execute("SELECT id,created_at FROM task_events WHERE task_id=? AND kind='request_withdrawn'",
                             (tid,)).fetchone()
        conn.rollback()
        assert published["origin_observation"]["verified"] is False
        if pending:
            assert published.get("withdrawal_notice") == dict(
                event_id=event[0], created_at=event[1], kind="request_withdrawn")
            assert "private request contents" not in str(published)
            # Pre-transport events deliberately do not opt in to historical replay.
            with kb.write_txn(conn):
                conn.execute("UPDATE task_events SET payload='{}' WHERE id=?", (event[0],))
            conn.execute("BEGIN")
            assert "withdrawal_notice" not in observe_owner_task(conn, task, now=0)
            conn.rollback()
        else:
            assert "withdrawal_notice" not in published


@pytest.mark.parametrize("payload", ['{bad', 'null', '[]', '{"notice_version":true}', '{"notice_version":1.0}', '{"notice_version":2}'])
def test_owner_malformed_opt_in_is_silent(payload):
    from hermes_cli.kanban_withdrawal_notice import publish
    with closing(kbc.connect()) as conn:
        tid = kb.create_task(conn, title="Malformed receipt", assignee="default")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='archived' WHERE id=?", (tid,))
            conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?, 'request_withdrawn', ?, 1)",
                         (tid, payload))
        assert publish(conn, dict(id=tid, status="archived")) is None


@pytest.mark.parametrize("raw", ['{bad', 'null', '[]', '{}', '{"origin":null}',
                                 '{"origin":{"gateway":[],"profile":"default","session":"chat"}}'])
def test_malformed_origin_does_not_prevent_archive(raw):
    from hermes_cli.kanban_db_sync import SYNC_SCHEMA_SQL
    with closing(kbc.connect()) as conn:
        conn.executescript(SYNC_SCHEMA_SQL)
        tid = kb.create_task(conn, title="Malformed origin", assignee="default", origin_intent=dict(
            request_id="malformed-origin", owner=dict(gateway="owner", profile="build", board="default"),
            origin=dict(gateway="origin", profile="default", session="chat")))
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='blocked',human_request=? WHERE id=?", ('{"action":"private"}', tid))
            conn.execute("UPDATE origin_creation_intents SET binding_json=? WHERE task_id=?", (raw, tid))
            conn.execute("DELETE FROM kanban_notify_subs WHERE task_id=?", (tid,))
        assert kb.archive_task(conn, tid)
        from hermes_cli.kanban_withdrawal_recipients import snapshot
        assert snapshot(conn, tid) == []


@pytest.mark.parametrize("status", ["todo", "blocked", "done"])
def test_owner_does_not_publish_old_withdrawal_after_reopen(status):
    from hermes_cli.kanban_withdrawal_notice import publish
    with closing(kbc.connect()) as conn:
        tid = kb.create_task(conn, title="Reopened receipt", assignee="default")
        with kb.write_txn(conn):
            conn.execute("INSERT INTO task_events(task_id,kind,payload,created_at) VALUES (?, 'request_withdrawn', ?, 1)",
                         (tid, '{"notice_version":1}'))
        assert publish(conn, dict(id=tid, status=status)) is None


def test_withdrawal_projects_one_event_identity_for_both_consumers():
    task = dict(id="t_one", status="archived", gateway="owner", profile="build", board="default",
                title="Decision", withdrawal_notice=dict(event_id=42, created_at=100, kind="request_withdrawn"),
                sessions=[dict(gw="origin", profile="work", session="chat", rel="shepherd")])
    key = "build/default/t_one"
    import json
    cards = list(remote._sweep_cards([("owner", key, 1, json.dumps(task))], "origin"))
    assert len(cards) == 1
    event_key, metadata, text = remote._sweep_payload("owner", key, task, "origin", "work", "chat")
    assert event_key == "remote:owner:build/default/t_one:42"
    assert metadata["blocked_on_human"] is False
    assert metadata["event_ids"] == [42]
    assert "request withdrawn" in text and "not approval" in text and "not proof of completion" in text
    assert remote._shared_key("owner", key, task, "work", "chat") == event_key
    item = remote._notice_item(dict(gw="owner", key=key, task=task), "chat", "origin", "work")
    assert item["event_key"] == event_key
    assert item["state"] == "request_withdrawn@42"
    assert item["blocked_on_human"] is False
    # Ordinary archive is silent, even with the same retained chat link.
    task.pop("withdrawal_notice")
    assert list(remote._sweep_cards([("owner", key, 2, json.dumps(task))], "origin")) == []
