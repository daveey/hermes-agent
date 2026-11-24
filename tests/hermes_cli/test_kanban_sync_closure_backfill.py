"""Upgrade a replica that consumed terminal history before closure certification existed."""
from contextlib import nullcontext
from pathlib import Path

import pytest

from hermes_cli import kanban_containment_connections as connections
from hermes_cli import kanban_containment_replica as replica
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as client
from tests.hermes_cli import test_kanban_containment_terminal_replica as fixtures

kanban_home = fixtures.kanban_home
synced_conn = fixtures.synced_conn


def legacy(conn, monkeypatch):
    target, child, stop, close, hub = fixtures.scenario(conn)
    fixtures.fixtures.apply(conn, hub, [fixtures.fixtures.event(stop, hub, 1)])
    marker = sync.RemoteChange(1, "tasks", target, "upsert",
                              {"status": "archived", "containment_episode": stop["event_uid"]}, 580, "", "home")
    fixtures.fixtures.apply(conn, hub, [marker])
    change = fixtures.fixtures.event(close, hub, 2)
    # Old clients treated containment_closed as an ordinary event: no protected
    # event permit, no canonical closure cache. Simulate only that old boundary.
    with monkeypatch.context() as old:
        old.setattr(replica, "event_permit", lambda *args: 1)
        sync.apply_remote_changes(conn, [change], new_cursor=2, board="fixture", observers=False)
    assert kb.get_task(conn, target).status == "triage"
    assert replica._cached(conn, close["event_uid"]) is None
    return target, child, stop, close, hub


def pull_fixture(conn, monkeypatch):
    monkeypatch.setattr(client, "shadow_path", lambda slug: Path("fixture.db"))
    monkeypatch.setattr(client, "connection", lambda *args: nullcontext(conn))
    monkeypatch.setattr(client, "pull", lambda *args, **kw: client.Pulled(0))
    from hermes_cli import kanban_sync_attachments as attachments
    monkeypatch.setattr(attachments, "transfer_pending", lambda *args: None)


def test_at_head_pull_backfills_historical_closure(synced_conn, monkeypatch):
    conn = synced_conn
    target, child, stop, close, hub = legacy(conn, monkeypatch)
    before = [tuple(r) for r in conn.execute("SELECT * FROM task_links")]
    pull_fixture(conn, monkeypatch)
    with client._pulled_replica("fixture", hub, "worker"):
        pass
    assert kb.get_task(conn, target).status == "archived"
    assert connections.held(conn, target)
    assert kb.get_task(conn, target).containment_episode == stop["event_uid"]
    assert kb.get_task(conn, child).status == "todo"
    assert kb.claim_task(conn, target) is None
    assert [tuple(r) for r in conn.execute("SELECT * FROM task_links")] == before
    assert sync.outbox(conn) == []
    assert conn.execute("SELECT cursor FROM kanban_sync_state").fetchone()[0] == 2
    assert replica._cached(conn, close["event_uid"]) is not None
    with monkeypatch.context() as no_network:
        no_network.setattr(hub, "row_evidence", lambda *a: pytest.fail("already converged"))
        with client._pulled_replica("fixture", hub, "worker"):
            pass


def test_diagnostic_does_not_backfill(synced_conn, monkeypatch):
    target, _, _, close, hub = legacy(synced_conn, monkeypatch)
    pull_fixture(synced_conn, monkeypatch)
    with client._pulled_replica("fixture", hub, "worker", diagnostic=True):
        pass
    assert kb.get_task(synced_conn, target).status == "triage"
    assert replica._cached(synced_conn, close["event_uid"]) is None


@pytest.mark.parametrize("fault", ["unavailable", "invalid", "conflict"])
def test_failed_evidence_never_archives(synced_conn, monkeypatch, fault):
    from hermes_cli.kanban_sync_closure_backfill import backfill
    from hermes_cli import kanban_sync_quarantine as quarantine
    conn = synced_conn
    target, _, _, close, hub = legacy(conn, monkeypatch)
    if fault == "unavailable":
        def unavailable(*args):
            raise OSError("offline")
        with monkeypatch.context() as offline:
            offline.setattr(hub, "row_evidence", unavailable)
            with pytest.raises(OSError, match="offline"):
                backfill(conn, hub, "fixture")
        assert backfill(conn, hub, "fixture") == {target: "archived"}
    else:
        if fault == "invalid":
            hub.events[close["event_uid"]]["containment_v1"] = None
            assert backfill(conn, hub, "fixture") == {}
        else:
            hub.events[close["event_uid"]]["row"]["origin_gateway"] = "other"
            hub.events[close["event_uid"]]["containment_v1"]["gateway"] = "other"
            backfill(conn, hub, "fixture")
            assert quarantine.held(conn, target)
        assert kb.get_task(conn, target).status == "triage"
    assert conn.execute("SELECT cursor FROM kanban_sync_state").fetchone()[0] == 2
    assert connections.held(conn, target)
    assert sync.outbox(conn) == []


def test_crash_after_certification_retries_projection(synced_conn, monkeypatch):
    from hermes_cli.kanban_sync_closure_backfill import backfill
    conn = synced_conn
    target, _, _, close, hub = legacy(conn, monkeypatch)
    replica.certify_page(conn, hub, "fixture", [fixtures.fixtures.event(close, hub, 2)])
    assert kb.get_task(conn, target).status == "triage"
    assert backfill(conn, hub, "fixture") == {target: "archived"}


def test_batch_eventually_services_all_candidates(synced_conn, monkeypatch):
    from hermes_cli import kanban_sync_closure_backfill as repair
    conn = synced_conn
    first, _, _, _, hub = legacy(conn, monkeypatch)
    second, _, _, _, peer = legacy(conn, monkeypatch)
    hub.events.update(peer.events)
    monkeypatch.setattr(repair, "BATCH", 1)
    assert len(repair.backfill(conn, hub, "fixture")) == 1
    assert len(repair.backfill(conn, hub, "fixture")) == 1
    assert repair.backfill(conn, hub, "fixture") == {}
    assert all(kb.get_task(conn, task).status == "archived" for task in (first, second))


def test_invalid_proof_does_not_poison_round(synced_conn, monkeypatch):
    from hermes_cli import kanban_sync_closure_backfill as repair
    conn = synced_conn
    target, _, _, close, hub = legacy(conn, monkeypatch)
    good = hub.events[close["event_uid"]]["containment_v1"]
    hub.events[close["event_uid"]]["containment_v1"] = None
    pull_fixture(conn, monkeypatch)
    stamps = []
    monkeypatch.setattr(sync, "mark_pulled", lambda *a: stamps.append(True))
    clock = [1000]
    monkeypatch.setattr(repair, "_clock", lambda: clock[0], raising=False)
    for _ in range(3):
        with client._pulled_replica("fixture", hub, "worker") as (_, pulled, _):
            assert pulled.status_changed == {}
    assert len(stamps) == 3
    assert kb.get_task(conn, target).status == "triage"
    with monkeypatch.context() as no_network:
        no_network.setattr(hub, "row_evidence", lambda *a: pytest.fail("cooldown must skip Board reads"))
        assert repair.backfill(conn, hub, "fixture") == {}
    hub.events[close["event_uid"]]["containment_v1"] = good
    clock[0] += repair.RETRY_S
    assert repair.backfill(conn, hub, "fixture") == {target: "archived"}
    assert conn.execute("SELECT cursor FROM kanban_sync_state").fetchone()[0] == 2
    assert sync.outbox(conn) == []


def test_refused_candidate_does_not_starve_valid_candidate(synced_conn, monkeypatch):
    from hermes_cli import kanban_sync_closure_backfill as repair
    conn = synced_conn
    bad, _, _, close, hub = legacy(conn, monkeypatch)
    good, _, _, _, peer = legacy(conn, monkeypatch)
    hub.events.update(peer.events)
    hub.events[close["event_uid"]]["containment_v1"] = None
    assert repair.backfill(conn, hub, "fixture") == {good: "archived"}
    assert kb.get_task(conn, bad).status == "triage"
    assert connections.held(conn, bad)
    assert sync.outbox(conn) == []


def test_quarantine_skip(synced_conn, monkeypatch):
    from hermes_cli import kanban_sync_closure_backfill as repair
    from hermes_cli import kanban_sync_quarantine as quarantine
    target, _, _, _, hub = legacy(synced_conn, monkeypatch)
    monkeypatch.setattr(quarantine, "tasks", lambda *a: {target})
    monkeypatch.setattr(quarantine, "certified_page", lambda *a: pytest.fail("held candidate"))
    assert repair.backfill(synced_conn, hub, "fixture") == {}


def test_cached_later_episode_skip(synced_conn, monkeypatch):
    from hermes_cli import kanban_sync_closure_backfill as repair
    from hermes_cli import kanban_sync_quarantine as quarantine
    from hermes_cli import kanban_containment_terminal as terminal
    conn = synced_conn
    _, _, _, close, hub = legacy(conn, monkeypatch)
    replica.certify_page(conn, hub, "fixture", [fixtures.fixtures.event(close, hub, 2)])
    monkeypatch.setattr(terminal, "closed", lambda *a: False)
    monkeypatch.setattr(quarantine, "certified_page", lambda *a: pytest.fail("later held episode"))
    assert repair.backfill(conn, hub, "fixture") == {}


def test_unmarked_triage_skip(synced_conn, monkeypatch):
    from hermes_cli import kanban_sync_closure_backfill as repair
    from hermes_cli import kanban_sync_quarantine as quarantine
    _, _, _, _, hub = legacy(synced_conn, monkeypatch)
    class Unmarked:
        def execute(self, sql, *args):
            if "FROM tasks t" in sql:
                sql = sql.replace("FROM tasks t", "FROM (SELECT id,status,NULL AS containment_episode FROM tasks) t")
            return synced_conn.execute(sql, *args)
    monkeypatch.setattr(quarantine, "certified_page", lambda *a: pytest.fail("unmarked triage"))
    assert repair.backfill(Unmarked(), hub, "fixture") == {}


def test_unchanged_projection_is_not_reported(synced_conn, monkeypatch):
    from hermes_cli import kanban_sync_closure_backfill as repair
    target, _, _, _, hub = legacy(synced_conn, monkeypatch)
    monkeypatch.setattr(sync, "apply_remote_changes", lambda *a, **kw: None)
    assert repair.backfill(synced_conn, hub, "fixture") == {}
    assert kb.get_task(synced_conn, target).status == "triage"


def test_missing_receipt_table_noop():
    import sqlite3
    from hermes_cli import kanban_sync_closure_backfill as repair
    with sqlite3.connect(":memory:") as conn:
        assert repair.backfill(conn, None, "fixture") == {}
