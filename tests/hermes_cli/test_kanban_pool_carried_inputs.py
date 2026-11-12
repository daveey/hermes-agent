"""Carried inputs under pool routing (``pool:worker``): review notes 2 and 3 on PR 1247. [daveey]

(3) ``without_carried_inputs`` read a ``heavy_route`` value ``pool:worker`` as a machine named "pool:worker", so no
receipt ever covered it and the exemption was off under pool routing. A pool label now means every registry machine
carrying it. (2) ``required_labels`` (the queue's ``runs_on`` labels) read the raw body, so a verified carried path
naming a tool (``.../token-broker-audit/BRIEF.md``) demanded ``capability:token-broker`` that the portability check
never asked for.
"""
from __future__ import annotations

import hashlib
import json
import time

import pytest

from hermes_cli import kanban_carried_evidence as ce
from hermes_cli import kanban_placement as kp
from hermes_cli import kanban_placement_coordination as coord
from hermes_cli import kanban_worker_node as wn
from hermes_cli import kanban_worker_placement as wp

POOL = "pool:worker"
NAME = "/home/example/.hermes/kanban/evidence/token-broker-audit/BRIEF.md"


def _machine(name, labels, **extra):
    return {"name": name, "state": "active", "labels": labels, "hold": False, **extra}


@pytest.fixture
def carried(tmp_path, monkeypatch):
    home = tmp_path / "home"
    file = home / "kanban/evidence/token-broker-audit/BRIEF.md"
    file.parent.mkdir(parents=True)
    file.write_text("frozen plan\n")
    digest = hashlib.sha256(file.read_bytes()).hexdigest()
    receipt = {"version": 1, "verified_at": time.time(), "machines": ["worker-1", "worker-2"],
               "files": {NAME: digest}}
    manifest = home / "kanban/carried-evidence.json"
    manifest.write_text(json.dumps(receipt))
    monkeypatch.setattr(ce, "get_hermes_home", lambda: home)
    real_path = ce.Path
    monkeypatch.setattr(ce, "Path", lambda value: file if value == NAME else real_path(value))
    machines = [_machine("hub", ["linux"]), _machine("worker-1", ["linux", POOL]),
                _machine("worker-2", ["linux", POOL], hold=True)]
    wn.set_registry_fetch(lambda: {"me": "hub", "machines": machines})
    monkeypatch.setattr("hermes_cli.kanban_capability_registry.self_gateway", lambda: "hub")
    yield {"manifest": manifest, "receipt": receipt, "machines": machines,
           "body": f"Read {NAME}, SHA256 {digest}. Implement the approved plan."}
    wn.set_registry_fetch(None)


# --- note 3: a label-valued route is a pool, not a host name ------------------------------------------------------

@pytest.mark.parametrize("cfg", [{"heavy_route": {"hub": POOL}}, {"worker_queue": {"hub": POOL}},
                                 {"heavy_route": {"hub": POOL}, "worker_pools": {"hub": ["worker-1"]}}],
                         ids=["heavy_route", "worker_queue", "with_pools"])
def test_pool_route_exempts_inputs_every_label_machine_carries(carried, cfg):
    out = ce.without_carried_inputs(carried["body"], cfg)
    assert NAME not in out and "[carried input]" in out
    assert kp.portable_reason({"title": "Implement plan", "body": carried["body"]}, cfg) is None


def _down():
    raise OSError("board unreachable")


def _string_labels(machines, receipt):
    machines[1]["labels"] = machines[2]["labels"] = "linux,pool:worker"  # substring is not membership
    return {"heavy_route": {"hub": "pool:work"}}


_FAULTS = {
    "uncovered_member": lambda machines, receipt: machines.append(_machine("worker-3", [POOL])),
    # worker-2 is held now, but leaving the hold must not skip the receipt.
    "held_member_uncovered": lambda machines, receipt: receipt.update(machines=["worker-1"]),
    # A label no machine carries is unknown destinations, even beside a covered pool.
    "empty_label": lambda machines, receipt: {"heavy_route": {"hub": "pool:gpu"},
                                              "worker_pools": {"hub": ["worker-1"]}},
    "registry_down": lambda machines, receipt: wn.set_registry_fetch(_down),
    "string_labels": _string_labels,
    "bad_queue": lambda machines, receipt: {"heavy_route": {"hub": "worker-1"}, "worker_queue": [POOL]},
}


@pytest.mark.parametrize("fault", sorted(_FAULTS))
def test_pool_route_fails_closed(carried, fault):
    receipt = carried["receipt"]
    cfg = _FAULTS[fault](carried["machines"], receipt) or {"heavy_route": {"hub": POOL}}
    carried["manifest"].write_text(json.dumps(receipt))
    assert ce.without_carried_inputs(carried["body"], cfg) == carried["body"]


def test_machine_route_still_names_one_host(carried):
    assert NAME not in ce.without_carried_inputs(carried["body"], {"heavy_route": {"hub": "worker-1"}})
    assert ce.without_carried_inputs(carried["body"], {"heavy_route": {"hub": "worker-3"}}) == carried["body"]


# --- note 2: the queue's labels read the same text as the portability check ---------------------------------------

def test_queue_labels_skip_a_verified_carried_path(carried):
    # worker_pools covers the receipt on main too, so this isolates note 2 from note 3.
    cfg = {"worker_queue": {"hub": POOL}, "worker_pools": {"hub": ["worker-1", "worker-2"]}}
    args = {"title": "Implement plan", "body": carried["body"], "assignee": "default"}
    assert kp.portable_reason(args, cfg) is None
    # Before: runs_on [pool:worker, capability:token-broker], a label no pool machine carries.
    assert wp.queue_decision(args, cfg, "hub", "", None, machine_ok=lambda _: True) == {
        "where": "local", "runs_on": [POOL], "why": wp.QUEUED_WHY.format(label=POOL)}
    assert coord.required_labels(args, cfg) == set()
    # The same path without a covering receipt keeps its label, and a real tool mention still needs one.
    assert coord.required_labels(args, {}) == {"capability:token-broker"}
    args["body"] += "\nThen run token-broker exec for the token."
    assert coord.required_labels(args, cfg) == {"capability:token-broker"}


def test_legacy_heavy_route_reads_the_same_text(carried):
    """``legacy_refusal`` (heavy routing) shares ``required_labels``: a carried path no longer refuses the route."""
    args = {"title": "Run the regression tests for the parser", "body": carried["body"]}
    d = kp.decide(args, cfg={"heavy_route": {"hub": "worker-1"}}, self_gw="hub")
    assert d["where"] == "local" and d["runs_on"] == ["machine:worker-1"], d
    assert coord.legacy_refusal(args, {}) is True  # uncovered: the path's tool name still counts


# --- rebase onto hub-admission worker_queue (abec81ec81): no sync identity, and the denied-card queue path -------

def _no_identity():
    from hermes_cli import kanban_sync_client as ksc
    raise ksc.SyncUnavailable("no sync identity")


def test_registry_without_sync_identity_fails_closed(carried, monkeypatch):
    """``registry()`` raises ``SyncUnavailable`` before its fetch try on a gateway with no sync identity."""
    wn.set_registry_fetch(None)
    monkeypatch.setattr("hermes_cli.kanban_sync_client.load_identity", _no_identity)
    assert ce.without_carried_inputs(carried["body"], {"worker_queue": {"hub": POOL}}) == carried["body"]


def test_text_without_carried_path_never_reads_the_registry(monkeypatch):
    calls = []
    monkeypatch.setattr(wn, "registry", lambda: calls.append(1))  # counted, since _label_members swallows raises
    monkeypatch.setattr(wn, "registry_fresh", lambda *_: calls.append(1), raising=False)
    body = "Implement the approved plan."
    assert ce.without_carried_inputs(body, {"worker_queue": {"hub": POOL}}) == body
    assert kp.portable_reason({"title": "Implement plan", "body": body}, {"worker_queue": {"hub": POOL}}) is None
    assert calls == []


def test_denied_card_queue_labels_skip_a_verified_carried_path(carried):
    from hermes_cli import kanban_hub_admission_placement as hap
    cfg = {"worker_queue": {"hub": POOL}, "worker_pools": {"hub": ["worker-1", "worker-2"]}}
    args = {"title": "Implement plan", "body": carried["body"], "assignee": "default"}
    out = hap.pool_route(args, cfg, "hub", {}, None, machine_ok=lambda _: True)
    assert out["runs_on"] == [POOL], out


# --- PR 1384 review note 3: a stale registry snapshot fails closed -----------------------------------------------

class _Clock:
    """``wn.time`` stand-in: a hand-moved ``monotonic``, everything else from :mod:`time`."""

    def __init__(self):
        self.now = 1000.0

    def monotonic(self):
        return self.now

    def __getattr__(self, name):
        return getattr(time, name)


@pytest.fixture
def stale(carried, monkeypatch):
    """One good fetch (``pool:worker`` = {worker-2}, covered by the receipt), then every fetch fails."""
    clock = _Clock()
    monkeypatch.setattr(wn, "time", clock)
    machines = [_machine("hub", ["linux"]), _machine("worker-2", ["linux", POOL])]
    state = {"up": True}

    def fetch():
        if not state["up"]:
            raise OSError("board unreachable")
        return {"me": "hub", "machines": machines}

    wn.set_registry_fetch(fetch)
    cfg = {"worker_queue": {"hub": POOL}}
    assert NAME not in ce.without_carried_inputs(carried["body"], cfg)  # fresh: exempt
    state["up"] = False
    return {**carried, "clock": clock, "cfg": cfg, "machines": machines}


def test_stale_registry_snapshot_fails_closed(stale):
    # A worker-3 labelled pool:worker during the outage would be invisible to a frozen snapshot.
    stale["clock"].now += 2 * wn.REGISTRY_TTL_S + 1
    assert ce.without_carried_inputs(stale["body"], stale["cfg"]) == stale["body"]


def test_short_registry_blip_keeps_the_exemption(stale):
    stale["clock"].now += wn.REGISTRY_TTL_S + 1  # the refetch is due and fails, but the last success is recent
    assert NAME not in ce.without_carried_inputs(stale["body"], stale["cfg"])


def test_dispatcher_registry_keeps_last_known_good(stale):
    stale["clock"].now += 10 * wn.REGISTRY_TTL_S
    snapshot = wn.registry()
    assert snapshot is not None and set(snapshot["machines"]) == {"hub", "worker-2"}
