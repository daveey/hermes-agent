"""Registry freshness for Kanban placement liveness: clock skew and named machines. [daveey]

PR #2100 review nits (t_01a1d48b563e779b, GitHub review 5889847287):

1. ``kanban_placement_coordination._fresh`` rejected any negative age. ``last_seen`` is stamped by the Board's
   clock, and observed margins are sub-second (worker-7: 0.06 s), so a host whose clock lags the Board by ~0.1 s saw
   a just-refreshed machine as "from the future", i.e. stale. It now gates queue liveness (``label_machines``) as
   well as attestation (``eligible``), so a small future skew (``SKEW_S``) counts as fresh; a far-future stamp does not.
3. ``_machine_picks_up`` for a named machine (``heavy_route: worker-1``) ignored ``last_seen`` while pool and
   label-set liveness required it. A machine silent past ``FRESH_S`` leases nothing either way (the Board's
   ``available`` skips it), so it now uses the same window.
"""
from __future__ import annotations

import time

import pytest

from hermes_cli import kanban_placement as kp
from hermes_cli import kanban_placement_coordination as coord
from hermes_cli import kanban_worker_node as wn


def _entry(age_s, **extra):
    """An active pool:worker entry last seen ``age_s`` seconds ago (negative = ahead of this host's clock)."""
    entry = {"state": "active", "labels": ["pool:worker", "capability:hub-write"], **extra}
    if age_s is not None:
        entry["last_seen"] = (time.time() - age_s) * 1000
    return entry


@pytest.mark.parametrize("age_s,fresh", [
    (-0.1, True),    # the Board's clock 0.1 s ahead of this host: a just-refreshed machine
    (-4.9, True),
    (-6, False),     # past the skew allowance: a bad stamp, not lag
    (-120, False),
    (0, True), (299, True), (301, False), (None, False),
])
def test_fresh_allows_small_future_skew(age_s, fresh):
    assert coord._fresh(_entry(age_s), time.time()) is fresh


def test_skew_allowance_is_small_next_to_the_window():
    assert getattr(coord, "SKEW_S", None) == 5 and coord.FRESH_S == 300


@pytest.mark.parametrize("age_s,live", [(-0.1, True), (-4.9, True), (-6, False)])
def test_queue_liveness_and_attestation_share_the_skew_allowance(monkeypatch, age_s, live):
    entry = _entry(age_s)
    monkeypatch.setattr(wn, "registry", lambda: {"me": "hub", "machines": {"worker-7": entry}})
    want = ["worker-7"] if live else []
    assert kp.label_machines(("pool:worker", "capability:hub-write")) == want
    assert kp._machine_picks_up(("pool:worker", "capability:hub-write")) is live
    assert coord.eligible(["worker-7"], {"worker-7": {**entry, "pickup": "explicit"}}) == want


@pytest.mark.parametrize("age_s,ok", [(0, True), (60, True), (-0.1, True), (None, False), (301, False),
                                      (3600, False), (-120, False)])
def test_named_machine_uses_the_same_freshness_window(monkeypatch, age_s, ok):
    entry = _entry(age_s)
    monkeypatch.setattr(wn, "registry", lambda: {"me": "hub", "machines": {"worker-1": entry}})
    assert kp._machine_picks_up("worker-1") is ok


def test_stale_named_heavy_route_keeps_legacy_routing(monkeypatch):
    """A heavy card bound for a named worker silent past FRESH_S is not pinned to it (nobody would claim it)."""
    cfg = {"heavy_route": {"hub": "worker-1"}}
    args = {"title": "Run the full test suite"}
    for age_s, pinned in ((5, True), (600, False)):
        entry = _entry(age_s)
        monkeypatch.setattr(wn, "registry", lambda: {"me": "hub", "machines": {"worker-1": entry}})
        d = kp.decide(args, cfg=cfg, self_gw="hub")
        assert (d.get("runs_on") == ["machine:worker-1"]) is pinned, (age_s, d)
