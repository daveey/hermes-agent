"""Acceptance: real Worker alarm + real dispatcher + persisted Desktop transcript.

Run with HERMES_TEST_BOARD_SOURCE pointing at the hermes-hub checkout containing
worker/test/core-failure-bridge.test.ts. No production service or user chat is used.
"""
import os
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_sync as sync
from hermes_cli import kanban_db_dispatch as dispatch, kanban_db_notify as kbn
from hermes_cli import kanban_owner_recovery as recovery, kanban_sync_client as client
from hermes_state import SessionDB
from tools.environments.local import build_subprocess_env
from tui_gateway import server as notices
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_real_board_bridge import BoardBridge


@pytest.fixture
def board_peer(tmp_path):
    source = os.environ.get("HERMES_TEST_BOARD_SOURCE")
    if not source:
        pytest.skip("requires the real hermes-hub Worker checkout")
    bridge = BoardBridge()
    log_path = tmp_path / "board-peer.log"
    with log_path.open("w", encoding="utf-8") as log:
        env = build_subprocess_env()
        env["CORE_FAILURE_BRIDGE"] = bridge.url
        process = subprocess.Popen(
            ["npx", "vitest", "run", "test/core-failure-bridge.test.ts"],
            cwd=Path(source) / "worker", env=env, stdout=log, stderr=subprocess.STDOUT,
            text=True, encoding="utf-8",
        )
        try:
            try:
                key = bridge.wait_ready(process)["key"]
            except (RuntimeError, TimeoutError) as exc:
                pytest.fail(f"{exc}: " + log_path.read_text(encoding="utf-8"))
            yield bridge, client.Hub(client.Identity(bridge.url, key, "failure-fixture"))
        finally:
            bridge.close(process)
    assert process.returncode == 0, log_path.read_text(encoding="utf-8")


@pytest.mark.parametrize("split", [False, True])
@pytest.mark.parametrize("alarm_first", [True, False])
def test_alarm_and_dispatcher_finalize_one_failure_and_one_chat(tmp_path, monkeypatch, board_peer, alarm_first, split):
    bridge, hub = board_peer
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    conn = kbc.connect(home / "kanban.db")
    client.ensure_identity(conn, "default", "failure-fixture")
    sync.set_mode(conn, "primary")
    previous = sync.set_lease_client(client.HubLeaseClient(hub))
    db = SessionDB(home / "state.db")
    db.create_session("failure-chat", source="desktop")
    session = {"session_key": "failure-chat", "profile_home": str(home), "agent": SimpleNamespace(session_db=db)}
    try:
        tid = kb.create_task(conn, title="real Board failure fixture", assignee="worker", runs_on=[])
        with kbc.write_txn(conn):
            conn.execute("UPDATE tasks SET consecutive_failures=1, max_retries=2 WHERE id=?", (tid,))
        assert client.push(conn, hub, "default", "failure-fixture", primary=True).rejected == 0
        client.pull(conn, hub, "default", observers=True)
        assert kb.claim_task(conn, tid, ttl_seconds=900, claimer=kb._host_prefix() + "fixture")
        with kbc.write_txn(conn):
            conn.execute("UPDATE tasks SET worker_pid=987654, started_at=1 WHERE id=?", (tid,))
        assert client.push(conn, hub, "default", "failure-fixture", primary=True).rejected == 0
        client.pull(conn, hub, "default", observers=True)
        uid = conn.execute("SELECT uid FROM task_runs WHERE id=(SELECT current_run_id FROM tasks WHERE id=?)", (tid,)).fetchone()[0]
        kbn.add_notify_sub(conn, task_id=tid, platform="tui", chat_id="failure-chat")

        def poll():
            claims = []
            texts = notices._collect_kanban_notifications(session, claims)
            session["_kanban_claims"] = claims
            if texts:
                verdict, _, _ = notices._kb_persist_batch(session, texts, True)
                assert verdict in {"written", "delivered"}
                notices._kb_ack_durable(session, {id(t) for t in texts})

        if split:
            real_changes = hub.changes
            monkeypatch.setattr(hub, "changes", lambda board, since: real_changes(board, since, limit=1))
            real_apply = sync.apply_remote_changes

            def apply_and_poll(*args, **kwargs):
                result = real_apply(*args, **kwargs)
                poll()
                return result

            monkeypatch.setattr(sync, "apply_remote_changes", apply_and_poll)
        monkeypatch.setattr(dispatch, "_worker_alive", lambda *a: False)
        monkeypatch.setattr(recovery, "owner_reclaim_blocked", lambda *a, **kw: False)
        monkeypatch.setattr(recovery, "managed_run_exit_code", lambda *a: 1)
        monkeypatch.setattr(recovery, "apply_attempt_note", lambda *a: None)
        monkeypatch.setattr(dispatch._host_reboot, "victims", lambda *a: (None, []))
        monkeypatch.setattr(dispatch._host_reboot, "note", lambda *a: None)
        monkeypatch.setattr(dispatch._host_reboot, "comment_culprit", lambda *a: None)
        monkeypatch.setattr(dispatch._owner_death, "note", lambda *a: None)
        monkeypatch.setattr(dispatch, "_classify_dead_worker", lambda *a, **kw:
                            dispatch._DeadWorker("crashed", 1, "fixture exit 1", "crashed", {"exit_code": 1}))
        if alarm_first:
            assert bridge.alarm("default", tid, uid)["status"] == 200
        # Deliberately retain the stale running mirror until the local observer acts.
        assert dispatch.detect_crashed_workers(conn) == [tid]
        poll()
        client.push(conn, hub, "default", "failure-fixture", primary=True)
        if not alarm_first:
            assert bridge.alarm("default", tid, uid)["status"] == 200
        client.pull(conn, hub, "default", observers=True)
        poll()
        row = conn.execute("SELECT status, consecutive_failures FROM tasks WHERE id=?", (tid,)).fetchone()
        assert tuple(row) == ("triage", 2)
        events = list(conn.execute("SELECT kind,payload FROM task_events WHERE task_id=? AND kind='gave_up'", (tid,)))
        assert len(events) == 1
        board_tasks = hub.snapshot_page("default", "tasks")["rows"]
        board_task = next(r for r in board_tasks if r["id"] == tid)
        assert board_task["consecutive_failures"] == 2
        board_events = hub.snapshot_page("default", "task_events")["rows"]
        assert len([e for e in board_events if e["task_id"] == tid and e["kind"] == "gave_up"]) == 1
        messages = db.get_messages("failure-chat")
        assert len(messages) == 1, messages
        assert "triage" in messages[0]["content"]
        assert "returned to ready" not in messages[0]["content"]
        assert "3 of 2" not in messages[0]["content"]
        db.close()
        db = SessionDB(home / "state.db")
        session = {"session_key": "failure-chat", "profile_home": str(home), "agent": SimpleNamespace(session_db=db)}
        client.pull(conn, hub, "default", observers=True)
        poll()
        assert len(db.get_messages("failure-chat")) == 1
    finally:
        sync.set_lease_client(previous)
        conn.close()
        db.close()
