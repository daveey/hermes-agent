"""A crashed sync watcher must not freeze claims, and a non-claiming machine must leave portable eligibility."""
import time

import pytest

from hermes_cli import kanban_sync_claims as claims
from hermes_cli.kanban_move import matching_machines
from hermes_cli.kanban_placement import label_machines


def _save():
    with claims._STATE.lock:
        return claims._STATE.expected, claims._STATE.loop, dict(claims._STATE.open), set(claims._STATE.logged)


def _restore(saved):
    expected, loop, open_, logged = saved
    with claims._STATE.lock:
        claims._STATE.expected = expected
        claims._STATE.loop = loop
        claims._STATE.open.clear()
        claims._STATE.open.update(open_)
        claims._STATE.logged.clear()
        claims._STATE.logged.update(logged)


@pytest.fixture
def gate():
    saved = _save()
    with claims._STATE.lock:
        claims._STATE.expected = False
        claims._STATE.loop = False
        claims._STATE.open.clear()
        claims._STATE.logged.clear()
    yield
    _restore(saved)


def test_give_up_clears_the_claim_hold(gate):
    claims.loop_expected()
    assert claims.claiming_bit() == 0
    claims.loop_abandoned()
    with claims._STATE.lock:
        assert claims._STATE.expected is False
        assert claims._STATE.loop is False
    assert claims.claiming_bit() == 1


def test_open_round_still_counts_as_claiming(gate):
    with claims._STATE.lock:
        claims._STATE.expected = True
        claims._STATE.loop = True
        claims._STATE.open["/board"] = "default"
    assert claims.claiming_bit() == 1


def test_portable_match_drops_a_machine_that_is_not_claiming():
    pool = {
        "worker-1": {"state": "active", "labels": ["pool:worker"], "metrics": {"claiming": 0, "at": time.time() * 1000}},
        "worker-a1": {"state": "active", "labels": ["pool:worker"], "metrics": {"claiming": 1}},
    }
    assert matching_machines(["pool:worker"], pool, me="hub", exclude_me=False) == ["worker-a1"]


def test_machine_pin_keeps_a_non_claiming_worker():
    pool = {"worker-1": {"state": "active", "labels": ["pool:worker"], "metrics": {"claiming": 0, "at": time.time() * 1000}}}
    assert matching_machines(["machine:worker-1"], pool, me="hub", exclude_me=False) == ["worker-1"]


def test_label_machines_drops_a_non_claiming_pool_worker():
    seen = int(time.time() * 1000)
    machines = {
        "worker-1": {"state": "active", "labels": ["pool:worker"], "pickup": "all",
                     "last_seen": seen, "metrics": {"claiming": 0, "at": time.time() * 1000}},
        "worker-a1": {"state": "active", "labels": ["pool:worker"], "pickup": "all",
                      "last_seen": seen, "metrics": {"claiming": 1}},
    }
    assert label_machines("pool:worker", machines) == ["worker-a1"]


def test_label_machines_keeps_a_pinned_non_claiming_worker():
    """A machine: pin in the wanted labels keeps a worker that is not claiming."""
    seen = int(time.time() * 1000)
    machines = {
        "worker-1": {"state": "active", "labels": ["pool:worker", "machine:worker-1"], "pickup": "all",
                     "last_seen": seen, "metrics": {"claiming": 0, "at": time.time() * 1000}},
    }
    assert label_machines({"pool:worker", "machine:worker-1"}, machines) == ["worker-1"]


def test_bare_name_pickup_drops_a_non_claiming_worker():
    """A bare-name pickup passes an empty wanted set, so the machine: pin does not keep it."""
    seen = int(time.time() * 1000)
    entry = {"state": "active", "labels": ["pool:worker", "machine:worker-1"], "pickup": "all",
             "last_seen": seen, "metrics": {"claiming": 0, "at": time.time() * 1000}}
    assert label_machines((), {"worker-1": entry}) == []
    entry["metrics"] = {"claiming": 1}
    assert label_machines((), {"worker-1": entry}) == ["worker-1"]


def test_sync_watcher_give_up_is_wired():
    from gateway.run_startup import GatewayStartupMixin

    seen = []

    class Runner:
        _PRE_RECONNECT_WATCHERS = GatewayStartupMixin._PRE_RECONNECT_WATCHERS
        _POST_RECONNECT_WATCHERS = ()
        _failed_platforms = ()

        def _spawn_supervised(self, factory, name, **kwargs):
            seen.append((name, kwargs.get("on_give_up")))

        def _spawn_reconnect_watcher(self):
            return None

        def _scale_to_zero_should_arm(self):
            return False

        def _log_scale_to_zero_not_armed_reason(self):
            return None

        def _drain_control_watcher(self):
            return None

        def __getattr__(self, name):
            return lambda: None

    GatewayStartupMixin._start_spawn_background_watchers(Runner())
    wired = {name: callback for name, callback in seen}
    assert wired["kanban_sync_watcher"] is not None
    assert wired["kanban_dispatcher_watcher"] is None
    assert wired["kanban_sync_watcher"].__name__ == "sync_watcher_gave_up"
