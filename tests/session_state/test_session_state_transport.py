"""SessionState v2: the next_change_at timer, the HTTP/WS transport and the user-action endpoint. [daveey]"""
from __future__ import annotations

import time

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import hermes_session_state_collect as collect_module
from hermes_cli import kanban_db as kb
from hermes_cli.web_routers import session_state as routes
from tests.session_state.conftest import HUMAN_REQUEST, drain


@pytest.fixture
def client(store, monkeypatch):
    monkeypatch.setattr(routes, "_ws_allowed", lambda ws: True)
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app) as tc:
        yield tc


def _triage_card(board):
    return kb.create_task(board, title="Stuck", assignee="worker", session_id="chat", triage=True)


def test_next_change_at_fires_the_600s_triage_threshold(store, board, clock):
    task = _triage_card(board)
    clock.now = time.time()  # the entry is stamped after the fixture clock was taken
    first = store.recompute("chat")
    card = first["cards"][f"default/{task}"]
    assert card["lifecycle"] == "triage" and card["red"] is False
    due = first["next_change_at"]
    assert due is not None and 599 <= due - clock.now <= 601  # the entry event stamps whole seconds
    clock.now = due - 1
    drain(store, rounds=1)
    assert store.metrics()["timer_fires"] == 0
    clock.now = due
    drain(store, rounds=1)
    assert store.metrics()["timer_fires"] == 1
    fired = store.get("chat")
    assert fired["rev"] > first["rev"] and fired["cards"][f"default/{task}"]["red"] is True
    assert fired["header"]["status"] == "stuck" and fired["next_change_at"] is None


def test_get_serves_the_persisted_row_without_collecting(store, board, client, monkeypatch):
    kb.create_task(board, title="Work", assignee="worker", session_id="chat")
    obj = store.recompute("chat")

    def refuse(*args, **kwargs):
        raise AssertionError("GET must not read sources")
    monkeypatch.setattr(store, "_collect", refuse)
    monkeypatch.setattr(collect_module, "collect", refuse)
    monkeypatch.setattr(collect_module, "local_graph", refuse)
    assert client.get("/api/session-state/chat").json() == obj
    listing = client.get("/api/session-state", params={"since": 0}).json()
    assert listing["rev"] == obj["rev"] and listing["objects"] == [obj]
    assert client.get("/api/session-state", params={"since": obj["rev"]}).json()["objects"] == []
    assert client.get("/api/session-state/missing").status_code == 404
    assert store.metrics()["recomputes"] == 1


def test_get_resolves_any_session_id_of_the_lineage(store, db, board, client):
    kb.create_task(board, title="Work", assignee="worker", session_id="chat")
    store.recompute("chat")
    assert client.get("/api/session-state/chat").json()["identity"]["lineage"] == "chat"


def test_websocket_since_rev_catch_up_then_pushes(store, board, db, client):
    db.create_session("other", source="desktop")
    task = kb.create_task(board, title="Work", assignee="worker", session_id="chat")
    kb.create_task(board, title="Other", assignee="worker", session_id="other")
    first = store.recompute("chat")
    store.recompute("other")
    assert kb.block_task(board, task, kind="needs_input", human_request=HUMAN_REQUEST)
    second = store.recompute("chat")
    with client.websocket_connect("/api/session-state/ws") as ws:
        ws.send_json({"type": "subscribe", "keys": ["chat"], "since_rev": first["rev"]})
        snap = ws.receive_json()
        assert snap["type"] == "snapshot" and snap["rev"] == second["rev"]
        assert [o["rev"] for o in snap["objects"]] == [second["rev"]]  # only chat, only what changed after
        assert kb.unblock_task(board, task)
        third = store.recompute("chat")
        push = ws.receive_json()
        assert push == {"type": "state", "object": third}
        ws.send_json({"type": "subscribe", "keys": ["chat"]})
        assert ws.receive_json()["objects"] == [third]


def test_websocket_refuses_a_disallowed_upgrade(store, monkeypatch):
    monkeypatch.setattr(routes, "_ws_allowed", lambda ws: False)
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app) as tc, pytest.raises(Exception):
        with tc.websocket_connect("/api/session-state/ws") as ws:
            ws.receive_json()


def test_action_unblock_returns_the_post_transition_object_and_a_reblock_bumps_rev(store, board, client):
    task = kb.create_task(board, title="Deploy", assignee="worker", session_id="chat")
    assert kb.block_task(board, task, kind="needs_input", human_request=HUMAN_REQUEST)
    store.recompute("chat")
    reply = client.post("/api/session-state/actions", json={
        "action": "unblock", "session_id": "chat", "card": f"default/{task}", "request_id": "req-1",
        "label": "Ship it"}).json()
    assert reply["ok"] is True
    card = reply["object"]["cards"][f"default/{task}"]
    assert card["lifecycle"] != "blocked" and card["red"] is False
    assert reply["object"]["header"]["status"] != "needs_you"
    assert [r["request_id"] for r in reply["object"]["resolutions"]] == ["req-1"]
    assert store.get("chat") == reply["object"]
    assert kb.block_task(board, task, kind="needs_input", human_request=HUMAN_REQUEST)  # a new episode
    again = store.recompute("chat")
    assert again["rev"] > reply["rev"]
    reblocked = again["cards"][f"default/{task}"]
    assert reblocked["red"] is True and reblocked["resolution"] is None  # the old answer covers the old episode


def test_answer_resolves_only_its_own_request(store, board, client, monkeypatch):
    from hermes_session_state_actions import register_answer_handler

    a = kb.create_task(board, title="A", assignee="worker", session_id="chat")
    b = kb.create_task(board, title="B", assignee="worker", session_id="chat")
    for task in (a, b):
        assert kb.block_task(board, task, kind="needs_input", human_request=HUMAN_REQUEST)
    store.recompute("chat")
    seen = []
    register_answer_handler(lambda request: seen.append(request) or {"ok": True})
    try:
        reply = client.post("/api/session-state/actions", json={
            "action": "answer", "session_id": "chat", "card": f"default/{a}", "request_id": "req-a",
            "choice_id": "yes", "label": "Ship it"}).json()
    finally:
        register_answer_handler(None)
    assert seen and seen[0]["request_id"] == "req-a"
    cards = reply["object"]["cards"]
    assert cards[f"default/{a}"]["resolution"]["request_id"] == "req-a" and cards[f"default/{a}"]["red"] is False
    assert cards[f"default/{b}"]["red"] is True and cards[f"default/{b}"]["resolution"] is None
    assert [x["card"] for x in reply["object"]["asks"]] == [f"default/{b}"]
    assert reply["object"]["header"]["status"] == "needs_you"


def test_answer_without_a_decision_handler_is_refused_and_records_nothing(store, board, client):
    task = kb.create_task(board, title="A", assignee="worker", session_id="chat")
    assert kb.block_task(board, task, kind="needs_input", human_request=HUMAN_REQUEST)
    before = store.recompute("chat")
    reply = client.post("/api/session-state/actions", json={
        "action": "answer", "session_id": "chat", "card": f"default/{task}", "request_id": "r"})
    assert reply.status_code == 409 and reply.json()["refusal"] == "answer_unavailable"
    assert store.resolutions("chat") == [] and store.get("chat") == before


def test_refused_transition_and_bad_requests(store, board, client):
    task = kb.create_task(board, title="A", assignee="worker", session_id="chat")
    store.recompute("chat")
    refused = client.post("/api/session-state/actions", json={
        "action": "unblock", "session_id": "chat", "card": f"default/{task}"})
    assert refused.status_code == 409 and refused.json()["refusal"] == "unblock_refused"
    assert store.resolutions("chat") == []
    assert client.post("/api/session-state/actions", json={"action": "nuke", "session_id": "chat",
                                                            "card": f"default/{task}"}).status_code == 400
    assert client.post("/api/session-state/actions", json={"action": "unblock", "session_id": "chat",
                                                            "card": "default/t_none"}).status_code == 404
    assert client.post("/api/session-state/actions", json={"action": "unblock", "session_id": "chat",
                                                            "card": f"elsewhere/{task}"}).status_code == 404


def test_archive_action(store, board, client):
    task = kb.create_task(board, title="A", assignee="worker", session_id="chat")
    store.recompute("chat")
    reply = client.post("/api/session-state/actions", json={
        "action": "archive", "session_id": "chat", "card": f"default/{task}"}).json()
    assert reply["ok"] and f"default/{task}" not in reply["object"]["cards"]


def test_routes_answer_503_without_a_store(monkeypatch):
    from hermes_session_state_store import install

    previous = install(None)
    try:
        app = FastAPI()
        app.include_router(routes.router)
        with TestClient(app) as tc:
            assert tc.get("/api/session-state/chat").status_code == 503
    finally:
        install(previous)
