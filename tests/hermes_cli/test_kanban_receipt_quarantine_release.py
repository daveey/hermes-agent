"""Release = operator acceptance + certified catch-up, never a bare delete (review r3, items 3 and 5)."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import time

import pytest

from hermes_cli import kanban_containment_connections as connections
from hermes_cli import kanban_containment_replica as replica
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_sync_quarantine as quarantine
from hermes_cli import kanban_sync_quarantine_release as qr
from tests.hermes_cli import kanban_quarantine_support as support
from tests.hermes_cli import test_kanban_containment_replica as fixtures

kanban_home = fixtures.kanban_home
synced_conn = fixtures.synced_conn


class Hub:
    def __init__(self):
        self.wires = []

    def ops(self, board, wire):
        self.wires.extend(wire)
        return {"results": []}


def _release(conn, case, uid=None):
    return qr.release(conn, case.hub, "fixture", uid or case.uid, reason="Board re-stamp ruled benign",
                      operator="test")


def _event(conn, uid):
    return conn.execute("SELECT kind FROM task_events WHERE uid=?", (uid,)).fetchone()


def test_release_applies_the_board_changes_withheld_during_the_hold(synced_conn):
    conn = synced_conn
    case = support.conflict(conn)
    released = fixtures.event(case.release, case.hub, 2)
    done = sync.RemoteChange(3, "tasks", case.target, "upsert", {"status": "done", "containment_episode": None},
                             3, "", "worker")
    # The release event re-checks the re-stamped stop receipt, so it is a second refused receipt on the task.
    assert quarantine.certified_page(conn, case.hub, "fixture", [released, done]) == []
    assert kb.get_task(conn, case.target).status == "ready"
    _release(conn, case)
    assert quarantine.held(conn, case.target)  # the release receipt still names it
    _release(conn, case, case.release_uid)
    assert kb.get_task(conn, case.target).status == "done"
    assert kb.claim_task(conn, case.target) is None
    assert _event(conn, case.uid)[0] == "contained" and _event(conn, case.release_uid)[0] == "containment_released"
    assert not connections.held(conn, case.target)
    assert support.count(conn, "containment_sync_withheld") == 0


def test_transient_then_identity_never_yields_a_claimable_contained_task(synced_conn, monkeypatch):
    conn = synced_conn
    target, _, stop, _, hub = fixtures.scenario(conn)
    uid = stop["event_uid"]
    replica.remember(conn, hub.events[uid], board="fixture", task=target, uid=uid)
    marker = sync.RemoteChange(1, "tasks", target, "upsert", {"status": "triage", "containment_episode": uid},
                               100, "", "home")
    page = {"changes": [marker, fixtures.event(stop, hub, 2)], "next": 2, "more": False}
    monkeypatch.setattr(ksc.acw, "changes_page", lambda *args: page)
    monkeypatch.setattr(ksc, "adapt", lambda change: change)
    hub.events[uid]["state"] = "unavailable"
    with pytest.raises(qr.ContainmentRefused):
        ksc.pull(conn, hub, "fixture", observers=False)
    assert quarantine.entries(conn) == []  # a transient refusal is never a quarantine
    hub.events[uid]["state"] = "live"
    hub.events[uid]["row"]["created_at"] += 1  # ...then the Board re-stamps the receipt
    ksc.pull(conn, hub, "fixture", observers=False)
    assert [e["receipt"] for e in quarantine.entries(conn)] == [uid]
    assert kb.claim_task(conn, target) is None
    qr.release(conn, hub, "fixture", uid, reason="re-stamp", operator="test")
    assert _event(conn, uid)[0] == "contained"
    assert kb.get_task(conn, target).containment_episode == uid
    assert connections.held(conn, target)  # by its certified contained event now, not by the quarantine
    assert not quarantine.held(conn, target)
    assert kb.claim_task(conn, target) is None


@pytest.mark.parametrize("fault", ["state", "unavailable"])
def test_release_refuses_unless_the_board_evidence_is_live(synced_conn, fault, monkeypatch):
    conn = synced_conn
    case = support.conflict(conn)
    if fault == "state":
        case.hub.events[case.uid]["state"] = "deleted"
    else:
        monkeypatch.setattr(case.hub, "row_evidence", lambda *a: (_ for _ in ()).throw(OSError("Board down")))
    with pytest.raises(qr.ReleaseRefused):
        _release(conn, case)
    assert quarantine.held(conn, case.target) and quarantine.held(conn, case.child)
    assert [e["receipt"] for e in quarantine.entries(conn)] == [case.uid]
    assert support.count(conn, "containment_sync_acceptances") == 0


def test_acceptance_is_durable_and_a_redelivery_of_that_evidence_certifies(synced_conn):
    conn = synced_conn
    case = support.conflict(conn)
    accepted = case.hub.events[case.uid]
    result = _release(conn, case)
    assert result["released"] and result["receipt"] == case.uid
    [row] = conn.execute("SELECT receipt_uid,sha256,reason,operator,accepted_at FROM containment_sync_acceptances")
    raw = json.dumps(accepted, sort_keys=True, separators=(",", ":"))
    assert row[0] == case.uid and row[1] == qr.digest(raw) and row[1] == hashlib.sha256(
        json.dumps(replica._certified(raw)).encode()).hexdigest()
    assert row[2] == "Board re-stamp ruled benign" and row[3] == "test" and abs(row[4] - time.time()) < 60
    assert replica._cached(conn, case.uid) == accepted
    first = conn.execute("SELECT evidence FROM containment_canonical_receipts WHERE uid=?", (case.uid,)).fetchone()[0]
    assert json.loads(first) == case.original  # the first observation is never rewritten
    again = fixtures.event(case.stop, case.hub, 9)
    assert quarantine.certified_page(conn, case.hub, "fixture", [again]) == [again]
    assert quarantine.entries(conn) == []
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("DELETE FROM containment_sync_acceptances")


def test_release_of_an_unknown_receipt_changes_nothing(synced_conn):
    conn = synced_conn
    case = support.conflict(conn)
    assert qr.release(conn, case.hub, "fixture", "no-such-receipt", reason="x", operator="t") is None
    assert quarantine.held(conn, case.target)
    names = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'containment_sync_%'")}
    _release(conn, case)
    after = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'containment_sync_%'")}
    assert names <= after  # immutability triggers are back after a release


def _link(conn, task, uid):
    conn.execute("INSERT INTO task_chat_links (uid,task_id,chat_gateway,chat_session,kind,created_at) "
                 "VALUES (?,?,?,?,?,?)", (uid, task, "gw", "sess", "owner", int(time.time())))
    conn.execute("DELETE FROM kanban_sync_outbox WHERE NOT (table_name='task_chat_links' AND op='delete')")
    conn.execute("DELETE FROM task_chat_links WHERE uid=?", (uid,))


def test_held_tasks_own_chat_link_delete_waits_and_an_unrelated_one_is_sent(synced_conn):
    conn = synced_conn
    case = support.conflict(conn)
    other = kb.create_task(conn, title="Unrelated", assignee="default")
    _link(conn, case.target, "link-held")
    hub = Hub()
    assert ksc.push(conn, hub, "fixture", "worker", primary=False, max_batches=1).sent == 0
    assert [tuple(r) for r in conn.execute("SELECT row_uid,state FROM kanban_sync_outbox")] == [("link-held", "pending")]
    _link(conn, other, "link-free")
    assert ksc.push(conn, hub, "fixture", "worker", primary=False, max_batches=1).sent == 1
    assert [w["uid"] for w in hub.wires] == ["link-free"]
    assert ksc.push(conn, hub, "fixture", "worker", primary=False, max_batches=1) is not None
    assert "link-held" not in [w["uid"] for w in hub.wires]  # never sent while its task is held
    assert conn.execute("SELECT state FROM kanban_sync_outbox WHERE row_uid='link-held'").fetchone()[0] == "pending"


def test_unidentifiable_table_stays_pending_instead_of_killing_push(synced_conn):
    conn = synced_conn
    support.conflict(conn)
    row = {"table_name": "mystery", "row_uid": "x", "op": "upsert", "fields": "{}"}
    assert quarantine.sendable(conn, [row]) == []


def test_adopt_copies_only_new_hold_rows_and_never_the_withheld_ledger(synced_conn, tmp_path):
    source = synced_conn
    case = support.conflict(source)
    assert support.count(source, "containment_sync_withheld") >= 1
    with kbc.connect_closing(db_path=tmp_path / "live.db") as live:
        sync.set_identity(live, "fixture", "worker")
        quarantine.adopt(source, live)
        assert quarantine.held(live, case.target)
        assert support.count(live, "containment_sync_withheld") == 0
        before = live.total_changes
        quarantine.adopt(source, live)
        assert live.total_changes == before  # nothing new: no write transaction on the live writer


def test_restore_consumes_checkpoint_file(synced_conn, tmp_path):
    conn = synced_conn
    support.conflict(conn)
    path = tmp_path / "replica.db"
    quarantine.save_rebuild(conn, path)
    assert path.with_suffix(".quarantine.json").exists()
    quarantine.restore(conn, quarantine.load_rebuild(path), path)
    assert not path.with_suffix(".quarantine.json").exists()


def test_unreadable_file_fails_closed(tmp_path):
    bad = tmp_path / "shadow.db"
    bad.write_bytes(b"not a database" * 100)
    assert quarantine.file_holds(bad) is True
