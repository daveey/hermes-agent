"""Writes on a card this gateway holds only as a Board replica copy name the owner (``foreign_card``). [daveey]

``kanban_complete`` used to answer ``stale_run`` ("unknown id, stale run, or already terminal"), ``kanban_archive``
"cannot archive <id>", and ``hermes kanban complete`` "unknown id or terminal state" for a card a Board worker
node created (t_01a19fd40c53bb19). Each now says the card lives on its creator. A copy the worker-node pickup made
into the live file is still the local path and completes normally.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync

OWNER_TEXT = "was created on worker-1 and lives there; this gateway has only a replica copy"


@pytest.fixture
def home(monkeypatch, tmp_path):
    h = tmp_path / ".hermes"
    h.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(h))
    monkeypatch.setenv("HERMES_PROFILE", "test-worker")
    for var in ("HERMES_SESSION_ID", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID", "HERMES_KANBAN_DB",
                "HERMES_KANBAN_BOARD"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    return kb.kanban_home()


def replica_card(home: Path, owner: str = "worker-1") -> str:
    path = home / "kanban" / sync.SHADOW_DIR / "default.db"
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = kbc.connect(path)
    try:
        tid = kb.create_task(conn, title="made on worker-1", assignee="test-worker")
        if "created_on" not in {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}:
            conn.execute("ALTER TABLE tasks ADD COLUMN created_on TEXT")
        conn.execute("UPDATE tasks SET created_on = ? WHERE id = ?", (owner, tid))
        conn.commit()
    finally:
        conn.close()
    return tid


@pytest.fixture
def synced(home, monkeypatch):
    tid = replica_card(home)
    monkeypatch.setattr(sync, "sync_config", lambda: {"enabled": True, "boards": ["default"]})
    return tid


def cli(*argv: str) -> int:
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    kc.build_parser(parser.add_subparsers(dest="command"))
    return kc.kanban_command(parser.parse_args(["kanban", *argv]))


def test_tool_complete_names_owner_not_stale_run(synced):
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_complete({"task_id": synced, "summary": "done"}))
    assert out["rejection"] == "foreign_card", out
    assert OWNER_TEXT in out["error"]
    assert "foreign_card" in kt.COMPLETE_REJECTION_CLASSES


def test_tool_complete_unknown_id_is_still_stale_run(synced):
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_complete({"task_id": "t_0000000000000000", "summary": "done"}))
    assert out["rejection"] == "stale_run", out


def test_tool_archive_names_owner(synced):
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_archive({"task_id": synced}))
    assert out.get("rejection") == "foreign_card", out
    assert OWNER_TEXT in out["error"]


def test_tool_comment_names_owner(synced):
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_comment({"task_id": synced, "body": "hello"}))
    assert out.get("rejection") == "foreign_card", out
    assert OWNER_TEXT in out["error"]


def test_cli_complete_names_owner(synced, capsys):
    assert cli("complete", synced, "--summary", "done") == 1
    err = capsys.readouterr().err
    assert f"cannot complete {synced}: foreign_card: {synced} {OWNER_TEXT}" in err
    assert "unknown id or terminal state" not in err


def test_cli_archive_names_owner(synced, capsys):
    assert cli("archive", synced) != 0
    err = capsys.readouterr().err
    assert f"cannot archive {synced}: foreign_card: {synced} {OWNER_TEXT}" in err


def test_picked_up_copy_completes_and_lands_in_live_file(synced, capsys):
    """The worker-node pickup copies the replica row into the live file: that copy is local and completes."""
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="picked-up copy", assignee="test-worker")
    assert cli("complete", tid, "--summary", "done") == 0
    with kbc.connect_closing() as conn:
        task = kb.get_task(conn, tid)
        assert task is not None and task.status == "done"
    assert "foreign_card" not in capsys.readouterr().err


# --- end to end: two gateways on the fake Board (the worker-node fixture) --------------------------------------

def _api_client():
    import importlib.util
    import sys
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    plugin = Path(__file__).resolve().parents[2] / "plugins" / "kanban" / "dashboard" / "plugin_api.py"
    spec = importlib.util.spec_from_file_location("hermes_dashboard_plugin_kanban_foreign_e2e", plugin)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    app = FastAPI()
    app.include_router(mod.router, prefix="/api/plugins/kanban")
    return TestClient(app)


@pytest.fixture
def gws(tmp_path, monkeypatch):
    from tests.hermes_cli import test_kanban_worker_node as twn
    from tests.hermes_cli.kanban_sync_fake_board import FakeService
    svc = FakeService(lease_runs=True)
    gen = twn.gws.__wrapped__(tmp_path, monkeypatch, svc)
    hub, worker = next(gen)
    yield hub, worker, svc
    next(gen, None)


def test_e2e_worker_created_card_reads_from_hub_replica_and_refuses_hub_writes(gws, capsys):
    hub, worker, _svc = gws
    worker.enter()
    tid = kb.create_task(worker.conn, title="made on worker-1", assignee="default", runs_on=["machine:hub"])
    worker.round()
    hub.round()
    hub.enter()
    assert kb.get_task(hub.conn, tid) is None  # the hub's live file holds only its own cards
    api = _api_client()
    body = api.get(f"/api/plugins/kanban/tasks/{tid}").json()
    assert (body["source"], body["owner"], body["task"]["title"]) == ("replica", "worker-1", "made on worker-1")
    r = api.post(f"/api/plugins/kanban/tasks/{tid}/comments", json={"body": "hi"})
    assert r.status_code == 409 and r.json()["detail"].startswith(f"foreign_card: {tid} {OWNER_TEXT}")
    assert cli("complete", tid, "--summary", "done") == 1
    assert f"foreign_card: {tid} {OWNER_TEXT}" in capsys.readouterr().err
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_complete({"task_id": tid, "summary": "done"}))
    assert out["rejection"] == "foreign_card"


def test_e2e_picked_up_copy_completes_into_the_outbox(gws, monkeypatch, capsys):
    hub, worker, _svc = gws
    hub.enter()
    tid = kb.create_task(hub.conn, title="hub card", assignee="default", runs_on=["linux"])
    hub.round()
    worker.round()
    worker.tick()
    worker.enter()
    task = kb.get_task(worker.conn, tid)
    assert task is not None and task.status == "running"  # the pickup copy, claimed under the Board lease
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
    assert cli("complete", tid, "--summary", "done on the worker") == 0, capsys.readouterr().err
    pending = worker.conn.execute(
        "SELECT COUNT(*) FROM kanban_sync_outbox WHERE state != 'rejected' AND (row_uid = ? OR "
        "json_extract(fields, '$.task_id') = ?)", (tid, tid)).fetchone()[0]
    assert pending > 0
    worker.round()
    hub.round()
    hub.enter()
    done = kb.get_task(hub.conn, tid)
    assert done is not None and done.status == "done"
