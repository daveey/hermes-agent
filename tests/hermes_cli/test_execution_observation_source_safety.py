"""Safety contracts of the bounded Python source adapters (turn lease, card scope, history carry)."""
from __future__ import annotations

import hashlib
import json
import os
import time

from hermes_cli import kanban_execution_observations as keo
from hermes_cli import turn_session_activity as tsa
from hermes_cli.execution_observations import identity_key
from hermes_cli.turn_process_execution_observations import holder_digest
from tests.hermes_cli.test_turn_process_execution_observations import (
    _home_fixture, _leases, _only, _rest_fixture,
)

SECRET = f"pid={os.getpid()}:pidns=pid:[4026531836]:turn=sess:nonce=SECRETNONCE"


def _generation(holder):
    return "holder-sha256:" + hashlib.sha256(
        b"hermes.execution-observations.turn-lease-holder.v1\x00" + holder.encode("utf-8")).hexdigest()


# ── raw lease holder is a CAS credential: never serialized ───────────────────────────────────────


def test_holder_never_appears_in_any_observation_field_and_digest_is_domain_separated(home):
    now = time.time()
    _leases(home / "state.db", [("sess", SECRET, now - 5, now + 600)])
    row = tsa.annotate_turn_activity([{"id": "sess"}])[0]
    obs = _only(row, "turn.lease")
    text = json.dumps(row)
    assert SECRET not in text and "SECRETNONCE" not in text and "4026531836" not in text
    assert obs["identity"]["generation"] == _generation(SECRET) == "holder-sha256:" + holder_digest(SECRET)
    assert obs["state"] == "running"


def test_holder_absent_from_api_sessions_payload(rest, monkeypatch):
    holder = "pid=1:turn=busy:SECRETNONCE2"
    import sqlite3
    from hermes_constants import get_hermes_home
    conn = sqlite3.connect(get_hermes_home() / "state.db")
    conn.execute("UPDATE session_turn_leases SET holder=? WHERE conversation_id='busy'", (holder,))
    conn.commit()
    conn.close()
    tsa._reset_cache_for_tests()
    monkeypatch.setattr(tsa, "_holder_dead", lambda h: False)
    response = rest.get("/api/sessions?order=recent&limit=20")
    assert "SECRETNONCE2" not in response.text and holder not in response.text
    rows = {r["id"]: r for r in response.json()["sessions"]}
    assert _only(rows["busy"], "turn.lease")["identity"]["generation"] == _generation(holder)


def test_differing_holders_do_not_alias_and_equal_holders_stay_equivalent(home):
    now = time.time()
    keys = []
    for holder in (SECRET, SECRET + "x", SECRET):
        tsa._reset_cache_for_tests()
        (home / "state.db").unlink(missing_ok=True)
        _leases(home / "state.db", [("sess", holder, now - 5, now + 600)])
        keys.append(identity_key(_only(tsa.annotate_turn_activity([{"id": "sess"}])[0], "turn.lease")["identity"]))
    assert keys[0] != keys[1] and keys[0] == keys[2]


def test_padded_holder_stays_unknown_without_a_digest(home):
    now = time.time()
    _leases(home / "state.db", [("sess", " " + SECRET, now - 5, now + 600)])
    obs = _only(tsa.annotate_turn_activity([{"id": "sess"}])[0], "turn.lease")
    assert obs["state"] == "unknown" and "generation" not in obs["identity"]
    assert "SECRETNONCE" not in json.dumps(obs)


# ── local scope: non-secret, never raises ────────────────────────────────────────────────────────


def test_local_scope_reads_no_key_and_survives_every_lookup_failure(monkeypatch):
    from hermes_cli import kanban_origin, kanban_sync_client as client

    def forbidden(*a, **k):
        raise AssertionError("gateway.key must not be read just to name the gateway")

    monkeypatch.setattr(client, "load_identity", forbidden)
    monkeypatch.setattr(client.sync, "sync_config", lambda: {"gateway": "cfg-gw"})
    monkeypatch.setattr(kanban_origin, "gateway_name", lambda: "")
    assert keo.local_scope()[0] == "cfg-gw"

    monkeypatch.setattr(client.sync, "sync_config", dict)
    monkeypatch.setattr(kanban_origin, "gateway_name", lambda: "hub-gw")
    assert keo.local_scope()[0] == "hub-gw"

    def boom(*a, **k):
        raise RuntimeError("lookup failed")

    monkeypatch.setattr(client.sync, "sync_config", boom)
    monkeypatch.setattr(kanban_origin, "gateway_name", boom)
    assert keo.local_scope()[0] is None  # unknown, not an exception

    monkeypatch.setattr(kanban_origin, "gateway_name", lambda: "")
    monkeypatch.setattr(client.sync, "sync_config", dict)
    assert keo.local_scope()[0] is None


def test_add_cards_never_breaks_the_legacy_rows_when_scope_or_projection_fails(monkeypatch):
    def boom():
        raise RuntimeError("scope")

    monkeypatch.setattr(keo, "local_scope", boom)
    rows = [{"id": "t1", "status": "ready", "title": "keep"}]
    out = keo.add_cards(rows)
    assert out[0]["id"] == "t1" and out[0]["title"] == "keep"
    (value,) = [v for v in out[0]["execution_observations"] if v.get("source") == "kanban.card"]
    assert value["state"] == "unknown" and value["unknown_reason"] == "missing_qualified_identity"


def test_add_cards_uses_passed_scope_without_local_scope(monkeypatch):
    def boom():
        raise AssertionError("local_scope")

    monkeypatch.setattr(keo, "local_scope", boom)
    rows = [{"id": "t1", "status": "running", "title": "keep", "board": "default"}]
    out = keo.add_cards(rows, gateway="gw-home", profile="researcher")
    (value,) = [v for v in out[0]["execution_observations"] if v.get("source") == "kanban.card"]
    assert value["identity"]["gateway"] == "gw-home"
    assert value["identity"]["profile"] == "researcher"


# ── source failure keeps same-execution history only ─────────────────────────────────────────────


def _ask(text):
    return {"action": text, "why_human": "Owner required", "interaction": "approval", "scope": "s",
            "evidence": "ref"}


def _card(run, ask, status="blocked"):
    return keo.card({"id": "c", "board": "b", "status": status, "current_run_id": run,
                     "human_request": _ask(ask)}, gateway="g", profile="p", now=920)


def test_source_failure_carries_each_execution_history_without_crossing_generations():
    old, new = _card(6, "old ask"), _card(7, "new ask")
    row = {"id": "s", "execution_observations": [old, new], "kanban_unknown": True}
    keo.count_snapshot(row, (), now=930)
    carried = {v["identity"]["run_id"]: v for v in row["execution_observations"] if v["source"] == "kanban.card"}
    assert set(carried) == {6, 7}
    for run, text in ((6, "old ask"), (7, "new ask")):
        value = carried[run]
        assert value["state"] == "unknown" and value["freshness"] != "current"
        assert [r["action"] for r in value["retained_human_requests"]] == [text]
        assert "resolved_check" not in json.dumps(value)


def test_successful_count_read_does_not_resurrect_unconfirmed_history():
    row = {"id": "s", "execution_observations": [_card(7, "stale ask")]}
    keo.count_snapshot(row, [("g/p/b/c", "active", False)], now=930)
    assert all(not v.get("retained_human_requests") and not v.get("human_request")
               for v in row["execution_observations"])


def test_previous_without_valid_identity_is_not_carried_on_failure():
    bad = {"source": "kanban.card", "state": "needs_user", "human_request": _ask("who?")}
    row = {"id": "s", "execution_observations": [bad], "kanban_unknown": True}
    keo.count_snapshot(row, (), now=930)
    assert not any(v.get("human_request") or v.get("retained_human_requests")
                   for v in row.get("execution_observations", []))
