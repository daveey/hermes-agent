"""The origin acknowledges a durable obligation, never a replaceable replica."""
import json
import sqlite3

import pytest

from hermes_cli import kanban_origin_ledger as ledger


@pytest.fixture
def world(tmp_path):
    replica = tmp_path / "hub.db"
    with sqlite3.connect(replica) as db:
        db.execute("CREATE TABLE replica (gw TEXT, coll TEXT, key TEXT, version INTEGER, data TEXT)")
    binding = {
        "binding_id": "binding-0001", "version": 1,
        "owner": {"gateway": "owner", "profile": "work", "board": "build", "task_id": "t_one"},
        "origin": {"gateway": "origin", "profile": "default", "session": "s_old"},
        "task_record_key": "work/build/t_one", "publication_version": 3,
    }
    return tmp_path / "origin-work.db", replica, binding


def publish(replica, binding, **changes):
    task = {**binding["owner"], "id": binding["owner"]["task_id"], "status": "running",
            "origin_bindings": [binding], **changes}
    with sqlite3.connect(replica) as db:
        db.execute("DELETE FROM replica")
        db.execute("INSERT INTO replica VALUES (?, 'tasks', ?, ?, ?)",
                   (binding["owner"]["gateway"], binding["task_record_key"], 3, json.dumps(task)))


def accept(world, **changes):
    path, replica, binding = world
    args = dict(path=path, replica=replica, binding=binding, request_id="request-0001",
                sender="gw:owner", gateway="origin", profile="default",
                resolve_session=lambda sid: "s_current")
    args.update(changes)
    return ledger.accept_binding(**args)


@pytest.mark.parametrize("change", [
    {"sender": "gw:impostor"}, {"gateway": "elsewhere"}, {"profile": "work"},
])
def test_wrong_authenticated_route_cannot_accept_or_replay(world, change):
    path, replica, binding = world
    publish(replica, binding)
    with pytest.raises(ValueError, match="origin_route_mismatch"):
        accept(world, **change)
    assert ledger.receipt(path, "request-0001") is None
    accept(world)
    with pytest.raises(ValueError, match="origin_route_mismatch"):
        accept(world, **change)


@pytest.mark.parametrize("field,value", [
    ("version", 0), ("version", True), ("publication_version", -1),
    ("publication_version", "3"), ("binding_id", ""),
    ("task_record_key", "default/build/t_one"),
    ("owner", {"gateway": "owner", "board": "build", "task_id": "t_one"}),
    ("origin", {"gateway": "origin", "profile": "../work", "session": "s_old"}),
])
def test_invalid_binding_is_rejected_before_storage(world, field, value):
    from copy import deepcopy
    binding = deepcopy(world[2])
    binding[field] = value
    publish(world[1], binding)
    with pytest.raises(ValueError, match="origin_binding_invalid"):
        accept(world, binding=binding)
    assert not world[0].exists()


def test_replay_conflict_and_concurrent_accept_are_atomic(world):
    from concurrent.futures import ThreadPoolExecutor
    from copy import deepcopy
    path, replica, binding = world
    publish(replica, binding)
    with ThreadPoolExecutor(max_workers=4) as pool:
        replies = list(pool.map(lambda _: accept(world), range(8)))
    assert all(r == replies[0] for r in replies)
    altered = deepcopy(binding)
    altered["origin"]["session"] = "s_other"
    with pytest.raises(ValueError, match="origin_binding_conflict"):
        accept(world, binding=altered)
    # New request ID for the same binding is idempotent, not a duplicate obligation.
    assert accept(world, request_id="request-0002")["binding_hash"] == replies[0]["binding_hash"]
    altered["binding_id"] = binding["binding_id"]
    publish(replica, altered)
    with pytest.raises(ValueError, match="origin_binding_conflict"):
        accept(world, binding=altered, request_id="request-0003")
    assert ledger.receipt(path, "request-0003") is None
    assert len(ledger.obligations(path, gateway="origin", profile="default", sessions=["s_old"])) == 1


def test_receipt_cannot_outlive_its_obligation(world):
    path, replica, binding = world
    publish(replica, binding)
    accept(world)
    with sqlite3.connect(path) as db:
        db.execute("DELETE FROM origin_obligations")
    with pytest.raises(ValueError, match="origin_obligation_missing"):
        accept(world)


@pytest.mark.parametrize("change", [{"profile": "other"}, {"id": "t_other"}, {"board": "other"}])
def test_exact_task_identity_must_match_even_with_binding_marker(world, change):
    publish(world[1], world[2], **change)
    with pytest.raises(ValueError, match="origin_publication_mismatch"):
        accept(world)
    assert ledger.receipt(world[0], "request-0001") is None


def test_missing_session_has_no_receipt(world):
    publish(world[1], world[2])
    with pytest.raises(ValueError, match="origin_session_missing"):
        accept(world, resolve_session=lambda sid: None)
    assert ledger.receipt(world[0], "request-0001") is None


@pytest.mark.parametrize("request_id", [None, "", 123, "x" * 129])
def test_bad_request_id_cannot_write(world, request_id):
    publish(world[1], world[2])
    with pytest.raises(ValueError, match="origin_request_invalid"):
        accept(world, request_id=request_id)
    assert not world[0].exists()


def test_duplicate_binding_receipt_describes_persisted_projection(world):
    publish(world[1], world[2])
    first = accept(world)
    second = accept(world, request_id="request-0002", resolve_session=lambda sid: "s_new")
    assert second["resolved_session"] == first["resolved_session"]
    assert len(ledger.obligations(world[0], gateway="origin", profile="default",
                                  sessions=[second["resolved_session"]])) == 1


def test_different_owners_can_reuse_binding_id(world):
    from copy import deepcopy
    publish(world[1], world[2])
    accept(world)
    other = deepcopy(world[2])
    other["owner"]["gateway"] = "other"
    publish(world[1], other)
    accept(world, binding=other, sender="gw:other", request_id="request-0002")
    assert len(ledger.obligations(world[0], gateway="origin", profile="default", sessions=["s_old"])) == 2


def test_publication_marker_does_not_need_future_server_version(world):
    _path, replica, binding = world
    marker = {k: v for k, v in binding.items() if k != "publication_version"}
    publish(replica, binding, origin_bindings=[marker])
    assert accept(world)["status"] == "accepted"


def test_receipt_requires_publication_and_survives_replica_loss(world):
    path, replica, binding = world
    assert accept(world) == {"status": "pending", "code": "origin_publication_pending"}
    publish(replica, binding)
    receipt = accept(world)
    assert receipt["status"] == "accepted"
    assert receipt["binding_hash"] == ledger.binding_hash(binding)
    replica.unlink()
    # A different invocation/process reads durable acknowledgement and obligation.
    assert accept(world) == receipt
    rows = ledger.obligations(path, gateway="origin", profile="default", sessions=["s_current"])
    assert len(rows) == 1
    assert rows[0]["owner"] == binding["owner"]
    assert rows[0]["unfinished"] is True
    assert ledger.receipt(path, "request-0001") == receipt


# -- the owner publishes its park (Desktop: "a producer the owner did not publish") [daveey] ----------------

@pytest.fixture
def owner_board(tmp_path, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    kb.init_db()
    conn = connect()
    try:
        yield kb, conn
    finally:
        conn.close()
        from hermes_cli import kanban_wake_check as kwc
        kwc._shutdown_for_tests()


def _observe(conn, tid):
    import time
    status, block_kind = conn.execute("SELECT status, block_kind FROM tasks WHERE id=?", (tid,)).fetchone()
    conn.execute("BEGIN")
    try:  # the collector's row carries block_kind (hermes_hub.collect_tasks)
        return ledger.observe_owner_task(conn, dict(id=tid, status=status, block_kind=block_kind, gateway="hub",
                                                    profile="default", board="default"), now=time.time())
    finally:
        conn.rollback()


def _parks(kb, conn):
    owner = kb.create_task(conn, title="builds the image", assignee="default")
    tids = {k: kb.create_task(conn, title=k, assignee="default") for k in ("card", "ci", "probe", "clock")}
    assert kb.schedule_task(conn, tids["card"], reason="the image", clock_only=True,
                            awaiting={"kind": "card", "ref": owner, "why": "it builds the image"})
    assert kb.schedule_task(conn, tids["ci"], reason="Waiting for CI run 40443505012 on PR92.",
                            wake_check="gh run view 40443505012 --json status | grep -q completed",
                            awaiting={"kind": "ci", "ref": "40443505012", "why": "in progress"})
    assert kb.schedule_task(conn, tids["probe"], reason="released when the flag appears",
                            wake_check="python3 check_flag.py")
    assert kb.schedule_task(conn, tids["clock"], reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z quota reset",
                            clock_only=True, awaiting={"kind": "clock", "why": "weekly quota resets"})
    return owner, tids


def test_scheduled_owner_record_publishes_its_park(owner_board):
    kb, conn = owner_board
    owner, tids = _parks(kb, conn)
    rows = {k: _observe(conn, tid) for k, tid in tids.items()}
    for kind, row in rows.items():
        assert ledger._owner_evidence(row), kind  # the hash-bound observation covers the published fields
        park = row["park"]
        assert set(park) == {"wake_at", "reason", "ended_at", "outcome"} and park["outcome"] is None, park
        assert isinstance(park["wake_at"], int) and park["wake_at"] > park["ended_at"], park
        assert row["awaiting"]["kind"] == {"card": "card", "ci": "ci", "probe": "external", "clock": "clock"}[kind]
    assert rows["card"]["awaiting"]["ref"] == owner
    assert rows["ci"]["wake_check"].startswith("gh run view 40443505012")
    assert rows["probe"]["wake_check"] == "python3 check_flag.py" and rows["probe"]["awaiting"]["inferred"] is True
    assert "wake_check" not in rows["clock"] and rows["clock"]["awaiting"]["why"] == "weekly quota resets"
    # SCHEDULED_UNTIL far out is clamped to the 14-day park maximum (kanban_timed_park.scheduled_until).
    assert rows["clock"]["park"]["wake_at"] - rows["clock"]["park"]["ended_at"] == 14 * 24 * 3600
    assert rows["clock"] == _observe(conn, tids["clock"])  # an unchanged park publishes an identical record


def test_published_park_labels_the_remote_wait(owner_board):
    """``describe_replica`` on the owner record, directly and through both remote-card projections (the hub-sync
    replica rows and the origin-ledger rows), names the producer; never "a producer the owner did not publish"."""
    from hermes_cli import kanban_replica_projection as proj
    from hermes_cli.kanban_wait_info import describe_replica
    kb, conn = owner_board
    owner, tids = _parks(kb, conn)
    want = {"card": f"card {owner}", "ci": "ci 40443505012", "probe": "probe check_flag.py",
            "clock": "the clock until "}
    for kind, tid in tids.items():
        row = _observe(conn, tid)
        (_, card), = proj.card_rows(("hub", "default", "default", tid), row, sids=("s1",), closed=False, synced=None,
                                    stale=False, me="mac", origin_profile="default", legacy_record=None,
                                    historical={})
        assert card["park"] == row["park"]  # the published park, not the last run's fields
        for shown in (row, card):
            wait = describe_replica(shown)
            assert wait["waiting_on"].startswith(want[kind]), (kind, wait)
            assert "did not publish" not in wait["waiting_on"]
        assert ledger._park_of(row) == row["park"]
    clock = describe_replica(_observe(conn, tids["clock"]))
    assert clock["agent_owned"] is False and "weekly quota resets" in clock["waiting_on"]


def test_unparked_owner_record_publishes_no_park(owner_board):
    kb, conn = owner_board
    tid = kb.create_task(conn, title="ready card", assignee="default")
    row = _observe(conn, tid)
    assert row["status"] in ("ready", "todo")
    assert not {"park", "awaiting", "wake_check"} & set(row), row
    # A woken (unblocked) card: its spent park is no longer published.
    _, tids = _parks(kb, conn)
    assert kb.unblock_task(conn, tids["clock"])
    row = _observe(conn, tids["clock"])
    assert row["status"] != "scheduled" and not {"park", "awaiting", "wake_check"} & set(row), row


def test_blocked_time_owner_record_publishes_its_park(owner_board):
    """A legacy ``blocked`` kind=time card publishes the park that holds it; another blocked kind does not."""
    from hermes_cli.kanban_wait_replica import published_park
    kb, conn = owner_board
    _, tids = _parks(kb, conn)
    tid = tids["clock"]
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='blocked', block_kind='time' WHERE id=?", (tid,))
    row = _observe(conn, tid)
    assert row["park"] == published_park(conn, tid)["park"] and row["awaiting"]["kind"] == "clock"
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET block_kind='transient' WHERE id=?", (tid,))
    assert not {"park", "awaiting"} & set(_observe(conn, tid))
