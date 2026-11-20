"""[daveey] "Force update" from Desktop's version pill: ``<root>/core-update/force-request`` asks hermes-sync to
push the pending core update through now. Its JSON is the cross-repo contract (hermes-sync reads it directly), and
``GET/POST /api/hermes/update/force`` report and write it."""
from __future__ import annotations

import asyncio
import json
import socket
import sqlite3
import time
import types

import pytest

from hermes_cli import core_update_force as cuf
from hermes_state import SessionDB


def test_write_then_read_round_trips_the_contract(tmp_path):
    before = time.time()
    body = cuf.write_force_request(tmp_path, "daveey")
    on_disk = json.loads((tmp_path / "core-update" / "force-request").read_text())
    assert on_disk == body
    assert set(body) == {"version", "requested_at", "expires_at", "by"}
    assert body["version"] == 1 and body["by"] == "daveey"
    assert before <= body["requested_at"] <= time.time()
    assert body["expires_at"] == pytest.approx(body["requested_at"] + 1800)
    assert cuf.read_force_request(tmp_path) == body
    assert not [p for p in (tmp_path / "core-update").iterdir() if p.name != "force-request"]  # no tmp left


def test_missing_request_reads_as_none(tmp_path):
    assert cuf.read_force_request(tmp_path) is None


def test_expired_request_reads_as_none(tmp_path):
    body = cuf.write_force_request(tmp_path, "dashboard")
    assert cuf.read_force_request(tmp_path, now=body["expires_at"] - 1) is not None
    assert cuf.read_force_request(tmp_path, now=body["expires_at"]) is None
    assert cuf.read_force_request(tmp_path, now=body["expires_at"] + 60) is None


@pytest.mark.parametrize("text", ["{not json", "[1, 2]", "null", json.dumps({"version": 1, "by": "x"}),
                                  json.dumps({"requested_at": "soon", "expires_at": 9e12}),
                                  json.dumps({"requested_at": 1.0, "expires_at": None})])
def test_garbage_reads_as_none(tmp_path, text):
    (tmp_path / "core-update").mkdir()
    (tmp_path / "core-update" / "force-request").write_text(text)
    assert cuf.read_force_request(tmp_path) is None


def test_running_turn_count_counts_only_live_leases(tmp_path):
    db = SessionDB(db_path=tmp_path / "state.db")
    try:
        for key in ("live", "expired", "other-live"):
            db.create_session(session_id=key, source="tui", model="test/model")
            assert db.try_acquire_session_turn_lease(key, f"holder-{key}", ttl_seconds=300)
    finally:
        db.close()
    with sqlite3.connect(tmp_path / "state.db") as conn:
        conn.execute("UPDATE session_turn_leases SET expires_at=? WHERE holder=?", (time.time() - 60, "holder-expired"))
    assert cuf.running_turn_count(tmp_path) == 2
    assert cuf.running_turn_count(tmp_path, now=time.time() + 3600) == 0


def test_running_turn_count_is_zero_without_a_readable_db(tmp_path, caplog):
    assert cuf.running_turn_count(tmp_path) == 0  # no state.db at all
    (tmp_path / "state.db").write_text("not a database")
    with caplog.at_level("WARNING", logger="hermes_cli.core_update_force"):
        assert cuf.running_turn_count(tmp_path) == 0
    assert any("could not count running turns" in r.getMessage() and r.exc_info for r in caplog.records)


# ── The REST routes ──────────────────────────────────────────────────────────────────────────────────


@pytest.fixture
def root(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    (home / "cache" / "core-update").mkdir(parents=True)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_HOME", str(home))
    return home


def _owe_update(root):
    now = int(time.time())  # window.json carries whole seconds
    (root / "cache" / "core-update" / "window.json").write_text(json.dumps(dict(
        version=1, host=socket.gethostname().split(".")[0], observed_at=now - 5, window_at=now - 300,
        state="draining", state_since=now - 300, drain_bound_s=7200, last_update_at=now - 5000, writer="drainer")))


def _request(principal=None):
    state = types.SimpleNamespace()
    if principal:
        state.token_principal = types.SimpleNamespace(principal=principal)
    return types.SimpleNamespace(state=state)


def _live_turn(root):
    db = SessionDB(db_path=root / "state.db")
    try:
        db.create_session(session_id="busy", source="tui", model="test/model")
        assert db.try_acquire_session_turn_lease("busy", "holder-busy", ttl_seconds=300)
    finally:
        db.close()


def test_routes_when_hermes_sync_owes_an_update(root):
    from hermes_cli.web_routers import actions

    _owe_update(root)
    _live_turn(root)
    state = asyncio.run(actions.hermes_update_force_state())
    assert state == {"available": True, "running_turns": 1, "pending": None}

    result = asyncio.run(actions.hermes_update_force(_request("token:desktop")))
    assert result["ok"] is True and result["running_turns"] == 1
    assert result["expires_at"] == pytest.approx(result["requested_at"] + 1800)
    on_disk = json.loads((root / "core-update" / "force-request").read_text())
    assert on_disk["by"] == "token:desktop" and on_disk["requested_at"] == result["requested_at"]
    assert asyncio.run(actions.hermes_update_force_state())["pending"] == on_disk

    asyncio.run(actions.hermes_update_force(_request()))
    assert json.loads((root / "core-update" / "force-request").read_text())["by"] == "dashboard"


def test_routes_when_no_update_is_owed(root):
    from hermes_cli.web_routers import actions

    state = asyncio.run(actions.hermes_update_force_state())
    assert state["available"] is False and state["running_turns"] == 0 and state["pending"] is None
    assert "nothing to force" in state["message"]

    response = asyncio.run(actions.hermes_update_force(_request("token:desktop")))
    assert response.status_code == 409
    body = json.loads(response.body)
    assert body["ok"] is False and body["message"] == state["message"] == body["detail"]
    assert not (root / "core-update" / "force-request").exists()
