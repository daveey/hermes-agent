"""Self-work intake cards are owned by their repository's standing owner chat. [daveey]

A card the self-work intake admits used to get no owning chat ("chat-less by contract", #4252), so its worker
could not file child cards (ship's independent review card) until the hourly owner-chat backfill caught it, and
the backfill then keyed it by the sender. Now ``admit`` settles it in its create transaction with the producer
``selfwork:<repository>`` (not the requester-chosen area), and the backfill uses the same key for intake
cards. The requester still needs
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
    return f"selfwork:{envelope['args']['create']['self_work']['repository']}"


def _owner_sessions(conn, tid):
    return [r[0] for r in conn.execute(
        "SELECT chat_session FROM task_chat_links WHERE task_id=? AND kind='created' AND released=0", (tid,))]


def _attached(conn):
    return [json.loads(r[0]) for r in conn.execute("SELECT payload FROM task_events WHERE kind=?", (owner.EVENT,))]


def test_intake_card_is_owned_by_its_repository_owner_chat(request_envelope, monkeypatch, capsys):
    with kbc.connect_closing() as conn:
        from hermes_cli import kanban_db_sync as sync
        sync.set_mode(conn, "primary")
    created = invoke(request_envelope, monkeypatch, capsys)
    assert created.get("created") is True, created
    tid, slot = created["task_id"], _slot(request_envelope)
    with kbc.connect_closing() as conn:
        from hermes_cli import kanban_origin_native as native
        binding = native._receipt(conn, tid)
        assert binding is not None
        assert binding["origin"]["session"] == owner.session_id(slot)
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


def test_backfill_links_an_existing_intake_card_to_its_repository_owner_chat(request_envelope, monkeypatch, capsys):
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


def test_two_areas_of_one_repository_share_one_owner_chat(request_envelope, monkeypatch, capsys):
    """The area is requester-chosen: an invented or mistyped one must not mint another pinned chat."""
    first = invoke(request_envelope, monkeypatch, capsys)
    request_envelope["id"] = "request-owner-002"
    request_envelope["args"]["create"]["title"] = "Another area"
    request_envelope["args"]["create"]["self_work"] = {**request_envelope["args"]["create"]["self_work"],
                                                       "area": "kanban/typo-area"}
    second = invoke(request_envelope, monkeypatch, capsys)
    assert second.get("created") is True and second["task_id"] != first["task_id"], second
    with kbc.connect_closing() as conn:
        sid = owner.session_id(_slot(request_envelope))
        assert _owner_sessions(conn, first["task_id"]) == _owner_sessions(conn, second["task_id"]) == [sid]
    import sqlite3
    from hermes_constants import get_hermes_home
    state = sqlite3.connect(f"file:{get_hermes_home() / 'state.db'}?mode=ro", uri=True)
    try:
        owners = [r[0] for r in state.execute("SELECT id FROM sessions WHERE id LIKE ?", (owner.PREFIX + "%",))]
    finally:
        state.close()
    assert owners == [sid]


def test_intake_card_bound_to_another_gateways_chat_gets_no_repository_chat(request_envelope, monkeypatch, capsys):
    request_envelope["args"]["origin"] = {"gateway": "origin", "profile": "work", "session": "chat"}
    request_envelope["args"]["create"]["session_id"] = "creator"
    result = invoke(request_envelope, monkeypatch, capsys)
    assert result.get("created") is True and result["code"] == "origin_bind_pending", result
    with kbc.connect_closing() as conn:
        assert _attached(conn) == []
        assert owner.session_id(_slot(request_envelope)) not in _owner_sessions(conn, result["task_id"])
        assert "origin" in {r[0] for r in conn.execute(
            "SELECT chat_gateway FROM task_chat_links WHERE task_id=? AND kind='created'", (result["task_id"],))}


def test_intake_card_with_a_local_creator_gets_no_owner_chat_at_create(tmp_path, monkeypatch, owner_chats):
    """Like any worker's card, it is owned through its creator chain; the backfill covers it after the grace."""
    from hermes_cli.kanban_selfwork_intake import admit
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    kb.init_db()
    value = {"version": 1, "repository": "hermes-core", "area": "kanban", "kind": "sweep"}
    with kbc.connect_closing() as conn:
        creator = kb.create_task(conn, title="creator", assignee="default", settle_owner_chat=False)
        receipt = admit(conn, request_id="request-creator-1", authority="hub", local_gateway="hub", now=100000,
                        options=dict(title="Found", body="evidence", self_work=value, creator_task_id=creator))
        assert receipt["created"] is True
        assert _attached(conn) == []
        cards = [tid for tid, _ in owner.ownerless(conn, live=lambda sid: True)]
        assert receipt["task_id"] not in cards  # inside the creator grace (the bare creator itself is listed)


def test_unavailable_owner_chat_at_create_is_backfilled_and_then_files_children(request_envelope, monkeypatch, capsys):
    with monkeypatch.context() as m:
        m.setattr(owner, "ensure", lambda producer, *, force=False: (_ for _ in ()).throw(OSError("state.db down")))
        created = invoke(request_envelope, monkeypatch, capsys)
    root, slot = created["task_id"], _slot(request_envelope)
    with kbc.connect_closing() as conn:
        assert _owner_sessions(conn, root) == []
        assert owner.backfill(conn, apply=True)["by_producer"] == {slot: 1}
        assert _owner_sessions(conn, root) == [owner.session_id(slot)]
        conn.execute("UPDATE tasks SET status='running' WHERE id=?", (root,))
        kb._append_event(conn, root, "owner_admitted", {"session_id": "20261124_180000_worker"})
        conn.commit()
        child = kb.create_task(conn, title="Review", assignee="default", creator_task_id=root,
                               require_scope_authority=True)
        assert kca.has_authority(conn, child, None, durable=True)
