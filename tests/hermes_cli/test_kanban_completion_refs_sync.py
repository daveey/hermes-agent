"""A hub-homed card completed on a pool worker is verified on the hub after the Board feed replays it. [daveey]

Hub cards t_01a1d41478234144, t_01a1d44d87ea4bef, t_01a1d4eead55e393 and t_01a1d4f2f591d595 (2026-11-13) ran on
worker-3/4/5, completed there with a green PR acceptance, and replayed onto the hub board with new event ids. The
``completed`` payload kept the worker's ``pr_acceptance_event_id`` (1392472, 486557, 179773, 181582), which on the
hub names another card's event or none, while the receipt itself sat right before the completion (1185670 ...).
The hub's ``completion_verdict`` published ``verified: false``, the origin ledger never released the cards, and
the MacBook chat listed all four as ``unknown`` for hours. ``kanban_completion_refs`` binds by the receipt's
stable uid, and a pre-uid replayed completion by its run's receipt.
"""
import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_pr_acceptance as kpa
from hermes_cli.kanban_completion_inputs import read_completions
from hermes_cli.kanban_origin_ledger import observe_owner_task
from tests.hermes_cli import kanban_sync_support as support

PR = "https://github.com/acme/repo/pull/7"


@pytest.fixture
def boards(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    monkeypatch.setattr(kpa, "collect_acceptance", lambda contract, published, assignee=None: {
        "ok": True, "classification": "success", "head_sha": "a" * 40, "pr_url": PR, "pr_state": "MERGED",
        "checks": [], "recovery": "", "required": []})
    with kbc.connect(home / "worker.db") as worker, kbc.connect(home / "hub.db") as hub:
        # The hub's own history: its event and run ids run ahead of the worker's, as on the real hub.
        for _ in range(4):
            tid = kb.create_task(hub, title="hub history", assignee="dev")
            kb.claim_task(hub, tid)
            kb.complete_task(hub, tid, summary="local work")
        yield worker, hub


def replay(source, target, origin="worker-3"):
    """The Board feed: the worker's outbox applied on the hub (new local ids, same uids)."""
    changes = [sync.RemoteChange(i, op["table_name"], op["row_uid"], op["op"], op["fields"], i, op["hlc"], origin)
               for i, op in enumerate(sync.outbox(source), 1)]
    sync.apply_remote_changes(target, changes, new_cursor=len(changes), observers=False)


def completed_on_worker(worker):
    tid = kb.create_task(worker, title="Ship it", assignee="dev", completion_contract="acme/repo")
    run = kb.claim_task(worker, tid).current_run_id
    assert kb.complete_task(worker, tid, summary="landed", expected_run_id=run, metadata={"published_pr": PR})
    return tid


def payload_of(conn, tid):
    row = conn.execute("SELECT id, payload FROM task_events WHERE task_id=? AND kind='completed'", (tid,)).fetchone()
    return row["id"], json.loads(row["payload"])


def verified(conn, tid):
    conn.execute("BEGIN")
    try:
        one = observe_owner_task(conn, {"id": tid, "status": "done"}, now=123)["origin_observation"]["verified"]
    finally:
        conn.execute("ROLLBACK")
    return one, read_completions(conn, [tid])[tid].verified


def rewrite(conn, event_id, mutate):
    row = conn.execute("SELECT payload FROM task_events WHERE id=?", (event_id,)).fetchone()
    payload = json.loads(row["payload"])
    mutate(payload)
    with kb.write_txn(conn):
        conn.execute("UPDATE task_events SET payload=? WHERE id=?", (json.dumps(payload), event_id))


def test_a_worker_completion_replayed_on_the_hub_is_verified_there(boards):
    worker, hub = boards
    tid = completed_on_worker(worker)
    assert verified(worker, tid) == (True, True)
    replay(worker, hub)
    _event_id, payload = payload_of(hub, tid)
    receipt = hub.execute("SELECT id, uid FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()
    # Precondition, the incident's shape: on the hub the payload integer is not this card's receipt.
    assert payload["pr_acceptance_event_id"] != receipt["id"]
    assert kb.get_task(hub, tid).status == "done"
    assert verified(hub, tid) == (True, True)
    assert payload["pr_acceptance_event_uid"] == receipt["uid"]


def test_a_pre_uid_completion_replayed_from_a_worker_binds_its_runs_receipt(boards):
    """The four live cards completed before the uid existed: their payload carries only the worker's integer."""
    worker, hub = boards
    tid = completed_on_worker(worker)
    replay(worker, hub)
    event_id, _ = payload_of(hub, tid)
    rewrite(hub, event_id, lambda p: p.pop("pr_acceptance_event_uid", None))
    assert verified(hub, tid) == (True, True)
    # A receipt of another run is not the one this completion bound.
    receipt = hub.execute("SELECT id FROM task_events WHERE task_id=? AND kind='pr_acceptance'", (tid,)).fetchone()
    hub.execute("UPDATE task_events SET run_id=NULL WHERE id=?", (receipt["id"],))
    assert verified(hub, tid) == (False, False)


def test_a_pre_uid_replayed_id_naming_another_runs_receipt_still_binds_its_own_run(boards):
    """The worker's integer can, by chance, name a receipt of this card and gateway written in another run."""
    worker, hub = boards
    tid = completed_on_worker(worker)
    replay(worker, hub)
    event_id, _payload = payload_of(hub, tid)
    # An earlier replayed event of this card (same origin gateway), relabelled as a receipt of another run.
    other = hub.execute("SELECT id FROM task_events WHERE task_id=? AND kind='created'", (tid,)).fetchone()["id"]
    other_run = hub.execute("SELECT id FROM task_runs WHERE task_id != ? ORDER BY id LIMIT 1", (tid,)).fetchone()["id"]
    assert other_run != hub.execute("SELECT run_id FROM task_events WHERE id=?", (event_id,)).fetchone()["run_id"]
    with kb.write_txn(hub):
        hub.execute("UPDATE task_events SET kind='pr_acceptance', run_id=?, payload=? WHERE id=?",
                    (other_run, json.dumps({"ok": False}), other))
    assert hub.execute("SELECT origin_gateway FROM task_events WHERE id=?", (other,)).fetchone()[0] == "worker-3"
    rewrite(hub, event_id, lambda p: (p.pop("pr_acceptance_event_uid", None), p.update(pr_acceptance_event_id=other)))
    assert verified(hub, tid) == (True, True)


def test_a_goal_mode_completion_replayed_from_a_worker_is_verified_there(boards):
    """The ``goal_verified`` receipt binds across the replay like ``pr_acceptance``, by uid and pre-uid alike."""
    from hermes_cli.kanban_goal_receipt import GoalVerification
    worker, hub = boards
    tid = kb.create_task(worker, title="Build", assignee="dev", goal_mode=True, completion_contract="local-only")
    run = kb.claim_task(worker, tid).current_run_id
    proof = GoalVerification.for_handoff(kb.get_task(worker, tid), "Tested")
    assert kb.complete_task(worker, tid, summary="Tested", expected_run_id=run, goal_verification=proof)
    assert verified(worker, tid) == (True, True)
    replay(worker, hub)
    event_id, payload = payload_of(hub, tid)
    receipt = hub.execute("SELECT id, uid FROM task_events WHERE task_id=? AND kind='goal_verified'", (tid,)).fetchone()
    assert payload["goal_verified_event_id"] != receipt["id"]
    assert payload["goal_verified_event_uid"] == receipt["uid"]
    assert verified(hub, tid) == (True, True)
    rewrite(hub, event_id, lambda p: p.pop("goal_verified_event_uid"))
    assert verified(hub, tid) == (True, True)


def test_a_completion_that_names_no_receipt_never_borrows_one(boards):
    worker, hub = boards
    tid = completed_on_worker(worker)
    replay(worker, hub)
    event_id, _ = payload_of(hub, tid)
    rewrite(hub, event_id, lambda p: (p.pop("pr_acceptance_event_uid", None), p.pop("pr_acceptance_event_id")))
    assert verified(hub, tid) == (False, False)


def test_a_uid_that_names_no_receipt_of_this_card_does_not_fall_back(boards):
    worker, hub = boards
    tid = completed_on_worker(worker)
    replay(worker, hub)
    event_id, _ = payload_of(hub, tid)
    rewrite(hub, event_id, lambda p: p.update(pr_acceptance_event_uid="01a1d000-0000-7000-8000-000000000000"))
    assert verified(hub, tid) == (False, False)


def test_a_local_completion_with_a_wrong_id_stays_unverified(boards):
    """Only a replayed completion (``origin_gateway`` set) may fall back: on its own board the id is exact."""
    worker, _hub = boards
    tid = completed_on_worker(worker)
    event_id, _payload = payload_of(worker, tid)
    rewrite(worker, event_id, lambda p: (p.pop("pr_acceptance_event_uid", None),
                                         p.update(pr_acceptance_event_id=p["pr_acceptance_event_id"] + 1000)))
    assert verified(worker, tid) == (False, False)


# Hub t_01a1ca1975471ed4, t_01a1cda0087499e3, t_01a1ce14309bff2c, t_01a1cf477dece56b, t_01a1cf6b6dd5feb4,
# t_01a1cf7adf592a71, t_01a1d0622df6ea87, t_01a1d087f0697ede (2026-11-12): run on worker-2..6 and replayed before
# the hub recorded ``origin_gateway`` (from event 1180961), so the completion AND its receipts carry NULL there.
# Each payload id named another card's event or none. The run's Board lease (``claimed`` with ``holder`` and the
# run's ``run_uid``) is what proves the completion was written elsewhere.

def own_gateway(conn):
    return conn.execute("SELECT gateway_id FROM kanban_sync_state").fetchone()[0]


def as_pre_origin_replay(hub, tid, holder="worker-3", run_uid=None, claim_run_id=None):
    """The hub before origins were recorded: no origin on the card's events, no uid in the completion, plus the
    Board's lease of the completing run (or ``claim_run_id``) to ``holder``."""
    event_id, _ = payload_of(hub, tid)
    rewrite(hub, event_id, lambda p: (p.pop("pr_acceptance_event_uid", None),
                                      p.update(pr_acceptance_event_id=p["pr_acceptance_event_id"] + 100000)))
    run_id, uid = hub.execute("SELECT r.id, r.uid FROM task_runs r WHERE r.id = ?",
                              (claim_run_id or hub.execute("SELECT run_id FROM task_events WHERE id = ?",
                                                           (event_id,)).fetchone()[0],)).fetchone()
    with kb.write_txn(hub):
        hub.execute("UPDATE task_events SET origin_gateway = NULL WHERE task_id = ?", (tid,))
        if holder is not None:
            hub.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) "
                        "VALUES (?, ?, 'claimed', ?, 1)",
                        (tid, run_id, json.dumps({"holder": holder, "run_uid": run_uid or uid, "expires": 1})))
    assert all(r[0] is None for r in hub.execute("SELECT origin_gateway FROM task_events WHERE task_id = ?", (tid,)))


def test_a_completion_replayed_before_origins_were_recorded_binds_its_leased_runs_receipt(boards):
    worker, hub = boards
    tid = completed_on_worker(worker)
    replay(worker, hub)
    assert own_gateway(hub) != "worker-3"
    as_pre_origin_replay(hub, tid)
    assert verified(hub, tid) == (True, True)


def test_a_waived_completion_replayed_before_origins_were_recorded_is_verified(boards, monkeypatch):
    """t_01a1ca1975471ed4's shape: a refused try, then a contract_waiver, both no_required_checks, one run."""
    worker, hub = boards
    monkeypatch.setattr(kpa, "collect_acceptance", lambda contract, published, assignee=None: {
        "ok": False, "classification": "no_required_checks", "head_sha": "b" * 40, "pr_url": PR,
        "pr_state": "MERGED", "checks": [], "recovery": "", "required": [], "detail": "use contract_waiver"})
    tid = kb.create_task(worker, title="Ship it", assignee="dev", completion_contract="acme/repo")
    run = kb.claim_task(worker, tid).current_run_id
    assert not kb.complete_task(worker, tid, summary="landed", expected_run_id=run, metadata={"published_pr": PR})
    assert kb.complete_task(worker, tid, summary="landed", expected_run_id=run,
                            metadata={"published_pr": PR, "contract_waiver": {"reason": "no required checks"}})
    replay(worker, hub)
    as_pre_origin_replay(hub, tid)
    assert verified(hub, tid) == (True, True)


@pytest.mark.parametrize("lease", ["own", "none", "other_run_uid", "another_run", "non_str_holder", "unknown_own"])
def test_a_null_origin_completion_without_a_foreign_lease_of_its_run_stays_exact(boards, lease):
    """A run this gateway held, no Board lease, a lease naming another run, a lease of another run, a malformed
    holder, or a board that cannot name itself: the wrong id stays wrong."""
    worker, hub = boards
    tid = completed_on_worker(worker)
    replay(worker, hub)
    holder = {"own": own_gateway(hub), "none": None, "non_str_holder": 7}.get(lease, "worker-3")
    other = hub.execute("SELECT id FROM task_runs WHERE task_id != ? ORDER BY id LIMIT 1", (tid,)).fetchone()[0]
    as_pre_origin_replay(hub, tid, holder=holder,
                         run_uid="01a1d000-0000-7000-8000-000000000001" if lease == "other_run_uid" else None,
                         claim_run_id=other if lease == "another_run" else None)
    if lease == "unknown_own":
        with kb.write_txn(hub):
            hub.execute("UPDATE kanban_sync_state SET gateway_id = ''")
    assert verified(hub, tid) == (False, False)


def test_a_lease_naming_no_run_uid_of_a_run_without_one_stays_exact(boards):
    worker, hub = boards
    tid = completed_on_worker(worker)
    replay(worker, hub)
    as_pre_origin_replay(hub, tid, holder=None)
    event_id, _ = payload_of(hub, tid)
    run_id = hub.execute("SELECT run_id FROM task_events WHERE id = ?", (event_id,)).fetchone()[0]
    with kb.write_txn(hub):
        hub.execute("UPDATE task_runs SET uid = NULL WHERE id = ?", (run_id,))
        hub.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, 'claimed', ?, 1)",
                    (tid, run_id, json.dumps({"holder": "worker-3"})))
    assert hub.execute("SELECT uid FROM task_runs WHERE id = ?", (run_id,)).fetchone()[0] is None
    assert verified(hub, tid) == (False, False)


def test_two_gateways_leasing_one_run_is_ambiguous(boards):
    worker, hub = boards
    tid = completed_on_worker(worker)
    replay(worker, hub)
    as_pre_origin_replay(hub, tid)
    event_id, _ = payload_of(hub, tid)
    run_id, uid = hub.execute("SELECT r.id, r.uid FROM task_events e JOIN task_runs r ON r.id = e.run_id "
                              "WHERE e.id = ?", (event_id,)).fetchone()
    with kb.write_txn(hub):
        hub.execute("INSERT INTO task_events (task_id, run_id, kind, payload, created_at) VALUES (?, ?, 'claimed', ?, 1)",
                    (tid, run_id, json.dumps({"holder": "worker-4", "run_uid": uid})))
    assert verified(hub, tid) == (False, False)
