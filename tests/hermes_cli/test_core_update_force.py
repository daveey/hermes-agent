"""[daveey] "Force update" from Desktop's version pill: ``<root>/core-update/force-request`` asks hermes-sync to
push the pending core update through now. Its JSON is the cross-repo contract (hermes-sync reads it directly), and
``GET/POST /api/hermes/update/force`` report and write it."""
from __future__ import annotations

import asyncio
import json
import socket
import sqlite3
import threading
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


def test_concurrent_writes_never_collide_on_the_temp_file(tmp_path):
    """Two request handlers in one process (same pid) used to share one temp name: one replace then failed."""
    start, errors = threading.Barrier(8), []

    def write(n):
        start.wait()
        try:
            for _ in range(25):
                cuf.write_force_request(tmp_path, f"writer-{n}")
        except Exception as exc:  # collected and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(n,)) for n in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)
    assert errors == []
    assert cuf.read_force_request(tmp_path)["by"].startswith("writer-")
    assert [p.name for p in (tmp_path / "core-update").iterdir()] == ["force-request"]  # no temp file left


def _leases(home, *keys, expired=()):
    home.mkdir(parents=True, exist_ok=True)
    db = SessionDB(db_path=home / "state.db")
    try:
        for key in (*keys, *expired):
            db.create_session(session_id=key, source="tui", model="test/model")
            assert db.try_acquire_session_turn_lease(key, f"holder-{key}", ttl_seconds=300)
    finally:
        db.close()
    with sqlite3.connect(home / "state.db") as conn:
        for key in expired:
            conn.execute("UPDATE session_turn_leases SET expires_at=? WHERE holder=?", (time.time() - 60, f"holder-{key}"))


def test_running_turn_count_counts_only_live_leases(tmp_path):
    _leases(tmp_path, "live", "other-live", expired=("expired",))
    assert cuf.running_turn_count(tmp_path) == 2
    assert cuf.running_turn_count(tmp_path, now=time.time() + 3600) == 0


def test_running_turn_count_spans_every_profile_home_under_the_root(tmp_path):
    _leases(tmp_path, "root-turn")
    _leases(tmp_path / "profiles" / "coder", "coder-turn", "coder-turn-2", expired=("coder-old",))
    (tmp_path / "profiles" / "empty").mkdir()  # a profile with no state.db yet adds nothing
    assert cuf.running_turn_count(tmp_path) == 3


def test_running_turn_count_is_unknown_when_a_db_is_unreadable(tmp_path, caplog):
    assert cuf.running_turn_count(tmp_path) == 0  # no state.db at all: nothing runs
    _leases(tmp_path, "root-turn")
    (tmp_path / "profiles" / "broken").mkdir(parents=True)
    (tmp_path / "profiles" / "broken" / "state.db").write_text("not a database")
    with caplog.at_level("WARNING", logger="hermes_cli.core_update_force"):
        assert cuf.running_turn_count(tmp_path) is None  # never reported as 0 (or as the readable part)
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


def _sync_idle(root):
    """hermes-sync runs here (fresh window record) but owes no update."""
    now = int(time.time())
    (root / "cache" / "core-update" / "window.json").write_text(json.dumps(dict(
        version=1, host=socket.gethostname().split(".")[0], observed_at=now - 5, window_at=now + 3600,
        state="idle", state_since=now - 300, drain_bound_s=7200, last_update_at=now - 5000, writer="drainer")))


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
    assert state == {"available": True, "running_turns": 1, "pending": None, "managed": True}

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
    assert state["managed"] is False and "nothing to force" in state["message"]

    response = asyncio.run(actions.hermes_update_force(_request("token:desktop")))
    assert response.status_code == 409
    body = json.loads(response.body)
    assert body["ok"] is False and body["message"] == state["message"] == body["detail"]
    assert not (root / "core-update" / "force-request").exists()


def test_unreadable_count_is_reported_as_unknown(root):
    from hermes_cli.web_routers import actions

    _owe_update(root)
    (root / "state.db").write_text("not a database")
    assert asyncio.run(actions.hermes_update_force_state())["running_turns"] is None
    assert asyncio.run(actions.hermes_update_force(_request()))["running_turns"] is None


# ── Through the FastAPI app: the auth gate and the 409's wire shape ──────────────────────────────────


@pytest.fixture
def client(root, monkeypatch):
    from fastapi.testclient import TestClient

    from hermes_cli import web_server

    monkeypatch.setattr(web_server, "_SESSION_TOKEN", "force-update-test-token")
    return TestClient(web_server.app, base_url="http://127.0.0.1")


AUTH = {"X-Hermes-Session-Token": "force-update-test-token"}


def test_app_routes_require_the_session_token(client):
    assert client.get("/api/hermes/update/force").status_code == 401
    assert client.post("/api/hermes/update/force").status_code == 401


def test_app_routes_report_write_and_refuse(client, root):
    refused = client.post("/api/hermes/update/force", headers=AUTH)
    assert refused.status_code == 409
    assert refused.json()["ok"] is False and refused.json()["error"] == "force_update_unavailable"
    assert "nothing to force" in refused.json()["detail"]
    assert refused.json()["detail"] == refused.json()["message"]
    got = client.get("/api/hermes/update/force", headers=AUTH)
    assert got.status_code == 200 and got.json()["available"] is False and got.json()["running_turns"] == 0

    _owe_update(root)
    _live_turn(root)
    assert client.get("/api/hermes/update/force", headers=AUTH).json() == {
        "available": True, "running_turns": 1, "pending": None, "managed": True}
    posted = client.post("/api/hermes/update/force", headers=AUTH)
    assert posted.status_code == 200 and posted.json()["ok"] is True and posted.json()["running_turns"] == 1
    assert client.get("/api/hermes/update/force", headers=AUTH).json()["pending"]["by"] == "dashboard"


# ── [daveey] Desktop hands Update to hermes-sync on a host it manages ───────────────────────────────────


def test_a_host_hermes_sync_manages_but_owes_nothing_says_so(root):
    from hermes_cli.web_routers import actions

    _sync_idle(root)
    state = asyncio.run(actions.hermes_update_force_state())
    assert state["managed"] is True and state["available"] is False
    assert "hermes-sync applies Hermes updates on this host" in state["message"]


def test_a_stale_window_record_is_not_managed(root):
    from hermes_cli.web_routers import actions

    _sync_idle(root)
    path = root / "cache" / "core-update" / "window.json"
    body = json.loads(path.read_text())
    body["observed_at"] -= 3600  # older than kanban_deploy_window.FRESH_SECONDS: hermes-sync stopped publishing
    path.write_text(json.dumps(body))
    assert asyncio.run(actions.hermes_update_force_state())["managed"] is False


def test_desktops_own_update_is_refused_on_a_managed_host(root):
    from hermes_cli.web_routers import actions

    _sync_idle(root)
    refused = asyncio.run(actions.update_hermes())
    assert refused["ok"] is False and refused["error"] == "managed_by_hermes_sync"
    assert refused["update_command"] == "hermes update"  # the manual way out when hermes-sync is stuck
    assert "hermes-sync applies Hermes updates on this host" in refused["message"]
