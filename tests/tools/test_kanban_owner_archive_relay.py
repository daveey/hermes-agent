"""kanban_archive of a relayed capability owner card from a Board worker node is forwarded home. [daveey]

On a ``relay=home`` route the owner card is opened on the waiting card's home gateway (``_local_owner``) and
exists only there. From the waiting card's own run on the worker node, ``kanban_archive`` read the local board
and answered ``no such task`` before the owner-card scope rule ran. Now the archive is forwarded through an
event on the waiting card, the home applies the same scope rule, and the worker returns its answer verbatim.

Two board files stand in for the two gateways. The Board round trip (worker -> home -> worker) is simulated in
the worker's wait: request events are copied to the home, the home's dispatcher step runs, results come back.
"""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_capability_routing as kcr
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_owner_archive_relay as oar
from hermes_cli.kanban_db_connect import connect

NEED = "host:macbook"


@pytest.fixture
def gateways(tmp_path, monkeypatch):
    hermes = tmp_path / ".hermes"
    hermes.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(hermes))
    monkeypatch.setenv("HOME", str(tmp_path))
    home_db, worker_db = tmp_path / "macbook.db", hermes / "kanban.db"
    home, worker = connect(home_db), connect(worker_db)
    monkeypatch.setattr(kcr, "_own_gateway", lambda conn: None)
    monkeypatch.setenv("HERMES_KANBAN_DB", str(worker_db))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", "default")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "192")
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    # Only the home answers: the worker's copy of a card has its home elsewhere.
    monkeypatch.setattr(oar, "_home_of", lambda conn, tid: None if conn is home else "macbook")

    def round_trip(_seconds):
        _copy_events(worker, home, oar.REQUEST_KIND)
        oar.apply_requests(home)
        _copy_events(home, worker, oar.RESULT_KIND)

    monkeypatch.setattr(oar, "_sleep", round_trip)
    yield home, worker, home_db
    home.close()
    worker.close()


def _copy_events(src, dst, kind):
    """The Board carrying events of ``kind`` from one gateway's board file to the other's (once each)."""
    have = {r[0] for r in dst.execute("SELECT payload FROM task_events WHERE kind = ?", (kind,))}
    for task_id, payload, created_at in src.execute(
            "SELECT task_id, payload, created_at FROM task_events WHERE kind = ? ORDER BY id", (kind,)).fetchall():
        if payload not in have:
            with kb.write_txn(dst):
                dst.execute("INSERT INTO task_events (task_id, kind, payload, created_at) VALUES (?, ?, ?, ?)",
                            (task_id, kind, payload, created_at))


def _copy_card(home, worker, home_db, tid):
    """The worker node's copy of a home card (same id), as Board pickup imports it."""
    if kb.get_task(worker, tid) is not None:
        return
    worker.execute("ATTACH DATABASE ? AS h", (str(home_db),))
    try:
        with kb.write_txn(worker):
            worker.execute("INSERT INTO main.tasks SELECT * FROM h.tasks WHERE id = ?", (tid,))
            worker.execute("INSERT INTO main.task_events (task_id, kind, payload, created_at) "
                           "SELECT task_id, kind, payload, created_at FROM h.task_events WHERE task_id = ?", (tid,))
    finally:
        worker.execute("DETACH DATABASE h")


def _relayed(home, worker, home_db, monkeypatch, *, creator=None):
    """A waiting card on the home, run by the worker node, whose need the home routed to a local owner card.

    Returns (waiting, owner, requester). ``creator`` makes the waiting card a probe that card created.
    """
    requester = kb.create_task(home, title="live verify") if creator is None else creator
    waiting = requester if creator is None else kb.create_task(home, title="PROBE", creator_task_id=creator)
    for tid in {requester, waiting}:
        _copy_card(home, worker, home_db, tid)
    rid = kcr.request_id(waiting, NEED)
    relay = {"routes": [{"need": NEED, "gateway": "macbook", "checked_at": None, "request_id": rid, "generation": 1,
                         "relay": kcr.RELAY_HOME, "relayed_at": 1, "from_gateway": "worker-1"}],
             "local": [], "self_gateway": "worker-1", "relay": kcr.RELAY_HOME, "reason": "run sw_vers"}
    for conn in (worker, home):  # _park_relay on the worker; the Board carries it home
        with kb.write_txn(conn):
            kb._append_event(conn, waiting, kcr.ROUTED_EVENT, relay)
    kcr._local_owner(home, kb.get_task(home, waiting), {"generation": 1}, need=NEED, rid=rid, self_gw="macbook",
                     from_gw="worker-1", reason="run sw_vers")
    owner = home.execute("SELECT id FROM tasks WHERE idempotency_key = ?", (rid,)).fetchone()[0]
    _copy_events(home, worker, kcr.ROUTED_EVENT)  # the owner_task route reaches the worker's copy
    assert kb.get_task(worker, owner) is None  # the owner card is only on the home
    monkeypatch.setenv("HERMES_KANBAN_TASK", requester)
    return waiting, owner, requester


def _archive(tid):
    from tools import kanban_tools as kt

    return json.loads(kt._handle_archive({"task_id": tid}))


def _archived_payload(conn, tid):
    row = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = 'archived' "
                       "ORDER BY id DESC LIMIT 1", (tid,)).fetchone()
    return json.loads(row[0]) if row and row[0] else None


def test_worker_node_archive_of_relayed_done_owner_is_forwarded_and_applied_home(gateways, monkeypatch):
    home, worker, home_db = gateways
    waiting, owner, requester = _relayed(home, worker, home_db, monkeypatch)
    assert kb.complete_task(home, owner, summary="sw_vers: 26.1")
    out = _archive(owner)
    assert out.get("ok") is True, out
    assert out.get("status") == "archived"
    assert kb.get_task(home, owner).status == "archived"
    event = _archived_payload(home, owner) or {}
    assert event.get("forwarded_task") == requester
    assert event.get("forwarded_run_id") == 192
    assert event.get("via", "").startswith(f"capability owner of {waiting} by {requester}")


def test_worker_node_archive_of_its_probes_relayed_owner_is_forwarded(gateways, monkeypatch):
    home, worker, home_db = gateways
    parent = kb.create_task(home, title="live verify")
    _copy_card(home, worker, home_db, parent)
    _waiting, owner, _ = _relayed(home, worker, home_db, monkeypatch, creator=parent)
    assert _archive(owner).get("ok") is True
    assert kb.get_task(home, owner).status == "archived"


def test_claimed_relayed_owner_is_refused_with_the_homes_text(gateways, monkeypatch):
    home, worker, home_db = gateways
    _waiting, owner, _ = _relayed(home, worker, home_db, monkeypatch)
    assert kb.claim_task(home, owner) is not None
    out = _archive(owner)
    assert out.get("ok") is not True
    assert "is claimed" in json.dumps(out)
    assert kb.get_task(home, owner).status == "running"


def test_another_cards_relayed_owner_is_not_forwarded(gateways, monkeypatch):
    home, worker, home_db = gateways
    _waiting, owner, _ = _relayed(home, worker, home_db, monkeypatch)
    other = kb.create_task(worker, title="unrelated worker task")
    monkeypatch.setenv("HERMES_KANBAN_TASK", other)
    assert kb.complete_task(home, owner, summary="done")
    out = _archive(owner)
    assert out.get("ok") is not True
    assert "no such task" in json.dumps(out)
    assert kb.get_task(home, owner).status == "done"
    assert not worker.execute("SELECT 1 FROM task_events WHERE kind = ?", (oar.REQUEST_KIND,)).fetchone()


def test_home_refuses_a_forged_request_for_another_cards_owner(gateways, monkeypatch):
    """The home re-checks scope: a request riding on card A for B's owner card is refused there."""
    home, worker, home_db = gateways
    _a, _owner_a, requester = _relayed(home, worker, home_db, monkeypatch)
    b = kb.create_task(home, title="other waiting card")
    rid_b = kcr.request_id(b, NEED)
    kcr._local_owner(home, kb.get_task(home, b), {"generation": 1}, need=NEED, rid=rid_b, self_gw="macbook",
                     from_gw="worker-1", reason="x")
    owner_b = home.execute("SELECT id FROM tasks WHERE idempotency_key = ?", (rid_b,)).fetchone()[0]
    assert kb.complete_task(home, owner_b, summary="done")
    route = {"waiting": requester, "gateway": "macbook", "request_id": kcr.request_id(requester, NEED)}
    out = oar.forward(worker, route, owner_b, requester=requester, run_id=192, wait_s=5)
    assert out.get("ok") is False, out
    assert "not a capability owner card of" in out["error"]
    assert kb.get_task(home, owner_b).status == "done"
