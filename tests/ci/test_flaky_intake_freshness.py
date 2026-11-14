"""Intake admission must not turn stale foreign ownership into absence."""
import json
import sqlite3
import time

import pytest

from scripts.ci import flaky_kanban as fk
from tests.ci import test_flaky_kanban_overlap as helpers

env = helpers.env


@pytest.mark.parametrize("cached", [False, True])
@pytest.mark.parametrize("proof", ["missing", "stale", "future", "unknown", "duplicate", "wrong-profile",
                                  "wrong-board", "wrong-gateway", "fresh", "published-period"])
def test_intake_foreign_scope_proof(env, cached, proof):
    env.remote("macbook", "default/default/t_owner", "running", f"Deflake {helpers.TEST}")
    path = env.tmp / "hub" / "hub.db"
    now = time.time()
    scope = {"profile": "default", "board": "default", "state": "ok", "observed_at": now}
    if proof == "stale":
        scope["observed_at"] = now - 1801
    if proof == "future":
        scope["observed_at"] = now + 3600
    if proof == "unknown":
        scope["state"] = "unknown"
    if proof == "wrong-profile":
        scope["profile"] = "other"
    if proof == "wrong-board":
        scope["board"] = "other"
    if proof == "published-period":
        scope["observed_at"] = now - 600
    with sqlite3.connect(path) as conn:
        conn.execute("DELETE FROM replica WHERE coll='gateway'")
        if proof != "missing":
            info = {"gateway": "macbook", "task_scopes": [scope] * (2 if proof == "duplicate" else 1)}
            if proof == "wrong-gateway":
                info["gateway"] = "other"
            if proof == "published-period":
                info["scope_heartbeat_period_s"] = 400
            conn.execute("INSERT INTO replica VALUES (?, 'gateway', 'info', 1, 1, ?, 0)",
                         ("macbook", json.dumps(info)))
    state = {"cards": {}}
    if cached:
        state["cards"][helpers.TEST] = {"card": "t_owner", "gateway": "macbook", "board": "default"}
    admitted = []
    def invoke():
        return fk.file_cards([helpers._entry()], run_url=helpers.RUN, branch="a" * 40,
                             board=fk.CliBoard(env.hermes), state=state, relay=fk.HubRelay(path, wait=0),
                             on_unowned=lambda: admitted.append(True) or "queued")
    if proof in ("fresh", "published-period"):
        assert invoke()[0].startswith("appended")
        assert len(env.remote_comments("macbook", "default/default/t_owner")) == 1
    else:
        with pytest.raises(RuntimeError, match="owner scope freshness unavailable"):
            invoke()
        assert env.hub_calls() == []
    assert not admitted
    assert not env.kanban()["cards"]


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("first_status,first_cites,second_cites,expected", [
    ("done", False, False, "t_second"),  # incomplete history is not proof against a positive owner
    ("running", False, True, "t_second"),  # exact incident outranks open test-only owner
    ("done", True, True, "t_second"),  # open incident owner outranks closed incident
    ("done", True, False, "t_first"),  # closed incident outranks open test-only owner
])
def test_foreign_owner_precedence_is_independent_of_replica_order(
    env, reverse, first_status, first_cites, second_cites, expected,
):
    owners = [("t_first", first_status, first_cites), ("t_second", "running", second_cites)]
    for cid, status, cites in reversed(owners) if reverse else owners:
        env.remote("macbook", f"default/default/{cid}", status, f"Deflake {helpers.TEST}",
                   helpers.RUN if cites else "")
    state = {"cards": {}}
    admitted = []
    def invoke():
        return fk.file_cards([helpers._entry()], run_url=helpers.RUN, branch="a" * 40,
                             board=fk.CliBoard(env.hermes), state=state,
                             relay=fk.HubRelay(env.tmp / "hub" / "hub.db", wait=0),
                             on_unowned=lambda: admitted.append(True) or "queued")
    result = invoke()
    assert state["cards"][helpers.TEST]["card"] == expected
    assert not admitted and not env.kanban()["cards"]
    if expected == "t_first":
        assert result[0].startswith("skipped")
        assert env.hub_calls() == []
    else:
        assert result[0].startswith("appended")
        assert len(env.remote_comments("macbook", "default/default/t_second")) == 1
        assert env.remote_comments("macbook", "default/default/t_first") == []


@pytest.mark.parametrize("unavailable", ["stale", "unreadable"])
@pytest.mark.parametrize("positive", [False, True])
def test_unknown_foreign_candidate_does_not_hide_known_positive(env, monkeypatch, unavailable, positive):
    env.remote("macbook", "default/default/t_unknown", "running", f"Deflake {helpers.TEST}")
    if positive:
        env.remote("worker", "default/default/t_known", "running", f"Deflake {helpers.TEST}")
    relay = fk.HubRelay(env.tmp / "hub" / "hub.db", wait=0)
    original = relay.card
    def card(gw, board, cid):
        return None if gw == "macbook" and unavailable == "unreadable" else original(gw, board, cid)
    monkeypatch.setattr(relay, "card", card)
    if unavailable == "stale":
        with sqlite3.connect(relay.db) as conn:
            conn.execute("DELETE FROM replica WHERE coll='gateway' AND gw='macbook'")
    state = {"cards": {}}
    def invoke():
        return fk.file_cards([helpers._entry()], run_url=helpers.RUN, branch="a" * 40,
                             board=fk.CliBoard(env.hermes), state=state, relay=relay,
                             on_unowned=lambda: pytest.fail("unknown owner must not admit intake"))
    if positive:
        assert invoke()[0].startswith("appended")
        assert state["cards"][helpers.TEST]["card"] == "t_known"
        assert len(env.remote_comments("worker", "default/default/t_known")) == 1
    else:
        with pytest.raises(RuntimeError, match="owner .*unavailable"):
            invoke()
        assert env.hub_calls() == []
    assert env.remote_comments("macbook", "default/default/t_unknown") == []


@pytest.mark.parametrize("status", ["done", "archived"])
@pytest.mark.parametrize("complete", [False, True])
@pytest.mark.parametrize("same_job", [False, True])
def test_closed_foreign_overlap_uses_full_history_proof(env, status, complete, same_job):
    ctx = fk._Flake(helpers.TEST, helpers.RUN, "a" * 40, "", fk.CliBoard(env.hermes),
                    {"cards": {}}, None, "hub")
    owner = {"id": "t_closed", "gateway": "macbook", "board": "default", "status": status,
             "title": f"Deflake {helpers.TEST}", "body": "", "comments": 1 if complete else 10,
             "last_comments": [{"body": helpers.RUN if same_job else "another job"}]}
    if same_job or complete:
        assert fk._overlap_owns(ctx, owner) is same_job
    else:
        with pytest.raises(RuntimeError, match="closed foreign owner history unavailable"):
            fk._overlap_owns(ctx, owner)
