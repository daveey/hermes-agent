"""Capability routing from a Board worker node relays to the card's home gateway. [daveey]

Replays MacBook card t_01a18b13ee350599 (2026-10-29 21:35 PDT): its run on worker-1 needed a MacBook-only step,
``kanban_block(needs=['host:macbook'])`` answered ``no_capability_holder`` because worker-1 runs no hermes-hub sync
(no ``hub.db`` replica, no capability registry, no ``create-task``), and the worker asked David to run the tests.
Now the worker parks the card on the same owner request id and the home gateway, which has the registry,
opens the owner card where the capability is held. Two gateways on the in-memory fake Board.
"""
from __future__ import annotations

import json
import sqlite3
import time

import pytest

from hermes_cli import kanban_capability_registry as reg
from hermes_cli import kanban_capability_routing as kcr
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli.kanban_sync_fake_board import FakeService
from tests.hermes_cli.test_kanban_worker_node import BOARD, Gateway

HOME, WORKER = "macbook", "worker-1"
NEED = "host:macbook"
REASON = ("Run the 8 hermes-sync drain test files with the Mac's /usr/bin/python3 at 8a00314ae7 and record "
          "pass/fail per file.")
HUB_SCHEMA = """
CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT);
CREATE TABLE replica (gw TEXT, coll TEXT, key TEXT, version INTEGER, seq INTEGER, data TEXT,
  updated_at INTEGER, PRIMARY KEY (gw, coll, key));
CREATE TABLE sent (id TEXT PRIMARY KEY, body TEXT, state TEXT, result TEXT, created_at REAL,
  done_at REAL, posted INTEGER DEFAULT 0);
"""


class Gw(Gateway):
    """A gateway plus its hermes-hub side: a mail identity, and (unless a worker node) a hub.db replica."""

    def __init__(self, root, name, svc, monkeypatch, *, pickup, hub_replica):
        self.root = root / name
        super().__init__(root, name, svc, monkeypatch, pickup=pickup)
        mail = self.root / "mail.json"
        mail.write_text(json.dumps({"gateway": name}))
        self.state = self.root / "hub-state"
        self.state.mkdir()
        self.hub = None
        if hub_replica:
            self.hub = sqlite3.connect(self.state / "hub.db")
            self.hub.executescript(HUB_SCHEMA)
            self.hub.execute("INSERT INTO meta VALUES ('gateway', ?)", (name,))
            self.hub.commit()
        self.enter()

    def enter(self):
        conn = super().enter()
        if hasattr(self, "state"):
            self.mp.setenv("HERMES_MAIL_CONFIG", str(self.root / "mail.json"))
            self.mp.setenv("HERMES_HUB_STATE", str(self.state))
            self.mp.setenv("HERMES_KANBAN_DB", str(self.home / "kanban.db"))
        return conn

    def holds(self, *caps, ok=True, at=None):
        """This gateway's own probe results (``<kanban home>/kanban/capabilities.json``)."""
        self.enter()
        at = time.time() if at is None else at
        path = reg.registry_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps({"schema": reg.SCHEMA, "gateway": self.name, "refreshed_at": at,
                                    "capabilities": [{"name": c, "ok": ok, "checked_at": at} for c in caps]}))

    def peer_holds(self, gw, *caps, at=None):
        """Another gateway's capabilities record in this gateway's hermes-hub replica."""
        at = time.time() if at is None else at
        rec = {"schema": reg.SCHEMA, "gateway": gw, "refreshed_at": at,
               "capabilities": [{"name": c, "ok": True, "checked_at": at} for c in caps]}
        self.hub.execute("INSERT OR REPLACE INTO replica VALUES (?, 'capabilities', 'info', 1, 1, ?, ?)",
                         (gw, json.dumps(rec), int(at)))
        self.hub.commit()


@pytest.fixture
def pair(tmp_path, monkeypatch):
    svc = FakeService(lease_runs=True)
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)
    sync.consume_dispatcher_wake()
    home = Gw(tmp_path, HOME, svc, monkeypatch, pickup=False, hub_replica=True)
    worker = Gw(tmp_path, WORKER, svc, monkeypatch, pickup="explicit", hub_replica=False)
    home.holds(NEED)
    worker.holds("sudo:nopasswd")
    calls: list[list[str]] = []

    def fake_hub(argv):  # `hermes-hub create-task` on the home: journal the request in its `sent`
        calls.append(argv)
        rid = argv[argv.index("--id") + 1]
        body = {"id": rid, "to_gw": argv[2], "coll": "tasks", "key": "default/default/new", "action": "create",
                "args": {"title": argv[3], "body": argv[argv.index("--body") + 1]}}
        home.hub.execute("INSERT OR IGNORE INTO sent (id, body, state, created_at) VALUES (?, ?, 'local', ?)",
                         (rid, json.dumps(body), time.time()))
        home.hub.commit()
        return 0, rid

    monkeypatch.setattr(kcr, "hub_runner", fake_hub)
    yield home, worker, svc, calls
    for g in (home, worker):
        g.conn.close()
        if g.hub is not None:
            g.hub.close()
    sync.set_lease_client(None)
    wn.set_registry_fetch(None)


def _worker_runs_home_card(home, worker):
    home.enter()
    tid = kb.create_task(home.conn, title="Drain fix: run the Mac half of the hermes-sync tests",
                         assignee="default", runs_on=[f"machine:{WORKER}"])
    home.round()
    worker.round()
    worker.tick()
    task = kb.get_task(worker.conn, tid)
    assert task is not None and task.status == "running"
    return tid, task.current_run_id


def _hand_back(home, worker):
    """The worker pushes the park and evicts its idle copy; the home pulls it."""
    worker.round()
    worker.tick()
    worker.round()
    home.round()


def test_worker_node_needs_relays_home_and_opens_the_owner_card_on_the_holder(pair):
    home, worker, _svc, calls = pair
    tid, run_id = _worker_runs_home_card(home, worker)

    # On worker-1 (no hub.db): before the fix this raised no_capability_holder.
    worker.enter()
    assert not reg.hub_db_path().exists()
    res = kcr.route(worker.conn, tid, [NEED], reason=REASON, expected_run_id=run_id)
    rid = kcr.request_id(tid, NEED)
    assert res["status"] == "scheduled"
    assert [(r["need"], r["gateway"], r["request_id"]) for r in res["routes"]] == [(NEED, HOME, rid)]
    assert res["wake_check"] == f"hermes kanban wait-check capability-owner {rid}"
    assert kb.get_task(worker.conn, tid).status == "scheduled"
    # The worker's own probe run says "relayed", never an error that would wake the card.
    rc, msg = kcr.owner_status(rid)
    assert rc == 1 and "relayed to macbook" in msg

    _hand_back(home, worker)
    assert kb.get_task(worker.conn, tid) is None  # evicted: the home now runs the wake probe
    home.enter()
    assert kb.get_task(home.conn, tid).status == "scheduled"
    # [daveey] The park names the home as its probe host; the home's sweep registers the probe from the event.
    from hermes_cli import kanban_wake_check as kwc
    parks = [e for e in kb.list_events(home.conn, tid) if e.kind == "scheduled"]
    assert parks[-1].payload["probe_host"] == HOME
    assert kwc.restore_missing_checks(home.conn)["restored"] == [tid]
    assert kwc.get_state(home.conn, tid)["cmd"] == f"hermes kanban wait-check capability-owner {rid}"

    # The home's wake probe routes it. The home holds host:macbook itself: the owner card is a local card
    # pinned to this machine, keyed by the same request id. No hermes-hub request is needed for that.
    rc, msg = kcr.wait_check(rid)
    assert rc == 1, msg
    owner = home.conn.execute("SELECT id, status, assignee, body, runs_on FROM tasks WHERE idempotency_key = ?",
                              (rid,)).fetchone()
    assert owner is not None and owner["status"] in ("ready", "todo")
    assert json.loads(owner["runs_on"]) == [f"machine:{HOME}"]
    assert tid in owner["body"] and REASON in owner["body"] and NEED in owner["body"]
    assert calls == []
    # Probing again is idempotent: still pending, still one owner card.
    assert kcr.wait_check(rid)[0] == 1
    assert home.conn.execute("SELECT COUNT(*) FROM tasks WHERE idempotency_key = ?", (rid,)).fetchone()[0] == 1

    # The owner finishes on the MacBook: the waiting card's probe passes and the card wakes.
    assert kb.complete_task(home.conn, owner["id"], result="90 tests passed")
    rc, msg = kcr.wait_check(rid)
    assert rc == 0 and owner["id"] in msg


def test_relayed_need_held_by_a_third_gateway_is_sent_through_the_homes_hermes_hub(pair):
    home, worker, _svc, calls = pair
    need = "aws:767406518141:admin"
    home.peer_holds("hub", need)
    tid, run_id = _worker_runs_home_card(home, worker)
    worker.enter()
    res = kcr.route(worker.conn, tid, [need], reason=REASON, expected_run_id=run_id)
    rid = res["routes"][0]["request_id"]
    _hand_back(home, worker)
    home.enter()
    rc, msg = kcr.wait_check(rid)
    assert rc == 1, msg
    [argv] = calls
    assert argv[1:3] == ["create-task", "hub"] and argv[argv.index("--id") + 1] == rid
    assert tid in argv[argv.index("--body") + 1]
    # From here on it is an ordinary owner request on the home's hermes-hub.
    home.hub.execute("UPDATE sent SET state='done', result=? WHERE id=?", (json.dumps({"task": "t_owner"}), rid))
    home.hub.execute("INSERT INTO replica VALUES ('hub', 'tasks', 'default/default/t_owner', 1, 1, ?, 0)",
                     (json.dumps({"id": "t_owner", "status": "done"}),))
    home.hub.commit()
    assert kcr.wait_check(rid)[0] == 0
    assert len(calls) == 1


def test_a_briefly_stale_holder_is_retried_then_fails_typed(pair, monkeypatch):
    """The holder restarted (its record lapsed): the probe keeps retrying, then wakes the card with the
    typed reason instead of leaving it parked forever."""
    home, worker, _svc, _calls = pair
    home.holds(NEED, at=time.time() - reg.DEFAULT_STALE_AFTER_SECONDS - 300)
    tid, run_id = _worker_runs_home_card(home, worker)
    worker.enter()
    rid = kcr.route(worker.conn, tid, [NEED], reason=REASON, expected_run_id=run_id)["routes"][0]["request_id"]
    _hand_back(home, worker)
    home.enter()
    rc, msg = kcr.wait_check(rid)
    assert rc == 1 and "retrying" in msg
    # The record comes back within the window: the next probe routes it.
    home.holds(NEED)
    assert kcr.wait_check(rid)[0] == 1
    assert home.conn.execute("SELECT 1 FROM tasks WHERE idempotency_key = ?", (rid,)).fetchone()


def test_a_relay_that_finds_no_holder_wakes_the_card_with_the_typed_reason(pair, monkeypatch):
    home, worker, _svc, _calls = pair
    tid, run_id = _worker_runs_home_card(home, worker)
    worker.enter()
    rid = kcr.route(worker.conn, tid, ["ssh:nowhere"], reason=REASON,
                    expected_run_id=run_id)["routes"][0]["request_id"]
    _hand_back(home, worker)
    home.enter()
    real = time.time
    monkeypatch.setattr(kcr.time, "time", lambda: real() + kcr.RELAY_RETRY_S + 60)
    rc, msg = kcr.wait_check(rid)
    assert rc == 0 and "no_capability_holder" in msg
    # The next worker run asking again gets the typed refusal, not a second relay.
    assert kcr.wait_check(rid)[0] == 0
    with kb.write_txn(home.conn):
        home.conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    with pytest.raises(kcr.CapabilityRouteError) as exc:
        kcr.route(home.conn, tid, ["ssh:nowhere"], reason=REASON)
    assert exc.value.code == "no_capability_holder"


def test_a_worker_nodes_own_card_gets_a_typed_registry_error(pair):
    """No home to relay to: the error says the registry is unavailable here, not that nobody holds it."""
    _home, worker, _svc, _calls = pair
    worker.enter()
    tid = kb.create_task(worker.conn, title="worker's own", assignee="default")
    with kb.write_txn(worker.conn):
        worker.conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    with pytest.raises(kcr.CapabilityRouteError) as exc:
        kcr.route(worker.conn, tid, [NEED], reason=REASON)
    assert exc.value.code == "capability_registry_unavailable"


def test_on_a_hub_gateway_a_stale_holder_parks_and_retries_instead_of_failing(pair):
    """The hub-side path of the 21:36 restart window: the holder's record lapsed while it restarted."""
    home, _worker, _svc, calls = pair
    need = "aws:767406518141:admin"
    home.peer_holds("hub", need, at=time.time() - reg.DEFAULT_STALE_AFTER_SECONDS - 300)
    home.enter()
    tid = kb.create_task(home.conn, title="home card", assignee="default")
    with kb.write_txn(home.conn):
        home.conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    res = kcr.route(home.conn, tid, [need], reason=REASON)
    rid = res["routes"][0]["request_id"]
    assert res["status"] == "scheduled" and res["relay"] == kcr.RELAY_RETRY and calls == []
    rc, msg = kcr.wait_check(rid)
    assert rc == 1 and "retrying" in msg and calls == []
    home.peer_holds("hub", need)  # the holder is live-checked again
    assert kcr.wait_check(rid)[0] == 1
    [argv] = calls
    assert argv[1:3] == ["create-task", "hub"] and argv[argv.index("--id") + 1] == rid


def test_placement_on_a_worker_node_names_the_relay_instead_of_a_dead_target(pair):
    from hermes_cli import kanban_placement as kp
    _home, worker, _svc, _calls = pair
    worker.enter()
    why = kp._target_not_live_why("macbook", "no recent hermes-hub sync")
    assert "Board worker node" in why and "needs=" in why
