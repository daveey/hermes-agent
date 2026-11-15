"""GET/PATCH/comment on a card another gateway created, which this gateway has only in the Board replica. [daveey]

A synced board's live file holds only this gateway's own cards; the replica (``kanban/sync-shadow/<board>.db``)
holds the whole board. A card created on a Board worker node (worker-1) read as 404 on every gateway, so a chat
concluded it was a phantom (t_01a19fd40c53bb19). Reads now fall back to the replica (``source: replica`` +
``owner``); writes are refused 409 ``foreign_card`` naming the owner. Ready live cards carry a ``queue`` position.
"""
from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync


def _load_plugin_router():
    plugin_file = Path(__file__).resolve().parents[2] / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_kanban_foreign_card_test", plugin_file)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.router


def make_replica_card(home: Path, *, title: str = "Made on worker-1", owner: str = "worker-1",
                      board: str = "default") -> str:
    """A full-schema replica at ``<home>/kanban/sync-shadow/<board>.db`` holding one card ``owner`` created."""
    path = home / "kanban" / sync.SHADOW_DIR / f"{board}.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = kbc.connect(path)
    try:
        tid = kb.create_task(conn, title=title, assignee="default")
        kb.add_comment(conn, tid, author="worker", body="from the worker")
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        if "created_on" not in cols:
            conn.execute("ALTER TABLE tasks ADD COLUMN created_on TEXT")
        conn.execute("UPDATE tasks SET created_on = ? WHERE id = ?", (owner, tid))
        conn.commit()
    finally:
        conn.close()
    return tid


def enable_sync(monkeypatch, boards=("default",)) -> None:
    monkeypatch.setattr(sync, "sync_config", lambda: {"enabled": True, "boards": list(boards)})


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return kb.kanban_home()


@pytest.fixture
def api(kanban_home):
    app = FastAPI()
    app.include_router(_load_plugin_router(), prefix="/api/plugins/kanban")
    return TestClient(app)


def _live_card(title="Live card", **kw) -> str:
    conn = kbc.connect()
    try:
        return kb.create_task(conn, title=title, **kw)
    finally:
        conn.close()


def test_replica_only_card_reads_with_source_and_owner(kanban_home, api, monkeypatch):
    tid = make_replica_card(kanban_home)
    enable_sync(monkeypatch)
    r = api.get(f"/api/plugins/kanban/tasks/{tid}")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "replica"
    assert body["owner"] == "worker-1"
    assert body["task"]["id"] == tid and body["task"]["title"] == "Made on worker-1"
    assert [c["body"] for c in body["comments"]] == ["from the worker"]
    assert "queue" not in body  # queue position is for the live dispatcher's own cards
    assert "wait_observation" not in body


def test_replica_ignored_when_board_does_not_sync(kanban_home, api, monkeypatch):
    tid = make_replica_card(kanban_home)
    enable_sync(monkeypatch, boards=("other",))
    assert api.get(f"/api/plugins/kanban/tasks/{tid}").status_code == 404
    monkeypatch.setattr(sync, "sync_config", lambda: {"enabled": False})
    assert api.get(f"/api/plugins/kanban/tasks/{tid}").status_code == 404


def test_live_card_unchanged_plus_source_live(kanban_home, api, monkeypatch):
    make_replica_card(kanban_home)
    enable_sync(monkeypatch)
    tid = _live_card()
    body = api.get(f"/api/plugins/kanban/tasks/{tid}").json()
    assert body["source"] == "live"
    assert body["wait_observation"] == {"board": "default", "human_request": False,
                                         "parked_agent": False, "orphan_reason": None}
    assert "owner" not in body
    assert set(body) >= {"task", "comments", "events", "attachments", "links", "link_tasks", "child_results", "runs"}


def test_missing_everywhere_is_404(kanban_home, api, monkeypatch):
    make_replica_card(kanban_home)
    enable_sync(monkeypatch)
    r = api.get("/api/plugins/kanban/tasks/t_0000000000000000")
    assert r.status_code == 404
    assert "not found" in r.json()["detail"]


def test_comment_on_replica_only_card_is_409_foreign_card(kanban_home, api, monkeypatch):
    tid = make_replica_card(kanban_home)
    enable_sync(monkeypatch)
    r = api.post(f"/api/plugins/kanban/tasks/{tid}/comments", json={"body": "hi", "author": "chat"})
    assert r.status_code == 409, r.text
    detail = r.json()["detail"]
    assert detail.startswith("foreign_card: ")
    assert f"{tid} was created on worker-1 and lives there" in detail
    assert "Its owner or the Board must write it." in detail


def test_patch_on_replica_only_card_is_409_foreign_card(kanban_home, api, monkeypatch):
    tid = make_replica_card(kanban_home)
    enable_sync(monkeypatch)
    r = api.patch(f"/api/plugins/kanban/tasks/{tid}", json={"priority": 5})
    assert r.status_code == 409, r.text
    assert r.json()["detail"].startswith(f"foreign_card: {tid} was created on worker-1")


def test_comment_on_missing_card_stays_404(kanban_home, api, monkeypatch):
    make_replica_card(kanban_home)
    enable_sync(monkeypatch)
    r = api.post("/api/plugins/kanban/tasks/t_0000000000000000/comments", json={"body": "hi"})
    assert r.status_code == 404


def test_comment_on_live_card_still_lands(kanban_home, api, monkeypatch):
    make_replica_card(kanban_home)
    enable_sync(monkeypatch)
    tid = _live_card()
    assert api.post(f"/api/plugins/kanban/tasks/{tid}/comments", json={"body": "ok"}).status_code == 200


# --- queue position (ready cards in the live file) -------------------------------------------

def _set(tid, **cols):
    conn = kbc.connect()
    try:
        for k, v in cols.items():
            conn.execute(f"UPDATE tasks SET {k} = ? WHERE id = ?", (v, tid))
        conn.commit()
    finally:
        conn.close()


def test_ready_card_reports_local_queue_position(kanban_home, api, monkeypatch):
    from hermes_cli import kanban_db_dispatch as kbd
    monkeypatch.setattr(kbd, "configured_max_in_progress", lambda: 6)
    kb.create_board("other")  # 2 workers on another board, really counted through its file
    with kbc.connect_closing(board="other") as other:
        for i in range(2):
            busy = kb.create_task(other, title=f"busy {i}", assignee="default")
            other.execute("UPDATE tasks SET status='running' WHERE id=?", (busy,))
        other.commit()
    high = _live_card("p5 new", assignee="default", priority=5)
    old = _live_card("p0 old", assignee="default", priority=0)
    new = _live_card("p0 new", assignee="default", priority=0)
    other = _live_card("p9 other assignee", assignee="someone-else", priority=9)
    running = _live_card("running", assignee="default", priority=0)
    claimed = _live_card("claimed ready", assignee="default", priority=9)
    pool = _live_card("worker-first", assignee="default", priority=9)
    _set(old, created_at=100)
    _set(new, created_at=200)
    _set(high, created_at=300)
    _set(other, created_at=50)
    _set(running, status="running", created_at=10)
    _set(claimed, claim_lock="host:1", created_at=10)  # being claimed this tick: not ahead in the lane
    from hermes_cli import kanban_worker_placement as kwp
    _set(pool, placement_policy=kwp.POLICY, created_at=10)  # waits for the worker pool, not a local slot
    # Fix the ready episode as well as card creation: real creation events use
    # whole seconds and can otherwise straddle a clock tick during this fixture.
    with kbc.connect_closing() as conn:
        conn.execute("UPDATE task_events SET created_at=1000 WHERE kind='created'")
        conn.commit()
    q = api.get(f"/api/plugins/kanban/tasks/{new}").json()["queue"]
    assert isinstance(q.pop("observed_at"), int)
    assert q == {"lane": "local", "ahead": 2, "running": 3, "limit": 6, "approximate": True, "board": "default"}
    assert api.get(f"/api/plugins/kanban/tasks/{old}").json()["queue"]["ahead"] == 1
    assert api.get(f"/api/plugins/kanban/tasks/{high}").json()["queue"]["ahead"] == 0


def test_done_card_has_no_queue(kanban_home, api):
    tid = _live_card()
    _set(tid, status="done")
    assert "queue" not in api.get(f"/api/plugins/kanban/tasks/{tid}").json()


def test_live_orphan_proof_is_owner_computed(kanban_home, api):
    tid = _live_card()
    _set(tid, status="blocked", block_kind=None, human_request=None)
    proof = api.get(f"/api/plugins/kanban/tasks/{tid}").json()["wait_observation"]
    assert proof["orphan_reason"] and proof["parked_agent"] is True


def test_worker_first_card_reports_worker_pool_reason(kanban_home, api):
    from hermes_cli import kanban_worker_placement as kwp
    tid = _live_card()
    _set(tid, placement_policy=kwp.POLICY)
    conn = kbc.connect()
    try:
        cols = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
        for col in ("lease_holder", "runs_on"):  # synced schema (worker-first cards live on synced boards)
            if col not in cols:
                conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} TEXT")
        conn.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, 1)",
                     (tid, "worker_queue_reason", json.dumps(
                         {"version": 1, "code": "no_live_candidate", "candidates": [],
                          "lifecycle": {"runs_on": None, "run_count": 0}})))
        conn.commit()
    finally:
        conn.close()
    q = api.get(f"/api/plugins/kanban/tasks/{tid}").json()["queue"]
    assert q == {"lane": "worker_pool", "reason": "no_live_candidate"}


def test_replica_open_falls_back_to_query_only(kanban_home, monkeypatch):
    """``mode=ro`` refuses a WAL replica whose -shm is absent; the fallback still reads it."""
    from hermes_cli import kanban_foreign_card as kfc
    tid = make_replica_card(kanban_home)
    enable_sync(monkeypatch)
    from hermes_cli import kanban_lockguard
    orig = kanban_lockguard.connect
    seen = []

    def flaky(database, *a, **kw):
        seen.append(str(database))
        if "mode=ro" in str(database):
            raise sqlite3.OperationalError("unable to open database file")
        return orig(database, *a, **kw)

    monkeypatch.setattr(kanban_lockguard, "connect", flaky)
    with kfc.open_replica(str(kb.kanban_db_path())) as replica:
        assert replica is not None
        assert replica.execute("PRAGMA query_only").fetchone()[0] == 1
        assert kfc.created_on(replica, tid) == "worker-1"
    assert any("mode=ro" in s for s in seen) and any("mode=rw" in s for s in seen)


def test_sync_on_without_a_replica_is_404_and_creates_nothing(kanban_home, api, monkeypatch):
    enable_sync(monkeypatch)
    assert api.get("/api/plugins/kanban/tasks/t_0000000000000000").status_code == 404
    assert not (kanban_home / "kanban" / sync.SHADOW_DIR / "default.db").exists()


def test_replica_path_is_none_for_the_replica_itself(kanban_home, monkeypatch):
    from hermes_cli import kanban_foreign_card as kfc
    make_replica_card(kanban_home)
    enable_sync(monkeypatch)
    shadow = kanban_home / "kanban" / sync.SHADOW_DIR / "default.db"
    assert kfc.replica_path(str(kb.kanban_db_path())) == shadow
    assert kfc.replica_path(str(shadow)) is None
    assert kfc.replica_path(None) is None


def test_created_on_without_the_sync_column(tmp_path):
    from hermes_cli import kanban_foreign_card as kfc
    conn = sqlite3.connect(tmp_path / "plain.db")
    conn.execute("CREATE TABLE tasks (id TEXT PRIMARY KEY)")
    conn.execute("INSERT INTO tasks VALUES ('t_1')")
    assert kfc.created_on(conn, "t_1") is None
    assert kfc.created_on(conn, "t_2") is kfc._UNSET


def test_replica_open_closes_a_connection_whose_probe_failed(kanban_home, monkeypatch):
    from hermes_cli import kanban_foreign_card as kfc
    from hermes_cli import kanban_lockguard
    make_replica_card(kanban_home)
    enable_sync(monkeypatch)
    orig = kanban_lockguard.connect
    closed = []

    class ProbeFails:
        def __init__(self, inner):
            self.inner = inner

        def execute(self, sql, *a):
            raise sqlite3.OperationalError("probe failed")

        def close(self):
            closed.append(True)
            self.inner.close()

    def connect(database, *a, **kw):
        conn = orig(database, *a, **kw)
        return ProbeFails(conn) if "mode=ro" in str(database) else conn

    monkeypatch.setattr(kanban_lockguard, "connect", connect)
    with kfc.open_replica(str(kb.kanban_db_path())) as replica:
        assert replica is not None and replica.execute("PRAGMA query_only").fetchone()[0] == 1
    assert closed == [True]
