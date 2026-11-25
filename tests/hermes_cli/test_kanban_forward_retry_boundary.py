"""Fail-closed transport recovery after a definitive binding refusal."""
import json

import pytest

from hermes_cli import kanban_placement as kp

CREATOR = {"gateway": "w", "profile": "default", "board": "default", "task_id": "t_0123456789abcdef"}
ORIGIN = {"gateway": "hub", "profile": "default", "session": "s1"}


def _history():
    return {"status": "rejected", "code": "origin_forward_binding_missing", "handoff_complete": False}, {
        "coll": "sessions", "action": "origin-forward", "args": {
            "origin": ORIGIN, "creator": CREATOR, "run_uid": "run-1", "create": {"idempotency_key": "product-key"}}}


@pytest.mark.parametrize("case", [
    "valid", "same-run", "keyless", "wrong-key", "wrong-creator", "wrong-origin", "missing-run",
    "empty-run", "null-run", "integer-run", "list-run", "object-run",
    "done", "sent", "queued", "failed", "expired", "unknown", "other-code", "handoff", "missing-handoff",
    "wrong-status", "wrong-action", "wrong-collection", "malformed-body", "malformed-result", "absent", "legacy-row",
])
def test_binding_retry_boundary(monkeypatch, case):
    result, body = _history()
    options, state, run = {"idempotency_key": "product-key"}, "rejected", "run-2"
    mutations = {
        "wrong-key": lambda: options.update(idempotency_key="different"),
        "wrong-creator": lambda: body["args"].update(creator={**CREATOR, "task_id": "t_abcdef0123456789"}),
        "wrong-origin": lambda: body["args"].update(origin={**ORIGIN, "session": "elsewhere"}),
        "missing-run": lambda: body["args"].pop("run_uid"),
        "empty-run": lambda: body["args"].update(run_uid=""),
        "null-run": lambda: body["args"].update(run_uid=None),
        "integer-run": lambda: body["args"].update(run_uid=3),
        "list-run": lambda: body["args"].update(run_uid=["run-1"]),
        "object-run": lambda: body["args"].update(run_uid={"uid": "run-1"}),
        "other-code": lambda: result.update(code="self_work_refused"),
        "handoff": lambda: result.update(handoff_complete=True),
        "missing-handoff": lambda: result.pop("handoff_complete"),
        "wrong-status": lambda: result.update(status="failed"),
        "wrong-action": lambda: body.update(action="create"),
        "wrong-collection": lambda: body.update(coll="tasks"),
    }
    mutations.get(case, lambda: None)()
    if case == "same-run":
        run = "run-1"
    if case == "keyless":
        options = {}
    if case in ("done", "sent", "queued", "failed", "expired", "unknown"):
        state = None if case == "unknown" else case
    row = (state, "{" if case == "malformed-result" else json.dumps(result),
           "{" if case == "malformed-body" else json.dumps(body))
    if case == "absent":
        row = None
    elif case == "legacy-row":
        row = row[:2]
    monkeypatch.setattr(kp, "_sent_row", lambda rid: row)
    assert kp._forward_binding_retry("rid", options, "hub", "default", "s1", CREATOR, run) == (case == "valid")


def test_retry_reads_real_immutable_history(monkeypatch, tmp_path):
    import sqlite3

    path = tmp_path / "courier.db"
    result, body = _history()
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE sent (id TEXT PRIMARY KEY, state TEXT, result TEXT, body TEXT)")
        db.execute("INSERT INTO sent VALUES (?, ?, ?, ?)", ("original", "rejected", json.dumps(result), json.dumps(body)))
    monkeypatch.setattr(kp.reg, "hub_db_path", lambda: path)
    before = path.read_bytes()
    assert kp._forward_binding_retry("original", {"idempotency_key": "product-key"}, "hub", "default", "s1", CREATOR, "run-2")
    assert not kp._forward_binding_retry("original", {"idempotency_key": "product-key"}, "hub", "default", "s1", CREATOR, "run-1")
    assert path.read_bytes() == before


@pytest.mark.parametrize("case", ["sql-null", "json-null", "json-list", "missing-args", "list-args", "string-create"])
def test_malformed_history_retains_original_transport_and_refusal(monkeypatch, case):
    result, body = _history()
    if case == "missing-args":
        body.pop("args")
    elif case == "list-args":
        body["args"] = []
    elif case == "string-create":
        body["args"]["create"] = "invalid"
    raw_result = {"sql-null": None, "json-null": "null", "json-list": "[]"}.get(case, json.dumps(result))
    monkeypatch.setattr(kp, "_sent_row", lambda rid: ("rejected", raw_result, json.dumps(body)))
    options = {"idempotency_key": "product-key"}
    assert not kp._forward_binding_retry("original", options, "hub", "default", "s1", CREATOR, "run-2")
    monkeypatch.setattr(kp, "hub_carries_options", lambda cli: True)
    monkeypatch.setattr(kp, "remote_state", lambda rid: "rejected")
    expected = kp.request_id("w", options, "s1", options)
    sent = []

    def refuse(argv):
        sent.append(argv[argv.index("--id") + 1])
        return 2, "", "request_id_conflict: unchanged immutable refusal"

    monkeypatch.setattr(kp, "hub_runner", refuse)
    out = kp.create_remote(options, target="hub", self_gw="w", origin_session="s1", origin_profile="default",
                           cfg={}, origin_gateway="hub", creator=CREATOR, run_uid="run-2")
    assert sent == [expected]
    assert out == {"ok": False, "request_id": expected, "rc": 2, "fatal": True,
                   "error": "request_id_conflict: unchanged immutable refusal"}
