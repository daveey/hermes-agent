"""Central Kanban sync client (phase 2): push the outbox as row ops, pull the feed (into a shadow copy or the live
board), snapshot resync, import, diff and the hub lease client. The service is an in-memory fake of the hermes-hub
Board contract (``kanban_sync_fake_board``). Design: hermes-mods ``core/kanban-central-service.md``."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as ksc, kanban_sync_wire as wire_ops
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_sync_fake_board import FakeService

ME, OTHER, BOARD = "macbook", "mini", "default"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    return support.kanban_home(tmp_path, monkeypatch)


@pytest.fixture
def svc():
    return FakeService()


@pytest.fixture
def live(kanban_home):
    support.write_sync_config(kanban_home, True)
    conn = kbc.connect(kanban_home / "kanban.db")
    ksc.ensure_identity(conn, BOARD, ME)
    try:
        yield conn
    finally:
        conn.close()


def _pending(conn) -> list[dict]:
    return [dict(r) for r in conn.execute("SELECT * FROM kanban_sync_outbox WHERE state = 'pending'")]


def _round(kanban_home, svc, gateway=ME):
    return ksc.sync_board(BOARD, kanban_home / "kanban.db", svc.hub(gateway), gateway)


def _shadow():
    return kbc.connect(ksc.shadow_path(BOARD))


def _other_creates(svc, task_id, **fields):
    """Another gateway pushes a card straight to the service."""
    hub = svc.hub(OTHER)
    return hub.ops(BOARD, [{"client_op_id": f"other-{task_id}-op", "op": "upsert", "table": "tasks", "uid": task_id,
                            "hlc": f"{1:013d}-0000-{OTHER}",
                            "fields": {"title": task_id, "status": "ready", "created_at": 1, **fields}}])


def _tables_digest(conn) -> str:
    h = hashlib.sha256()
    for table in ("tasks", "task_links", "task_comments", "task_runs", "task_events"):
        cols = [c for c in sync.table_columns(conn, table) if c not in ("sync_version", "lease_holder", "lease_expires")]
        for row in conn.execute(f"SELECT {', '.join(cols)} FROM {table} ORDER BY 1"):
            h.update(repr(tuple(row)).encode())
    return h.hexdigest()


# --- translate --------------------------------------------------------------------------------------------------

def test_translate_upsert_import_and_delete_rows():
    up = wire_ops.translate({"op_id": "0192-a", "table_name": "tasks", "row_uid": "t_1", "op": "upsert",
                        "fields": json.dumps({"title": "x", "status": "ready", "lease_holder": "me", "brand_new": 1})},
                       "0000000000001-0000-me")
    assert up == {"client_op_id": "0192-a", "table": "tasks", "uid": "t_1", "hlc": "0000000000001-0000-me",
                  "op": "upsert", "fields": {"title": "x", "status": "ready"}}  # local/undeclared columns stay home
    imp = wire_ops.translate({"op_id": "0192-b", "table_name": "tasks", "row_uid": "t_1", "op": "import",
                         "fields": {"title": "x"}}, "h")
    assert imp["op"] == "upsert" and imp["import"] is True
    child = wire_ops.translate({"op_id": "0192-c", "table_name": "task_runs", "row_uid": "r-1", "op": "import",
                           "fields": {"task_id": "t_1", "status": "done"}}, "h")
    assert child["op"] == "upsert" and "import" not in child
    gone = wire_ops.translate({"op_id": "0192-d", "table_name": "task_links", "row_uid": "a>b", "op": "delete",
                          "fields": "{}"}, "h")
    assert gone == {"client_op_id": "0192-d", "table": "task_links", "uid": "a>b", "hlc": "h", "op": "delete"}


def test_hlc_is_monotonic_and_survives_a_reopen(live, kanban_home):
    with kbc.write_txn(live):
        a = ksc._next_hlc(live, ME)
        b = ksc._next_hlc(live, ME)
    assert b > a and a.endswith(f"-{ME}")
    live.close()
    conn = kbc.connect(kanban_home / "kanban.db")
    with kbc.write_txn(conn):
        assert ksc._next_hlc(conn, ME) > b
    conn.close()


# --- push -------------------------------------------------------------------------------------------------------

def test_push_drains_the_outbox_and_the_service_records_this_gateway_as_creator(live, svc):
    tid = kb.create_task(live, title="pushed", assignee="worker")
    kb.add_comment(live, tid, "me", "a note")
    assert _pending(live)
    res = ksc.push(live, svc.hub(ME), BOARD, ME, primary=False)
    assert res.rejected == 0 and res.sent >= 2 and not _pending(live)
    row = svc.board(BOARD)["rows"]["tasks"][tid]
    assert row["title"] == "pushed" and row["created_on"] == ME
    assert any(r["body"] == "a note" for r in svc.board(BOARD)["rows"]["task_comments"].values())
    assert kb.get_task(live, tid) is not None
    assert live.execute("SELECT sync_version FROM tasks WHERE id = ?", (tid,)).fetchone()[0] >= 1


def test_transport_failure_keeps_rows_pending_and_counts_the_attempt(live, svc):
    kb.create_task(live, title="offline", assignee="worker")
    n = len(_pending(live))
    svc.offline = True
    with pytest.raises(OSError):
        ksc.push(live, svc.hub(ME), BOARD, ME, primary=False)
    rows = _pending(live)
    assert len(rows) == n and all(r["attempts"] == 1 and "unreachable" in r["last_error"] for r in rows)


def test_outbox_survives_a_restart_and_a_lost_ack_replays_idempotently(live, svc, kanban_home):
    tid = kb.create_task(live, title="replay", assignee="worker")
    wire = [wire_ops.translate(r, f"{i + 1:013d}-0000-{ME}") for i, r in enumerate(_pending(live))]
    svc.hub(ME).ops(BOARD, wire)  # the service applied them, but the answer never arrived
    live.close()
    conn = kbc.connect(kanban_home / "kanban.db")  # restart: the rows are still queued
    assert len(_pending(conn)) == len(wire)
    res = ksc.push(conn, svc.hub(ME), BOARD, ME, primary=False)
    assert res.rejected == 0 and not _pending(conn)
    assert len([c for c in svc.board(BOARD)["changes"] if c["table"] == "tasks" and c["uid"] == tid]) == 1
    conn.close()


def test_rejected_op_is_marked_and_a_primary_board_restores_the_service_row(live, svc):
    tid = kb.create_task(live, title="contested", assignee="worker")
    ksc.push(live, svc.hub(ME), BOARD, ME, primary=False)
    svc.board(BOARD)["leases"][tid] = {"task_id": tid, "holder": OTHER, "run_uid": "run-other-1", "expires": 9e15}
    sync.set_mode(live, "primary")
    with kbc.write_txn(live):
        live.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid,))
    res = ksc.push(live, svc.hub(ME), BOARD, ME, primary=True)
    assert res.rejected == 1
    rej = live.execute("SELECT state, last_error FROM kanban_sync_outbox").fetchone()
    assert rej["state"] == "rejected" and rej["last_error"].startswith("live_claim")
    assert kb.get_task(live, tid).status == "ready"  # the service's row, not the refused local edit


# --- pull -------------------------------------------------------------------------------------------------------

def test_shadow_pull_fills_the_shadow_copy_and_never_touches_the_live_board(live, svc, kanban_home):
    mine = kb.create_task(live, title="mine", assignee="worker")
    _other_creates(svc, "t_theirs")
    live_before = _tables_digest(live)
    result = _round(kanban_home, svc)
    assert result.mode == "shadow" and result.error is None
    shadow = _shadow()
    try:
        rows = {r["id"]: r["created_on"] for r in shadow.execute("SELECT id, created_on FROM tasks")}
    finally:
        shadow.close()
    assert rows.get("t_theirs") == OTHER  # the other gateway's card reached our shadow copy
    _round(kanban_home, svc)  # our own push comes back on the next pull
    shadow = _shadow()
    try:
        assert shadow.execute("SELECT created_on FROM tasks WHERE id = ?", (mine,)).fetchone()[0] == ME
    finally:
        shadow.close()
    assert kb.get_task(live, "t_theirs") is None
    assert _tables_digest(live) == live_before  # pushes only clear outbox rows; no remote row lands live


def test_a_replica_pull_stamps_pulled_at_and_a_failed_one_does_not(live, svc, kanban_home):
    """Readers derive a replica's staleness from its last successful pull (kanban_chat_links.replica_cards)."""
    before = time.time()
    assert _round(kanban_home, svc).error is None
    shadow = _shadow()
    try:
        stamped = sync.sync_state(shadow)["pulled_at"]
    finally:
        shadow.close()
    assert stamped is not None and stamped >= before
    svc.offline = True
    assert ksc.sync_all_once(svc.hub(ME))[0].error  # the round failed
    shadow = _shadow()
    try:
        assert sync.sync_state(shadow)["pulled_at"] == stamped  # an offline round never refreshes it
    finally:
        shadow.close()


def test_pull_advances_the_cursor_with_the_page_and_keeps_it_when_apply_fails(live, svc, monkeypatch):
    sync.set_mode(live, "primary")
    _other_creates(svc, "t_a")
    _other_creates(svc, "t_b")
    hub = svc.hub(ME)
    assert ksc.pull(live, hub, BOARD, observers=False) == 2
    assert sync.sync_state(live)["cursor"] == svc.board(BOARD)["seq"]
    _other_creates(svc, "t_c")
    real = sync.apply_remote_changes

    def broken(conn, changes, **kw):
        real(conn, changes, **kw)
        raise RuntimeError("crash after apply, before the caller saw it")
    monkeypatch.setattr(sync, "apply_remote_changes", broken)
    with pytest.raises(RuntimeError):
        ksc.pull(live, hub, BOARD, observers=False)
    monkeypatch.setattr(sync, "apply_remote_changes", real)
    # The page committed with its cursor: a replay reads nothing new and the row is there once.
    assert ksc.pull(live, hub, BOARD, observers=False) == 0
    assert kb.get_task(live, "t_c") is not None


def test_cursor_below_the_retention_floor_rebuilds_the_shadow_from_a_snapshot(live, svc, kanban_home):
    _other_creates(svc, "t_old")
    _round(kanban_home, svc)
    _other_creates(svc, "t_new")
    svc.compact(BOARD)
    svc.board(BOARD)["floor"] += 1  # our cursor is now below the floor
    result = _round(kanban_home, svc)
    assert result.error is None
    shadow = _shadow()
    try:
        ids = {r[0] for r in shadow.execute("SELECT id FROM tasks")}
        assert {"t_old", "t_new"} <= ids
        assert sync.sync_state(shadow)["cursor"] == svc.board(BOARD)["seq"]
    finally:
        shadow.close()


def test_primary_pull_keeps_fields_with_an_unanswered_local_edit(live, svc):
    tid = kb.create_task(live, title="local", assignee="worker")
    ksc.push(live, svc.hub(ME), BOARD, ME, primary=False)
    sync.set_mode(live, "primary")
    with kbc.write_txn(live):
        live.execute("UPDATE tasks SET title = 'local edit' WHERE id = ?", (tid,))
    svc.hub(OTHER).ops(BOARD, [{"client_op_id": "other-title-op1", "op": "upsert", "table": "tasks", "uid": tid,
                                "hlc": f"{time.time_ns() // 1_000_000 + 1000:013d}-0000-{OTHER}",
                                "fields": {"title": "remote edit", "priority": 5}}])
    ksc.pull(live, svc.hub(ME), BOARD, observers=False)
    t = kb.get_task(live, tid)
    assert t.title == "local edit" and t.priority == 5


def test_primary_pull_mirrors_the_lease_from_the_task_row(live, svc):
    sync.set_mode(live, "primary")
    _other_creates(svc, "t_leased")
    b = svc.board(BOARD)
    row = b["rows"]["tasks"]["t_leased"]
    svc._put(b, "tasks", "t_leased", {"lease_holder": OTHER, "lease_expires": 123}, OTHER, None, row["hlc"])
    ksc.pull(live, svc.hub(ME), BOARD, observers=False)
    r = live.execute("SELECT lease_holder, lease_expires, created_on FROM tasks WHERE id = 't_leased'").fetchone()
    assert (r["lease_holder"], r["lease_expires"], r["created_on"]) == (OTHER, 123, OTHER)


# --- import -----------------------------------------------------------------------------------------------------

def test_import_queues_every_row_once_and_is_safe_to_rerun(live, svc, kanban_home):
    with kbc.write_txn(live):
        sync.drop_triggers(live)  # rows written before sync was switched on
    a = kb.create_task(live, title="old a", assignee="worker")
    b = kb.create_task(live, title="old b", assignee="worker", parents=[a])
    kb.add_comment(live, a, "me", "history")
    sync.install_triggers(live)
    with kbc.write_txn(live):
        for table in sync._UID_TABLES:
            live.execute(f"UPDATE {table} SET uid = lower(hex(randomblob(8))) WHERE uid IS NULL")
    counts = ksc.import_board(live)
    assert counts["tasks"] == 2 and counts["task_links"] == 1 and counts["task_comments"] == 1
    assert ksc.push(live, svc.hub(ME), BOARD, ME, primary=False).rejected == 0
    ksc.import_board(live)
    assert ksc.push(live, svc.hub(ME), BOARD, ME, primary=False).rejected == 0  # a re-import is accepted
    rows = svc.board(BOARD)["rows"]
    assert set(rows["tasks"]) == {a, b} and f"{a}>{b}" in rows["task_links"]


def test_import_of_an_id_another_gateway_created_is_rejected_and_shows_in_the_diff(live, svc, kanban_home):
    tid = kb.create_task(live, title="clash", assignee="worker")
    with kbc.write_txn(live):
        live.execute("DELETE FROM kanban_sync_outbox")
    _other_creates(svc, tid)
    ksc.import_board(live, tables=("tasks",))
    assert ksc.push(live, svc.hub(ME), BOARD, ME, primary=False).rejected == 1
    _round(kanban_home, svc)
    shadow = _shadow()
    try:
        report = ksc.diff(live, shadow, ME)
    finally:
        shadow.close()
    assert not report["clean"] and report["rejected_ops"] == 1


# --- diff -------------------------------------------------------------------------------------------------------

def test_diff_is_clean_after_sync_and_reports_an_injected_mismatch(live, svc, kanban_home):
    tid = kb.create_task(live, title="same", assignee="worker")
    kb.add_comment(live, tid, "me", "c")
    _other_creates(svc, "t_foreign")
    _round(kanban_home, svc)
    _round(kanban_home, svc)
    shadow = _shadow()
    try:
        report = ksc.diff(live, shadow, ME)
        assert report["clean"], report
        assert report["checked"] == 1 and report["foreign_cards"] == 1
        with kbc.write_txn(shadow):
            shadow.execute("UPDATE kanban_sync_state SET applying = 1")
            shadow.execute("UPDATE tasks SET title = 'drifted' WHERE id = ?", (tid,))
            shadow.execute("UPDATE kanban_sync_state SET applying = 0")
        report = ksc.diff(live, shadow, ME)
    finally:
        shadow.close()
    assert not report["clean"] and report["field_mismatch"] == {tid: ["title"]}


def test_diff_reports_unpushed_rows_as_pending_not_as_differences(live, svc, kanban_home):
    _round(kanban_home, svc)
    kb.create_task(live, title="not yet pushed", assignee="worker")
    shadow = _shadow()
    try:
        report = ksc.diff(live, shadow, ME)
    finally:
        shadow.close()
    assert report["clean"] and report["pending"] == 1


# --- leases -----------------------------------------------------------------------------------------------------

def test_hub_lease_client_acquire_renew_release(svc):
    svc.board(BOARD)
    client = ksc.HubLeaseClient(svc.hub(ME))
    got = client.acquire(board_id=BOARD, task_uid="t_l", run_uid="run-me-0001", gateway_id=ME, ttl_s=60, lane="review")
    assert got.granted and got.holder == ME and got.expires_ms
    assert client.renew(board_id=BOARD, task_uid="t_l", run_uid="run-me-0001", gateway_id=ME, ttl_s=60).granted
    other = ksc.HubLeaseClient(svc.hub(OTHER)).acquire(board_id=BOARD, task_uid="t_l", run_uid="run-ot-0001",
                                                        gateway_id=OTHER, ttl_s=60)
    assert not other.granted and other.holder == ME and other.reason == "lease_held"
    client.release(board_id=BOARD, task_uid="t_l", run_uid="run-me-0001", gateway_id=ME)
    assert "t_l" not in svc.board(BOARD)["leases"]
    svc.offline = True
    with pytest.raises(sync.LeaseUnavailable):
        client.renew(board_id=BOARD, task_uid="t_l", run_uid="run-me-0001", gateway_id=ME, ttl_s=60)


# --- config -----------------------------------------------------------------------------------------------------

def test_only_listed_boards_capture(kanban_home):
    (kanban_home / "config.yaml").write_text("kanban:\n  sync:\n    enabled: true\n    boards: [other]\n")
    conn = kbc.connect(kanban_home / "kanban.db")
    try:
        kb.create_task(conn, title="not synced", assignee="worker")
        assert sync.board_mode(conn) == "off"
        assert not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'kanban_sync_%'").fetchone()
    finally:
        conn.close()


def test_identity_comes_from_the_hub_enrollment_files(tmp_path, monkeypatch):
    monkeypatch.setattr(ksc.Path, "home", lambda: tmp_path)
    mail = tmp_path / ".config" / "hermes-mail"
    mail.mkdir(parents=True)
    (mail / "config.json").write_text(json.dumps({"url": "https://hub.example", "gateway": "mini"}))
    (mail / "gateway.key").write_text("secret\n")
    ident = ksc.load_identity({})
    assert (ident.url, ident.gateway, ident.key) == ("https://hub.example", "mini", "secret")
    assert ksc.load_identity({"url": "https://other/", "gateway": "x"}).url == "https://other"
    (mail / "gateway.key").unlink()
    with pytest.raises(ksc.SyncUnavailable):
        ksc.load_identity({})


# --- entry paths: CLI and gateway watcher -------------------------------------------------------------------------

def _cli(*argv: str) -> int:
    import argparse
    from hermes_cli import kanban as kc
    parser = argparse.ArgumentParser()
    kc.build_parser(parser.add_subparsers(dest="cmd"))
    return kc.kanban_command(parser.parse_args(["kanban", *argv]))


def test_cli_attach_import_run_diff_status(kanban_home, svc, monkeypatch, capsys):
    support.write_sync_config(kanban_home, True)
    conn = kbc.connect(kanban_home / "kanban.db")
    tid = kb.create_task(conn, title="cli card", assignee="worker")
    conn.close()
    hub = svc.hub(ME)
    monkeypatch.setattr(ksc, "load_identity", lambda cfg=None: hub.ident)
    monkeypatch.setattr(ksc, "Hub", lambda ident, **kw: hub)
    assert _cli("sync", "attach", "--mode", "shadow") == 0
    assert _cli("sync", "import") == 0
    assert _cli("sync", "run") == 0
    assert _cli("sync", "run") == 0  # second round pulls our own rows back into the shadow
    assert svc.board(BOARD)["rows"]["tasks"][tid]["created_on"] == ME
    assert _cli("sync", "diff") == 0
    capsys.readouterr()
    assert _cli("sync", "status", "--json") == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "shadow" and out["board_id"] == BOARD and out["gateway"] == ME and out["pending"] == 0


def test_cli_attach_refuses_when_sync_is_off(kanban_home, capsys):
    support.write_sync_config(kanban_home, False)
    assert _cli("sync", "attach") == 2
    assert "enable it first" in capsys.readouterr().out


def test_gateway_watcher_runs_the_loop_once_per_host_and_not_when_disabled(kanban_home, monkeypatch):
    import asyncio
    from gateway import kanban_watchers as watchers
    calls = []

    def fake_run_forever(keep_running, interval=None, sleep=None):
        calls.append(keep_running())
    monkeypatch.setattr(ksc, "run_forever", fake_run_forever)
    runner = watchers.GatewayKanbanWatchersMixin()
    runner._running = True
    support.write_sync_config(kanban_home, False)
    asyncio.run(runner._kanban_sync_watcher())
    assert calls == []
    support.write_sync_config(kanban_home, True)
    asyncio.run(runner._kanban_sync_watcher())
    assert calls == [True]
    from gateway.kanban_watchers_common import _acquire_singleton_lock, _release_singleton_lock
    held, state = _acquire_singleton_lock(kb.kanban_home() / "kanban" / ".sync.lock")
    waits = []

    async def other_gateway_exits(interval):  # the retry sleep: the holder goes away meanwhile
        waits.append(calls[:])
        _release_singleton_lock(held)
    runner._sleep_between_ticks = other_gateway_exits
    try:
        assert state == "held"
        # Another gateway on this host holds it: this one waits instead of giving up for its lifetime,
        # and syncs once the holder lets go.
        asyncio.run(runner._kanban_sync_watcher())
        assert waits == [[True]]
        assert calls == [True, True]
    finally:
        _release_singleton_lock(held)


def test_import_skips_events_older_than_the_service_retention(live):
    tid = kb.create_task(live, title="evented", assignee="worker")
    old = int(time.time()) - ksc.EVENT_IMPORT_WINDOW_S - 60
    with kbc.write_txn(live):
        live.execute("INSERT INTO task_events (task_id, kind, payload, created_at, uid) VALUES (?, 'ancient', '{}', ?, 'ev-old')",
                     (tid, old))
        live.execute("DELETE FROM kanban_sync_outbox")
    ksc.import_board(live, tables=("task_events",))
    uids = {r[0] for r in live.execute("SELECT row_uid FROM kanban_sync_outbox WHERE table_name = 'task_events'")}
    assert "ev-old" not in uids and uids  # the recent "created" event is still imported


def test_a_worker_under_another_profile_keeps_the_hosts_sync_on(kanban_home, monkeypatch):
    """The switch lives in the kanban root's config: a profile process (HERMES_HOME=<root>/profiles/x, its own
    config without the setting) must not see sync as off and drop the board's capture triggers."""
    support.write_sync_config(kanban_home, True)
    conn = kbc.connect(kanban_home / "kanban.db")
    conn.close()
    profile = kanban_home / "profiles" / "worker"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("model: {}\n")
    monkeypatch.setenv("HERMES_HOME", str(profile))
    monkeypatch.setattr(kbc, "_INITIALIZED_PATHS", set())
    assert sync.sync_enabled()
    conn = kbc.connect(kanban_home / "kanban.db")
    try:
        assert conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'kanban_sync_%'").fetchone()
    finally:
        conn.close()


# --- primary: the live board holds this gateway's cards; the replica holds the whole board ------------------------

def test_primary_keeps_other_gateways_cards_out_of_the_live_board_but_in_the_replica(live, svc, kanban_home):
    mine = kb.create_task(live, title="mine", assignee="worker")
    sync.set_mode(live, "primary")
    _other_creates(svc, "t_theirs")
    _round(kanban_home, svc)
    _round(kanban_home, svc)
    assert kb.get_task(live, "t_theirs") is None  # the dispatcher never sees another gateway's card
    replica = _shadow()
    try:
        assert {r[0] for r in replica.execute("SELECT id FROM tasks")} >= {mine, "t_theirs"}
    finally:
        replica.close()
    # Another device edits OUR card on the service: the edit lands on the live board.
    svc.hub(OTHER).ops(BOARD, [{"client_op_id": "other-edit-mine-1", "op": "upsert", "table": "tasks", "uid": mine,
                                "hlc": f"{time.time_ns() // 1_000_000 + 1000:013d}-0000-{OTHER}",
                                "fields": {"title": "renamed elsewhere"}}])
    _round(kanban_home, svc)
    assert kb.get_task(live, mine).title == "renamed elsewhere"
    assert live.execute("SELECT created_on FROM tasks WHERE id = ?", (mine,)).fetchone()[0] == ME


def test_own_partition_keeps_child_rows_of_own_cards_only(live):
    tid = kb.create_task(live, title="own", assignee="worker")
    ch = [
        sync.RemoteChange(1, "tasks", "t_new_mine", "upsert", {"created_on": ME, "title": "n"}, 1, "", ME),
        sync.RemoteChange(2, "task_comments", "c1", "upsert", {"task_id": "t_new_mine"}, 1, "", ME),
        sync.RemoteChange(3, "task_comments", "c2", "upsert", {"task_id": "t_foreign"}, 1, "", OTHER),
        sync.RemoteChange(4, "tasks", "t_foreign", "upsert", {"created_on": OTHER, "title": "f"}, 1, "", OTHER),
        sync.RemoteChange(5, "task_links", f"{tid}>t_new_mine", "upsert", {}, 1, "", ME),
        sync.RemoteChange(6, "task_links", f"{tid}>t_foreign", "upsert", {}, 1, "", OTHER),
    ]
    assert [c.seq for c in ksc.own_partition(live, ch, ME)] == [1, 2, 5]


def test_an_edit_of_a_card_the_service_never_saw_is_sent_as_the_full_row(live, svc):
    with kbc.write_txn(live):
        sync.drop_triggers(live)
    tid = kb.create_task(live, title="made before sync", assignee="worker")  # never captured, never imported
    sync.install_triggers(live)
    with kbc.write_txn(live):
        live.execute("UPDATE tasks SET priority = 7 WHERE id = ?", (tid,))  # captures only {"priority": 7}
    res = ksc.push(live, svc.hub(ME), BOARD, ME, primary=False)
    assert res.rejected == 0
    row = svc.board(BOARD)["rows"]["tasks"][tid]
    assert (row["title"], row["priority"], row["created_on"]) == ("made before sync", 7, ME)


def test_import_clears_rows_the_service_refused_before_it(live, svc):
    tid = kb.create_task(live, title="x", assignee="worker")
    with kbc.write_txn(live):
        live.execute("UPDATE kanban_sync_outbox SET state = 'rejected'")
    counts = ksc.import_board(live, tables=("tasks",))
    assert counts["cleared_rejected"] >= 1 and counts["tasks"] == 1
    assert not live.execute("SELECT 1 FROM kanban_sync_outbox WHERE state = 'rejected'").fetchone()
    assert ksc.push(live, svc.hub(ME), BOARD, ME, primary=False).rejected == 0
    assert tid in svc.board(BOARD)["rows"]["tasks"]


def test_sync_config_does_not_need_pyyaml(kanban_home, monkeypatch):
    """Some installs ship only ruamel (no PyYAML): the switch must still read as on."""
    import builtins
    real_import = builtins.__import__

    def no_pyyaml(name, *args, **kwargs):
        if name == "yaml":
            raise ModuleNotFoundError("No module named 'yaml'")
        return real_import(name, *args, **kwargs)
    support.write_sync_config(kanban_home, True)
    monkeypatch.setattr(builtins, "__import__", no_pyyaml)
    assert sync.sync_enabled()


def test_a_worker_process_builds_its_own_lease_client_from_the_hub_enrollment(kanban_home, monkeypatch):
    """Lease renewals happen in the worker (kanban_heartbeat), not the gateway: with sync on, the first lease
    call builds a hub client from this host's enrollment; without one it fails closed."""
    support.write_sync_config(kanban_home, True)
    previous = sync.set_lease_client(None)
    try:
        with pytest.raises(sync.LeaseUnavailable):
            sync._client()  # no enrollment files in this test home
        mail = kanban_home.parent / ".config" / "hermes-mail"
        mail.mkdir(parents=True)
        (mail / "config.json").write_text(json.dumps({"url": "https://hub.test", "gateway": ME}))
        (mail / "gateway.key").write_text("k\n")
        client = sync._client()
        assert isinstance(client, ksc.HubLeaseClient) and client.hub.ident.gateway == ME
    finally:
        sync.set_lease_client(previous)


def test_diff_counts_an_acknowledged_row_the_replica_has_not_pulled_yet_as_in_flight(live, svc, kanban_home):
    tid = kb.create_task(live, title="moving", assignee="worker")
    _round(kanban_home, svc)
    _round(kanban_home, svc)
    with kbc.write_txn(live):
        live.execute("UPDATE tasks SET status = 'blocked' WHERE id = ?", (tid,))
    ksc.push(live, svc.hub(ME), BOARD, ME, primary=False)  # acknowledged; the replica has not pulled it yet
    shadow = _shadow()
    try:
        report = ksc.diff(live, shadow, ME)
        assert report["clean"] and report["pending"] == 1, report
        ksc.pull(shadow, svc.hub(ME), BOARD, observers=False)
        report = ksc.diff(live, shadow, ME)
    finally:
        shadow.close()
    assert report["clean"] and report["pending"] == 0 and report["checked"] == 1


# --- capture gaps: trigger installs are atomic, skip when current, and heal missed rows --------------------------

def _reinstall(kanban_home):
    """A new process opening the board (its first connect runs install_or_remove)."""
    kbc._INITIALIZED_PATHS.discard(str((kanban_home / "kanban.db").resolve()))
    return kbc.connect(kanban_home / "kanban.db")


def test_a_new_process_leaves_matching_triggers_untouched(live, kanban_home, monkeypatch):
    drops = []
    monkeypatch.setattr(sync, "drop_triggers", lambda conn: drops.append(1))
    other = _reinstall(kanban_home)
    other.close()
    assert drops == []  # no drop/recreate window on an ordinary start


def test_rows_written_in_a_capture_gap_are_queued_on_the_next_install(live, kanban_home):
    tid = kb.create_task(live, title="gap", assignee="worker")
    with kbc.write_txn(live):
        live.execute("DELETE FROM kanban_sync_outbox")
        sync.drop_triggers(live)  # the gap: another process's write lands with no trigger
        live.execute("INSERT INTO task_runs (task_id, status, started_at) VALUES (?, 'running', 1)", (tid,))
        live.execute("INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, 'me', 'missed', 1)", (tid,))
    other = _reinstall(kanban_home)
    try:
        queued = {(r["table_name"], json.loads(r["fields"]).get("status") or json.loads(r["fields"]).get("body"))
                  for r in other.execute("SELECT table_name, fields FROM kanban_sync_outbox")}
        assert ("task_runs", "running") in queued and ("task_comments", "missed") in queued
        assert other.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' AND name LIKE 'kanban_sync_%'").fetchone()[0]
    finally:
        other.close()


def _gap_link(live, table, parent, child):
    with kbc.write_txn(live):
        live.execute("UPDATE kanban_sync_state SET applying = 1")
        if table == "task_links":
            uid = f"{parent}>{child}"
            live.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", (parent, child))
        else:
            uid = f"{child}:chat-created"
            live.execute("INSERT INTO task_chat_links "
                         "(uid, task_id, chat_gateway, chat_session, kind, created_at) "
                         "VALUES (?, ?, ?, 'session-gap', 'created', 1)", (uid, child, ME))
        live.execute("UPDATE kanban_sync_state SET applying = 0")
    return uid


@pytest.mark.parametrize("table", ["task_links", "task_chat_links"])
@pytest.mark.parametrize("mode", ["shadow", "primary"])
def test_missing_child_capture_gap_is_healed_by_a_sync_round(live, svc, kanban_home, caplog, table, mode):
    sync.set_mode(live, mode)
    parent = kb.create_task(live, title="parent", assignee="worker")
    child = kb.create_task(live, title="child", assignee="worker")
    _round(kanban_home, svc)
    _round(kanban_home, svc)
    uid = _gap_link(live, table, parent, child)
    assert not _pending(live)
    _round(kanban_home, svc)
    assert uid in svc.board(BOARD)["rows"].get(table, {})
    assert table in caplog.text and uid in caplog.text and "re-queued" in caplog.text
    settled = _round(kanban_home, svc)
    assert settled.push.sent == 0
    assert sum(table in r.message and uid in r.message and "re-queued" in r.message
               for r in caplog.records) == 1
    shadow = _shadow()
    try:
        assert ksc.diff(live, shadow, ME)["child_mismatch"] == {}
    finally:
        shadow.close()


@pytest.mark.parametrize("table", ["task_links", "task_chat_links"])
@pytest.mark.parametrize("mode", ["shadow", "primary"])
def test_missing_child_repair_does_not_duplicate_an_inflight_op(live, svc, kanban_home, monkeypatch, table, mode):
    sync.set_mode(live, mode)
    parent = kb.create_task(live, title="parent", assignee="worker")
    child = kb.create_task(live, title="child", assignee="worker")
    _round(kanban_home, svc)
    _round(kanban_home, svc)
    uid = _gap_link(live, table, parent, child)
    hub = svc.hub(ME)

    def no_ack(board, ops):
        return {"results": []}

    monkeypatch.setattr(hub, "ops", no_ack)
    for _ in range(2):
        ksc.sync_board(BOARD, kanban_home / "kanban.db", hub, ME)
    rows = [r for r in _pending(live) if r["table_name"] == table and r["row_uid"] == uid]
    assert len(rows) == 1
    assert json.loads(rows[0]["fields"])["child_id" if table == "task_links" else "task_id"] == child


@pytest.mark.parametrize("table", ["task_links", "task_chat_links"])
@pytest.mark.parametrize("guard", ["pending", "rejected", "local_tombstone", "remote_tombstone",
                                  "version_ahead", "replica_ahead", "unacknowledged", "foreign", "task_pending",
                                  "parent_version", "parent_unacknowledged", "parent_pending", "present", "none"])
def test_missing_child_repair_honors_existing_state(live, svc, kanban_home, table, guard):
    parent = kb.create_task(live, title="parent", assignee="worker")
    child = kb.create_task(live, title="child", assignee="worker")
    _round(kanban_home, svc)
    _round(kanban_home, svc)
    uid = _gap_link(live, table, parent, child)
    shadow = _shadow()
    try:
        subject = parent if table == "task_links" and guard.startswith("parent_") else child
        with kbc.write_txn(live), kbc.write_txn(shadow):
            live.execute("UPDATE kanban_sync_state SET applying = 1")
            if guard in ("pending", "rejected", "task_pending", "parent_pending"):
                live.execute("INSERT INTO kanban_sync_outbox "
                             "(op_id, table_name, row_uid, op, fields, hlc, created_at, state) "
                             "VALUES ('held-op', ?, ?, 'upsert', '{}', '', 1, ?)",
                             ("tasks" if guard in ("task_pending", "parent_pending") else table,
                              subject if guard in ("task_pending", "parent_pending") else uid,
                              "rejected" if guard == "rejected" else "pending"))
            elif guard.endswith("tombstone"):
                target = live if guard == "local_tombstone" else shadow
                if target is shadow:  # a replica tombstone is a pulled one: written under applying
                    shadow.execute("UPDATE kanban_sync_state SET applying = 1")
                target.execute("INSERT INTO kanban_sync_tombstones VALUES (?, ?, 1)", (table, uid))
                if target is shadow:
                    shadow.execute("UPDATE kanban_sync_state SET applying = 0")
            elif guard in ("version_ahead", "parent_version"):
                live.execute("UPDATE tasks SET sync_version = sync_version + 1 WHERE id = ?", (subject,))
            elif guard == "replica_ahead":
                shadow.execute("UPDATE tasks SET sync_version = sync_version + 1 WHERE id = ?", (child,))
            elif guard in ("unacknowledged", "parent_unacknowledged"):
                live.execute("UPDATE tasks SET sync_version = NULL WHERE id = ?", (subject,))
            elif guard == "foreign":
                shadow.execute("UPDATE tasks SET created_on = ? WHERE id = ?", (OTHER, child))
            live.execute("UPDATE kanban_sync_state SET applying = 0")
        if guard == "present":
            row = live.execute(f"SELECT * FROM {table}").fetchone()
            fields = ksc._row_fields(live, table, row)
            if table == "task_chat_links":
                fields["released"] = 1  # existing replica differs: never clobber it
            sync.apply_remote_changes(shadow, [sync.RemoteChange(
                1, table, uid, "upsert", fields, 1, "", ME)], new_cursor=1, board=BOARD)
        before = [dict(r) for r in live.execute("SELECT * FROM kanban_sync_outbox")]
        ksc.repair_missing_children(live, shadow, ME)
        after = [dict(r) for r in live.execute("SELECT * FROM kanban_sync_outbox")]
        if guard == "none":
            assert len(after) == 1 and after[0]["row_uid"] == uid
        else:
            assert after == before
    finally:
        shadow.close()


def _gap_wake(live, tid):
    """kanban_wake_check._wake landing in a capture gap: a raw status UPDATE plus an event, no trigger."""
    with kbc.write_txn(live):
        sync.drop_triggers(live)
        live.execute("UPDATE tasks SET status = 'ready' WHERE id = ? AND status = 'scheduled'", (tid,))
        live.execute("INSERT INTO task_events (task_id, kind, payload, created_at) "
                     "VALUES (?, 'wake_check_broken', '{}', 1)", (tid,))


def _service_task(svc, tid):
    return svc.board(BOARD)["rows"]["tasks"][tid]


@pytest.mark.parametrize("mode", ["shadow", "primary"])
def test_a_status_change_in_a_capture_gap_reaches_the_service_with_its_event(live, svc, kanban_home, mode):
    # Hub incident 2026-10-28 (t_01a180daf776c25e): the gap heal queued the uid-less event but never the tasks row,
    # so the service kept 'scheduled' and refused every lease as not_claimable while the board said 'ready'.
    sync.set_mode(live, mode)
    tid = kb.create_task(live, title="parked", assignee="worker")
    with kbc.write_txn(live):
        live.execute("UPDATE tasks SET status = 'scheduled' WHERE id = ?", (tid,))
    _round(kanban_home, svc)
    _round(kanban_home, svc)  # feed settled: board and replica agree on the version, as on the hub
    assert _service_task(svc, tid)["status"] == "scheduled"
    _gap_wake(live, tid)
    _reinstall(kanban_home).close()  # the next process heals the gap
    _round(kanban_home, svc)
    _round(kanban_home, svc)
    kinds = {c["fields"]["kind"] for c in svc.board(BOARD)["changes"]
             if c["table"] == "task_events" and c["fields"] and c["fields"].get("task_id") == tid}
    assert "wake_check_broken" in kinds
    assert _service_task(svc, tid)["status"] == "ready"
    shadow = _shadow()
    try:
        assert ksc.diff(live, shadow, ME)["field_mismatch"] == {}
    finally:
        shadow.close()


@pytest.mark.parametrize("mode", ["shadow", "primary"])
def test_divergence_repair_leaves_an_unpulled_remote_edit_alone(live, svc, kanban_home, mode):
    sync.set_mode(live, mode)
    tid = kb.create_task(live, title="mine", assignee="worker")
    _round(kanban_home, svc)
    other = svc.hub(OTHER)
    other.ops(BOARD, [{"client_op_id": "o-1", "op": "upsert", "table": "tasks", "uid": tid,
                       "hlc": f"{int(time.time() * 1000) + 60_000:013d}-0000-{OTHER}", "fields": {"title": "renamed"}}])
    _round(kanban_home, svc)  # the replica pulls the remote edit; this board never wrote it
    shadow = _shadow()
    try:
        assert ksc.repair_divergence(live, shadow, ME) == {}  # versions differ: an edit not applied here, not a gap
    finally:
        shadow.close()
    _round(kanban_home, svc)
    assert _service_task(svc, tid)["title"] == "renamed"
    assert not _pending(live)


def test_rows_from_before_the_first_attach_are_left_to_import(kanban_home):
    support.write_sync_config(kanban_home, True)
    conn = kbc.connect(kanban_home / "kanban.db")  # never attached: board_id is local:…
    try:
        tid = kb.create_task(conn, title="pre", assignee="worker")
        with kbc.write_txn(conn):
            conn.execute("DELETE FROM kanban_sync_outbox")
            sync.drop_triggers(conn)
            conn.execute("INSERT INTO task_comments (task_id, author, body, created_at) VALUES (?, 'me', 'old', 1)", (tid,))
    finally:
        conn.close()
    other = _reinstall(kanban_home)
    try:
        assert other.execute("SELECT COUNT(*) FROM kanban_sync_outbox").fetchone()[0] == 0
        assert other.execute("SELECT uid FROM task_comments WHERE body = 'old'").fetchone()[0]
    finally:
        other.close()


# --- push channel: a Board change frame or a local write starts a round at once ---------------------------------

def test_feed_watcher_wakes_the_loop_on_a_change_frame_and_keeps_the_socket_alive():
    import threading
    pytest.importorskip("websockets")
    from websockets.sync.server import serve
    seen = {"auth": None, "path": None, "pings": 0}
    stop = threading.Event()

    def handler(ws):
        seen["auth"] = ws.request.headers.get("authorization")
        seen["path"] = ws.request.path
        ws.send(json.dumps({"type": "hello", "head": 1}))
        ws.send(json.dumps({"type": "changes", "changes": [], "cursor": 2}))
        while not stop.is_set():
            try:
                if ws.recv(timeout=0.2) == "ping":
                    seen["pings"] += 1
            except TimeoutError:
                continue
            except Exception:
                return

    with serve(handler, "127.0.0.1", 0) as server:
        port = server.socket.getsockname()[1]
        threading.Thread(target=server.serve_forever, daemon=True).start()
        wake = threading.Event()
        ident = ksc.Identity(url=f"http://127.0.0.1:{port}", key="gw-key", gateway=ME)
        changed_boards = []
        watcher = ksc.FeedWatcher(ident, BOARD, wake, lambda: not stop.is_set(), ping_s=0.2,
                                  on_change=changed_boards.append)
        t = threading.Thread(target=watcher.run, daemon=True)
        t.start()
        try:
            assert wake.wait(timeout=5), "a change frame must wake the sync loop"
            deadline = time.monotonic() + 5
            while seen["pings"] == 0 and time.monotonic() < deadline:
                time.sleep(0.05)
            assert seen["auth"] == "Bearer gw-key" and seen["path"] == f"/v2/boards/{BOARD}/ws"
            assert seen["pings"] >= 1 and watcher.frames == 1  # hello is not a change
            assert changed_boards == [BOARD, BOARD]  # connection recovery, then the change frame
        finally:
            stop.set()
            server.shutdown()
            t.join(timeout=5)


def test_wait_for_work_returns_on_a_remote_change_a_local_write_or_the_period(live, kanban_home):
    import threading
    path = kanban_home / "kanban.db"
    with kbc.write_txn(live):
        live.execute("DELETE FROM kanban_sync_outbox")
    wake = threading.Event()
    started = time.monotonic()
    assert ksc.wait_for_work(wake, [path], period=0.3, keep_running=lambda: True, tick=0.05) == "period"
    assert time.monotonic() - started >= 0.29
    wake.set()
    started = time.monotonic()
    assert ksc.wait_for_work(wake, [path], period=10, keep_running=lambda: True) == "remote"
    assert time.monotonic() - started < 0.2 and not wake.is_set()
    kb.create_task(live, title="local write", assignee="worker")  # captured into the outbox
    started = time.monotonic()
    assert ksc.wait_for_work(wake, [path], period=10, keep_running=lambda: True, tick=0.05) == "local"
    assert time.monotonic() - started < 0.5


def test_transient_rejections_retry_next_round_and_give_up_after_a_cap(live, svc, monkeypatch):
    tid = kb.create_task(live, title="during a deploy", assignee="worker")
    real = svc.hub(ME).ops
    calls = {"n": 0}

    remembered = set()

    def rolling_out(board, ops):  # the service has not picked up the new code yet
        calls["n"] += 1
        remembered.update(o["client_op_id"] for o in ops)
        return {"results": [{"client_op_id": o["client_op_id"], "status": "rejected", "reason": "invalid",
                             "detail": f"unknown table {o['table']}"} for o in ops], "head": 0}
    hub = svc.hub(ME)
    hub.ops = rolling_out
    res = ksc.push(live, hub, BOARD, ME, primary=False)
    assert res.rejected == 0 and res.deferred >= 1 and calls["n"] == 1  # one batch, no hot loop
    assert not live.execute("SELECT 1 FROM kanban_sync_outbox WHERE state = 'rejected'").fetchone()
    hub.ops = real  # the rollout finished
    assert ksc.push(live, hub, BOARD, ME, primary=False).sent == 0  # backing off: not retried at once
    with kbc.write_txn(live):
        live.execute("UPDATE kanban_sync_outbox SET retry_after = 0")  # the backoff elapsed
    # The retry goes under a fresh op id: the service would replay its remembered rejection for the old one.
    assert not ({r[0] for r in live.execute("SELECT op_id FROM kanban_sync_outbox")} & remembered)
    assert ksc.push(live, hub, BOARD, ME, primary=False).rejected == 0
    assert tid in svc.board(BOARD)["rows"]["tasks"] and not _pending(live)
    # A rejection that never clears ends up rejected after the cap.
    kb.create_task(live, title="old service forever", assignee="worker")
    hub.ops = rolling_out
    for _ in range(ksc.MAX_TRANSIENT_ATTEMPTS + 1):
        ksc.push(live, hub, BOARD, ME, primary=False)
        with kbc.write_txn(live):
            live.execute("UPDATE kanban_sync_outbox SET retry_after = 0")
    assert live.execute("SELECT COUNT(*) FROM kanban_sync_outbox WHERE state = 'rejected'").fetchone()[0] >= 1


def test_transient_backoff_doubles_to_five_minutes():
    assert [ksc._backoff_s(a) for a in (0, 1, 2, 7, 8, 30)] == [2, 4, 8, 256, 300, 300]


def test_a_child_row_update_is_sent_whole_so_it_stands_alone(live, svc):
    tid = kb.create_task(live, title="parent", assignee="worker")
    ksc.push(live, svc.hub(ME), BOARD, ME, primary=False)
    kb.add_comment(live, tid, "me", "first")
    with kbc.write_txn(live):
        live.execute("DELETE FROM kanban_sync_outbox")  # the insert was lost (refused during a rollout)
        live.execute("UPDATE task_comments SET body = 'edited' WHERE task_id = ?", (tid,))  # captures {"body": ...}
    assert ksc.push(live, svc.hub(ME), BOARD, ME, primary=False).rejected == 0
    comment = next(c for c in svc.board(BOARD)["rows"]["task_comments"].values() if c["task_id"] == tid)
    assert comment["body"] == "edited" and comment["author"] == "me"


def test_a_child_queued_before_its_card_does_not_hold_the_card_back(live, svc):
    with kbc.write_txn(live):
        sync.drop_triggers(live)
    tid = kb.create_task(live, title="made before attach", assignee="worker")  # not captured, never acknowledged
    sync.install_triggers(live)
    kb.add_comment(live, tid, "me", "captured first")  # a child queued before any row of its card
    res = ksc.push(live, svc.hub(ME), BOARD, ME, primary=False)
    assert res.rejected == 0 and res.deferred == 0
    assert svc.board(BOARD)["rows"]["tasks"][tid]["title"] == "made before attach"
    assert live.execute("SELECT sync_version FROM tasks WHERE id = ?", (tid,)).fetchone()[0] is not None
    assert any(c["body"] == "captured first" for c in svc.board(BOARD)["rows"]["task_comments"].values())


def test_board_discovery_skips_the_archive_folder(kanban_home):
    support.write_sync_config(kanban_home, True)
    for slug in ("_archived", "runners"):
        d = kanban_home / "kanban" / "boards" / slug
        d.mkdir(parents=True, exist_ok=True)
        sqlite3.connect(d / "kanban.db").close()
    assert [s for s, _ in ksc.board_paths()] == ["runners"]


def test_board_discovery_reads_the_sync_config_once(kanban_home, monkeypatch):
    """[daveey] Every Desktop session-list poll enumerates the replicas (``kanban_session_activity._replica_paths``):
    one ``kanban.sync`` read per board re-read the kanban config 8x per poll on David-MBP (t_01a19d0bcab45558)."""
    support.write_sync_config(kanban_home, True)
    for slug in ("alpha", "beta", "gamma"):
        d = kanban_home / "kanban" / "boards" / slug
        d.mkdir(parents=True, exist_ok=True)
        (d / "board.json").write_text("{}", encoding="utf-8")
        sqlite3.connect(d / "kanban.db").close()
    reads = []
    real = sync.sync_config
    monkeypatch.setattr(sync, "sync_config", lambda: reads.append(1) or real())
    assert [s for s, _ in ksc.board_paths()] == ["alpha", "beta", "gamma"]
    assert len(reads) == 1
    monkeypatch.setattr(sync, "sync_config", lambda: {**real(), "boards": ["beta"]})
    assert [s for s, _ in ksc.board_paths()] == ["beta"]


def test_hub_lease_client_release_marks_an_unused_grant_only_when_asked(svc, monkeypatch):
    """Wire contract with the Board (review F2): ``unused_grant: true`` only for a grant released before any local
    start; an ordinary release sends no new field (old Boards ignore unknown lease fields either way)."""
    svc.board(BOARD)
    client = ksc.HubLeaseClient(svc.hub(ME))
    bodies = []
    real = client.hub.leases
    monkeypatch.setattr(client.hub, "leases", lambda board, body: bodies.append(body) or real(board, body))
    for run, unused in (("run-me-0002", True), ("run-me-0003", False)):
        assert client.acquire(board_id=BOARD, task_uid="t_u", run_uid=run, gateway_id=ME, ttl_s=60).granted
        client.release(board_id=BOARD, task_uid="t_u", run_uid=run, gateway_id=ME, unused_grant=unused)
        assert "t_u" not in svc.board(BOARD)["leases"]
    releases = [b for b in bodies if b["action"] == "release"]
    assert releases[0]["unused_grant"] is True and "unused_grant" not in releases[1]
