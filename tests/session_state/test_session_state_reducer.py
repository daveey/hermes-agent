"""SessionState v2 schema and pure reducer (P1 adapter over ``reduce_status``). [daveey]"""
from __future__ import annotations

import copy
import json
import time
from pathlib import Path

import pytest

import hermes_session_state_reducer as reducer
from hermes_cli import kanban_db as kb
from hermes_session_state_collect import collect
from hermes_session_state_schema import JSON_SCHEMA, validate
from hermes_session_state_store import digest
from tests.session_state.conftest import HUMAN_REQUEST

ROOT = Path(__file__).resolve().parents[2]


def test_committed_json_schema_is_generated_from_the_dict():
    committed = json.loads((ROOT / "schemas" / "session_state_v2.schema.json").read_text())
    assert committed == json.loads(json.dumps(JSON_SCHEMA))


def test_validate_rejects_malformed_objects(store, board):
    kb.create_task(board, title="Work", assignee="worker", session_id="chat")
    obj = store.recompute("chat")
    assert validate(obj) == []
    for mutate, expected in [
        (lambda o: o.update(version=1), "$.version"),
        (lambda o: o.update(rev=True), "$.rev"),
        (lambda o: o["header"].update(colour="pink"), "$.header.colour"),
        (lambda o: o.update(extra=1), "$.extra: unexpected"),
        (lambda o: o["identity"].pop("lineage"), "$.identity.lineage: required"),
        (lambda o: next(iter(o["cards"].values())).update(blocked_on="moon"), "blocked_on"),
    ]:
        bad = copy.deepcopy(obj)
        mutate(bad)
        assert any(expected in e for e in validate(bad)), expected


def test_reduce_is_pure_and_deterministic(db, board):
    task = kb.create_task(board, title="Deploy", assignee="worker", session_id="chat")
    assert kb.block_task(board, task, kind="needs_input", human_request=HUMAN_REQUEST)
    inputs = collect(db, "chat")
    frozen = copy.deepcopy(inputs)
    now = time.time()
    a = reducer.reduce(inputs, now=now).to_dict()
    b = reducer.reduce(inputs, now=now).to_dict()
    assert a == b and digest(a) == digest(b) and inputs == frozen


def test_a_committed_resolution_for_the_current_episode_clears_red(db, board):
    task = kb.create_task(board, title="Deploy", assignee="worker", session_id="chat")
    assert kb.block_task(board, task, kind="needs_input", human_request=HUMAN_REQUEST)
    inputs = collect(db, "chat")
    episode = inputs["graphs"]["default"]["episodes"][task]
    key = f"default/{task}"
    base = {"request_id": "r", "card": key, "action": "answer", "choice_id": "yes", "label": "Ship it",
            "by": "user", "at": 1.0, "delivery": "committed", "episode": episode}
    resolved = reducer.reduce(inputs, now=time.time(), resolutions=[base]).to_dict()
    assert resolved["cards"][key]["red"] is False and resolved["asks"] == []
    assert resolved["header"]["status"] == "waiting" and "Ship it" in resolved["cards"][key]["detail"]
    for other in ({**base, "episode": "evt:0"}, {**base, "delivery": "failed"}, {**base, "card": "default/x"}):
        assert reducer.reduce(inputs, now=time.time(), resolutions=[other]).to_dict()["cards"][key]["red"]
    assert digest(resolved) != digest(reducer.reduce(inputs, now=time.time()).to_dict())


def test_register_reducer_replaces_and_restores_the_adapter(db, board):
    kb.create_task(board, title="Work", assignee="worker", session_id="chat")
    inputs = collect(db, "chat")
    seen = []

    def custom(inp, *, now, resolutions):
        seen.append(now)
        return reducer.legacy_adapter(inp, now=now, resolutions=resolutions)
    reducer.register_reducer(custom)
    try:
        reducer.reduce(inputs, now=5.0)
    finally:
        reducer.register_reducer(None)
    assert seen == [5.0]
    with pytest.raises(ValueError):
        reducer.register_reducer("nope")  # type: ignore[arg-type]


def test_an_unreadable_closure_is_unknown_coverage_not_empty(db, board, monkeypatch):
    import hermes_session_state_collect as collect_module

    kb.create_task(board, title="Work", assignee="worker", session_id="chat")

    def broken(path, linked):
        raise OSError("gone")
    monkeypatch.setattr(collect_module, "local_graph", broken)
    obj = reducer.reduce(collect(db, "chat"), now=time.time()).to_dict()
    assert obj["sources"]["closure"] == {"coverage": "unknown", "reason": "closure_read_failed:OSError"}


@pytest.mark.parametrize("status,parents_open,expected", [
    ("running", False, None), ("done", False, None), ("ready", False, "capacity"), ("todo", True, "card"),
    ("blocked", True, "card"), ("triage", False, "agent"), ("review", False, "agent"), ("unknown", False, "unknown"),
])
def test_blocked_on_classification(status, parents_open, expected):
    assert reducer._blocked_on({"status": status}, human=False, parents_open=parents_open) == expected


def test_blocked_on_prefers_human_then_producer_waits_and_stale_is_unknown():
    assert reducer._blocked_on({"status": "blocked"}, human=True, parents_open=True) == "human"
    for kind in ("ci", "deploy", "clock"):
        assert reducer._blocked_on({"status": "blocked", "wait": {"wait_kind": kind}}, human=False,
                                   parents_open=False) == kind
    assert reducer._blocked_on({"status": "blocked", "stale": True}, human=True, parents_open=False) == "unknown"


def test_single_wait_kind_colours_the_header():
    from hermes_session_state_schema import Card

    def card(kind):
        return Card(key=f"b/{kind}", id=kind, board="b", gateway="g", title=kind, lifecycle="blocked",
                    blocked_on=kind, detail="")
    status = {"kind": "waiting", "presentation": {"stripe": "waiting"}}
    assert reducer._header(status, {"b/ci": card("ci")}, [], False).colour == "light_blue"
    assert reducer._header(status, {"b/deploy": card("deploy")}, [], False).colour == "purple"
    mixed = {"b/ci": card("ci"), "b/deploy": card("deploy")}
    assert reducer._header(status, mixed, [], False).colour == "blue"
