"""Review round 4 of #3796: notice key per host and episode, acceptance inside the catch-up transaction,
uncertifiable catch-up events quarantined, withhold-order replay, live release branches, reconcile, schema check
and apply-time errors."""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_containment_connections as connections
from hermes_cli import kanban_containment_replica as replica
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_cli as cli
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_sync_quarantine as quarantine
from hermes_cli import kanban_sync_quarantine_notice as notice
from hermes_cli import kanban_sync_quarantine_release as qr
from hermes_cli import kanban_sync_refusal as refusal
from hermes_cli.kanban_containment_lifecycle import ContainmentRefused
from hermes_cli.kanban_containment_state import fingerprint
from tests.hermes_cli import kanban_quarantine_support as support
from tests.hermes_cli import test_kanban_containment_canonical as wire
from tests.hermes_cli import test_kanban_containment_replica as fixtures

kanban_home = fixtures.kanban_home
synced_conn = fixtures.synced_conn


class Courier:
    def __init__(self):
        self.calls = []

    def __call__(self, argv):
        self.calls.append(argv)
        return 0, "", ""


def _id(argv):
    return argv[argv.index("--id") + 1]


def _release(conn, hub, uid):
    return qr.release(conn, hub, "fixture", uid, reason="Board re-stamp ruled benign", operator="test")


def _link(conn, parent, child):
    return conn.execute("SELECT 1 FROM task_links WHERE parent_id=? AND child_id=?", (parent, child)).fetchone()


# --- 1. owner notice key: per host and per quarantine episode -----------------------------------------------

def test_two_hosts_on_one_receipt_file_two_cards(synced_conn):
    conn = synced_conn
    case = support.conflict(conn)
    courier = Courier()
    notice.notify(conn, "fixture", runner=courier, hub_cli="hermes-hub", host="worker-1", now=1000.0)
    notice.notify(conn, "fixture", runner=courier, hub_cli="hermes-hub", host="worker-2", now=1000.0)
    ids = [_id(argv) for argv in courier.calls]
    assert len(ids) == 2 and len(set(ids)) == 2
    assert {i.split(":")[3] for i in ids} == {"worker-1", "worker-2"}
    assert all(i.startswith(f"containment-quarantine:fixture:{case.uid}:") for i in ids)


def test_requarantine_after_release_files_a_new_card(synced_conn, monkeypatch):
    conn = synced_conn
    monkeypatch.setattr(quarantine, "_clock", lambda: 1000.0)
    case = support.conflict(conn)
    courier = Courier()
    notice.notify(conn, "fixture", runner=courier, hub_cli="hermes-hub", host="worker-1", now=1000.0)
    _release(conn, case.hub, case.uid)
    notice.notify(conn, "fixture", runner=courier, hub_cli="hermes-hub", host="worker-1", now=1001.0)
    monkeypatch.setattr(quarantine, "_clock", lambda: 2000.0)
    case.hub.events[case.uid]["row"]["created_at"] += 1  # the Board re-stamps the receipt again
    assert quarantine.certified_page(conn, case.hub, "fixture", [fixtures.event(case.stop, case.hub, 30)]) == []
    notice.notify(conn, "fixture", runner=courier, hub_cli="hermes-hub", host="worker-1", now=1002.0)
    ids = [_id(argv) for argv in courier.calls]
    assert len(ids) == 2 and ids[0] != ids[1]
    assert ids == [f"containment-quarantine:fixture:{case.uid}:worker-1:1000000",
                   f"containment-quarantine:fixture:{case.uid}:worker-1:2000000"]


# --- 2. acceptance written in the catch-up transaction ------------------------------------------------------

def test_failed_catch_up_leaves_no_acceptance_and_every_hold(synced_conn, monkeypatch):
    conn = synced_conn
    case = support.conflict(conn)

    def broken(*_a, **_k):
        raise RuntimeError("apply failed mid-transaction")
    monkeypatch.setattr(sync, "_apply_one", broken)
    with pytest.raises(RuntimeError, match="apply failed"):
        _release(conn, case.hub, case.uid)
    assert support.count(conn, "containment_sync_acceptances") == 0
    assert quarantine.held(conn, case.target) and quarantine.held(conn, case.child)
    assert [e["receipt"] for e in quarantine.entries(conn)] == [case.uid]
    assert support.count(conn, "containment_sync_withheld") >= 1


# --- 3. a freed event whose evidence no longer certifies is quarantined under its own receipt ----------------

def _second_episode(case):
    op, stop, episode = wire.operation()
    op["task"] = case.target
    stop["target"]["task_id"] = case.target
    op["payload"] = stop
    proof = wire.evidence(op, stop, episode)
    proof["row"]["task_id"] = case.target
    proof["row"]["created_at"] = 1
    proof["containment_v1"]["containment_sha256"] = fingerprint(stop)
    case.hub.events[episode] = proof
    return op, episode


def test_uncertifiable_catch_up_event_is_quarantined_under_its_own_receipt(synced_conn):
    conn = synced_conn
    case = support.conflict(conn)
    op, episode = _second_episode(case)
    assert quarantine.certified_page(conn, case.hub, "fixture", [fixtures.event(op, case.hub, 5)]) == []
    case.hub.events[episode]["row"]["created_at"] += 1  # re-stamped after it was certified and withheld
    result = _release(conn, case.hub, case.uid)
    assert result["released"] and result["quarantined"] == [episode]
    assert quarantine.holders(conn, case.target) == [episode]
    assert quarantine.held(conn, case.child) and kb.claim_task(conn, case.target) is None
    assert support.count(conn, "containment_sync_acceptances") == 1  # only the released receipt: not widened
    assert _release(conn, case.hub, episode)["released"]
    assert not quarantine.held(conn, case.target)
    assert connections.held(conn, case.target)  # both certified contained events are applied now
    for uid in (case.uid, episode):
        assert conn.execute("SELECT 1 FROM task_events WHERE uid=?", (uid,)).fetchone()


# --- 4. replay order: snapshot rows withheld during a resync replay after earlier feed rows ------------------

def test_link_delete_then_snapshot_readd_replays_in_withhold_order(synced_conn):
    conn = synced_conn
    # An edge below the held task (the contained task's own edges stay guarded by containment itself).
    case = support.conflict(conn, before=lambda _target, child: kb.create_task(
        conn, title="Fixture grandchild", assignee="default", parents=[child]))
    grandchild = case.extra
    assert quarantine.held(conn, grandchild)
    edge = f"{case.child}>{grandchild}"
    drop = sync.RemoteChange(10, "task_links", edge, "delete", {}, 10, "", "worker")
    assert quarantine.certified_page(conn, case.hub, "fixture", [drop]) == []
    readd = sync.RemoteChange(0, "task_links", edge, "upsert", {"parent_id": case.child, "child_id": grandchild},
                              0, "", "worker")  # a later resync's snapshot row: seq 0
    assert quarantine.certified_page(conn, case.hub, "fixture", [readd]) == []
    assert _link(conn, case.child, grandchild)  # both withheld so far
    _release(conn, case.hub, case.uid)
    assert _link(conn, case.child, grandchild)  # delete (epoch 2) then re-add (epoch 3), not by seq


# --- 5. live branches of release_board ------------------------------------------------------------------------

def _cli(monkeypatch, live: Path, shadow: Path, hub):
    monkeypatch.setattr(cli, "_path", lambda slug: live)
    monkeypatch.setattr(ksc, "shadow_path", lambda slug: shadow)
    monkeypatch.setattr(ksc, "load_identity", lambda: SimpleNamespace(gateway="worker", url="https://board.test"))
    monkeypatch.setattr(ksc, "Hub", lambda ident: hub)


def _run(release, reason="ruled benign"):
    return cli.cmd_sync(argparse.Namespace(sync_action="quarantine", board="fixture", json=False,
                                           release=release, reason=reason))


def test_non_primary_live_copy_takes_the_replica_state_in_the_lift_transaction(synced_conn, kanban_home,
                                                                                 monkeypatch, capsys):
    shadow = synced_conn
    target, _child, stop, _release_op, hub = fixtures.scenario(shadow)
    live_path = kanban_home / "live.db"
    from hermes_cli.kanban_worker_node import import_cards
    with kbc.connect_closing(db_path=live_path) as live:
        sync.set_identity(live, "fixture", "worker")
        assert import_cards(live, shadow, [target]) == [target]  # a picked-up copy in a non-primary live file
    uid = stop["event_uid"]
    replica.remember(shadow, hub.events[uid], board="fixture", task=target, uid=uid)
    hub.events[uid]["row"]["created_at"] += 1
    marker = sync.RemoteChange(1, "tasks", target, "upsert", {"status": "triage", "containment_episode": uid},
                               100, "", "home")
    assert quarantine.certified_page(shadow, hub, "fixture", [marker, fixtures.event(stop, hub, 2)]) == []
    with kbc.connect_closing(db_path=live_path) as live:
        quarantine.adopt(shadow, live)
        assert sync.board_mode(live) != "primary" and quarantine.held(live, target)
        assert kb.claim_task(live, target) is None
    _cli(monkeypatch, live_path, Path(shadow.execute("PRAGMA database_list").fetchone()[2]), hub)
    assert _run(uid) == 0, capsys.readouterr().out
    assert "replica, live" in capsys.readouterr().out
    with kbc.connect_closing(db_path=live_path) as live:
        assert not quarantine.held(live, target)
        assert live.execute("SELECT 1 FROM task_events WHERE uid=?", (uid,)).fetchone()  # the replica's event
        assert connections.held(live, target)
        assert kb.claim_task(live, target) is None


def test_primary_live_replays_its_own_withheld_rows(synced_conn, kanban_home, monkeypatch, capsys):
    live = synced_conn
    sync.set_mode(live, "primary")
    case = support.conflict(live)
    _cli(monkeypatch, Path(live.execute("PRAGMA database_list").fetchone()[2]), kanban_home / "no-shadow.db",
         case.hub)
    assert _run(case.uid) == 0
    assert "in live" in capsys.readouterr().out
    assert not quarantine.held(live, case.target)
    assert live.execute("SELECT 1 FROM task_events WHERE uid=?", (case.uid,)).fetchone()
    assert connections.held(live, case.target) and kb.claim_task(live, case.target) is None


def test_cli_names_an_event_quarantined_by_the_catch_up(synced_conn, kanban_home, monkeypatch, capsys):
    conn = synced_conn
    case = support.conflict(conn)
    op, episode = _second_episode(case)
    assert quarantine.certified_page(conn, case.hub, "fixture", [fixtures.event(op, case.hub, 5)]) == []
    case.hub.events[episode]["state"] = "deleted"  # no longer certifiable at all
    with kbc.connect_closing(db_path=kanban_home / "live.db") as live:
        sync.set_identity(live, "fixture", "worker")
    _cli(monkeypatch, kanban_home / "live.db", Path(conn.execute("PRAGMA database_list").fetchone()[2]), case.hub)
    assert _run(case.uid) == 0
    out = capsys.readouterr().out
    assert f"QUARANTINED: {episode}" in out and f"--release {episode} --reason" in out
    [entry] = quarantine.entries(conn)
    assert entry["receipt"] == episode and entry["kind"] == "uncertifiable"


# --- 6. reconcile withholds held tasks' rows ---------------------------------------------------------------

def _hold(conn, task):
    with kbc.write_txn(conn):
        quarantine.install(conn)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(containment_sync_quarantine)")]
        values = {"uid": "receipt-r", "task_id": task, "board": "default", "kind": "identity_changed",
                  "reason": "test", "wire": "{}", "quarantined_at": 1}
        conn.execute(f"INSERT INTO containment_sync_quarantine ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                     [values[c] for c in cols])
        cols = [r[1] for r in conn.execute("PRAGMA table_info(containment_sync_quarantine_tasks)")]
        values = {"task_id": task, "receipt_uid": "receipt-r", "via": "direct"}
        conn.execute(f"INSERT INTO containment_sync_quarantine_tasks ({','.join(cols)}) "
                     f"VALUES ({','.join('?' * len(cols))})", [values[c] for c in cols])


def test_reconcile_does_not_restore_rows_of_a_held_task(kanban_home):
    from tests.hermes_cli import kanban_sync_support as sync_support
    from tests.hermes_cli.kanban_sync_fake_board import FakeService
    svc = FakeService()
    sync_support.write_sync_config(kanban_home, True)
    path = kanban_home / "kanban.db"
    live = kbc.connect(path)
    try:
        ksc.ensure_identity(live, "default", "hub")
        sync.set_mode(live, "primary")
        tid = kb.create_task(live, title="card", assignee="worker")
        kb.add_comment(live, tid, "me", "synced note")
        ksc.push(live, svc.hub("hub"), "default", "hub", primary=True)
        with kbc.write_txn(live):  # the row is lost the way a rebuild loses it: no capture
            live.execute("UPDATE kanban_sync_state SET applying = 1")
            live.execute("DELETE FROM task_comments WHERE body = 'synced note'")
            live.execute("UPDATE kanban_sync_state SET applying = 0")
        _hold(live, tid)
        report = ksc.reconcile("default", path, hub=svc.hub("hub"))
        assert report["restored"] == {}
        assert live.execute("SELECT 1 FROM task_comments WHERE body = 'synced note'").fetchone() is None
        assert support.count(live, "containment_sync_withheld") == 1  # replayed by the release, like a pull's
    finally:
        live.close()


# --- 7. an earlier unreleased table shape is refused, per board ---------------------------------------------

def test_older_quarantine_shape_is_refused_and_ends_only_that_boards_round(synced_conn, tmp_path, monkeypatch):
    conn = synced_conn
    conn.execute("CREATE TABLE containment_sync_quarantine (uid TEXT PRIMARY KEY,task_id TEXT NOT NULL,"
                 "board TEXT NOT NULL,reason TEXT NOT NULL,wire TEXT NOT NULL)")
    with pytest.raises(quarantine.QuarantineSchemaRefused, match="earlier unreleased quarantine shape") as caught:
        quarantine.install(conn)
    assert isinstance(caught.value, ContainmentRefused)

    def round_for_fixture(slug, path, transport, gateway):
        quarantine.install(conn)
    monkeypatch.setattr(ksc, "sync_board", round_for_fixture)
    monkeypatch.setattr(ksc, "publish_pickup_if_changed", lambda hub: None)
    from hermes_cli import kanban_source_clones
    monkeypatch.setattr(kanban_source_clones, "attest_if_due", lambda hub: None)
    refusal.reset()
    try:
        [result] = ksc.sync_all_once(SimpleNamespace(ident=SimpleNamespace(gateway="worker")),
                                     boards=[("fixture", tmp_path / "fixture.db")])
        assert result.error.startswith("ContainmentRefused:") and "unreleased quarantine shape" in result.error
    finally:
        refusal.reset()


# --- 8. apply-time database/runtime errors: exit 1, holds kept -------------------------------------------------

@pytest.mark.parametrize("error", [sqlite3.OperationalError("database is locked"), RuntimeError("apply aborted")])
def test_cli_apply_time_error_exits_1_and_keeps_the_holds(synced_conn, kanban_home, monkeypatch, capsys, error):
    conn = synced_conn
    case = support.conflict(conn)
    with kbc.connect_closing(db_path=kanban_home / "live.db") as live:
        sync.set_identity(live, "fixture", "worker")
    _cli(monkeypatch, kanban_home / "live.db", Path(conn.execute("PRAGMA database_list").fetchone()[2]), case.hub)

    def broken(*_a, **_k):
        raise error
    monkeypatch.setattr(sync, "_apply_one", broken)
    assert _run(case.uid) == 1
    out = capsys.readouterr().out
    assert "not released" in out and "every hold is kept" in out and type(error).__name__ in out
    assert quarantine.held(conn, case.target) and quarantine.held(conn, case.child)
    assert support.count(conn, "containment_sync_acceptances") == 0
