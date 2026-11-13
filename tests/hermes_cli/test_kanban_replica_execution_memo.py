"""A heartbeat pulled into a trigger-backed Board replica reaches the remote card's observation, although it never
moves the replica's content key (review t_01a1d0f7c4cc4688, B1). [daveey]

Heartbeat events and the ``last_heartbeat_at`` columns are bookkeeping for ``kanban_activity_revision``: they do not
bump the shadow key, so the replica link-row memo and the counts fingerprint stay put (by design: heartbeats must not
re-read every board). The run terms (current run uid + newest liveness event) are therefore read outside that memo,
on every ``replica_cards`` call, with one bounded query over this gateway's linked running cards. Without that, a live
remote card froze on its first memoised beat and expired to unknown 180 s later on a quiet board.
"""
from __future__ import annotations

import sqlite3
import time

import pytest

from hermes_cli import kanban_activity_revision as rev
from hermes_cli import kanban_chat_links as links
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_session_activity as ksa
from hermes_cli import kanban_sync_client as ksc
from tests.hermes_cli import kanban_sync_support as support

ME, OWNER = "macbook", "hub"


@pytest.fixture
def shadow(tmp_path, monkeypatch):
    from hermes_cli import profiles
    home = support.kanban_home(tmp_path, monkeypatch)
    (home / "config.yaml").write_text("kanban:\n  sync:\n    enabled: true\n    links_reader: replica\n")
    local = kbc.connect(home / "kanban.db")
    ksc.ensure_identity(local, "default", ME)
    local.close()
    path = home / "kanban" / sync.SHADOW_DIR / "default.db"
    monkeypatch.setattr(ksc, "load_identity", lambda cfg=None: ksc.Identity("https://hub.test", "k", ME))
    monkeypatch.setattr(ksc, "board_paths", lambda: [("default", home / "kanban.db")])
    monkeypatch.setattr(ksc, "shadow_path", lambda slug: path)
    monkeypatch.setattr(profiles, "get_active_profile_name", lambda: "default")
    monkeypatch.setattr(ksa, "_remote_links", lambda stale_ok=False: [
        {"id": "t_hub", "board": "default", "gateway": OWNER, "profile": "work", "session": "s1", "released": False}])
    ksa._reset_cache_for_tests()
    links._reset_for_tests()
    conn = kbc.connect(path)  # real writer init: the revision triggers are installed
    now = time.time()
    with kbc.write_txn(conn):
        conn.execute("INSERT INTO tasks (id, title, status, created_at, created_on, current_run_id) "
                     "VALUES ('t_hub', 'hub card', 'running', 1, ?, 7)", (OWNER,))
        conn.execute("INSERT INTO task_runs (id, task_id, status, started_at, uid) "
                     "VALUES (7, 't_hub', 'running', 1, 'run-uid-7')")
        conn.execute("INSERT INTO task_events (task_id, run_id, kind, created_at, uid) "
                     "VALUES ('t_hub', 7, 'heartbeat', ?, 'ev1')", (int(now - 170),))
        conn.execute("INSERT INTO task_chat_links (uid, task_id, chat_gateway, chat_profile, chat_session, kind, "
                     "released, created_at) VALUES ('u1', 't_hub', ?, 'default', 's1', 'shepherd', 0, 1)", (ME,))
    sync.mark_pulled(conn, at=now)
    try:
        yield path, conn, now
    finally:
        conn.close()
        ksa._reset_cache_for_tests()
        links._reset_for_tests()


def _beat(conn, uid: str, at: float, *, run_id: int = 7) -> None:
    """What a pull applies for one executor heartbeat: the event plus the bookkeeping heartbeat columns only."""
    with kbc.write_txn(conn):
        conn.execute("INSERT INTO task_events (task_id, run_id, kind, created_at, uid) "
                     "VALUES ('t_hub', ?, 'heartbeat', ?, ?)", (run_id, int(at), uid))
        conn.execute("UPDATE tasks SET last_heartbeat_at = ? WHERE id = 't_hub'", (int(at),))
        conn.execute("UPDATE task_runs SET last_heartbeat_at = ? WHERE id = ?", (int(at), run_id))
    sync.mark_pulled(conn, at=at)


def test_new_heartbeat_reaches_the_memoised_rows(shadow, monkeypatch):
    path, conn, now = shadow
    reads = []
    real = links._read_replica_links
    monkeypatch.setattr(links, "_read_replica_links", lambda *a: reads.append(1) or real(*a))
    (first,) = links.replica_cards(["s1"], ME, replicas=[("default", path)], now=now)
    assert first["_replica_run"] == {"run_id": "run-uid-7", "at": int(now - 170), "ref": "event:ev1"}
    key = ksa._board_content_key(path, rev.SHADOW_TABLES)
    _beat(conn, "ev2", now)
    assert ksa._board_content_key(path, rev.SHADOW_TABLES) == key  # still bookkeeping: no board re-read
    (second,) = links.replica_cards(["s1"], ME, replicas=[("default", path)], now=now)
    assert second["_replica_run"]["ref"] == "event:ev2" and second["_replica_run"]["at"] == int(now)
    assert len(reads) == 1  # the link rows stayed memoised; only the bounded run read ran again


def test_run_rollover_on_a_quiet_board_never_borrows_the_old_run(shadow):
    path, conn, now = shadow
    with kbc.write_txn(conn):
        conn.execute("UPDATE task_runs SET status = 'released', ended_at = ? WHERE id = 7", (int(now - 5),))
        conn.execute("INSERT INTO task_runs (id, task_id, status, started_at, uid) "
                     "VALUES (8, 't_hub', 'running', ?, 'run-uid-8')", (int(now - 4),))
        conn.execute("UPDATE tasks SET current_run_id = 8 WHERE id = 't_hub'")
    _beat(conn, "ev-late-old", now, run_id=7)  # the previous attempt's late heartbeat arrives last
    (card,) = links.replica_cards(["s1"], ME, replicas=[("default", path)], now=now)
    assert "_replica_run" not in card  # the new run has no liveness event yet: explicit unknown, not run 7's beat
    _beat(conn, "ev-new", now, run_id=8)
    (card,) = links.replica_cards(["s1"], ME, replicas=[("default", path)], now=now)
    assert card["_replica_run"] == {"run_id": "run-uid-8", "at": int(now), "ref": "event:ev-new"}


def _counted_observation():
    row = {"id": "s1", "_lineage_ids": ["s1"]}
    ksa.annotate_kanban_activity([row])
    (obs,) = row["execution_observations"]
    return obs


def test_sidebar_counts_pick_up_the_new_heartbeat_on_their_next_recompute(shadow, monkeypatch):
    _path, conn, _now = shadow
    # The shadow key will not move for the heartbeat, so only the counts' own age bound (3 s TTL, 30 s revalidation;
    # unchanged by this fix) triggers their recompute; zero it instead of sleeping 30 s.
    monkeypatch.setattr(ksa, "_CACHE_TTL_S", 0.0)
    monkeypatch.setattr(ksa, "_COUNTS_MAX_AGE_S", 0.0)
    first = _counted_observation()
    assert first["state"] == "running" and first["source_revision"] == "event:ev1"
    _beat(conn, "ev2", time.time())
    second = _counted_observation()
    assert second["state"] == "running" and second["source_revision"] == "event:ev2"
    assert second["identity"]["run_id"] == "run-uid-7"
    # The task list (``/api/session-kanban-tasks``) projects the same record from the same read.
    from hermes_cli.kanban_execution_observations import add_cards
    (listed,) = add_cards([{**c, "session_id": "s1"} for c in ksa.session_tasks(["s1"])])
    assert listed["execution_observations"][0]["source_revision"] == "event:ev2"
    assert listed["execution_observations"][0]["identity"] == second["identity"]


@pytest.mark.parametrize("failure", ["replica", "runs"])
def test_last_good_preserves_run_evidence_when_replica_read_fails(shadow, monkeypatch, failure):
    from hermes_cli.kanban_execution_observations import add_cards

    path, _conn, now = shadow
    monkeypatch.setattr(time, "time", lambda: now)
    (first,) = links.replica_cards(["s1"], ME, replicas=[("default", path)])
    (listed,) = add_cards([dict(first)])  # add_cards consumes the private run evidence
    (before,) = listed["execution_observations"]
    assert before["state"] == "running"
    assert before["observed_at"] == int(now - 170)
    assert before["identity"]["run_id"] == "run-uid-7"
    memo_key = (str(path), ME, "default")
    memo = links._replica_rows[memo_key]
    assert rev.is_revision(memo[0])
    calls = []

    def unreadable(*args):
        calls.append(1)
        raise sqlite3.OperationalError("replica unavailable")

    def unexpected_row_read(*args):
        pytest.fail("the successful link-row memo must be reused before read_runs fails")

    monkeypatch.setattr(links, "_read_replica_links", unexpected_row_read)
    monkeypatch.setattr(links, "_read_replica" if failure == "replica" else "read_runs", unreadable)
    unreadable_boards = []
    (stale,) = links.replica_cards(["s1"], ME, replicas=[("default", path)],
                                   unreadable_out=unreadable_boards)
    assert calls == [1] and unreadable_boards == ["default"]
    assert links._replica_rows[memo_key] is memo
    assert stale["source"] == "replica" and stale["stale"] is True
    assert stale["_replica_run"] == first["_replica_run"]
    (listed,) = add_cards([stale])
    (after,) = listed["execution_observations"]
    assert after["state"] == "unknown"
    assert after["unknown_reason"] == "source_unavailable"
    assert after["last_known_state"] == "running"
    for field in ("identity", "observed_at", "valid_until", "source_revision", "evidence_refs"):
        assert after[field] == before[field]


@pytest.mark.parametrize("status, request_json, last_known", [
    ("scheduled", None, "waiting"), ("review", None, "waiting"), ("blocked", None, "waiting"),
    ("blocked", '{"action": "reply A or B"}', "needs_user")])
def test_parked_remote_card_projects_one_record_in_list_counts_and_checker(shadow, monkeypatch, status,
                                                                         request_json, last_known):
    """A parked replica card has no executor clock: every consumer is unknown, and all of them keep the SAME
    last-known state. The counts used to rebuild the row from its count key alone (no status), so the sidebar and
    session-idle-check (``annotate_kanban_activity``) showed ``last_known_state`` null while
    ``/api/session-kanban-tasks`` showed the row's status (t_01a1d495b43909e8)."""
    _path, conn, now = shadow
    with kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET status = ?, human_request = ? WHERE id = 't_hub'", (status, request_json))
        conn.execute("UPDATE task_runs SET status = 'released', ended_at = ? WHERE id = 7", (int(now - 60),))
    sync.mark_pulled(conn, at=now)
    monkeypatch.setattr(time, "time", lambda: now)
    counted = _counted_observation()  # the sidebar counts row, which session-idle-check reads too
    (listed,) = ksa.session_tasks(["s1"])
    (obs,) = listed["execution_observations"]
    assert obs == counted
    assert obs["state"] == "unknown" and obs["unknown_reason"] == "missing_qualified_identity"
    assert obs["last_known_state"] == last_known
    assert obs["observed_at"] is None and obs["evidence_refs"] == []  # the pull clock never dates it
    assert obs["identity"] == {"gateway": OWNER, "profile": "work", "board": "default", "kind": "card",
                               "work_id": "t_hub"}


# Every other remote row ``raw`` declines (no running replica evidence) takes the same ``card_raw`` path in
# ``kanban_replica_execution.collect`` (review 5890273353 of PR #2144). A replica ``done`` row whose link is still
# unreleased is listed as status ``unknown`` (``replica_cards``); a running card whose current run has no liveness
# event yet has no ``_replica_run``.
_OTHER_REMOTE_ROWS = {
    "todo": ["UPDATE tasks SET status = 'todo' WHERE id = 't_hub'",
             "UPDATE task_runs SET status = 'released', ended_at = 1 WHERE id = 7"],
    "unknown": ["UPDATE tasks SET status = 'done' WHERE id = 't_hub'",
                "UPDATE task_runs SET status = 'done', ended_at = 1 WHERE id = 7"],
    "running-no-beat": ["DELETE FROM task_events WHERE task_id = 't_hub'"],
}


@pytest.mark.parametrize("case, last_known", [("todo", "queued"), ("unknown", None),
                                              ("running-no-beat", "running")])
def test_other_remote_rows_project_one_record_in_list_and_counts(shadow, monkeypatch, case, last_known):
    path, conn, now = shadow
    with kbc.write_txn(conn):
        for statement in _OTHER_REMOTE_ROWS[case]:
            conn.execute(statement)
    sync.mark_pulled(conn, at=now)
    monkeypatch.setattr(time, "time", lambda: now)
    (card,) = links.replica_cards(["s1"], ME, replicas=[("default", path)], now=now)
    assert "_replica_run" not in card  # the fixture really is a row ``raw`` declines
    counted = _counted_observation()
    (listed,) = ksa.session_tasks(["s1"])
    (obs,) = listed["execution_observations"]
    assert obs == counted
    assert obs["state"] == "unknown" and obs.get("last_known_state") == last_known
    assert obs["observed_at"] is None and obs["evidence_refs"] == []  # the pull clock never dates it
    assert obs["identity"]["gateway"] == OWNER and obs["identity"]["profile"] == "work"
    assert obs["identity"]["work_id"] == "t_hub"


@pytest.mark.parametrize("status", ["scheduled", "running"])
def test_remote_card_adapter_error_projects_one_record_in_list_and_counts(shadow, monkeypatch, status):
    """An adapter error is the same explicit unknown everywhere: ``add_cards`` projects ``failed_card`` (adapter_error,
    the ask kept), and the counts used to fall back to the count key alone (no ask, another unknown_reason)."""
    from hermes_cli import kanban_execution_observations as keo
    from hermes_cli import kanban_replica_execution as kre

    _path, conn, now = shadow
    with kbc.write_txn(conn):
        conn.execute("UPDATE tasks SET status = ?, human_request = ? WHERE id = 't_hub'",
                     (status, '{"action": "reply A or B", "resolved_check": "true"}'))
        if status != "running":
            conn.execute("UPDATE task_runs SET status = 'released', ended_at = ? WHERE id = 7", (int(now - 60),))
    sync.mark_pulled(conn, at=now)
    monkeypatch.setattr(time, "time", lambda: now)

    def broken(*args, **kwargs):
        raise RuntimeError("adapter failed")

    # ``running`` breaks the running-evidence adapter, the other status the generic card adapter.
    if status == "running":
        monkeypatch.setattr(kre, "raw", broken)
    else:
        monkeypatch.setattr(keo, "card_raw", broken)
    counted = _counted_observation()
    (listed,) = ksa.session_tasks(["s1"])
    (obs,) = listed["execution_observations"]
    assert obs == counted
    assert obs["state"] == "unknown" and obs["unknown_reason"] == "adapter_error"
    assert obs["human_request"] == {"action": "reply A or B"}  # probe fields stripped, the ask kept
    assert obs["identity"] == {"gateway": OWNER, "profile": "work", "board": "default", "kind": "card",
                               "work_id": "t_hub"}


def test_warm_counts_reproject_expired_run_without_recompute(shadow, monkeypatch):
    _path, _conn, now = shadow
    beat = int(now - 170)
    clock = [beat + 179.0]
    monotonic = [time.monotonic()]
    monkeypatch.setattr(time, "time", lambda: clock[0])
    monkeypatch.setattr(time, "monotonic", lambda: monotonic[0])
    first = _counted_observation()
    assert first["state"] == "running"
    assert first["observed_at"] == beat
    cached = ksa._execution_cache[0]
    generation = ksa._counts_generation

    def unexpected_recompute(*args, **kwargs):
        pytest.fail("expiry must be projected from the warm counts cache")

    monkeypatch.setattr(ksa, "_recompute_all_counts", unexpected_recompute)
    clock[0] += 2
    monotonic[0] += 2  # inside the unchanged 3 s counts TTL, but beyond the run's 180 s validity
    second = _counted_observation()
    assert ksa._execution_cache[0] is cached
    assert ksa._counts_generation == generation
    assert second["state"] == "unknown"
    assert second["last_known_state"] == "running"
    assert second["identity"] == first["identity"]
    assert second["observed_at"] == beat
    assert second["valid_until"] == beat + 180
