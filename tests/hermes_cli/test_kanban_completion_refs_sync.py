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
