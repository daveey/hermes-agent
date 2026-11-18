"""A mandatory model pin rides the pool: enrolled, claimed by a worker that runs exactly it, refused elsewhere. [daveey]

t_01a1cefa6145100f (an Opus exact-head review, ``pin_mandatory=1``) stayed on the hub with ``uncarried option``:
pool portability reused the courier's uncarried list, though the pin is three Board row fields a pickup copies.
Here: create enrolls such a card for the worker queue only when every pool member attests
``capability:mandatory-model-pin`` (an old-core pool keeps it home), the worker that claims it records the pinned
model in its ``run_route``, a worker that cannot serve the pin leaves it on the Board, and no repair ever swaps
the pin for the profile default. The courier still cannot carry it.
"""
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_mandatory_pin as mp
from hermes_cli import kanban_placement as kp
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli.kanban_sync_fake_board import FakeService
from tests.hermes_cli.test_kanban_worker_node import BOARD, HUB, MACHINES, WORKER, Gateway

POOL = "pool:worker"
CFG = {"worker_queue": {HUB: POOL}}
PIN = {"model": "claude-opus-5-5", "provider": "anthropic", "pin_mandatory": True}
REVIEW = {"title": "Independent exact-head review: PR 1 @abc", "assignee": "default", "requires": []}


def _attest(monkeypatch, *, carries=True):
    """worker-1 is the pool; with ``carries`` its core attests the mandatory-pin refusal."""
    labels = ["linux", POOL] + ([mp.LABEL] if carries else [])
    monkeypatch.setitem(MACHINES[WORKER], "labels", labels)
    monkeypatch.setitem(MACHINES[WORKER], "last_seen", time.time() * 1000)  # a fresh attestation


@pytest.fixture
def svc():
    return FakeService(lease_runs=True, machines=MACHINES)


@pytest.fixture
def gws(tmp_path, monkeypatch, svc):
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)
    monkeypatch.setattr(kp, "config", lambda: CFG)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    sync.consume_dispatcher_wake()
    hub = Gateway(tmp_path, HUB, svc, monkeypatch, pickup=False)
    worker = Gateway(tmp_path, WORKER, svc, monkeypatch, pickup="true")
    yield hub, worker
    for g in (hub, worker):
        g.conn.close()
    sync.set_lease_client(None)
    wn.set_registry_fetch(None)


def _create(hub, **extra):
    from tools import kanban_tools
    hub.enter()
    monkey_self = "hermes_cli.kanban_capability_registry.self_gateway"
    with pytest.MonkeyPatch.context() as m:
        m.setattr(monkey_self, lambda: HUB)
        out = json.loads(kanban_tools._handle_create({**REVIEW, **extra}))
    assert out.get("ok"), out
    return out


def _runs_on(conn, tid):
    raw = conn.execute("SELECT runs_on FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    return json.loads(raw) if raw else None


def _route(conn, tid):
    rows = conn.execute("SELECT payload FROM task_events WHERE task_id = ? AND kind = 'run_route' ORDER BY id",
                        (tid,)).fetchall()
    return [json.loads(p) for (p,) in rows]


# --- create ----------------------------------------------------------------------------------------------------

def test_a_mandatory_pin_card_is_enrolled_for_the_attested_pool(gws, monkeypatch):
    hub, _ = gws
    _attest(monkeypatch)
    args = {**REVIEW, **PIN}
    hub.enter()
    with monkeypatch.context() as m:
        m.setattr("hermes_cli.kanban_capability_registry.self_gateway", lambda: HUB)
        assert kp.portable_reason(args, CFG) is None
    out = _create(hub, **PIN)
    assert sorted(_runs_on(hub.conn, out["task_id"])) == sorted([POOL, mp.LABEL]), out
    row = hub.conn.execute("SELECT model_override, provider_override, model_pin_mandatory FROM tasks WHERE id = ?",
                           (out["task_id"],)).fetchone()
    assert tuple(row) == ("claude-opus-5-5", "anthropic", 1)


def test_an_old_core_pool_keeps_the_mandatory_pin_card_home(gws, monkeypatch):
    hub, _ = gws
    _attest(monkeypatch, carries=False)  # worker-1 runs a core that would not refuse a pin it cannot serve
    hub.enter()
    with monkeypatch.context() as m:
        m.setattr("hermes_cli.kanban_capability_registry.self_gateway", lambda: HUB)
        assert kp.portable_reason({**REVIEW, **PIN}, CFG) == mp.REASON
    out = _create(hub, **PIN)
    assert _runs_on(hub.conn, out["task_id"]) is None, out
    # Without the mark the same pin is ordinary portable work, attestation or not.
    out = _create(hub, title="Summarise the failing CI jobs", model="claude-opus-5-5", provider="anthropic")
    assert _runs_on(hub.conn, out["task_id"]) == [POOL], out


# --- claim -----------------------------------------------------------------------------------------------------

@pytest.mark.parametrize("review_without_evidence", [True, False])
def test_a_worker_that_serves_the_pin_claims_it_and_runs_exactly_that_model(
        gws, svc, monkeypatch, review_without_evidence):
    hub, worker = gws
    _attest(monkeypatch)
    title = REVIEW["title"] if review_without_evidence else "Implement the change"
    tid = _create(hub, title=title, **PIN)["task_id"]
    assert sorted(_runs_on(hub.conn, tid)) == sorted([POOL, mp.LABEL])  # queued for the pool, not a default-placement copy
    hub.round()
    worker.round()
    worker.tick()
    assert svc.board(BOARD)["leases"][tid]["holder"] == WORKER
    if review_without_evidence:
        # Pool pickup carries the pin, but cannot turn unknown authorship into independence.
        assert _route(worker.conn, tid) == []
        return
    [route] = _route(worker.conn, tid)
    assert (route["model"], route["provider"]) == ("claude-opus-5-5", "anthropic"), route
    assert kb.get_task(worker.conn, tid).model_pin_mandatory == 1


def test_a_worker_that_cannot_serve_the_pin_refuses_the_claim(gws, svc, monkeypatch):
    hub, worker = gws
    _attest(monkeypatch)
    cfg = worker.home / "config.yaml"
    cfg.write_text(cfg.read_text() + "providers:\n  anthropic:\n    enabled: false\n")
    tid = _create(hub, **PIN)["task_id"]
    assert sorted(_runs_on(hub.conn, tid)) == sorted([POOL, mp.LABEL])  # queued for the pool, not a default-placement copy
    hub.round()
    worker.round()
    worker.tick()
    assert tid not in svc.board(BOARD)["leases"]
    assert kb.get_task(worker.conn, tid) is None  # left on the Board for a machine that serves it
    reason = mp.refusal({"id": tid, "assignee": "default", "model_override": "claude-opus-5-5",
                         "provider_override": "anthropic", "model_pin_mandatory": 1}, me=WORKER)
    assert reason and "provider disabled" in reason


@pytest.mark.parametrize("card,want", [
    ({"model_override": "claude-opus-5-5", "provider_override": "openai-codex", "model_pin_mandatory": 1},
     "model_provider_mismatch"),
    ({"model_override": "claude-opus-5-5", "provider_override": "openai-codex"}, None),  # not mandatory
    ({"model_override": "claude-opus-5-5", "provider_override": "anthropic", "model_pin_mandatory": 1}, None),
    ({"model_override": "claude-opus-5-5", "model_pin_mandatory": 1, "assignee": "nobody"}, "no profile"),
])
def test_refusal_names_why_the_exact_pin_cannot_run_here(tmp_path, monkeypatch, card, want):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text("model:\n  provider: anthropic\n")
    got = mp.refusal({"id": "t", "assignee": "default", **card})
    assert (got is None) if want is None else (want in got), got


def test_a_mark_without_a_model_pins_nothing():
    # A Board row can carry model_pin_mandatory after `set-model` cleared the model (kanban_db keeps the mark
    # NULL then, but a wire row is not trusted): nothing to serve, so no label demand and no refusal.
    card = {"id": "t", "assignee": "default", "model_override": None, "model_pin_mandatory": 1}
    assert mp.pin_of(card) is None and mp.requirements(card) == set() and mp.refusal(card) is None
    assert mp.requirements({"title": "x", "pin_mandatory": True, "model": "  "}) == set()


def test_a_class_pin_is_refused_where_class_routing_is_off(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    card = {"id": "t", "assignee": "default", "model_override": "class:deep", "model_pin_mandatory": 1}
    (tmp_path / "config.yaml").write_text("model:\n  provider: anthropic\n")
    assert "class unroutable (routing_disabled)" in mp.refusal(card)
    (tmp_path / "config.yaml").write_text("model:\n  provider: anthropic\nmodel_routing:\n  enabled: true\n")
    assert "routing_disabled" not in (mp.refusal(card) or "")


def test_a_home_card_waiting_on_the_attestation_is_named_stranded_once_the_pool_attests(monkeypatch):
    from hermes_cli import kanban_placement_coordination as coord
    entry = {"state": "active", "pickup": "all", "last_seen": time.time() * 1000, "labels": [],
             "max_card_runs": 2, "metrics": {"active_card_runs": 0}}
    monkeypatch.setattr(wn, "registry", lambda: {"machines": {"worker": entry}})
    monkeypatch.setattr(kp.reg, "self_gateway", lambda: HUB)
    cfg = {"worker_pools": {HUB: ["worker"]}, "assignees": ["default"]}
    card = {"title": "Review PR 1", "body": "", "assignee": "default", "workspace_kind": "scratch",
            "requires": "[]", "model_override": "claude-opus-5-5", "model_pin_mandatory": 1}
    assert coord.stranded(card, cfg, mp.REASON) == []
    entry["labels"] = [mp.LABEL]
    assert coord.stranded(card, cfg, mp.REASON) == ["worker"]


def test_a_claim_on_an_unattested_destination_is_refused(monkeypatch):
    from hermes_cli import kanban_placement_coordination as coord
    machines = {"w": {"state": "active", "pickup": "true", "last_seen": time.time() * 1000,
                      "labels": [POOL]}}
    monkeypatch.setattr(wn, "registry", lambda: {"machines": machines})
    card = {"title": "review", "placement_policy": "worker-first-v1", "model_override": "claude-opus-5-5",
            "model_pin_mandatory": 1}
    assert coord.claim_refusal(card, "w") == mp.REASON
    machines["w"]["labels"].append(mp.LABEL)
    assert coord.claim_refusal(card, "w") is None


# --- no fallback -----------------------------------------------------------------------------------------------

def test_a_refused_mandatory_pin_is_never_cleared_to_the_profile_default(tmp_path):
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli.kanban_model_assignment import clear_refused_pin
    conn = kbc.connect(tmp_path / "kanban.db")
    keep = kb.create_task(conn, title="keep", assignee="default", model_override="claude-opus-5-5",
                          provider_override="anthropic", pin_mandatory=True)
    drop = kb.create_task(conn, title="drop", assignee="default", model_override="claude-opus-5-5",
                          provider_override="anthropic")
    assert not clear_refused_pin(conn, keep, "claude-opus-5-5", "anthropic", [])
    assert kb.get_task(conn, keep).model_override == "claude-opus-5-5"
    assert clear_refused_pin(conn, drop, "claude-opus-5-5", "anthropic", [])  # a plain pin may still be dropped
    assert kb.get_task(conn, drop).model_override is None
    conn.close()


def test_the_courier_still_cannot_carry_a_mandatory_pin():
    used = kp._uncarried({**REVIEW, **PIN}, kp._Cfg({}), True)
    assert used == ["pin_mandatory"]


# --- review 5878972889: no route around the label ---------------------------------------------------------------
# The worker-first pool's Board lease checks only profile/requires/skill labels, and a move rewrites runs_on, so
# either could hand an old core (no refusal(), no CAS) a mandatory pin. Each test here fails on ebc69953d4.

def test_a_mandatory_pin_never_enrolls_worker_first(monkeypatch):
    from hermes_cli import kanban_worker_placement as wp
    monkeypatch.setattr(kp, "portable_reason", lambda args, cfg=None: None)  # an attested, portable pool
    cfg = {"worker_pools": {HUB: [WORKER]}}
    assert wp.creation_decision({**REVIEW, **PIN}, cfg, HUB, "auto", None, queue=False) is None
    plain = wp.creation_decision({**REVIEW, "model": "claude-opus-5-5"}, cfg, HUB, "auto", None, queue=False)
    assert plain["placement_policy"] == wp.POLICY  # a plain pin still enrolls


def test_a_denied_mandatory_pin_with_no_queue_is_refused_not_sent_worker_first():
    from hermes_cli import kanban_hub_admission_placement as hp
    out = hp.pool_route({**REVIEW, **PIN}, {}, HUB, {"admit": False}, None, queue=False)
    assert out["where"] == "refuse" and mp.WORKER_FIRST_REASON in out["why"], out
    assert hp.pool_route(REVIEW, {}, HUB, {"admit": False}, None, queue=False)["placement_policy"] == "worker-first-v1"


def test_recovery_never_promotes_a_home_mandatory_pin(tmp_path, monkeypatch):
    from hermes_cli import kanban_db_connect as kbc
    from hermes_cli import kanban_placement_recovery as rec
    monkeypatch.setattr(kp, "portable_reason", lambda args, cfg=None: None)
    monkeypatch.setattr(kp, "config", dict)
    conn = kbc.connect(tmp_path / "kanban.db")
    pinned = kb.create_task(conn, title="review", assignee="default", model_override="claude-opus-5-5",
                            provider_override="anthropic", pin_mandatory=True)
    plain = kb.create_task(conn, title="review", assignee="default", model_override="claude-opus-5-5",
                           provider_override="anthropic")
    row = lambda tid: dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (tid,)).fetchone())
    assert rec.current_portable(conn, row(plain), {}) is True
    assert rec.current_portable(conn, row(pinned), {}) is False
    conn.close()


def test_a_hub_pickup_re_place_keeps_the_label(monkeypatch):
    from hermes_cli import kanban_hub_admission_pickup as hap
    monkeypatch.setattr(kp, "structural_reason", lambda args, cfg=None: None)
    monkeypatch.setattr(kp, "portable_reason", lambda args, cfg=None: None)
    monkeypatch.setattr(kp, "_machine_picks_up", lambda label: True)
    card = {"title": "review", "body": "", "assignee": "default", "workspace_kind": "scratch", "requires": "[]",
            "skills": None, "model_override": "claude-opus-5-5", "provider_override": "anthropic",
            "model_pin_mandatory": 1}
    assert sorted(hap._target(hap.card_fields(card, BOARD), HUB, CFG, {})) == sorted([POOL, mp.LABEL])
    plain = {**card, "model_pin_mandatory": None}
    assert hap._target(hap.card_fields(plain, BOARD), HUB, CFG, {}) == [POOL]


def _machines(*, attest):
    worker = {**MACHINES[WORKER], "labels": ["linux", POOL] + ([mp.LABEL] if attest else [])}
    return {**MACHINES, WORKER: worker}


@pytest.mark.parametrize("target", [[POOL], [f"machine:{WORKER}"]])
def test_a_move_keeps_the_label_and_needs_an_attested_target(gws, target):
    from hermes_cli import kanban_move as km
    hub, _ = gws
    hub.enter()
    tid = kb.create_task(hub.conn, title="review", assignee="default", model_override="claude-opus-5-5",
                         provider_override="anthropic", pin_mandatory=True)
    kw: dict = {"reason": "r", "me": HUB, "host_reason": "the review runs on that host"}
    res = km.move_queued(hub.conn, tid, runs_on=target, machines=_machines(attest=False), **kw)
    assert res["reason_code"] == "no_machine" and mp.LABEL in res["error"], res
    res = km.move_queued(hub.conn, tid, runs_on=target, machines=_machines(attest=True), **kw)
    assert res["ok"] and _runs_on(hub.conn, tid) == sorted([*target, mp.LABEL]), res
    plain = kb.create_task(hub.conn, title="plain", assignee="default", model_override="claude-opus-5-5",
                           provider_override="anthropic")
    res = km.move_queued(hub.conn, plain, runs_on=target, machines=_machines(attest=False), **kw)
    assert res["ok"] and _runs_on(hub.conn, plain) == target, res


def test_carry_leaves_a_pin_to_this_machine_alone():
    card = {"model_override": "claude-opus-5-5", "model_pin_mandatory": 1}
    assert mp.carry([f"machine:{HUB}"], card, me=HUB) == [f"machine:{HUB}"]  # this core refuses what it can't serve
    assert mp.carry([f"machine:{WORKER}"], card, me=HUB) == [mp.LABEL, f"machine:{WORKER}"]
    assert mp.carry([POOL], {"model_override": "x"}, me=HUB) == [POOL]
    # A pool label always takes the attestation, even beside this machine: the pool may hold an old core.
    assert mp.carry([f"machine:{HUB}", POOL], card, me=HUB) == sorted([mp.LABEL, f"machine:{HUB}", POOL])


def test_marking_labels_a_pool_card_in_a_process_without_sync(gws, monkeypatch):
    # Review 5879616348 on #1622: a process that did not install sync (board_mode 'off') writing to an attached
    # board used to skip runs_on, so the Board got model_pin_mandatory=1 without the label for an old core to lease.
    from hermes_cli import kanban_pin_reroute as pr
    hub, _ = gws
    tid = _placed(hub)
    mine = _placed(hub)
    with kb.write_txn(hub.conn):
        wn.set_runs_on(hub.conn, mine, [f"machine:{HUB}"])
    monkeypatch.setattr(sync, "_SYNCED_PATHS", set())
    assert sync.board_mode(hub.conn) == "off"
    assert pr.set_pin_mandatory(hub.conn, tid, True)
    assert _runs_on(hub.conn, tid) == sorted([POOL, mp.LABEL])
    # The file's own gateway id still names this machine: a pin to it stays unlabeled.
    assert pr.set_pin_mandatory(hub.conn, mine, True)
    assert _runs_on(hub.conn, mine) == [f"machine:{HUB}"]


# --- review 5879416189: a later mark is fenced like a create-time one -------------------------------------------
# `hermes kanban set-model <id> --mandatory` (the quota-reroute comment advertises it) used to set only the column:
# a placed [pool:worker] card kept runs_on without the label, and a worker-first card kept its Board-owned lease.

def _placed(hub, *, model="claude-opus-5-5"):
    hub.enter()
    tid = kb.create_task(hub.conn, title="review", assignee="default", model_override=model,
                         provider_override="anthropic" if model else None)
    with kb.write_txn(hub.conn):
        wn.set_runs_on(hub.conn, tid, [POOL])
    return tid


@pytest.mark.parametrize("raw_model", [None, "claude-opus-5-5"])
def test_marking_a_placed_card_mandatory_adds_the_label(gws, raw_model):
    from hermes_cli import kanban_pin_reroute as pr
    hub, _ = gws
    tid = _placed(hub)
    found, _ = pr.set_model_cli(hub.conn, tid, raw_model, raw_model, "anthropic" if raw_model else None, True)
    assert found
    assert kb.get_task(hub.conn, tid).model_pin_mandatory == 1
    assert _runs_on(hub.conn, tid) == sorted([POOL, mp.LABEL])
    [ev] = [json.loads(p) for (p,) in hub.conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'model_pin_mandatory_set'", (tid,))]
    assert ev == {"mandatory": True, "runs_on": sorted([POOL, mp.LABEL]), "previous_runs_on": [POOL]}
    # Unmarking leaves the label (harmless: every attesting worker also serves plain pins); a home card has none.
    assert pr.set_pin_mandatory(hub.conn, tid, False)
    assert kb.get_task(hub.conn, tid).model_pin_mandatory is None
    home = kb.create_task(hub.conn, title="home", assignee="default", model_override="claude-opus-5-5",
                          provider_override="anthropic")
    assert pr.set_pin_mandatory(hub.conn, home, True) and _runs_on(hub.conn, home) is None


@pytest.mark.parametrize("raw_model", [None, "claude-sonnet-5"])
def test_marking_a_worker_first_card_mandatory_is_refused_and_changes_nothing(gws, raw_model):
    from hermes_cli import kanban_pin_reroute as pr
    hub, _ = gws
    tid = _placed(hub)
    with kb.write_txn(hub.conn):
        hub.conn.execute("UPDATE tasks SET placement_policy = 'worker-first-v1' WHERE id = ?", (tid,))
    with pytest.raises(ValueError, match="worker-first"):
        pr.set_model_cli(hub.conn, tid, raw_model, raw_model, "anthropic" if raw_model else None, True)
    task = kb.get_task(hub.conn, tid)
    assert (task.model_override, task.model_pin_mandatory) == ("claude-opus-5-5", None)
    assert _runs_on(hub.conn, tid) == [POOL]
    # Without the mark the same card still takes a new plain pin.
    assert pr.set_model_cli(hub.conn, tid, "claude-sonnet-5", "claude-sonnet-5", "anthropic", None) == (True, True)


def test_marking_needs_a_pin_and_a_live_card(gws):
    from hermes_cli import kanban_pin_reroute as pr
    hub, _ = gws
    tid = _placed(hub, model=None)
    with pytest.raises(ValueError, match="needs a model pin"):
        pr.set_pin_mandatory(hub.conn, tid, True)
    assert _runs_on(hub.conn, tid) == [POOL]
    assert pr.set_pin_mandatory(hub.conn, "t_missing", True) is False
    from hermes_cli.kanban_db_archive import archive_task
    gone = _placed(hub)
    archive_task(hub.conn, gone)
    with pytest.raises(RuntimeError, match="archived"):
        pr.set_pin_mandatory(hub.conn, gone, True)
    assert kb.get_task(hub.conn, gone).model_pin_mandatory is None
