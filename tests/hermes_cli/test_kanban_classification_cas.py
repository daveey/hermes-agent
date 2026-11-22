"""The existing metadata-only CLI must use canonical CAS, never a local rekind."""
import argparse
import json
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from hermes_cli import kanban, kanban_db as db
from hermes_cli.kanban_db_connect import connect_closing
from hermes_cli.kanban_parser import build_parser


def args(tid, cas):
    parser = argparse.ArgumentParser()
    build_parser(parser.add_subparsers())
    return parser.parse_args(["kanban", "block", tid, "Keep parked", "--metadata-only",
                              "--human-request", "null", "--kind", "dependency",
                              "--classification-cas", json.dumps(cas)])


def request(tid):
    return dict(client_op_id="correction-1", op="reclassify_parked", task=tid, expected_revision=1,
                expected_event_uid="withdrawn-ask", expected_event_version=2, expected_spend_episode="unchanged-stop",
                expected_parents=["parent"], expected_parent_links=[dict(uid="parent>task", version=1)],
                disposition_comment_uid="owner-disposition", disposition_comment_version=3, reason="Keep parked")


def parked():
    with connect_closing() as conn:
        return db.create_task(conn, title="Unfinished acceptance", assignee="default",
                              initial_status="blocked", lint_blocked=False)


def test_metadata_only_cas_flag_routes_exact_request(monkeypatch):
    from hermes_cli import kanban_human_request_migration as migration
    tid = parked()
    op = request(tid)
    calls = []
    monkeypatch.setattr(kanban, "_cli_route_needs", lambda *_a, **_kw: pytest.fail("metadata CAS entered capability/lifecycle routing"))
    monkeypatch.setattr(migration, "reclassify_cli_request", lambda parsed, reason: calls.append((parsed.classification_cas, reason)) or 0,
                        raising=False)
    assert kanban.kanban_command(args(tid, op)) == 0
    assert calls == [(op, "Keep parked")]


def test_cas_without_metadata_only_cannot_fall_through_to_block(capsys):
    tid = parked()
    parsed = args(tid, request(tid))
    parsed.metadata_only = False
    with connect_closing() as conn:
        before = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    assert kanban.kanban_command(parsed) != 0
    captured = capsys.readouterr()
    assert "classification CAS requires --metadata-only" in captured.err + captured.out
    with connect_closing() as conn:
        assert dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()) == before


@pytest.mark.parametrize("marker", ["HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKER_TASK"])
def test_worker_cannot_initiate_classification_cas(monkeypatch, marker):
    from hermes_cli import kanban_human_request_migration as migration
    tid = parked()
    before = None
    with connect_closing() as conn:
        before = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
    parsed = args(tid, request(tid))
    monkeypatch.setenv(marker, "some-worker")
    with pytest.raises(PermissionError, match="operator"):
        migration.reclassify_cli_request(parsed, "Keep parked")
    with connect_closing() as conn:
        assert dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone()) == before


@pytest.fixture
def adapter(tmp_path, monkeypatch):
    """Real isolated owner SQLite/feed adapter; only the external Hub transport is fake.

    Applied-result shape is the canonical /ops result exercised in the Board HTTP
    regression. This is client glue coverage, not a claim to exercise Board CAS.
    """
    from hermes_cli import kanban_db_sync as sync, kanban_sync_client as client
    from tests.hermes_cli import kanban_sync_support as support
    root = tmp_path / "owner"
    root.mkdir()
    home = support.kanban_home(root, monkeypatch)
    for conn in support.synced_conn(home):
        sync.set_mode(conn, "primary")
        sync.set_identity(conn, "fixture", "owner")
        tid = db.create_task(conn, title="Unfinished acceptance", assignee="default",
                             initial_status="blocked", lint_blocked=False)
        with db.write_txn(conn):
            conn.execute("UPDATE tasks SET created_on='owner',block_kind='needs_input' WHERE id=?", (tid,))
            conn.execute("DELETE FROM kanban_sync_outbox")
        calls = []
        canonical = dict(status="blocked", block_kind="dependency", human_request=None,
                         current_run_uid=None, lease_holder=None)
        result = {"results": [{"client_op_id": "correction-1", "status": "applied"}]}

        class Hub:
            def ops(self, board, ops):
                calls.append((board, ops))
                return result

            def row_evidence(self, board, table, uid):
                assert (board, table, uid) == ("fixture", "tasks", tid)
                return {"row": canonical}

        @contextmanager
        def connect():
            yield conn

        def pull(target, hub, board, **kw):
            assert target is conn and board == "fixture" and kw == {"observers": True, "partition": "owner"}
            sync.apply_remote_changes(conn, [sync.RemoteChange(
                2, "tasks", tid, "upsert", {"block_kind": "dependency"}, 2, "", "owner")], new_cursor=2)

        monkeypatch.setattr(kanban.kbc, "connect_closing", connect)
        monkeypatch.setattr(client, "load_identity", lambda: SimpleNamespace(gateway="owner"))
        monkeypatch.setattr(client, "Hub", lambda _ident: Hub())
        monkeypatch.setattr(client, "pull", pull)
        yield SimpleNamespace(conn=conn, tid=tid, calls=calls, canonical=canonical, result=result)


def test_owner_adapter_uses_exact_op_and_certified_remote_apply(adapter):
    from hermes_cli import kanban_human_request_migration as migration
    parsed = args(adapter.tid, request(adapter.tid))
    before = dict(adapter.conn.execute("SELECT * FROM tasks WHERE id=?", (adapter.tid,)).fetchone())
    assert migration.reclassify_cli_request(parsed, "Keep parked") == 0
    assert adapter.calls == [("fixture", [request(adapter.tid)])]
    after = dict(adapter.conn.execute("SELECT * FROM tasks WHERE id=?", (adapter.tid,)).fetchone())
    assert after["status"] == "blocked" and after["block_kind"] == "dependency"
    assert all(after[k] == before[k] for k in before if k not in {"block_kind", "sync_version", "sync_hlc"})


@pytest.mark.parametrize("fault", ["replica", "foreign", "run", "claim", "worker", "pending"])
def test_owner_adapter_refuses_before_network_or_mutation(adapter, fault):
    from hermes_cli import kanban_human_request_migration as migration, kanban_db_sync as sync
    conn = adapter.conn
    if fault == "replica":
        sync.set_mode(conn, "shadow")
    else:
        columns = {"foreign": ("created_on", "other"), "run": ("current_run_id", 99),
                   "claim": ("claim_lock", "active"), "worker": ("worker_pid", 99), "pending": ("title", "pending change")}
        column, value = columns[fault]
        with db.write_txn(conn):
            conn.execute(f"UPDATE tasks SET {column}=? WHERE id=?", (value, adapter.tid))
            if fault != "pending":
                conn.execute("DELETE FROM kanban_sync_outbox")
    before = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (adapter.tid,)).fetchone())
    with pytest.raises((PermissionError, ValueError), match="primary owner|active or locally pending"):
        migration.reclassify_cli_request(args(adapter.tid, request(adapter.tid)), "Keep parked")
    assert adapter.calls == []
    assert dict(conn.execute("SELECT * FROM tasks WHERE id=?", (adapter.tid,)).fetchone()) == before


@pytest.mark.parametrize("field,value", [("needs", ["test:capability"]), ("awaiting", "clock"),
    ("human_request", {"action": "new ask"}), ("wake_check", "true"), ("kind", "time"), ("ids", ["other"])])
def test_cas_rejects_mixed_lifecycle_arguments_before_submission(adapter, field, value):
    from hermes_cli import kanban_human_request_migration as migration
    parsed = args(adapter.tid, request(adapter.tid))
    setattr(parsed, field, value)
    with pytest.raises(ValueError, match="one exact task"):
        migration.reclassify_cli_request(parsed, "Keep parked")
    assert adapter.calls == []


def test_canonical_rejection_never_performs_local_reclassification(adapter):
    from hermes_cli import kanban_human_request_migration as migration
    adapter.result["results"] = [{"status": "rejected", "reason": "revision_conflict"}]
    with pytest.raises(ValueError, match="preserve the exact request"):
        migration.reclassify_cli_request(args(adapter.tid, request(adapter.tid)), "Keep parked")
    assert adapter.conn.execute("SELECT block_kind FROM tasks WHERE id=?", (adapter.tid,)).fetchone()[0] == "needs_input"


def test_replay_with_successor_ask_reports_unconfirmed_without_retry(adapter):
    from hermes_cli import kanban_human_request_migration as migration
    adapter.canonical["human_request"] = '{"action":"new ask"}'
    with pytest.raises(ValueError, match="do not resubmit with a new identity"):
        migration.reclassify_cli_request(args(adapter.tid, request(adapter.tid)), "Keep parked")
    assert len(adapter.calls) == 1

