"""Self-work intake cards are owned by their policy slot's standing owner chat. [daveey]

A card the self-work intake admits used to get no owning chat ("chat-less by contract", #4252), so its worker
could not file child cards (ship's independent review card) until the hourly owner-chat backfill caught it, and
the backfill then keyed it by the sender. Now ``admit`` settles it in its create transaction with the producer
``selfwork:<repository>/<area>``, and the backfill uses the same key for intake cards. The requester still needs
no chat of its own.
"""
import json

import pytest

from hermes_cli import kanban_create_authority as kca
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_owner_chat as owner
from tests.hermes_cli import test_kanban_selfwork_nochat as nochat

world = nochat.world
request_envelope = nochat.request_envelope
invoke = nochat.invoke


@pytest.fixture(autouse=True)
def _owner_chats_on(monkeypatch, owner_chats):
    monkeypatch.setattr(owner, "_ENSURED", {})


def _slot(envelope):
    value = envelope["args"]["create"]["self_work"]
    return f"selfwork:{value['repository']}/{value['area']}"


def _owner_sessions(conn, tid):
    return [r[0] for r in conn.execute(
        "SELECT chat_session FROM task_chat_links WHERE task_id=? AND kind='created' AND released=0", (tid,))]


def _attached(conn):
    return [json.loads(r[0]) for r in conn.execute("SELECT payload FROM task_events WHERE kind=?", (owner.EVENT,))]


def test_intake_card_is_owned_by_its_slot_owner_chat(request_envelope, monkeypatch, capsys):
    created = invoke(request_envelope, monkeypatch, capsys)
    assert created.get("created") is True, created
    tid, slot = created["task_id"], _slot(request_envelope)
    with kbc.connect_closing() as conn:
        assert _owner_sessions(conn, tid) == [owner.session_id(slot)]
        assert kca.has_authority(conn, tid, None, durable=True)
        assert [e["producer"] for e in _attached(conn)] == [slot]
        assert kb.get_task(conn, tid).created_by == "gw:origin"  # the requester is unchanged and needs no chat


def test_append_and_replay_make_no_second_owner_chat(request_envelope, monkeypatch, capsys):
    first = invoke(request_envelope, monkeypatch, capsys)
    request_envelope["id"] = "request-owner-002"
    request_envelope["args"]["create"]["title"] = "Additional evidence"
    assert invoke(request_envelope, monkeypatch, capsys)["disposition"] == "append"
    assert invoke(request_envelope, monkeypatch, capsys)["replayed"] is True
    with kbc.connect_closing() as conn:
        assert len(_attached(conn)) == 1
        assert _owner_sessions(conn, first["task_id"]) == [owner.session_id(_slot(request_envelope))]
        assert len(kb.list_tasks(conn)) == 1


def test_intake_create_survives_an_unavailable_owner_chat(request_envelope, monkeypatch, capsys):
    """#4252's guarantee: an intake create never depends on a chat; state.db trouble leaves it ownerless."""
    def broken(producer, *, force=False):
        raise OSError("state.db unavailable")
    monkeypatch.setattr(owner, "ensure", broken)
    created = invoke(request_envelope, monkeypatch, capsys)
    assert created.get("created") is True, created
    with kbc.connect_closing() as conn:
        events = [e.kind for e in kb.list_events(conn, created["task_id"])]
        assert owner.UNAVAILABLE in events and _owner_sessions(conn, created["task_id"]) == []


def test_running_worker_files_ship_review_child_through_the_tool(request_envelope, monkeypatch, capsys):
    from agent import delegation_context
    from tools import kanban_tools as kt
    created = invoke(request_envelope, monkeypatch, capsys)
    root = created["task_id"]
    with kbc.connect_closing() as conn:
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (root,))
        kb._append_event(conn, root, "owner_admitted", {"session_id": "20261124_170000_worker"})
        conn.commit()
    monkeypatch.setattr(kt, "_is_dispatcher_owned_worker", lambda: True)
    monkeypatch.setattr(delegation_context, "is_dispatcher_owned_worker_context", lambda: True)
    monkeypatch.setenv("HERMES_KANBAN_TASK", root)
    monkeypatch.setenv("HERMES_SESSION_ID", "20261124_170000_worker")
    out = json.loads(kt._handle_create({
        "title": "Review PR #9999 @ 0123abcd", "assignee": "default", "requires": [], "placement": "local",
        "idempotency_key": "core-wt-review:softmax-universe/daveey-hermes#9999:0123abcd",
        "body": "Independent exact-head review."}))
    assert out.get("ok"), out
    with kbc.connect_closing() as conn:
        assert kca.has_authority(conn, out["task_id"], None, durable=True)


def test_backfill_links_an_existing_intake_card_to_its_slot_owner_chat(request_envelope, monkeypatch, capsys):
    monkeypatch.setattr(owner, "enabled", lambda: False)  # a card admitted before intake cards were owned
    created = invoke(request_envelope, monkeypatch, capsys)
    monkeypatch.setattr(owner, "enabled", lambda: True)
    tid, slot = created["task_id"], _slot(request_envelope)
    with kbc.connect_closing() as conn:
        assert _owner_sessions(conn, tid) == []
        assert owner.ownerless(conn, live=lambda sid: True) == [(tid, slot)]
        assert owner.backfill(conn, apply=True)["by_producer"] == {slot: 1}
        assert _owner_sessions(conn, tid) == [owner.session_id(slot)]
        assert owner.session_id("gw:origin") not in _owner_sessions(conn, tid)


def test_a_non_intake_card_keeps_its_created_by_producer_in_the_backfill(request_envelope):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="watcher card", assignee="default", created_by="ci-main-watch",
                             settle_owner_chat=False)
        assert owner.ownerless(conn, live=lambda sid: True) == [(tid, "ci-main-watch")]
