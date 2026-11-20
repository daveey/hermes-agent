"""Holds are keyed per receipt: a task stays held while ANY unreleased receipt names it (review r3, item 2)."""
from __future__ import annotations

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_quarantine as quarantine
from hermes_cli import kanban_sync_quarantine_release as qr
from tests.hermes_cli import kanban_quarantine_support as support
from tests.hermes_cli import test_kanban_containment_replica as fixtures

kanban_home = fixtures.kanban_home
synced_conn = fixtures.synced_conn


def _release(conn, case, uid):
    return qr.release(conn, case.hub, "fixture", uid, reason="Board re-stamp ruled benign", operator="test")


def test_two_receipts_on_one_task_hold_it_until_both_are_released(synced_conn):
    conn = synced_conn
    case = support.conflict(conn, both=True)
    listed = {e["receipt"]: e["held_tasks"] for e in quarantine.entries(conn)}
    assert set(listed) == {case.uid, case.release_uid}
    assert all(case.target in held and case.child in held for held in listed.values())
    assert quarantine.holders(conn, case.target) == sorted([case.uid, case.release_uid])
    assert _release(conn, case, case.uid)["released"]
    assert quarantine.held(conn, case.target) and quarantine.held(conn, case.child)
    assert [e["receipt"] for e in quarantine.entries(conn)] == [case.release_uid]
    assert _release(conn, case, case.release_uid)["released"]
    assert not quarantine.held(conn, case.target) and not quarantine.held(conn, case.child)
    assert quarantine.entries(conn) == []


def test_release_drops_only_that_receipts_dependency_marks(synced_conn):
    conn = synced_conn
    a = support.conflict(conn)
    b = support.conflict(conn)
    dependent = kb.create_task(conn, title="Dependent of A via a withheld edge", assignee="default")
    edge = sync.RemoteChange(5, "task_links", f"{a.target}>{dependent}", "upsert",
                             {"parent_id": a.target, "child_id": dependent}, 5, "", "worker")
    assert quarantine.certified_page(conn, a.hub, "fixture", [edge]) == []
    marks = {tuple(r) for r in conn.execute("SELECT task_id,receipt_uid,via FROM containment_sync_quarantine_tasks")}
    assert (dependent, a.uid, "dependency") in marks and (a.child, a.uid, "dependency") in marks
    assert (b.child, b.uid, "dependency") in marks and (b.child, a.uid, "dependency") not in marks
    assert quarantine.held(conn, dependent)
    assert _release(conn, a, a.uid)["released"]
    remaining = {tuple(r) for r in conn.execute("SELECT task_id,receipt_uid FROM containment_sync_quarantine_tasks")}
    assert all(receipt == b.uid for _task, receipt in remaining)
    for task in (a.target, a.child, dependent):
        assert not quarantine.held(conn, task)
    for task in (b.target, b.child):
        assert quarantine.held(conn, task)
    # the withheld edge was replayed with the release, so the dependency is real again, now unheld
    assert conn.execute("SELECT 1 FROM task_links WHERE parent_id=? AND child_id=?", (a.target, dependent)).fetchone()
