"""Text CAS schema, concurrency, custody and consumer contracts."""
import concurrent.futures
import threading

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc, kanban_db_sync as sync
from tests.hermes_cli.test_kanban_text_edit import api, request, kanban_home as _kanban_home

kanban_home = _kanban_home


def new(conn):
    return kb.create_task(conn, title="Text fixture", body="old")


@pytest.mark.parametrize("current", [False, True])
def test_open_run_custody(kanban_home, current):
    with kbc.connect_closing() as conn:
        tid = new(conn)
        with kb.write_txn(conn):
            rid = conn.execute("INSERT INTO task_runs(task_id,status,started_at) VALUES (?,'running',1)",
                               (tid,)).lastrowid
            if current:
                conn.execute("UPDATE tasks SET current_run_id=? WHERE id=?", (rid, tid))
        before = list(conn.iterdump())
        assert api().edit_text(conn, tid, request())["disposition"] == "active_custody"
        assert list(conn.iterdump()) == before


def test_empty_sync_identity_refused(kanban_home):
    with kbc.connect_closing() as conn:
        tid = new(conn)
        sync.apply_sync_schema_steps(conn)
        before = list(conn.iterdump())
        assert api().edit_text(conn, tid, request())["disposition"] == "foreign_owner"
        assert list(conn.iterdump()) == before


def test_downstream_diagnostics_and_decomposition_preserved(kanban_home):
    from hermes_cli.kanban_diagnostics import _active_hallucination_events, _block_loop_ask
    with kbc.connect_closing() as conn:
        tid = new(conn)
        parent = kb.create_task(conn, title="Parent")
        with kb.write_txn(conn):
            conn.execute("INSERT INTO task_links(parent_id,child_id) VALUES (?,?)", (parent, tid))
            conn.execute("UPDATE tasks SET status='triage' WHERE id=?", (tid,))
            kb._append_event(conn, tid, "block_loop_detected", {"reason": "keep held"})
            kb._append_event(conn, tid, "completion_blocked_hallucination", {"ids": ["missing"]})
        links = list(conn.execute("SELECT * FROM task_links"))
        refusal = kb.decomposition_refusal(conn, tid)
        assert refusal and kb.awaiting_human_after_block_loop(conn, tid)
        assert api().edit_text(conn, tid, request())["ok"]
        events = kb.list_events(conn, tid)
        assert _block_loop_ask(events) == "keep held"
        assert len(_active_hallucination_events(events, "completion_blocked_hallucination")) == 1
        assert kb.awaiting_human_after_block_loop(conn, tid)
        assert kb.decomposition_refusal(conn, tid) == refusal
        assert list(conn.execute("SELECT * FROM task_links")) == links


def test_concurrent_connections_exactly_one_apply(kanban_home):
    with kbc.connect_closing() as conn:
        tid = new(conn)
    barrier = threading.Barrier(2)
    def apply(index):
        with kbc.connect_closing() as conn:
            req = request(replacement=str(index)); req["request_id"] = f"request-{index}"
            barrier.wait(timeout=10)
            return api().edit_text(conn, tid, req)
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(apply, [1, 2]))
    assert sorted(r["disposition"] for r in results) == ["applied", "stale_preimage"]


def test_unknown_after_commit_replays_same_identity(kanban_home, monkeypatch):
    with kbc.connect_closing() as conn:
        tid = new(conn)
        def lost(*args, **kwargs):
            raise OSError("lost response after commit")
        monkeypatch.setattr(kb, "notify_task_updated", lost)
        assert api().edit_text(conn, tid, request())["disposition"] == "unknown"
        before = list(conn.iterdump())
        result = api().edit_text(conn, tid, request())
        assert result["disposition"] == "replayed" and result["event_uid"]
        assert list(conn.iterdump()) == before


@pytest.mark.parametrize("option,value", [("priority", 0), ("result", False), ("audit_comment", False),
                                            ("priority_bump", 0), ("require_lower_reason", 0)])
def test_internal_mixed_options_refused(kanban_home, option, value):
    with kbc.connect_closing() as conn:
        tid = new(conn); before = list(conn.iterdump())
        with pytest.raises(ValueError, match="cannot mix"):
            kb.edit_task(conn, tid, title="Text fixture", body="new", expected_body="old",
                         home_open_only=True, text_cas=api().TextCAS(request()), **{option: value})
        assert list(conn.iterdump()) == before


def test_unsynced_original_history_not_stamped(kanban_home):
    with kbc.connect_closing() as conn:
        tid = new(conn)
        before = [tuple(row) for row in conn.execute("SELECT * FROM task_events")]
        assert "uid" not in {r[1] for r in conn.execute("PRAGMA table_info(task_events)")}
        assert api().edit_text(conn, tid, request())["ok"]
        after = list(conn.execute("SELECT * FROM task_events ORDER BY id"))
        assert [tuple(row)[:-1] for row in after[:-1]] == before
        assert all(row["uid"] is None for row in after[:-1])


def synced(conn, home, tid, mode="primary", owner="owner"):
    from tests.hermes_cli.kanban_sync_support import write_sync_config
    write_sync_config(home, True)
    sync.install_or_remove(conn, conn.execute("PRAGMA database_list").fetchone()[2])
    sync.set_identity(conn, "test-board", "owner")
    sync.set_mode(conn, mode)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET created_on=? WHERE id=?", (owner, tid))


@pytest.mark.parametrize("mode,owner", [("primary", "foreign"), ("primary", None), ("primary", ""),
                                       ("shadow", "owner"), ("off", "owner")])
def test_strict_owner_refusal(kanban_home, mode, owner):
    with kbc.connect_closing() as conn:
        tid = new(conn); synced(conn, kanban_home, tid, mode, owner)
        before = list(conn.iterdump())
        assert api().edit_text(conn, tid, request())["disposition"] == "foreign_owner"
        assert list(conn.iterdump()) == before


def test_synced_atomic_outbox(kanban_home):
    with kbc.connect_closing() as conn:
        tid = new(conn); synced(conn, kanban_home, tid)
        before = conn.execute("SELECT COUNT(*) FROM kanban_sync_outbox").fetchone()[0]
        result = api().edit_text(conn, tid, request())
        assert result["disposition"] == "applied", result
        assert conn.execute("SELECT COUNT(*) FROM kanban_sync_outbox").fetchone()[0] > before
        receipt = conn.execute("SELECT uid FROM task_events WHERE kind='text_cas_edited'").fetchone()
        assert receipt[0] == result["event_uid"]


@pytest.mark.parametrize("expiry", [None, 9999999999])
def test_own_gateway_lease_refused(kanban_home, expiry):
    with kbc.connect_closing() as conn:
        tid = new(conn); synced(conn, kanban_home, tid)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET lease_holder='owner',lease_expires=? WHERE id=?", (expiry, tid))
        before = list(conn.iterdump())
        assert api().edit_text(conn, tid, request())["disposition"] == "active_custody"
        assert list(conn.iterdump()) == before


@pytest.mark.parametrize("corruption,expected", [("foreign", "request_id_conflict"),
                                                 ("json", "malformed_receipt"), ("uid", "malformed_receipt")])
def test_bad_receipts_fail_closed(kanban_home, corruption, expected):
    with kbc.connect_closing() as conn:
        tid = new(conn); synced(conn, kanban_home, tid)
        assert api().edit_text(conn, tid, request())["ok"]
        with kb.write_txn(conn):
            if corruption == "foreign":
                conn.execute("UPDATE task_events SET origin_gateway='peer' WHERE kind='text_cas_edited'")
            elif corruption == "json":
                conn.execute("UPDATE task_events SET payload='invalid' WHERE kind='text_cas_edited'")
            else:
                conn.execute("UPDATE task_events SET uid=NULL WHERE kind='text_cas_edited'")
        before = list(conn.iterdump())
        assert api().edit_text(conn, tid, request())["disposition"] == expected
        assert list(conn.iterdump()) == before


def test_no_progress_marker_unchanged(kanban_home):
    from hermes_cli.kanban_no_progress import progress_marker
    with kbc.connect_closing() as conn:
        tid = new(conn)
        before = progress_marker(conn, tid)
        assert api().edit_text(conn, tid, request())["ok"]
        assert progress_marker(conn, tid) == before
        kb.edit_task(conn, tid, body="ordinary edit")
        assert progress_marker(conn, tid) > before


def test_replacement_model_policy_remains(kanban_home):
    with kbc.connect_closing() as conn:
        tid = new(conn); before = list(conn.iterdump())
        result = api().edit_text(conn, tid, request(replacement="require_model: true"))
        assert result["disposition"] == "invalid_request"
        assert list(conn.iterdump()) == before
