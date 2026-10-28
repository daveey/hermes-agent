"""Worker nodes (RFC "Worker nodes and transcripts", Placement): a gateway with ``kanban.sync.pickup`` copies cards
another gateway created from its replica, claims them through a Board lease and runs them; the creator's own
dispatcher never requeues or counts a card running under another gateway's lease. ``runs_on`` and
``placement=<machine>`` on create. The Board service is the in-memory fake (``kanban_sync_fake_board``); the
Board's own registry and grant checks are tested in the service repo (``worker/test/machines.test.ts``)."""

from __future__ import annotations

import json
import os

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_sync_fake_board import FakeService

HUB, WORKER, BOARD = "hub", "worker-1", "default"
MACHINES = {
    HUB: {"name": HUB, "state": "active", "labels": ["linux"], "pinned_only": False, "max_card_runs": None,
          "hold": False},
    WORKER: {"name": WORKER, "state": "active", "labels": ["linux"], "pinned_only": False, "max_card_runs": 12,
             "hold": False},
    "macbook": {"name": "macbook", "state": "active", "labels": ["macos"], "pinned_only": True,
                "max_card_runs": None, "hold": False},
}


class Gateway:
    """One gateway's kanban root (HERMES_HOME) with a primary ``default`` board, on a shared fake service."""

    def __init__(self, root, name, svc, monkeypatch, *, pickup):
        self.home, self.name, self.svc, self.mp = root / name / ".hermes", name, svc, monkeypatch
        self.home.mkdir(parents=True)
        self.enter()
        (self.home / "config.yaml").write_text(
            "kanban:\n  sync:\n    enabled: true\n" + (f"    pickup: {pickup}\n" if pickup else ""))
        self.conn = kbc.connect(self.home / "kanban.db")
        ksc.ensure_identity(self.conn, BOARD, name)
        sync.set_mode(self.conn, "primary")

    def enter(self):
        self.mp.setenv("HERMES_HOME", str(self.home))
        sync.set_lease_client(ksc.HubLeaseClient(self.svc.hub(self.name)))
        wn.set_registry_fetch(lambda: {"me": self.name, "machines": list(MACHINES.values())})
        return self.conn if hasattr(self, "conn") else None

    def round(self):
        self.enter()
        return ksc.sync_board(BOARD, self.home / "kanban.db", self.svc.hub(self.name), self.name)

    def tick(self, **kw):
        self.enter()
        return kbd.dispatch_once(self.conn, spawn_fn=lambda task, ws, board=None: os.getpid(), board=BOARD, **kw)


@pytest.fixture
def svc():
    return FakeService(lease_runs=True)  # like the Board: a grant writes the run and the task's lease mirror


@pytest.fixture
def gws(tmp_path, monkeypatch, svc):
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)
    sync.consume_dispatcher_wake()
    hub = Gateway(tmp_path, HUB, svc, monkeypatch, pickup=False)
    worker = Gateway(tmp_path, WORKER, svc, monkeypatch, pickup="true")
    yield hub, worker
    for g in (hub, worker):
        g.conn.close()
    sync.set_lease_client(None)
    wn.set_registry_fetch(None)


def _runs(conn, tid):
    return conn.execute("SELECT uid, status FROM task_runs WHERE task_id = ? ORDER BY id", (tid,)).fetchall()


# --- placement rules ------------------------------------------------------------------------------------------

@pytest.mark.parametrize("card,me,want", [
    ({"runs_on": '["linux"]', "created_on": HUB}, WORKER, None),
    ({"runs_on": '["macos"]', "created_on": HUB}, WORKER, "labels_mismatch"),
    ({"runs_on": "[]", "created_on": HUB}, WORKER, None),
    ({"runs_on": '["machine:worker-1"]', "created_on": HUB}, WORKER, None),
    ({"runs_on": '["machine:hub"]', "created_on": HUB}, WORKER, "labels_mismatch"),
    ({"runs_on": '["linux"]', "created_on": HUB}, "macbook", "pinned_only"),
    ({"runs_on": '["machine:macbook"]', "created_on": HUB}, "macbook", None),
    ({"runs_on": None, "created_on": "macbook"}, "macbook", None),  # pinned_only never refuses its own cards
    ({"runs_on": '["macos"]', "created_on": "macbook"}, "macbook", None),
    ({"runs_on": None, "created_on": HUB, "workspace_kind": "scratch"}, WORKER, None),  # creator labels ⊆ mine
    ({"runs_on": None, "created_on": HUB, "workspace_kind": "worktree"}, WORKER, "labels_mismatch"),
    ({"runs_on": None, "created_on": HUB, "workspace_kind": "dir"}, WORKER, "labels_mismatch"),
    ({"runs_on": None, "created_on": "macbook", "workspace_kind": "scratch"}, WORKER, "labels_mismatch"),
    ({"runs_on": None, "created_on": WORKER}, WORKER, None),
])
def test_eligible_mirrors_the_board_rules(card, me, want):
    assert wn.eligible(card, me, MACHINES) == want


def test_a_held_machine_takes_nothing():
    held = {**MACHINES, WORKER: {**MACHINES[WORKER], "hold": True}}
    assert wn.eligible({"runs_on": "[]", "created_on": HUB}, WORKER, held) == "held"


def test_a_machine_label_is_always_its_own():
    assert wn.effective_labels({"labels": ["linux"]}, "w") == {"linux", "machine:w"}


@pytest.mark.parametrize("raw,want", [("linux", ["linux"]), ("macos,xcode", ["macos", "xcode"]), ("", []),
                                      ("[]", []), ('["machine:w"]', ["machine:w"]), (["b", "a", "a"], ["a", "b"]),
                                      (None, None)])
def test_normalize_runs_on(raw, want):
    assert wn.normalize_runs_on(raw) == want


@pytest.mark.parametrize("raw", ["Linux", "has space", ["ok", "BAD!"], 3])
def test_normalize_runs_on_refuses_bad_labels(raw):
    with pytest.raises(ValueError):
        wn.normalize_runs_on(raw)


# --- create paths ---------------------------------------------------------------------------------------------

def test_create_task_runs_on_syncs_to_the_board(gws, svc):
    hub, _ = gws
    hub.enter()
    tid = kb.create_task(hub.conn, title="linux only", assignee="default", runs_on=["linux"])
    hub.round()
    assert json.loads(svc.board(BOARD)["rows"]["tasks"][tid]["runs_on"]) == ["linux"]


def test_runs_on_on_an_unsynced_board_is_refused(tmp_path, monkeypatch):
    home = support.kanban_home(tmp_path, monkeypatch)
    conn = kbc.connect(home / "kanban.db")
    try:
        with pytest.raises(ValueError, match="syncs"):
            kb.create_task(conn, title="x", assignee="default", runs_on=["linux"])
    finally:
        conn.close()


def test_cli_create_runs_on(gws, svc):
    hub, _ = gws
    hub.enter()
    from hermes_cli import kanban as kcli
    out = kcli.run_slash('create "cli card" --assignee default --runs-on linux,machine:worker-1 --json')
    tid = json.loads(out)["id"]
    assert json.loads(hub.conn.execute("SELECT runs_on FROM tasks WHERE id = ?", (tid,)).fetchone()[0]) == [
        "linux", "machine:worker-1"]


def _tool_create(args):
    from tools import kanban_tools
    return json.loads(kanban_tools._handle_create(args))


def test_tool_create_runs_on_and_placement_to_an_enrolled_machine(gws, monkeypatch):
    hub, _ = gws
    hub.enter()
    monkeypatch.setattr("hermes_cli.kanban_capability_registry.self_gateway", lambda: HUB)
    out = _tool_create({"title": "on the worker", "assignee": "default", "placement": WORKER})
    assert out.get("ok", True) and "error" not in out, out
    tid = out["task_id"]
    row = hub.conn.execute("SELECT runs_on, created_on FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert json.loads(row["runs_on"]) == ["machine:worker-1"]  # created here, runs only there
    out = _tool_create({"title": "explicit", "assignee": "default", "runs_on": ["linux"]})
    assert json.loads(hub.conn.execute("SELECT runs_on FROM tasks WHERE id = ?",
                                       (out["task_id"],)).fetchone()[0]) == ["linux"]


def test_tool_placement_to_an_unknown_gateway_still_refuses(gws, monkeypatch):
    hub, _ = gws
    hub.enter()
    monkeypatch.setattr("hermes_cli.kanban_capability_registry.self_gateway", lambda: HUB)
    before = hub.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    out = _tool_create({"title": "nowhere", "assignee": "default", "placement": "no-such-box"})
    assert "placement_unavailable" in json.dumps(out)
    assert hub.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before


# --- pickup, run, creator side --------------------------------------------------------------------------------

def _hub_card(hub, **kw):
    hub.enter()
    tid = kb.create_task(hub.conn, title=kw.pop("title", "work"), assignee="default", **kw)
    hub.round()
    return tid


def test_worker_runs_a_hub_card_and_the_hub_never_requeues_or_counts_it(gws, svc):
    hub, worker = gws
    tid = _hub_card(hub, runs_on=["linux"])
    # The hub is at capacity this tick, so the worker gets the card.
    worker.round()
    res = worker.tick()
    assert tid in [c for c in (getattr(res, "spawned", None) or [])] or kb.get_task(worker.conn, tid).status == "running"
    lease = svc.board(BOARD)["leases"][tid]
    assert lease["holder"] == WORKER
    [(run_uid, status)] = _runs(worker.conn, tid)
    assert status == "running" and lease["run_uid"] == run_uid
    worker.round()  # push the claim
    hub.round()
    t = hub.conn.execute("SELECT status, lease_holder, claim_lock FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert (t["status"], t["lease_holder"], t["claim_lock"]) == ("running", WORKER, None)
    # The hub's sweeps leave it alone, and it does not take a hub slot.
    hub.enter()
    assert kbd.count_running_tasks(hub.conn) == 0
    assert tid not in kbd.reconcile_orphaned_running(hub.conn)
    hub.tick(stale_timeout_seconds=1)
    assert kb.get_task(hub.conn, tid).status == "running"
    assert len(svc.board(BOARD)["rows"].get("task_runs", {})) == 1
    # Nor a per-profile slot: with a cap of 1 for `default`, the hub still starts its own `default` card.
    own = kb.create_task(hub.conn, title="hub's own", assignee="default", runs_on=["machine:hub"])
    hub.tick(max_in_progress_per_profile=1)
    assert kb.get_task(hub.conn, own).status == "running"
    # The worker finishes; the hub sees the result; the worker's copy goes away.
    worker.enter()
    run_id = worker.conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    assert kb.complete_task(worker.conn, tid, result="done on the worker", expected_run_id=run_id)
    worker.round()
    hub.round()
    t = kb.get_task(hub.conn, tid)
    assert t.status == "done" and t.result == "done on the worker"
    worker.tick()
    assert kb.get_task(worker.conn, tid) is None


def test_default_placement_cards_and_pinned_cards_follow_the_rules(gws, svc):
    hub, worker = gws
    wt = _hub_card(hub, title="worktree", workspace_kind="worktree", workspace_path="/tmp/x")
    mac = _hub_card(hub, title="mac", runs_on=["macos"])
    pinned_hub = _hub_card(hub, title="hub only", runs_on=["machine:hub"])
    worker.round()
    worker.tick()
    for tid in (wt, mac, pinned_hub):
        assert kb.get_task(worker.conn, tid) is None
        assert tid not in svc.board(BOARD)["leases"]


@pytest.mark.parametrize("mode,taken", [("true", True), ("explicit", False)])
def test_explicit_pickup_takes_only_cards_with_runs_on(tmp_path, monkeypatch, svc, mode, taken):
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)
    hub = Gateway(tmp_path, HUB, svc, monkeypatch, pickup=False)
    worker = Gateway(tmp_path, WORKER, svc, monkeypatch, pickup=mode)
    try:
        default = _hub_card(hub, title="default placement")  # the Board would let worker-1 take it after home_grace
        placed = _hub_card(hub, title="placed", runs_on=["machine:worker-1"])
        worker.round()
        worker.tick()
        assert (kb.get_task(worker.conn, default) is not None) is taken
        assert kb.get_task(worker.conn, placed).status == "running"
    finally:
        hub.conn.close()
        worker.conn.close()
        sync.set_lease_client(None)
        wn.set_registry_fetch(None)


def test_pickup_is_off_unless_configured(tmp_path, monkeypatch, svc):
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)
    hub = Gateway(tmp_path, HUB, svc, monkeypatch, pickup=False)
    other = Gateway(tmp_path, "mini", svc, monkeypatch, pickup=False)
    try:
        tid = _hub_card(hub, runs_on=[])
        other.round()
        other.tick()
        assert kb.get_task(other.conn, tid) is None
    finally:
        hub.conn.close()
        other.conn.close()
        sync.set_lease_client(None)
        wn.set_registry_fetch(None)


def test_a_refused_grant_leaves_no_run_and_the_copy_is_evicted(gws, svc):
    hub, worker = gws
    tid = _hub_card(hub, runs_on=["linux"])
    worker.round()
    # Another gateway wins the lease between the replica read and the grant.
    svc.board(BOARD)["leases"][tid] = {"task_id": tid, "holder": "mini", "run_uid": "r-mini", "expires": 9e12}
    worker.tick()
    assert _runs(worker.conn, tid) == []
    t = kb.get_task(worker.conn, tid)
    assert t is None or t.status == "ready"
    b = svc.board(BOARD)
    svc._put(b, "tasks", tid, {"lease_holder": "mini", "lease_expires": 9_999_999_999_999}, "mini", None,
             f"{9_999_999_999_997:013d}-0000-board")
    worker.round()
    worker.tick()
    assert kb.get_task(worker.conn, tid) is None
    assert not [o for o in worker.conn.execute("SELECT row_uid FROM kanban_sync_outbox")]  # nothing pushed


def test_hub_and_worker_never_both_run_one_card(gws, svc):
    hub, worker = gws
    tids = [_hub_card(hub, title=f"c{i}", runs_on=["linux"]) for i in range(6)]
    worker.round()
    worker.tick()
    hub.tick()
    worker.round()
    hub.round()
    worker.tick()
    hub.tick()
    holders = {t: svc.board(BOARD)["leases"].get(t, {}).get("holder") for t in tids}
    assert all(holders.values())
    for tid in tids:
        local_runs = [conn for conn in (hub.conn, worker.conn)
                      if kb.get_task(conn, tid) is not None and _runs(conn, tid)
                      and _runs(conn, tid)[-1]["status"] == "running"
                      and conn.execute("SELECT claim_lock FROM tasks WHERE id = ?", (tid,)).fetchone()[0]]
        assert len(local_runs) == 1, tid  # exactly one gateway holds a live local claim
