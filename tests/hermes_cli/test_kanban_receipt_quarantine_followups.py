"""Follow-ups from the final review of #3796: the live-file refusal and commit-time certify are exercised, the sync
round files its owner notice, one card per receipt and host across the replica and a primary live file, the CLI
names a receipt the catch-up quarantined, a non-primary live file drops its freed withheld rows, and a refused
quarantine table shape is a clear exit 1."""
from __future__ import annotations

import contextlib
import itertools
from pathlib import Path
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_capability_registry as reg
from hermes_cli import kanban_containment_replica as replica
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_placement as kp
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_sync_quarantine as quarantine
from hermes_cli import kanban_sync_quarantine_release as qr
from hermes_cli import kanban_sync_watcher_alert as watcher_alert
from tests.hermes_cli import kanban_quarantine_support as support
from tests.hermes_cli import test_kanban_containment_replica as fixtures
from tests.hermes_cli import test_kanban_receipt_quarantine_round4 as r4

kanban_home = fixtures.kanban_home
synced_conn = fixtures.synced_conn


def _path(conn) -> Path:
    return Path(conn.execute("PRAGMA database_list").fetchone()[2])


def _courier(monkeypatch, gateway="worker"):
    courier = r4.Courier()
    monkeypatch.setattr(kp, "hub_runner", courier)
    monkeypatch.setattr(reg, "hub_cli_path", lambda *_a, **_k: "hermes-hub")
    monkeypatch.setattr(watcher_alert, "_gateway", lambda: gateway)
    return courier


def _picked_up(shadow, kanban_home):
    """A non-primary live file holding a picked-up copy of the scenario's target, quarantined (receipt = the
    re-stamped stop receipt) in the replica and adopted into the live file."""
    from hermes_cli.kanban_worker_node import import_cards
    target, _child, stop, release_op, hub = fixtures.scenario(shadow)
    live_path = kanban_home / "live.db"
    with kbc.connect_closing(db_path=live_path) as live:
        sync.set_identity(live, "fixture", "worker")
        assert import_cards(live, shadow, [target]) == [target]
    uid = stop["event_uid"]
    replica.remember(shadow, hub.events[uid], board="fixture", task=target, uid=uid)
    hub.events[uid]["row"]["created_at"] += 1
    marker = sync.RemoteChange(1, "tasks", target, "upsert", {"status": "triage", "containment_episode": uid},
                               100, "", "home")
    assert quarantine.certified_page(shadow, hub, "fixture", [marker, fixtures.event(stop, hub, 2)]) == []
    with kbc.connect_closing(db_path=live_path) as live:
        quarantine.adopt(shadow, live)
        assert sync.board_mode(live) != "primary" and quarantine.receipts(live) == [uid]
    return SimpleNamespace(target=target, uid=uid, release=release_op, release_uid=release_op["event_uid"], hub=hub,
                           live_path=live_path)


# --- 1. the live-file refusal while the replica still holds the task -----------------------------------------

def test_non_primary_live_release_is_refused_while_the_replica_holds_the_task_under_another_receipt(
        synced_conn, kanban_home, monkeypatch, capsys):
    shadow = synced_conn
    case = _picked_up(shadow, kanban_home)
    # A second receipt names the task in the replica only (the release event re-checks the re-stamped stop).
    assert quarantine.certified_page(shadow, case.hub, "fixture", [fixtures.event(case.release, case.hub, 3)]) == []
    assert sorted(quarantine.receipts(shadow)) == sorted([case.uid, case.release_uid])
    r4._cli(monkeypatch, case.live_path, _path(shadow), case.hub)
    assert r4._run(case.uid) == 2
    out = capsys.readouterr().out
    assert f"the replica still holds {case.target} under receipt {case.release_uid}" in out
    assert quarantine.release_command("fixture", case.release_uid) in out
    assert quarantine.receipts(shadow) == [case.release_uid]  # the replica released its copy of the receipt
    with kbc.connect_closing(db_path=case.live_path) as live:
        assert quarantine.held(live, case.target) and quarantine.receipts(live) == [case.uid]
        assert support.count(live, "containment_sync_acceptances") == 0


# --- 2. the commit step certifies against the prefetched evidence --------------------------------------------

def test_evidence_changed_between_prefetch_and_commit_is_refused_inside_the_transaction(synced_conn, monkeypatch):
    conn = synced_conn
    case = support.conflict(conn)
    op, episode = r4._second_episode(case)
    assert quarantine.certified_page(conn, case.hub, "fixture", [fixtures.event(op, case.hub, 5)]) == []
    withheld = support.count(conn, "containment_sync_withheld")
    real = qr._commit

    def changed(c, evidence, *args, **kwargs):
        evidence.seen[("task_events", episode)]["row"]["created_at"] += 1  # differs from the pre-check's copy
        return real(c, evidence, *args, **kwargs)

    monkeypatch.setattr(qr, "_commit", changed)
    with pytest.raises(qr.ReleaseRefused, match="did not certify"):
        r4._release(conn, case.hub, case.uid)
    assert support.count(conn, "containment_sync_acceptances") == 0
    assert [e["receipt"] for e in quarantine.entries(conn)] == [case.uid]
    assert quarantine.held(conn, case.target) and quarantine.held(conn, case.child)
    assert support.count(conn, "containment_sync_withheld") == withheld
    assert conn.execute("SELECT 1 FROM task_events WHERE uid IN (?,?)", (case.uid, episode)).fetchone() is None


# --- 3. the sync round files the owner notice -----------------------------------------------------------------

class _Stop(Exception):
    pass


def test_sync_round_that_quarantines_a_receipt_files_one_notice_for_this_host(synced_conn, kanban_home,
                                                                               monkeypatch):
    shadow = synced_conn
    target, _child, stop, _release, hub = fixtures.scenario(shadow)
    live_path = kanban_home / "live.db"
    with kbc.connect_closing(db_path=live_path) as live:
        shadow.backup(live)
    uid = stop["event_uid"]
    replica.remember(shadow, hub.events[uid], board="fixture", task=target, uid=uid)
    hub.events[uid]["row"]["created_at"] += 1

    @contextlib.contextmanager
    def pulled(*_a, **_k):  # this round's pull: the Board re-stamped the receipt
        assert quarantine.certified_page(shadow, hub, "fixture", [fixtures.event(stop, hub, 1)]) == []
        yield shadow, ksc.Pulled(1), False

    def stop_round(*_a, **_k):
        raise _Stop  # the notice is filed; the rest of the round is not under test

    monkeypatch.setattr(ksc, "shadow_path", lambda slug: kanban_home / "no-shadow.db")
    monkeypatch.setattr(ksc, "_send", lambda *a, **k: None)
    monkeypatch.setattr(ksc, "_pulled_replica", pulled)
    monkeypatch.setattr(ksc.acw, "expedite", stop_round)
    courier = _courier(monkeypatch)
    for _ in range(2):
        with pytest.raises(_Stop):
            ksc._sync_board_locked("fixture", live_path, hub, "worker")
    assert [r4._id(argv) for argv in courier.calls] == [
        f"containment-quarantine:fixture:{uid}:worker:{quarantine.entries(shadow)[0]['quarantined_at']}"]
    with kbc.connect_closing(db_path=live_path) as live:
        assert quarantine.receipts(live) == [uid]


# --- 4. a primary board files ONE card for a receipt both files quarantine at release ------------------------

def test_primary_release_files_one_card_for_an_event_both_files_newly_quarantine(synced_conn, kanban_home,
                                                                                  monkeypatch, capsys):
    shadow = synced_conn
    case = support.conflict(shadow)
    op, episode = r4._second_episode(case)
    assert quarantine.certified_page(shadow, case.hub, "fixture", [fixtures.event(op, case.hub, 5)]) == []
    live_path = kanban_home / "live.db"
    with kbc.connect_closing(db_path=live_path) as live:
        shadow.backup(live)  # the primary live file saw the same pages
        sync.set_mode(live, "primary")
    case.hub.events[episode]["row"]["created_at"] += 1  # re-stamped after both files certified and withheld it
    clock = itertools.count(5000.0, 7.0)
    monkeypatch.setattr(quarantine, "_clock", lambda: next(clock))  # each file would stamp its own first-seen
    courier = _courier(monkeypatch)
    r4._cli(monkeypatch, live_path, _path(shadow), case.hub)
    assert r4._run(case.uid) == 0, capsys.readouterr().out
    assert "replica, live" in capsys.readouterr().out
    with kbc.connect_closing(db_path=live_path) as live:
        [mine] = quarantine.entries(live)
    [theirs] = quarantine.entries(shadow)
    assert mine["receipt"] == theirs["receipt"] == episode
    assert mine["quarantined_at"] == theirs["quarantined_at"]
    assert [r4._id(argv) for argv in courier.calls] == [
        f"containment-quarantine:fixture:{episode}:worker:{theirs['quarantined_at']}"]


# --- 5. the CLI names a receipt the replica's catch-up quarantined -------------------------------------------

def test_cli_names_the_new_receipt_when_a_non_primary_live_release_stops_behind_it(synced_conn, kanban_home,
                                                                                    monkeypatch, capsys):
    shadow = synced_conn
    case = _picked_up(shadow, kanban_home)
    op, episode = r4._second_episode(case)
    assert quarantine.certified_page(shadow, case.hub, "fixture", [fixtures.event(op, case.hub, 5)]) == []
    case.hub.events[episode]["row"]["created_at"] += 1
    _courier(monkeypatch)
    r4._cli(monkeypatch, case.live_path, _path(shadow), case.hub)
    assert r4._run(case.uid) == 2
    out = capsys.readouterr().out
    assert f"QUARANTINED {episode} (release with `{quarantine.release_command('fixture', episode)}`)" in out
    assert "rerun to finish" not in out
    with kbc.connect_closing(db_path=case.live_path) as live:
        assert quarantine.held(live, case.target)


# --- 6. a non-primary live file drops its freed withheld rows in the lift ------------------------------------

def test_non_primary_release_leaves_no_withheld_rows_of_the_freed_task(synced_conn, kanban_home, monkeypatch,
                                                                        capsys):
    shadow = synced_conn
    case = _picked_up(shadow, kanban_home)
    stale = sync.RemoteChange(4, "tasks", case.target, "upsert", {"title": "stale reconcile copy"}, 4, "", "worker")
    with kbc.connect_closing(db_path=case.live_path) as live:
        assert quarantine.withhold(live, [stale]) == []
        assert support.count(live, "containment_sync_withheld") == 1
    r4._cli(monkeypatch, case.live_path, _path(shadow), case.hub)
    assert r4._run(case.uid) == 0, capsys.readouterr().out
    with kbc.connect_closing(db_path=case.live_path) as live:
        assert not quarantine.held(live, case.target)
        assert support.count(live, "containment_sync_withheld") == 0
        assert live.execute("SELECT title FROM tasks WHERE id=?", (case.target,)).fetchone()[0] != stale.fields["title"]


# --- 7. a refused quarantine table shape while opening the replica is exit 1, not a traceback ------------------

def test_refused_quarantine_shape_while_opening_the_replica_exits_1(synced_conn, kanban_home, monkeypatch, capsys):
    shadow_path, live_path = kanban_home / "shadow.db", kanban_home / "live.db"
    with kbc.connect_closing(db_path=shadow_path) as bad:
        sync.set_identity(bad, "fixture", "worker")
        bad.execute("CREATE TABLE containment_sync_quarantine (uid TEXT PRIMARY KEY,task_id TEXT NOT NULL,"
                    "board TEXT NOT NULL,reason TEXT NOT NULL,wire TEXT NOT NULL)")
    shadow_path.with_suffix(".quarantine.json").write_text('{"receipts": []}')  # a rebuild checkpoint to restore
    with kbc.connect_closing(db_path=live_path) as live:
        sync.set_identity(live, "fixture", "worker")
    r4._cli(monkeypatch, live_path, shadow_path, hub=object())
    assert r4._run("receipt-x") == 1
    out = capsys.readouterr().out
    assert "receipt-x not released" in out and "earlier unreleased quarantine shape" in out


# --- review of #3828: the item-6 deletion keeps rows another receipt holds and rows naming no known task -------

def test_non_primary_lift_keeps_withheld_rows_of_another_receipt_and_rows_naming_no_known_task(
        synced_conn, kanban_home, monkeypatch, capsys):
    shadow = synced_conn
    case = _picked_up(shadow, kanban_home)
    other = "t_held_by_receipt_r"
    stale = sync.RemoteChange(4, "tasks", case.target, "upsert", {"title": "stale reconcile copy"}, 4, "", "worker")
    edge = sync.RemoteChange(5, "task_links", f"{case.target}>{other}", "upsert",
                             {"parent_id": case.target, "child_id": other}, 5, "", "worker")
    orphan = sync.RemoteChange(6, "task_comments", "comment-gone", "delete", {}, 6, "", "worker")  # no known task
    with kbc.connect_closing(db_path=case.live_path) as live:
        r4._hold(live, other)  # a second live receipt holds the edge's other end
        assert quarantine.withhold(live, [stale, edge, orphan]) == []
        assert support.count(live, "containment_sync_withheld") == 3
    r4._cli(monkeypatch, case.live_path, _path(shadow), case.hub)
    assert r4._run(case.uid) == 0, capsys.readouterr().out
    with kbc.connect_closing(db_path=case.live_path) as live:
        assert not quarantine.held(live, case.target) and quarantine.held(live, other)
        kept = {r[0] for r in live.execute("SELECT wire FROM containment_sync_withheld")}
    assert kept == {quarantine.wire(edge), quarantine.wire(orphan)}  # only the freed task's own row is dropped


def test_containment_refusal_in_release_board_exits_1_and_keeps_the_holds(synced_conn, kanban_home, monkeypatch,
                                                                          capsys):
    shadow = synced_conn
    case = _picked_up(shadow, kanban_home)
    other = kanban_home / "other-board.db"
    with kbc.connect_closing(db_path=other) as conn:
        sync.set_identity(conn, "other-board", "worker")
    real_open = ksc.open_shadow

    def open_and_adopt(slug, gateway):  # opening the replica copies its holds into a file of another board
        conn = real_open(slug, gateway)
        with kbc.connect_closing(db_path=other) as dest:
            try:
                quarantine.adopt(conn, dest)  # the real board check raises ContainmentRefused
            except Exception:
                conn.close()
                raise
        return conn

    monkeypatch.setattr(ksc, "open_shadow", open_and_adopt)
    r4._cli(monkeypatch, case.live_path, _path(shadow), case.hub)
    assert r4._run(case.uid) == 1
    out = capsys.readouterr().out
    assert f"{case.uid} not released: ContainmentRefused: quarantine belongs to a different board" in out
    assert "every hold is kept" in out
    with kbc.connect_closing(db_path=case.live_path) as live:
        assert quarantine.held(live, case.target) and quarantine.receipts(live) == [case.uid]
        assert support.count(live, "containment_sync_acceptances") == 0
    assert quarantine.receipts(shadow) == [case.uid] and support.count(shadow, "containment_sync_acceptances") == 0
