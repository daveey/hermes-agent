"""Done work is observed only through the landed completion-receipt seam, driven by real producers.

Cards are created, claimed and completed with the real ``kanban_db`` writers (which admit the receipt),
then read through the real session-task and sidebar-count entry paths. No receipt store, writer or
outcome precedence is added: the adapter consumes ``read_completed`` / ``_receipt_epoch`` evidence.
"""
import copy
import json
import sqlite3
import time
from contextlib import contextmanager
from typing import Any

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_operator
from hermes_cli import kanban_execution_observations as observations
from hermes_cli import kanban_origin
from hermes_cli import kanban_origin_ledger as ledger
from hermes_cli import kanban_session_activity as activity
from hermes_cli.execution_observations import identity_key, project
from hermes_cli.kanban_completion_receipt_projection import read_completed, read_completed_many
from tests.hermes_state.test_completion_receipt import receipt

GATEWAY, PROFILE = "fixture-gateway", "default"


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))  # the board is under the root: owner profile default
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path / "hub-state"))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.setattr(kanban_origin, "gateway_names", lambda: (GATEWAY, {GATEWAY, "away"}))
    monkeypatch.setattr(kanban_origin, "gateway_name", lambda: GATEWAY)
    monkeypatch.setattr(observations, "local_scope", lambda: (GATEWAY, PROFILE))
    activity._reset_cache_for_tests()
    path = kb.kanban_db_path(board="default")
    with kbc.connect_closing(path) as conn:
        yield conn
    activity._reset_cache_for_tests()


def card(conn, *, contract: str | None = "local-only", goal=False, session="session"):
    return kb.create_task(conn, title="Bounded work", assignee="coder", session_id=session,
                          completion_contract=contract, goal_mode=goal)


def finish(conn, tid, value: Any = "delivered", **kw):
    assert kb.claim_task(conn, tid) is not None
    meta = {"completion_receipt": receipt(value) if isinstance(value, str) else value} if value else None
    assert kb.complete_task(conn, tid, summary="Finished", metadata=meta, **kw)
    return tid


def closing_run(conn, tid):
    uid = "uid" if "uid" in {r[1] for r in conn.execute("PRAGMA table_info(task_runs)")} else "NULL"
    row = conn.execute(f"SELECT id,{uid} AS uid FROM task_runs WHERE task_id=? ORDER BY id DESC LIMIT 1",
                       (tid,)).fetchone()
    return row["uid"] or row["id"]


def done_listing(tid=None):
    rows = activity.session_tasks(["session"], statuses=("done",))
    return {r["id"]: r for r in rows} if tid is None else next(r for r in rows if r["id"] == tid)


def observation(row):
    values = row["execution_observations"]
    assert len(values) == 1
    return values[0]


# --- the canonical session-task entry path -------------------------------------------------------

def test_done_count_derives_after_source_connection_close(board, monkeypatch):
    from hermes_cli import kanban_execution_receipt as adapter

    tid = finish(board, card(board))
    opened, derived = [], []
    original_open, original_raw = activity._open_ro, adapter.done_raw

    def open_one(path):
        conn = original_open(path)
        opened.append(conn)
        return conn

    def derive(*args, **kwargs):
        assert len(opened) == 1
        with pytest.raises(sqlite3.ProgrammingError, match='closed'):
            opened[0].execute('SELECT 1')
        result = original_raw(*args, **kwargs)
        derived.append(result)
        return result

    monkeypatch.setattr(activity, '_open_ro', open_one)
    monkeypatch.setattr(adapter, 'done_raw', derive)
    reads, complete = activity._read_board(kb.kanban_db_path(board='default'), parked=True, stalled=True)
    assert complete and derived
    assert reads['done_execution'][tid]['state'] == 'done'
    assert reads['done_pairs'] == [('session', tid)]


def test_real_delivered_completion_is_done_with_exact_run_and_receipt_ref(board):
    tid = finish(board, card(board))
    value = observation(done_listing(tid))
    assert value["state"] == "done" and value["freshness"] == "current"
    assert value["identity"] == {"gateway": GATEWAY, "profile": PROFILE, "board": "default", "kind": "card",
                                 "work_id": tid, "run_id": closing_run(board, tid)}
    receipt_ = value["delivery_receipt"]
    assert receipt_["verified"] is True and receipt_["disposition"] == "delivered"
    assert receipt_["identity"] == value["identity"]
    source = read_completed(board, tid, gateway=GATEWAY, board="default")["completion_receipt_source"]
    assert receipt_["evidence_ref"].startswith(source["ref"] + "#event:")
    assert receipt_["evidence_ref"] in value["evidence_refs"]
    assert value["last_progress"] is None  # a completion receipt is not heartbeat/progress evidence
    assert not [k for k in done_listing(tid) if k.startswith("_execution")]  # internal carriers never leak


def test_expiry_without_a_fetch_keeps_the_historical_receipt_but_not_the_done_state(board):
    tid = finish(board, card(board))
    value = observation(done_listing(tid))
    stale = project(value, now=value["observed_at"] + 181)
    assert stale["state"] == "unknown" and stale["unknown_reason"] == "source_expired"
    assert stale["last_known_state"] == "done"
    assert stale["delivery_receipt"] == value["delivery_receipt"]
    assert project(value, now=value["observed_at"] + 179)["state"] == "done"


def test_receipt_cannot_date_itself_fresh_by_being_refetched(board, monkeypatch):
    tid = finish(board, card(board))
    first = observation(done_listing(tid))
    monkeypatch.setattr(activity.time, "time", lambda: first["observed_at"] + 400)
    later = observation(done_listing(tid))
    assert later["observed_at"] == first["observed_at"] + 400  # a read of the owner board is the source clock
    assert project(first, now=first["observed_at"] + 400)["state"] == "unknown"


@pytest.mark.parametrize("disposition", ["superseded", "contained", "rejected"])
def test_nondelivered_dispositions_are_never_done(board, disposition):
    tid = finish(board, card(board), disposition)
    value = observation(done_listing(tid))
    assert value["state"] == "unknown" and "delivery_receipt" not in value and value["last_known_state"] is None


def test_mixed_item_dispositions_are_not_delivered(board):
    mixed = receipt("delivered")
    mixed["items"].append(receipt("rejected")["items"][0])
    tid = finish(board, card(board), mixed)
    value = observation(done_listing(tid))
    assert value["state"] == "unknown" and "delivery_receipt" not in value


def test_done_without_a_receipt_is_unspecified_not_delivered(board):
    tid = finish(board, card(board), None)
    value = observation(done_listing(tid))
    assert value["state"] == "unknown" and "delivery_receipt" not in value


def test_receipt_without_release_verification_is_not_done(board):
    """No local-only contract and no PR acceptance: the existing verifier refuses, so does the adapter."""
    tid = finish(board, card(board))
    board.execute("UPDATE tasks SET completion_contract='https://github.com/example/repo/pull/7' WHERE id=?", (tid,))
    board.commit()
    assert read_completed(board, tid, gateway=GATEWAY, board="default") is not None
    value = observation(done_listing(tid))
    assert value["state"] == "unknown" and "delivery_receipt" not in value


def test_goal_card_without_judge_receipt_is_not_done(board):
    tid = finish(board, card(board, goal=True))
    value = observation(done_listing(tid))
    assert value["state"] == "unknown" and "delivery_receipt" not in value


def test_archive_is_never_success_even_after_a_delivered_completion(board):
    tid = finish(board, card(board))
    assert kb.archive_task(board, tid)
    assert board.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0] == "archived"
    rows = activity.session_tasks(["session"], statuses=("done", "archived"))
    value = observation(next(r for r in rows if r["id"] == tid))
    assert value["state"] != "done" and "delivery_receipt" not in value


def test_archived_without_completion_is_cancelled_not_done(board):
    tid = card(board)
    assert kb.archive_task(board, tid)
    rows = activity.session_tasks(["session"], statuses=("archived",))
    assert all(v["state"] != "done" for r in rows for v in r["execution_observations"])


def test_reopened_run_is_not_marked_done_by_the_old_receipt(board):
    tid = finish(board, card(board))
    old_run = closing_run(board, tid)
    assert kanban_db_operator.reopen_done_task(board, tid, reason="more work", actor="operator")
    assert kb.claim_task(board, tid) is not None
    assert kb.complete_task(board, tid, summary="Second pass")  # no receipt on the new run
    new_run = closing_run(board, tid)
    assert new_run != old_run
    value = observation(done_listing(tid))
    assert value["state"] == "unknown" and "delivery_receipt" not in value
    assert value["identity"].get("run_id") != old_run


def test_receipt_bound_to_a_different_run_than_the_verified_completion_is_not_done(board):
    tid = finish(board, card(board))
    other = finish(board, card(board))
    other_run = board.execute("SELECT run_id FROM task_events WHERE task_id=? AND kind='completed'",
                              (other,)).fetchone()[0]
    with kb.write_txn(board):  # a later completion event stolen onto a foreign run
        board.execute("UPDATE task_events SET run_id=? WHERE task_id=? AND kind='completed'", (other_run, tid))
    value = observation(done_listing(tid))
    assert value["state"] == "unknown" and "delivery_receipt" not in value


def test_later_unadmitted_completion_event_supersedes_the_old_receipt(board):
    tid = finish(board, card(board))
    run = board.execute("SELECT run_id FROM task_events WHERE task_id=? AND kind='completed'", (tid,)).fetchone()[0]
    with kb.write_txn(board):
        kb._append_event(board, tid, "completed", {}, run_id=run)
    value = observation(done_listing(tid))
    assert value["state"] == "unknown" and "delivery_receipt" not in value


def test_tampered_receipt_metadata_is_not_done(board):
    tid = finish(board, card(board))
    with kb.write_txn(board):
        board.execute("UPDATE task_runs SET metadata=? WHERE task_id=?",
                      (json.dumps({"completion_receipt": {"version": 1, "items": []}}), tid))
    assert observation(done_listing(tid))["state"] == "unknown"


def test_legacy_basis_completion_never_becomes_delivered(board):
    """The owner's legacy release basis is a release pass, not a delivery receipt."""
    tid = finish(board, card(board), None)
    board.execute("BEGIN")
    try:
        verdict = ledger.observe_owner_task(board, {"id": tid, "status": "done"}, now=1000)["origin_observation"]
    finally:
        board.execute("COMMIT")
    assert verdict["verified"] is True  # contract local-only: verified, but no receipt
    assert observation(done_listing(tid))["state"] == "unknown"


def test_adapter_failure_is_an_explicit_unknown_and_keeps_asks(board, monkeypatch):
    tid = finish(board, card(board))
    from hermes_cli import kanban_execution_receipt as adapter

    def boom(*a, **k):
        raise sqlite3.DatabaseError("simulated receipt read failure")

    monkeypatch.setattr(adapter, "assemble_done", boom)
    value = observation(done_listing(tid))
    assert value["state"] == "unknown" and "delivery_receipt" not in value


def _open_source_fails(monkeypatch):
    from hermes_cli import kanban_execution_source as source

    def boom(*a, **k):
        raise sqlite3.OperationalError("simulated open-card source failure")
    monkeypatch.setattr(source, "assemble", boom)


def test_open_card_source_failure_keeps_the_independent_receipt(board, monkeypatch):
    tid = finish(board, card(board))
    _open_source_fails(monkeypatch)
    value = observation(done_listing(tid))
    assert value["state"] == "done" and value["delivery_receipt"]["verified"] is True
    assert value["identity"]["profile"] == PROFILE
    assert not [k for k in done_listing(tid) if k.startswith("_execution")]


def test_failed_source_identity_is_the_board_root_owner_not_the_reader(board, monkeypatch):
    """C9 on the failure path: a reader running as another profile never stamps its own profile."""
    open_tid, done_tid = card(board), finish(board, card(board))
    monkeypatch.setattr(observations, "local_scope", lambda: (GATEWAY, "coder"))
    _open_source_fails(monkeypatch)
    rows = {r["id"]: r for r in activity.session_tasks(["session"], statuses=("ready", "todo", "done"))}
    assert {open_tid, done_tid} <= set(rows)
    failed = observation(rows[open_tid])
    assert failed["state"] == "unknown" and failed["unknown_reason"] == "adapter_error"
    assert failed["identity"]["profile"] == "default" and failed["owner"]["profile"] == "default"
    assert failed["identity"]["gateway"] == GATEWAY and failed["identity"]["board"] == "default"
    assert observation(rows[done_tid])["identity"]["profile"] == "default"
    counted = [v for v in annotate()["execution_observations"] if v["identity"]["work_id"] == open_tid]
    assert counted and {v["identity"]["profile"] for v in counted} == {"default"}
    assert {identity_key(v["identity"]) for v in counted} == {identity_key(failed["identity"])}


def test_listing_open_statuses_never_reads_completion_evidence(board, monkeypatch):
    finish(board, card(board))
    from hermes_cli import kanban_execution_receipt as adapter
    monkeypatch.setattr(adapter, "assemble_done", lambda *a, **k: pytest.fail("open listing read done evidence"))
    assert activity.session_tasks(["session"]) == []


# --- the sidebar count / checker collector entry path ----------------------------------------------

def annotate():
    rows = [{"id": "session"}]
    activity.annotate_kanban_activity(rows)
    return rows[0]


def test_sidebar_counts_carry_a_done_observation_without_counting_it_as_work(board):
    tid = finish(board, card(board))
    row = annotate()
    done = [v for v in row["execution_observations"] if v["state"] == "done"]
    assert len(done) == 1 and done[0]["identity"]["work_id"] == tid
    assert done[0]["delivery_receipt"]["verified"] is True
    assert "kanban_active" not in row and "kanban_blocked" not in row


def test_sidebar_done_expires_without_a_fetch_and_keeps_history(board):
    tid = finish(board, card(board))
    value = next(v for v in annotate()["execution_observations"] if v["identity"]["work_id"] == tid)
    stale = project(value, now=value["observed_at"] + 600)
    assert stale["state"] == "unknown" and stale["last_known_state"] == "done"
    assert stale["delivery_receipt"]["verified"] is True


def test_sidebar_omits_unverified_done_and_old_done(board, monkeypatch):
    finish(board, card(board), None)
    old = finish(board, card(board))
    board.execute("UPDATE tasks SET completed_at=? WHERE id=?", (int(time.time()) - activity.ATTENTION_WINDOW_S - 5, old))
    board.commit()
    row = annotate()
    assert not [v for v in row.get("execution_observations", []) if v["state"] == "done"]


def test_sidebar_open_card_and_done_card_coexist_without_aliasing(board):
    done = finish(board, card(board))
    running = card(board)
    assert kb.claim_task(board, running) is not None
    row = annotate()
    states = {v["identity"]["work_id"]: v["state"] for v in row["execution_observations"]}
    assert states[done] == "done" and states[running] != "done"
    assert row["kanban_active"] == 1


def test_mirrored_primary_board_never_reports_done(board):
    """A pulled mirror is not owner evidence: no fresh done (history on mirrors is not covered here)."""
    finish(board, card(board))
    board.execute("CREATE TABLE IF NOT EXISTS kanban_sync_state (mode TEXT)")
    board.execute("DELETE FROM kanban_sync_state")
    board.execute("INSERT INTO kanban_sync_state VALUES ('primary')")
    board.commit()
    activity._reset_cache_for_tests()
    values = [v for r in activity.session_tasks(["session"], statuses=("done",)) for v in r["execution_observations"]]
    assert values and all(v["state"] != "done" for v in values)
    assert not [v for v in annotate().get("execution_observations", []) if v["state"] == "done"]


# --- bounded reads ---------------------------------------------------------------------------------

def statements_for(board, monkeypatch, call):
    statements = []
    original = activity.cached_ro

    @contextmanager
    def traced(path, **kwargs):
        with original(path, **kwargs) as conn:
            conn.set_trace_callback(statements.append)
            try:
                yield conn
            finally:
                conn.set_trace_callback(None)

    monkeypatch.setattr(activity, "cached_ro", traced)
    call()
    return len([s for s in statements if s.lstrip().upper().startswith(("SELECT", "WITH"))])


@pytest.mark.parametrize("warm", [False, True])
def test_done_read_cost_is_independent_of_card_count_for_listing_and_counts(board, monkeypatch, warm):
    finish(board, card(board))
    listing = lambda: activity.session_tasks(["session"], statuses=("done",))
    counts = lambda: (activity._reset_cache_for_tests(), annotate())

    def listing_cost():
        # Counts reset the retained readers. Compare like cache states, rather
        # than a cold one-card selection with a warm many-card selection.
        activity._reset_cache_for_tests()
        if warm:
            listing()
        return statements_for(board, monkeypatch, listing)

    listing(), counts()  # one-time schema/revision warm-up is not a per-card cost
    one_list = listing_cost()
    one_count = statements_for(board, monkeypatch, counts)
    for _ in range(25):
        finish(board, card(board), "delivered" if _ % 2 else "rejected")
    activity._reset_cache_for_tests()
    assert len(listing()) == 26
    assert listing_cost() == one_list
    assert statements_for(board, monkeypatch, counts) == one_count


def test_batch_reader_matches_the_per_task_seam_for_every_shape(board):
    ids = [finish(board, card(board), v) for v in ("delivered", "superseded", "contained", "rejected", None)]
    legacy = card(board)
    assert kb.claim_task(board, legacy) is not None
    unfinished = card(board)
    with kb.write_txn(board):
        kb._synthesize_ended_run(board, ids[0], outcome="review", summary="later review",
                                 metadata={"completion_receipt": receipt("rejected")})
    many = read_completed_many(board, [*ids, legacy, unfinished, "t_missing"], board="default", gateway=GATEWAY)
    assert set(many) == {*ids, legacy, unfinished, "t_missing"}
    for tid in many:
        single = read_completed(board, tid, gateway=GATEWAY, board="default") if tid != "t_missing" else None
        assert many[tid] == single, tid
        assert copy.deepcopy(many[tid]) == many[tid]


def test_batch_reader_requires_source_namespace(board):
    with pytest.raises(ValueError, match="source gateway"):
        read_completed_many(board, ["t"], board="default", gateway="")


def test_batch_reader_uses_created_on_like_inventory(board):
    from hermes_cli.kanban_completion_receipt_projection import read_inventory
    tid = finish(board, card(board))
    columns = {r[1] for r in board.execute("PRAGMA table_info(tasks)")}
    if "created_on" in columns:
        board.execute("UPDATE tasks SET created_on='origin-gw' WHERE id=?", (tid,))
    many = read_completed_many(board, [tid], board="default", gateway=GATEWAY)
    assert many[tid] == read_inventory(board, tid, board="default", gateway=GATEWAY)


# --- the extracted pure verifier is the one verdict ------------------------------------------------

def owner_verdict(conn, tid):
    conn.execute("BEGIN")
    try:
        status = conn.execute("SELECT status FROM tasks WHERE id=?", (tid,)).fetchone()[0]
        out = ledger.observe_owner_task(conn, {"id": tid, "status": status}, now=1000)["origin_observation"]
        return out["verified"], out.get("basis") == "legacy_completed"
    finally:
        conn.execute("COMMIT")


def test_pure_verifier_and_owner_observation_agree_on_every_shape(board):
    from hermes_cli.kanban_completion_inputs import read_verdicts
    ids = {
        "local": finish(board, card(board)),
        "pr_contract": finish(board, card(board)),
        "goal_no_proof": finish(board, card(board, goal=True)),
    }
    board.execute("UPDATE tasks SET completion_contract='https://github.com/example/repo/pull/7' WHERE id=?",
                  (ids["pr_contract"],))
    board.commit()
    open_card = card(board)
    verdicts = read_verdicts(board, [*ids.values(), open_card])
    for name, tid in ids.items():
        assert verdicts[tid] == owner_verdict(board, tid)[:2], name
    assert verdicts[ids["local"]][0] is True and verdicts[ids["pr_contract"]][0] is False
    assert open_card not in verdicts or verdicts[open_card][0] is False


def test_verifier_batch_read_cost_is_constant(board, monkeypatch):
    from hermes_cli.kanban_completion_inputs import read_verdicts
    first = finish(board, card(board))
    def measure(ids):
        statements = []
        board.set_trace_callback(statements.append)
        try:
            read_verdicts(board, ids)
        finally:
            board.set_trace_callback(None)
        return len(statements)

    one = measure([first])
    ids = [first] + [finish(board, card(board)) for _ in range(40)]
    assert measure(ids) == one


# --- the pure adapter: each binding clause is load-bearing ------------------------------------------

def adapter_inputs(board):
    from hermes_cli.kanban_completion_inputs import read_completions
    tid = finish(board, card(board))
    fact = read_completions(board, [tid])[tid]
    bundle = read_completed_many(board, [tid], board="default", gateway=GATEWAY)[tid]
    return tid, fact, bundle


def adapt_done(tid, fact, bundle, **over):
    from hermes_cli.kanban_execution_receipt import done_raw
    kw = {"board": "default", "gateway": GATEWAY, "profile": PROFILE, "observed_at": 1000.0, **over}
    return done_raw(tid, fact, bundle, **kw)


def test_pure_adapter_accepts_exactly_the_real_shape(board):
    tid, fact, bundle = adapter_inputs(board)
    raw = adapt_done(tid, fact, bundle)
    assert raw["state"] == "done" and raw["valid_until"] == 1180.0
    assert raw["identity"]["run_id"] == fact.run_id
    assert project(raw, now=1179)["state"] == "done" and project(raw, now=1180)["state"] == "unknown"


def test_pure_adapter_rejects_each_broken_binding(board):
    tid, fact, bundle = adapter_inputs(board)
    source = bundle["completion_receipt_source"]
    broken = {
        "archived": (fact._replace(status="archived"), bundle, {}),
        "open": (fact._replace(status="running"), bundle, {}),
        "unverified": (fact._replace(verified=False), bundle, {}),
        "legacy": (fact._replace(legacy=True), bundle, {}),
        "no fact": (None, bundle, {}),
        "no receipt": (fact, None, {}),
        "no gateway": (fact, bundle, {"gateway": None}),
        "no profile": (fact, bundle, {"profile": ""}),
        "no board": (fact, bundle, {"board": None}),
        "bad clock": (fact, bundle, {"observed_at": float("nan")}),
        "other gateway": (fact, bundle, {"gateway": "elsewhere"}),
        "other board": (fact, bundle, {"board": "bb"}),
        "event mismatch": (fact._replace(event_id=fact.event_id + 1), bundle, {}),
        "no event": (fact._replace(event_id=None), bundle, {}),
        "event run mismatch": (fact._replace(event_run_id=fact.event_run_id + 1), bundle, {}),
        "closing run mismatch": (fact._replace(run_id=fact.run_id + 1), bundle, {}),
        "uid vs none": (fact._replace(run_uid="u1"), bundle, {}),
        "none vs uid": (fact, {**bundle, "completion_receipt_source": {**source, "run_uid": "u1"}}, {}),
        "uid mismatch": (fact._replace(run_uid="u2"),
                         {**bundle, "completion_receipt_source": {**source, "run_uid": "u1"}}, {}),
    }
    for name, (f, b, over) in broken.items():
        assert adapt_done(tid, f, b, **over) is None, name
    assert adapt_done("t_other", fact, bundle) is None  # the receipt ref names another task
    mixed = copy.deepcopy(bundle)
    mixed["completion_receipt"]["items"].append(receipt("rejected")["items"][0])
    assert adapt_done(tid, fact, mixed) is None
    empty = copy.deepcopy(bundle)
    empty["completion_receipt"]["items"] = []
    assert adapt_done(tid, fact, empty) is None


def test_pure_adapter_portable_run_uid_identity(board):
    tid, fact, bundle = adapter_inputs(board)
    source = {**bundle["completion_receipt_source"], "run_uid": "run-uid-1", "event_uid": "event-uid-1"}
    raw = adapt_done(tid, fact._replace(run_uid="run-uid-1"), {**bundle, "completion_receipt_source": source})
    assert raw["identity"]["run_id"] == "run-uid-1"
    assert raw["delivery_receipt"]["evidence_ref"].endswith("#event:event-uid-1")
    assert raw["delivery_receipt"]["identity"] == raw["identity"]


def test_pure_adapter_owner_fields_and_progress_stay_separate(board):
    tid, fact, bundle = adapter_inputs(board)
    raw = adapt_done(tid, fact._replace(assignee="coder", session_id="session"), bundle)
    assert raw["owner"] == {"gateway": GATEWAY, "profile": PROFILE, "assignee": "coder", "session": "session"}
    assert raw["last_progress"] is None and raw["last_activity_kind"] == "completed"
    bare = adapt_done(tid, fact._replace(assignee=None, session_id=None), bundle)
    assert bare["owner"] == {"gateway": GATEWAY, "profile": PROFILE}


# --- batched reads agree with the per-task seam and owner verdict on adversarial evidence ------------

def run_of(conn, tid, kind="completed"):
    return conn.execute("SELECT run_id FROM task_events WHERE task_id=? AND kind=? ORDER BY id DESC",
                        (tid, kind)).fetchone()[0]


def test_batch_receipt_takes_the_latest_completion_across_reopen(board):
    tid = finish(board, card(board), "delivered")
    assert kanban_db_operator.reopen_done_task(board, tid, reason="redo", actor="operator")
    assert kb.claim_task(board, tid) is not None
    assert kb.complete_task(board, tid, summary="Redo", metadata={"completion_receipt": receipt("rejected")})
    many = read_completed_many(board, [tid], board="default", gateway=GATEWAY)[tid]
    assert many == read_completed(board, tid, gateway=GATEWAY, board="default")
    assert many["completion_receipt"]["items"][0]["disposition"] == "rejected"
    assert observation(done_listing(tid))["state"] == "unknown"


def test_batch_receipt_never_borrows_another_tasks_run(board):
    tid, other = finish(board, card(board)), finish(board, card(board))
    foreign = run_of(board, other)
    with kb.write_txn(board):
        board.execute("UPDATE task_events SET run_id=? WHERE task_id=? AND kind='completed'", (foreign, tid))
    assert read_completed(board, tid, gateway=GATEWAY, board="default") is None
    assert read_completed_many(board, [tid, other], board="default", gateway=GATEWAY)[tid] is None
    assert observation(done_listing(tid))["state"] == "unknown"


def test_batch_receipt_prefers_created_on_like_the_inventory_seam(board):
    from hermes_cli.kanban_completion_receipt_projection import read_inventory
    tid = finish(board, card(board))
    board.execute("ALTER TABLE tasks ADD COLUMN created_on TEXT")
    board.execute("UPDATE tasks SET created_on='origin-gw' WHERE id=?", (tid,))
    board.commit()
    many = read_completed_many(board, [tid], board="default", gateway=GATEWAY)[tid]
    assert many["completion_receipt_source"]["ref"] == f"@task:default@origin-gw/{tid}"
    assert many == read_inventory(board, tid, board="default", gateway=GATEWAY)
    # A foreign-created card is not this gateway's owner observation, so it is not observed done here.
    assert observation(done_listing(tid))["state"] == "unknown"


PR = "https://github.com/example/repo/pull/7"
HEAD = "a" * 40


def append_event(conn, tid, kind, payload, run):
    kb._append_event(conn, tid, kind, payload, run_id=run)
    return conn.execute("SELECT max(id) FROM task_events").fetchone()[0]


def pr_card(conn, *, kind="pr_acceptance", owner=None, after=False, ok=True):
    """A card whose release gate is a PR acceptance event; the fabricated event shape is parameterized."""
    tid = card(conn, contract=PR)
    assert kb.claim_task(conn, tid) is not None
    payload = json.dumps({"ok": ok, "classification": "success", "pr_url": PR, "head_sha": HEAD})
    eid = None
    if not after:
        with kb.write_txn(conn):
            run = conn.execute("SELECT current_run_id FROM tasks WHERE id=?", (tid,)).fetchone()[0]
            eid = append_event(conn, owner or tid, kind, json.loads(payload), run)
    with conn:
        conn.execute("UPDATE tasks SET completion_contract=? WHERE id=?", ("local-only", tid))
    assert kb.complete_task(conn, tid, summary="Merged", metadata={"completion_receipt": receipt()})
    with conn:
        conn.execute("UPDATE tasks SET completion_contract=? WHERE id=?", (PR, tid))
        if after:
            run = run_of(conn, tid)
            eid = append_event(conn, tid, kind, json.loads(payload), run)
        event = conn.execute("SELECT id,payload FROM task_events WHERE task_id=? AND kind='completed'",
                             (tid,)).fetchone()
        body = json.loads(event[1] or "{}")
        body["pr_acceptance_event_id"] = eid
        conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(body), event[0]))
    return tid


def verdict_pair(conn, tid):
    from hermes_cli.kanban_completion_inputs import read_verdicts
    return read_verdicts(conn, [tid])[tid], owner_verdict(conn, tid)


def test_pr_acceptance_event_binding_is_one_verdict_for_batch_and_owner(board):
    genuine = pr_card(board)
    wrong_kind = pr_card(board, kind="goal_verified")
    other = card(board)
    foreign = pr_card(board, owner=other)
    later = pr_card(board, after=True)
    refused = pr_card(board, ok=False)
    for tid in (genuine, wrong_kind, foreign, later, refused):
        batch, owner = verdict_pair(board, tid)
        assert batch == owner, tid
    assert verdict_pair(board, genuine)[0] == (True, False)
    for tid in (wrong_kind, foreign, later, refused):
        assert verdict_pair(board, tid)[0][0] is False, tid
    assert observation(done_listing(genuine))["state"] == "done"
    assert observation(done_listing(later))["state"] == "unknown"


def test_latest_completion_and_latest_run_decide_the_verdict(board):
    mismatched, extra_run, bad = (finish(board, card(board)) for _ in range(3))
    with kb.write_txn(board):
        kb._append_event(board, mismatched, "completed", {}, run_id=99999)
        board.execute("INSERT INTO task_runs (task_id, status, started_at) VALUES (?, 'running', 1)", (extra_run,))
        board.execute("UPDATE task_events SET payload='{not json' WHERE task_id=? AND kind='completed'", (bad,))
        board.execute("UPDATE tasks SET completion_contract=? WHERE id=?", (PR, bad))  # gate must parse the event
    from hermes_cli.kanban_completion_inputs import read_verdicts
    batch = read_verdicts(board, [mismatched, extra_run, bad])
    assert batch[mismatched] == owner_verdict(board, mismatched)[:2] == (False, False)
    assert batch[extra_run] == owner_verdict(board, extra_run)[:2] == (False, False)
    assert batch[bad] == (False, False)  # malformed evidence fails that card closed, not the batch
    healthy = finish(board, card(board))
    assert read_verdicts(board, [bad, healthy])[healthy] == (True, False)


def test_done_pair_window_is_bounded_and_per_statement(board):
    from hermes_cli import kanban_session_activity_recent_done as recent
    assert recent.MAX_DONE_PAIRS <= 200
    for _ in range(5):
        finish(board, card(board))
    since = time.time() - 60
    assert len(recent.done_pairs_on(board, since, activity._counted_sub_clause)) == 5
    assert len(recent.done_pairs_on(board, since, activity._counted_sub_clause, limit=2)) == 2
    assert recent.done_pairs_on(board, time.time() + 60, activity._counted_sub_clause) == []


def test_attach_done_marks_only_done_rows_on_failure_and_reads_open_source_failures(board, monkeypatch):
    from hermes_cli import kanban_execution_receipt as adapter
    done_id, open_id, failed_id = "t_done", "t_open", "t_failed"
    found = {done_id: {"status": "done"}, open_id: {"status": "running"},
             failed_id: {"status": "done", "_execution_error": True}}
    seen = []

    def boom(conn, ids, **kw):
        seen.append(list(ids))
        raise sqlite3.DatabaseError("simulated")

    monkeypatch.setattr(adapter, "assemble_done", boom)
    adapter.attach_done(board, found, board="default", gateway=GATEWAY, profile=PROFILE, observed_at=1000.0)
    # open rows are never read; a done row whose open-card read failed still has an independent receipt
    assert seen == [[done_id, failed_id]]
    assert found[done_id]["_execution_error"] is True and "_execution_error" not in found[open_id]
    assert "_execution_done" not in found[done_id]
