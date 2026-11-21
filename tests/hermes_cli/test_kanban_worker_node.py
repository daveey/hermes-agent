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
from tests.hermes_cli._placement_helpers import seen_now
from tests.hermes_cli.kanban_sync_fake_board import FakeService

HUB, WORKER, BOARD = "hub", "worker-1", "default"
MACHINES = {
    HUB: {"name": HUB, "state": "active", "labels": ["linux"], "pinned_only": False, "max_card_runs": None,
          "hold": False},
    # worker-1 runs with kanban.sync.pickup on and advertises it; hub and macbook say nothing (= no pickup).
    WORKER: {"name": WORKER, "state": "active", "labels": ["linux"], "pinned_only": False, "max_card_runs": 12,
             "hold": False, "pickup": "all"},
    "macbook": {"name": "macbook", "state": "active", "labels": ["macos"], "pinned_only": True,
                "max_card_runs": None, "hold": False},
}


class Gateway:
    """One gateway's kanban root (HERMES_HOME) with a primary ``default`` board, on a shared fake service."""

    def __init__(self, root, name, svc, monkeypatch, *, pickup):
        self.home, self.name, self.svc, self.mp = root / name / ".hermes", name, svc, monkeypatch
        self.home.mkdir(parents=True)
        self.enter()
        from tests.hermes_cli.test_kanban_managed_pin_policy import _managed_yaml
        (self.home / "config.yaml").write_text(
            "kanban:\n  sync:\n    enabled: true\n" + (f"    pickup: {pickup}\n" if pickup else "")
            + _managed_yaml())
        self.conn = kbc.connect(self.home / "kanban.db")
        ksc.ensure_identity(self.conn, BOARD, name)
        sync.set_mode(self.conn, "primary")

    def enter(self):
        self.mp.setenv("HERMES_HOME", str(self.home))
        sync.set_lease_client(ksc.HubLeaseClient(self.svc.hub(self.name)))
        wn.set_registry_fetch(lambda: {"me": self.name, "machines": seen_now(MACHINES.values())})
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


def test_tool_create_runs_on_and_placement_to_an_enrolled_machine(gws, monkeypatch, caplog):
    hub, _ = gws
    hub.enter()
    monkeypatch.setattr("hermes_cli.kanban_capability_registry.self_gateway", lambda: HUB)
    with caplog.at_level("INFO", logger="tools.kanban_tools_placement"):
        out = _tool_create({"title": "on the worker", "assignee": "default", "placement": WORKER})
    assert out.get("ok", True) and "error" not in out, out
    tid = out["task_id"]
    row = hub.conn.execute("SELECT runs_on, created_on FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert json.loads(row["runs_on"]) == ["machine:worker-1"]  # created here, runs only there
    # The response says so, so a caller doesn't read the pin as ignored and re-create the card.
    assert out["runs_on"] == ["machine:worker-1"]
    why = f"placement pinned to machine {WORKER} (runs there through the Board lease)"
    assert out["placement"]["why"] == why
    assert out["placement"] == {"where": "local", "runs_on": ["machine:worker-1"], "machine": WORKER, "why": why}
    assert f"kanban placement: local, runs_on ['machine:worker-1'] ({why})" in caplog.messages
    assert "nothing in the card names a machine-only need" not in out["note"]
    assert "runs on " + WORKER + " via the board lease" in out["note"] and tid in out["note"]
    out = _tool_create({"title": "explicit", "assignee": "default", "runs_on": ["linux"]})
    # No machine pin: no machine placement block, only the plain local reason every local card carries.
    assert out["runs_on"] == ["linux"] and "machine" not in out.get("placement", {})
    assert out["placement"] == {"where": "local", "why": "no placement route for this gateway"}
    assert json.loads(hub.conn.execute("SELECT runs_on FROM tasks WHERE id = ?",
                                       (out["task_id"],)).fetchone()[0]) == ["linux"]


def _route_self_to_hub(monkeypatch, worker, *, hub_pickup=None):
    """worker-1 routes kanban_create to gateway `hub` (kanban.placement.route), and `hub` is also an active
    machine in the registry: the MacBook's setup on 10-28."""
    worker.enter()
    from hermes_cli import kanban_placement as kp
    entry = {**MACHINES[HUB], **({"pickup": hub_pickup} if hub_pickup is not None else {})}
    wn.set_registry_fetch(lambda: {"me": WORKER, "machines": [entry, MACHINES[WORKER], MACHINES["macbook"]]})
    monkeypatch.setattr("hermes_cli.kanban_capability_registry.self_gateway", lambda: WORKER)
    monkeypatch.setattr(kp, "config", lambda: {"route": {WORKER: HUB}})
    monkeypatch.setattr(kp, "target_live", lambda *a, **k: None)
    sent = []
    monkeypatch.setattr(kp, "create_remote", lambda view, **kw: sent.append(kw["target"]) or {
        "ok": True, "request_id": "req-1", "queued_only": True})
    return sent


@pytest.mark.parametrize("hub_pickup", [None, False, "off"])
def test_a_routed_gateway_pin_to_a_machine_without_pickup_takes_the_gateway_route(gws, monkeypatch, hub_pickup):
    """MacBook 10-28: placement='hub' became runs_on=[machine:hub] on the MacBook board, and the hub (pickup off)
    never took it. A machine that does not advertise pickup gets no runs_on card; the routed pin goes through
    _place_remote as before 6d29976569."""
    _, worker = gws
    sent = _route_self_to_hub(monkeypatch, worker, hub_pickup=hub_pickup)
    before = worker.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0]
    out = _tool_create({"title": "for the hub", "assignee": "default", "placement": HUB})
    assert sent == [HUB], out
    assert out["placed"]["gateway"] == HUB and "runs_on" not in out, out
    assert out["runs_where"] == HUB
    assert worker.conn.execute("SELECT COUNT(*) FROM tasks").fetchone()[0] == before  # nothing stranded here


def test_a_routed_gateway_pin_to_a_machine_with_pickup_still_gets_runs_on(gws, monkeypatch):
    _, worker = gws
    sent = _route_self_to_hub(monkeypatch, worker, hub_pickup="explicit")
    out = _tool_create({"title": "for the hub", "assignee": "default", "placement": HUB})
    assert sent == [] and out["runs_on"] == ["machine:hub"], out
    assert json.loads(worker.conn.execute("SELECT runs_on FROM tasks WHERE id = ?",
                                          (out["task_id"],)).fetchone()[0]) == ["machine:hub"]
    assert "machine:hub" in out["runs_where"]


def test_placement_runs_on_needs_advertised_pickup(gws):
    hub, _ = gws
    hub.enter()
    assert wn.placement_runs_on(hub.conn, WORKER) == ["machine:worker-1"]
    assert wn.placement_runs_on(hub.conn, "macbook") is None  # active, but no pickup field


def test_a_plain_local_create_says_it_runs_here(gws, monkeypatch):
    hub, _ = gws
    hub.enter()
    monkeypatch.setattr("hermes_cli.kanban_capability_registry.self_gateway", lambda: HUB)
    out = _tool_create({"title": "here", "assignee": "default"})
    assert out["ok"] and "placed" not in out and "runs_on" not in out, out
    assert out["runs_where"] == f"here ({HUB})"


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
    # Label-only cards carried solely by foreign pinned-only machines are now
    # refused at creation; retain the pickup exclusion proof with a legacy row.
    mac = _hub_card(hub, title="mac", runs_on=["linux"])
    hub.conn.execute("UPDATE tasks SET runs_on = ? WHERE id = ?", (json.dumps(["macos"]), mac))
    hub.conn.commit()
    hub.round()
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


def test_tool_create_sends_heavy_work_to_the_worker_by_runs_on(gws, monkeypatch):
    """kanban.placement.heavy_route: a MacBook-style gateway (here worker-1's role is played by the hub's
    registry entry for WORKER) creates a test/bench card locally with runs_on=[machine:<worker>] instead of
    queuing it on the routed gateway; light cards still take the route."""
    hub, _ = gws
    hub.enter()
    from hermes_cli import kanban_placement as kp
    monkeypatch.setattr("hermes_cli.kanban_capability_registry.self_gateway", lambda: HUB)
    monkeypatch.setattr(kp, "config", lambda: {"route": {HUB: "elsewhere"}, "heavy_route": {HUB: WORKER}})
    monkeypatch.setattr(kp, "target_live", lambda *a, **k: None)
    sent = []
    monkeypatch.setattr(kp, "create_remote", lambda view, **kw: sent.append(kw["target"]) or {
        "ok": True, "request_id": "req-1", "queued_only": True})
    out = _tool_create({"title": "Fix the re-wake loop + regression test", "assignee": "default"})
    assert sent == [] and out["runs_on"] == ["machine:worker-1"], out
    assert out["placement"]["runs_on"] == ["machine:worker-1"] and out["placement"]["why"].startswith("heavy work")
    assert json.loads(hub.conn.execute("SELECT runs_on FROM tasks WHERE id = ?",
                                       (out["task_id"],)).fetchone()[0]) == ["machine:worker-1"]
    assert "machine:worker-1" in out["runs_where"]
    out = _tool_create({"title": "Summarise the failing CI jobs", "assignee": "default"})
    assert sent == ["elsewhere"] and out["placed"]["gateway"] == "elsewhere", out


def test_tool_create_heavy_route_works_when_the_registry_has_no_pickup_field(gws, monkeypatch):
    """Production 10-28: GET /v2/machines entries carry labels/state but no ``pickup``."""
    hub, _ = gws
    hub.enter()
    from hermes_cli import kanban_placement as kp
    wn.set_registry_fetch(lambda: {"me": HUB, "machines": seen_now([
        {k: v for k, v in MACHINES[m].items() if k != "pickup"} for m in (HUB, WORKER, "macbook")])})
    monkeypatch.setattr("hermes_cli.kanban_capability_registry.self_gateway", lambda: HUB)
    monkeypatch.setattr(kp, "config", lambda: {"route": {HUB: "elsewhere"}, "heavy_route": {HUB: WORKER}})
    monkeypatch.setattr(kp, "target_live", lambda *a, **k: None)
    monkeypatch.setattr(kp, "create_remote", lambda view, **kw: {"ok": True, "request_id": "r", "queued_only": True})
    out = _tool_create({"title": "Run the bench-v2 benchmark", "assignee": "default"})
    assert out["runs_on"] == ["machine:worker-1"], out


def test_client_publishes_pickup_mode_once_per_round_when_it_changed(gws, monkeypatch):
    """The sync client posts pickup_mode() (None → null) once per sync round, and only when it changed."""
    hub_gw, worker = gws
    posted: list = []

    class PublishingHub:
        def __init__(self, gateway):
            self.ident = ksc.Identity(url="https://board.test", key="k" * 16, gateway=gateway)

        def report_pickup(self, pickup):
            posted.append((self.ident.gateway, pickup))
            return {"pickup": pickup}

    ksc.reset_published_pickup()
    worker.enter()  # kanban.sync.pickup: true → pickup_mode() == "all"
    assert wn.pickup_mode() == "all"
    assert ksc.publish_pickup_if_changed(PublishingHub(WORKER)) is True
    assert ksc.publish_pickup_if_changed(PublishingHub(WORKER)) is False  # unchanged: no second post
    hub_gw.enter()  # pickup off → None, posted as null
    assert wn.pickup_mode() is None
    assert ksc.publish_pickup_if_changed(PublishingHub(HUB)) is True
    assert posted == [(WORKER, "all"), (HUB, None)]
    assert json.dumps({"pickup": posted[-1][1]}) == '{"pickup": null}'

    calls = []
    client = ksc.Hub(ksc.Identity(url="https://board.test", key="k" * 16, gateway=WORKER))
    monkeypatch.setattr(client, "_call", lambda method, path, body=None: calls.append((method, path, body)) or {})
    client.report_pickup(None)
    assert calls == [("POST", "/v2/machines/me", {"pickup": None})]

    seen = []
    monkeypatch.setattr(ksc, "publish_pickup_if_changed", lambda hub: seen.append(hub.ident.gateway) or False)
    worker.enter()
    ksc.sync_all_once(worker.svc.hub(WORKER))
    ksc.sync_all_once(worker.svc.hub(WORKER))
    assert seen == [WORKER, WORKER]  # once per round, not once per board


def test_pin_to_a_machine_that_published_all_gets_runs_on(gws, monkeypatch):
    """A registry entry whose machine posted pickup=all is a real pin target; one without pickup is not."""
    hub, _ = gws
    hub.enter()
    monkeypatch.setattr("hermes_cli.kanban_capability_registry.self_gateway", lambda: HUB)
    wn.set_registry_fetch(lambda: {"me": HUB, "machines": [
        {"name": WORKER, "state": "active", "labels": ["linux"], "pickup": "all"},
        {"name": "macbook", "state": "active", "labels": ["macos"], "pinned_only": True},
    ]})
    out = _tool_create({"title": "pinned to a publisher", "assignee": "default", "placement": WORKER})
    assert out["runs_on"] == ["machine:worker-1"], out
    assert json.loads(hub.conn.execute("SELECT runs_on FROM tasks WHERE id = ?",
                                        (out["task_id"],)).fetchone()[0]) == ["machine:worker-1"]
    # macbook is active and has no pickup field: the pin does not become runs_on
    assert wn.placement_runs_on(hub.conn, "macbook") is None
