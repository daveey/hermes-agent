"""SessionState v2: the next_change_at timer, the HTTP/WS transport and the user-action endpoint. [daveey]"""
from __future__ import annotations

import time
from contextlib import closing

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import hermes_session_state_collect as collect_module
from hermes_cli import kanban_db as kb
from hermes_cli.kanban_db_connect import connect
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
    # Anchor to the committed episode, not elapsed wall time after create_task's post-commit work.
    clock.now = board.execute(
        "SELECT created_at FROM task_events WHERE task_id = ? AND kind = 'created' ORDER BY id LIMIT 1",
        (task,),
    ).fetchone()[0]
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


def _profile_bound_client(home):
    """The router behind the production per-request profile binder (``native_profile_scope``)."""
    from hermes_cli.dashboard_auth.native_http import native_profile_scope

    app = FastAPI()
    app.include_router(routes.router)

    @app.middleware("http")
    async def scope(request, call_next):
        request.state.native_http_principal = {"profile_id": str(home)}
        with native_profile_scope(request):
            return await call_next(request)
    return TestClient(app)


def test_a_request_bound_to_another_profile_never_gets_this_profiles_objects(store, board, tmp_path):
    task = kb.create_task(board, title="Deploy", assignee="worker", session_id="chat")
    assert kb.block_task(board, task, kind="needs_input", human_request=HUMAN_REQUEST)
    obj = store.recompute("chat")
    other = tmp_path / "secondary"
    other.mkdir()
    with _profile_bound_client(other) as tc:
        assert tc.get("/api/session-state/chat").status_code == 503
        assert tc.get("/api/session-state", params={"since": 0}).status_code == 503
        refused = tc.post("/api/session-state/actions", json={
            "action": "unblock", "session_id": "chat", "card": f"default/{task}", "request_id": "req-x",
            "label": "Ship it", "episode": obj["cards"][f"default/{task}"]["block_episode"]})
        assert refused.status_code == 503
    assert kb.get_task(board, task).status == "blocked"  # the refused action committed nothing
    with _profile_bound_client(tmp_path) as tc:  # control: the store's own profile, through the same binder
        assert tc.get("/api/session-state/chat").json() == obj
        assert tc.get("/api/session-state", params={"since": 0}).json()["objects"] == [obj]


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
    shown = store.recompute("chat")["cards"][f"default/{task}"]["block_episode"]
    assert shown
    reply = client.post("/api/session-state/actions", json={
        "action": "unblock", "session_id": "chat", "card": f"default/{task}", "request_id": "req-1",
        "label": "Ship it", "episode": shown}).json()
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
    shown = store.recompute("chat")["cards"][f"default/{a}"]["block_episode"]
    seen = []
    register_answer_handler(lambda request: seen.append(request) or {"ok": True})
    try:
        reply = client.post("/api/session-state/actions", json={
            "action": "answer", "session_id": "chat", "card": f"default/{a}", "request_id": "req-a",
            "choice_id": "yes", "label": "Ship it", "episode": shown}).json()
    finally:
        register_answer_handler(None)
    assert seen and seen[0]["request_id"] == "req-a" and seen[0]["episode"] == shown
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
        "action": "answer", "session_id": "chat", "card": f"default/{task}", "request_id": "r",
        "episode": before["cards"][f"default/{task}"]["block_episode"]})
    assert reply.status_code == 409 and reply.json()["refusal"] == "answer_unavailable"
    assert store.resolutions("chat") == [] and store.get("chat") == before


def test_refused_transition_and_bad_requests(store, board, client):
    task = kb.create_task(board, title="A", assignee="worker", session_id="chat")
    store.recompute("chat")
    refused = client.post("/api/session-state/actions", json={
        "action": "unblock", "session_id": "chat", "card": f"default/{task}", "episode": None})
    assert refused.status_code == 409 and refused.json()["refusal"] == "unblock_refused"
    assert store.resolutions("chat") == []
    assert client.post("/api/session-state/actions", json={"action": "nuke", "session_id": "chat",
                                                            "card": f"default/{task}"}).status_code == 400
    for episode in ({}, {"episode": ""}, {"episode": 5}):  # the shown episode is required (a string or null)
        missing = client.post("/api/session-state/actions", json={
            "action": "unblock", "session_id": "chat", "card": f"default/{task}", **episode})
        assert missing.status_code == 400 and missing.json()["refusal"] == "episode_required"
    for card in ("default/t_none", f"elsewhere/{task}"):
        assert client.post("/api/session-state/actions", json={
            "action": "unblock", "session_id": "chat", "card": card, "episode": None}).status_code == 404


def test_archive_action(store, board, client):
    task = kb.create_task(board, title="A", assignee="worker", session_id="chat")
    store.recompute("chat")
    reply = client.post("/api/session-state/actions", json={
        "action": "archive", "session_id": "chat", "card": f"default/{task}", "episode": None}).json()
    assert reply["ok"] and f"default/{task}" not in reply["object"]["cards"]


def _post(client, action, task, episode, **extra):
    return client.post("/api/session-state/actions", json={
        "action": action, "session_id": "chat", "card": f"default/{task}", "episode": episode, **extra})


def _unblocked_events(board, task):
    return board.execute("SELECT COUNT(*) FROM task_events WHERE task_id = ? AND kind IN ('unblocked', 'archived')",
                         (task,)).fetchone()[0]


@pytest.mark.parametrize("action", ["unblock", "archive", "answer"])
def test_a_stale_click_never_resolves_a_newer_episode(store, board, client, action):
    """A click shown at one block episode, after the card was answered and re-blocked (a new episode), is refused
    and records nothing: it must not unblock/archive/answer an ask the user never saw."""
    from hermes_session_state_actions import register_answer_handler

    task = kb.create_task(board, title="Deploy", assignee="worker", session_id="chat")
    assert kb.block_task(board, task, kind="needs_input", human_request=HUMAN_REQUEST)
    shown = store.recompute("chat")["cards"][f"default/{task}"]["block_episode"]
    assert kb.unblock_task(board, task)  # answered elsewhere ...
    assert kb.block_task(board, task, kind="needs_input", human_request=HUMAN_REQUEST)  # ... and a new ask
    newer = store.recompute("chat")
    assert newer["cards"][f"default/{task}"]["block_episode"] not in (None, shown)
    before = _unblocked_events(board, task)
    seen = []
    register_answer_handler(lambda request: seen.append(request) or {"ok": True})
    try:
        reply = _post(client, action, task, shown, request_id="stale")
    finally:
        register_answer_handler(None)
    assert reply.status_code == 409 and reply.json()["refusal"] == "stale_episode"
    assert seen == [] and _unblocked_events(board, task) == before
    held = kb.get_task(board, task).status
    assert held == newer["cards"][f"default/{task}"]["lifecycle"] and held in ("blocked", "triage")
    assert store.resolutions("chat") == []
    assert store.get("chat") == newer and store.get("chat")["cards"][f"default/{task}"]["red"] is True


def test_the_fence_is_asked_inside_the_transition_transaction(store, board, client, monkeypatch):
    """A re-block landing between the request's read and the transition's write is still refused."""
    from hermes_cli import kanban_db

    task = kb.create_task(board, title="Deploy", assignee="worker", session_id="chat")
    assert kb.block_task(board, task, kind="needs_input", human_request=HUMAN_REQUEST)
    shown = store.recompute("chat")["cards"][f"default/{task}"]["block_episode"]
    real = kanban_db.unblock_task

    def racing(conn, task_id, **kw):  # another writer (own connection: this runs on the endpoint's thread)
        with closing(connect(collect_module.board_paths()["default"])) as other:
            assert real(other, task_id) and kb.block_task(
                other, task_id, kind="needs_input", human_request=HUMAN_REQUEST)
        return real(conn, task_id, **kw)
    monkeypatch.setattr(kanban_db, "unblock_task", racing)
    reply = _post(client, "unblock", task, shown, request_id="race")
    assert reply.status_code == 409 and reply.json()["refusal"] == "stale_episode"
    assert kb.get_task(board, task).status in ("blocked", "triage") and store.resolutions("chat") == []


def _spend_stopped_with_offer(board, *, filed=True):
    """A spend-stopped card linked to the chat whose stored ask carries the home-filed release offer (what the
    stranded-park brief writes), as ``tests/hermes_cli/test_kanban_release.py`` builds it."""
    import json

    from hermes_cli import kanban_release
    from hermes_cli import kanban_spend_decision as decision

    task = kb.create_task(board, title="Retained work", assignee="default", session_id="chat")
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='triage' WHERE id=?", (task,))
        kb._append_event(board, task, decision.EVENT, {"reason": "Spend budget: 15 runs"})
    offer = kanban_release.offer(board, task)
    assert offer is not None
    ask = {**HUMAN_REQUEST, "release_target": offer}
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET human_request=? WHERE id=?", (json.dumps(ask), task))
        if filed:
            kb._append_event(board, task, "stranded_park_escalated", {"request": True, "release_target": offer})
    return task


def test_release_runs_as_an_explicit_human_writer_on_a_filed_offer(store, board, client, monkeypatch):
    from hermes_cli import kanban_release

    task = _spend_stopped_with_offer(board)
    shown = store.recompute("chat")["cards"][f"default/{task}"]["block_episode"]
    calls = []
    real = kanban_release.release

    def spy(conn, task_id, **kw):
        calls.append(kw)
        return real(conn, task_id, **kw)
    monkeypatch.setattr(kanban_release, "release", spy)
    reply = _post(client, "release", task, shown, request_id="rel-1")
    assert reply.status_code == 200 and reply.json()["ok"] is True, reply.json()
    (kw,) = calls
    assert kw["writer"] == kanban_release.human_writer("session-state-action:rel-1")  # never the process walk
    assert kw["author"] == "session-state" and callable(kw["precondition"])
    assert kb.get_task(board, task).status != "triage"
    record = board.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='stop_released'",
                           (task,)).fetchone()
    assert record is not None and '"authority_kind": "human"' in record[0]
    assert [r["request_id"] for r in store.resolutions("chat")] == ["rel-1"]


@pytest.mark.parametrize("filed", [False, True])
def test_release_is_refused_without_a_standing_offer_or_on_a_stale_episode(store, board, client, filed):
    task = _spend_stopped_with_offer(board, filed=filed)
    shown = store.recompute("chat")["cards"][f"default/{task}"]["block_episode"]
    if filed:  # the offer stands, but the click names an episode the card is no longer in
        reply = _post(client, "release", task, "evt:999999", request_id="rel-stale")
        assert reply.status_code == 409 and reply.json()["refusal"] == "stale_episode", reply.json()
    else:  # an ask that carries the stamp but was never filed by the home is not an offer
        reply = _post(client, "release", task, shown, request_id="rel-forged")
        assert reply.status_code == 409 and reply.json()["refusal"] == "not_offered"
    assert kb.get_task(board, task).status == "triage" and store.resolutions("chat") == []
    assert board.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='stop_released'",
                         (task,)).fetchone()[0] == 0


def test_release_is_refused_when_the_offer_was_filed_after_the_shown_state(store, board, client):
    """The card spend-stops in triage and is shown with no ask; the stranded-park sweep then files the release ask
    without a new lifecycle event. A click naming the shown (ask-less) episode must not release it."""
    import json

    from hermes_cli import kanban_release
    from hermes_cli import kanban_spend_decision as decision

    task = kb.create_task(board, title="Retained work", assignee="default", session_id="chat")
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET status='triage' WHERE id=?", (task,))
        kb._append_event(board, task, decision.EVENT, {"reason": "Spend budget: 15 runs"})
    shown = store.recompute("chat")["cards"][f"default/{task}"]["block_episode"]
    assert shown and "/ask:" not in shown
    offer = kanban_release.offer(board, task)
    with kb.write_txn(board):
        board.execute("UPDATE tasks SET human_request=? WHERE id=?",
                      (json.dumps({**HUMAN_REQUEST, "release_target": offer}), task))
        kb._append_event(board, task, "stranded_park_escalated", {"request": True, "release_target": offer})
    now = store.recompute("chat")["cards"][f"default/{task}"]["block_episode"]
    assert now.startswith(shown + "/ask:")
    reply = _post(client, "release", task, shown, request_id="rel-unseen")
    assert reply.status_code == 409 and reply.json()["refusal"] == "stale_episode", reply.json()
    assert kb.get_task(board, task).status == "triage" and store.resolutions("chat") == []
    assert board.execute("SELECT COUNT(*) FROM task_events WHERE task_id=? AND kind='stop_released'",
                         (task,)).fetchone()[0] == 0
    reply = _post(client, "release", task, now, request_id="rel-seen")  # the ask actually shown releases
    assert reply.status_code == 200, reply.json()


def test_unblock_is_refused_when_a_different_ask_replaced_the_shown_one(store, board, client):
    """A blocked card is shown with one ask; a different human_request lands while it stays blocked (same span).
    A click on the shown ask must not unblock the new one, and the shown ask's resolution must not cover it."""
    import json

    from hermes_session_state_actions import register_answer_handler

    task = kb.create_task(board, title="Deploy", assignee="worker", session_id="chat")
    assert kb.block_task(board, task, kind="needs_input", human_request=HUMAN_REQUEST)
    shown = store.recompute("chat")["cards"][f"default/{task}"]["block_episode"]
    assert "/ask:" in shown
    register_answer_handler(lambda request: {"ok": True})
    try:  # the shown ask is answered (the card stays blocked until its owner acts) ...
        assert _post(client, "answer", task, shown, request_id="answered", choice_id="a").status_code == 200
    finally:
        register_answer_handler(None)
    assert store.get("chat")["cards"][f"default/{task}"]["red"] is False
    other = {**HUMAN_REQUEST, "action": "Reply B in this chat. Unblock = ship B; archive = drop it."}
    with kb.write_txn(board):  # ... then a different ask lands while it stays blocked
        board.execute("UPDATE tasks SET human_request=? WHERE id=?", (json.dumps(other), task))
        kb._append_event(board, task, "blocked", {"kind": "needs_input"})
    now = store.recompute("chat")["cards"][f"default/{task}"]
    assert now["block_episode"] not in (None, shown) and now["red"] is True and now["resolution"] is None
    before = _unblocked_events(board, task)
    reply = _post(client, "unblock", task, shown, request_id="stale-ask")
    assert reply.status_code == 409 and reply.json()["refusal"] == "stale_episode", reply.json()
    assert _unblocked_events(board, task) == before and kb.get_task(board, task).status == "blocked"
    assert [r["request_id"] for r in store.resolutions("chat")] == ["answered"]
    reply = _post(client, "unblock", task, now["block_episode"], request_id="fresh-ask")
    assert reply.status_code == 200 and kb.get_task(board, task).status != "blocked"


def test_a_dropped_resolved_check_keeps_the_ask_its_episode_and_its_resolution(store, board, client):
    """``mark_dropped`` writes probe-custody markers into the stored human_request in place. They are not the ask:
    an answered ask stays answered (same episode, not red) and a click on the shown state is not stale."""
    from hermes_cli import kanban_ask_resolved as kar
    from hermes_session_state_actions import register_answer_handler

    task = kb.create_task(board, title="Deploy", assignee="worker", session_id="chat")
    assert kb.block_task(board, task, kind="needs_input",
                         human_request={**HUMAN_REQUEST, "resolved_check": "exit 1"})
    shown = store.recompute("chat")["cards"][f"default/{task}"]["block_episode"]
    assert "/ask:" in shown
    register_answer_handler(lambda request: {"ok": True})
    try:
        assert _post(client, "answer", task, shown, request_id="answered", choice_id="a").status_code == 200
    finally:
        register_answer_handler(None)
    with kb.write_txn(board):
        kar.mark_dropped(board, task, "broken", int(time.time()))
    assert "resolved_check_dropped_at" in board.execute(
        "SELECT human_request FROM tasks WHERE id=?", (task,)).fetchone()[0]  # the markers really landed
    card = store.recompute("chat")["cards"][f"default/{task}"]
    assert card["block_episode"] == shown and card["red"] is False and card["resolution"] is not None
    reply = _post(client, "unblock", task, shown, request_id="after-drop")
    assert reply.status_code == 200, reply.json()


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
