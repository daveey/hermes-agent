"""A registered probe's host is not the execution host of its ready card."""
import pytest

from hermes_cli import kanban_db as kb
from tests.hermes_cli import test_kanban_hub_admission_claim as claim

hub_judge = claim.hub_judge
gws = claim.gws
_claim_screen_first = claim._claim_screen_first


@pytest.mark.parametrize("wake_event", [False, True])
def test_registered_hub_probe_does_not_pin_ready_execution(gws, hub_judge, wake_event):
    hub, worker, svc = gws
    hub_judge.admit_next = False
    tid = claim._hub_card(hub)
    probe = "test -f ~/.hermes/cache/ready.flag"
    with kb.write_txn(hub.conn):
        kb._append_event(hub.conn, tid, "scheduled", {"wake_check": probe, "probe_host": claim.HUB})
        if wake_event:
            kb._append_event(hub.conn, tid, "condition_met", {"wake_check": probe})
    claim._tick(hub)
    assert claim._holder(svc, tid) != claim.HUB
    [verdict] = claim._events(hub.conn, tid, "hub_admission")
    assert verdict["admit"] is False and "kept" not in verdict
    [moved] = claim._events(hub.conn, tid, "moved")
    assert moved["runs_on"][0] == claim.POOL
    hub.round()
    worker.round()
    worker.tick()
    assert claim._holder(svc, tid) == claim.WORKER


@pytest.mark.parametrize("payload", [
    {}, {"probe_host": None}, {"probe_host": []}, {"probe_host": "worker-2"},
    {"probe_host": claim.HUB, "wake_check": "test -f ~/.hermes/cache/different.flag"},
])
def test_unknown_or_different_probe_custody_keeps_the_local_guard(gws, payload):
    from hermes_cli.kanban_hub_probe_custody import registered_here
    hub, _worker, _svc = gws
    tid = claim._hub_card(hub)
    probe = "test -f ~/.hermes/cache/ready.flag"
    assert not registered_here(hub.conn, tid, probe)
    with kb.write_txn(hub.conn):
        kb._append_event(hub.conn, tid, "scheduled", {"wake_check": probe, **payload})
    assert not registered_here(hub.conn, tid, probe)


@pytest.mark.parametrize("own", [None, ""])
def test_missing_local_identity_cannot_establish_custody(gws, monkeypatch, own):
    from hermes_cli import kanban_wake_check
    from hermes_cli.kanban_hub_probe_custody import registered_here
    hub, _worker, _svc = gws
    tid = claim._hub_card(hub)
    probe = "test -f ~/.hermes/cache/ready.flag"
    with kb.write_txn(hub.conn):
        kb._append_event(hub.conn, tid, "scheduled", {"wake_check": probe, "probe_host": own})
    monkeypatch.setattr(kanban_wake_check, "own_gateway", lambda _conn: own)
    assert not registered_here(hub.conn, tid, probe)


def test_a_later_park_cannot_borrow_an_older_probe_host(gws):
    from hermes_cli.kanban_hub_probe_custody import registered_here
    hub, _worker, _svc = gws
    tid = claim._hub_card(hub)
    probe = "test -f ~/.hermes/cache/ready.flag"
    with kb.write_txn(hub.conn):
        kb._append_event(hub.conn, tid, "scheduled", {"wake_check": probe, "probe_host": claim.HUB})
    assert registered_here(hub.conn, tid, probe)
    with kb.write_txn(hub.conn):
        kb._append_event(hub.conn, tid, "scheduled", {"wake_check": probe})
    assert not registered_here(hub.conn, tid, probe)
