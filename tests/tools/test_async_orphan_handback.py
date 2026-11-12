"""Offer custody survives handback; terminal replay expiry ignores in-memory custody."""

import logging
import os
import queue

import pytest

from tests.tools import test_async_delegation_orphan_sweep as helpers
from tools import async_delegation as ad
from tools.process_registry import process_registry


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch):
    ad._reset_for_tests()
    monkeypatch.setattr(process_registry, "completion_queue", queue.Queue())
    yield
    ad._reset_for_tests()


@pytest.fixture
def orphan(tmp_path):
    home = tmp_path / "home"
    delegation_id = helpers._orphan(home)
    return home, delegation_id, helpers._row(home, delegation_id)


@pytest.mark.parametrize("startup", [False, True])
def test_dead_owner_handbacks_retain_every_offer_lease(orphan, monkeypatch, startup):
    home, delegation_id, row = orphan
    q = queue.Queue()
    now = row["updated_at"] + ad._ORPHAN_STALE_S + 1
    monkeypatch.setattr(ad.time, "time", lambda: now)
    with helpers._Home(home):
        if startup:
            assert ad.restore_undelivered_completions(q) == 1
        else:
            assert ad.sweep_orphaned_completions(q, now=now) == 1
        for _ in range(3):
            (evt,) = helpers._drain(q)
            ad.return_completion_offer(evt)
            if startup:
                assert ad.restore_undelivered_completions(q) == 0
            assert ad.sweep_orphaned_completions(q, now=now + 30) == 0
            assert ad.sweep_orphaned_completions(q, now=now + ad._CLAIM_LEASE_S) == 0
            assert helpers._row(home, delegation_id)["delivery_attempts"] == 0
            now += ad._CLAIM_LEASE_S + 1
            assert ad.sweep_orphaned_completions(q, now=now) == 1
        # Copies from competing delivery loops still arbitrate through the durable claim.
        (evt,) = helpers._drain(q)
        claim = ad.claim_event_delivery(evt, "winner")
        assert claim and ad.claim_event_delivery(evt, "competitor") is None
        assert ad.complete_completion_delivery(delegation_id, claim)
        assert ad.sweep_orphaned_completions(q, now=now + 1000) == 0
    assert helpers._row(home, delegation_id)["delivery_state"] == "delivered"


@pytest.mark.parametrize("custody", ["queued", "held"])
@pytest.mark.parametrize("owner", ["dead", "current", "foreign"])
def test_live_copies_survive_leases_but_not_terminal_replay_age(orphan, monkeypatch, caplog, custody, owner):
    home, delegation_id, row = orphan
    q = queue.Queue()
    now = row["updated_at"] + ad._ORPHAN_STALE_S + 1
    with helpers._Home(home):
        assert ad.sweep_orphaned_completions(q, now=now) == 1
        if owner != "dead":
            pid = os.getpid() if owner == "current" else os.getppid()
            helpers._set(home, delegation_id, owner_pid=pid, owner_started_at=None)
            monkeypatch.setattr(ad, "_owner_liveness", lambda: lambda pid, started: True)
        if custody == "held":
            ad.mark_copy_held(delegation_id)
            helpers._drain(q)
        try:
            assert ad.sweep_orphaned_completions(q, now=now + ad._CLAIM_LEASE_S + 1) == 0
            assert helpers._row(home, delegation_id)["delivery_state"] == "pending"
            caplog.set_level(logging.WARNING, logger="tools.async_delegation")
            expiry = row["completed_at"] + ad._MAX_COMPLETION_REPLAY_AGE_S + 1
            assert ad.sweep_orphaned_completions(q, now=expiry) == 0
            dropped = helpers._row(home, delegation_id)
            assert dropped["delivery_state"] == "dropped"
            assert dropped["delivery_attempts"] > 0
            assert dropped["event_json"] == row["event_json"]
            assert any("replay age expired" in r.message and "attempt" in r.message for r in caplog.records)
            assert not ad.claim_completion_delivery(delegation_id, "late-copy")
        finally:
            if custody == "held":
                ad.unmark_copy_held(delegation_id)


@pytest.mark.parametrize("entry", ["startup", "sweep"])
@pytest.mark.parametrize("budget", ["age", "attempts"])
def test_active_claim_is_not_expired_or_replayed(orphan, monkeypatch, entry, budget):
    home, delegation_id, row = orphan
    now = row["completed_at"] + ad._MAX_COMPLETION_REPLAY_AGE_S + 1
    attempts = ad._MAX_DELIVERY_ATTEMPTS if budget == "attempts" else 2
    if budget == "attempts":
        now = row["updated_at"] + ad._CLAIM_LEASE_S + 1
    helpers._set(home, delegation_id, delivery_claim="active", delivery_claimed_at=now - 1,
                 delivery_attempts=attempts)
    before = helpers._row(home, delegation_id)
    monkeypatch.setattr(ad.time, "time", lambda: now)
    q = queue.Queue()
    with helpers._Home(home):
        count = ad.restore_undelivered_completions(q) if entry == "startup" else ad.sweep_orphaned_completions(q, now=now)
        assert count == 0 and q.empty()
        assert helpers._row(home, delegation_id) == before
        now += ad._CLAIM_LEASE_S + 1
        count = ad.restore_undelivered_completions(q) if entry == "startup" else ad.sweep_orphaned_completions(q, now=now)
        assert count == 0 and q.empty()
    dropped = helpers._row(home, delegation_id)
    assert dropped["delivery_state"] == "dropped"
    assert dropped["delivery_claim"] is None and dropped["delivery_claimed_at"] is None
    assert dropped["delivery_attempts"] == attempts


@pytest.mark.parametrize("entry", ["startup", "sweep"])
def test_attempt_cap_resolves_even_a_queued_copy(orphan, monkeypatch, caplog, entry):
    home, delegation_id, row = orphan
    q = queue.Queue()
    now = row["updated_at"] + ad._ORPHAN_STALE_S + 1
    monkeypatch.setattr(ad.time, "time", lambda: now)
    with helpers._Home(home):
        assert ad.restore_undelivered_completions(q) == 1
        helpers._set(home, delegation_id, delivery_attempts=ad._MAX_DELIVERY_ATTEMPTS)
        count = ad.restore_undelivered_completions(q) if entry == "startup" else ad.sweep_orphaned_completions(q, now=now)
        assert count == 0
    dropped = helpers._row(home, delegation_id)
    assert dropped["delivery_state"] == "dropped"
    assert dropped["delivery_attempts"] == ad._MAX_DELIVERY_ATTEMPTS
    assert any("delivery attempts exhausted" in r.message for r in caplog.records)


@pytest.mark.parametrize("owner", ["current", "foreign"])
def test_missing_copy_recovery_only_for_the_current_live_owner(orphan, monkeypatch, owner):
    home, delegation_id, row = orphan
    helpers._set(home, delegation_id, owner_pid=os.getpid() if owner == "current" else os.getppid(),
                 owner_started_at=None)
    monkeypatch.setattr(ad, "_owner_liveness", lambda: lambda pid, started: True)
    q = queue.Queue()
    with helpers._Home(home):
        assert ad.sweep_orphaned_completions(q, now=row["updated_at"] + ad._CLAIM_LEASE_S + 1) == (owner == "current")
    assert helpers._row(home, delegation_id)["delivery_attempts"] == 0


def test_unready_admission_defers_without_spending_an_attempt(orphan):
    home, delegation_id, row = orphan
    q = queue.Queue()
    with helpers._Home(home):
        assert ad.sweep_orphaned_completions(q, now=row["updated_at"] + ad._ORPHAN_STALE_S + 1) == 1
        (evt,) = helpers._drain(q)
        claim = ad.claim_event_delivery(evt, "unready")
        assert claim and ad.defer_completion_delivery(delegation_id, claim)
        ad.return_completion_offer(evt)
    deferred = helpers._row(home, delegation_id)
    assert deferred["delivery_state"] == "pending"
    assert deferred["delivery_claim"] is None and deferred["delivery_attempts"] == 0


@pytest.mark.parametrize("age_basis", ["dispatch", "absent"])
def test_startup_expiry_uses_dispatch_fallback_but_not_an_absent_age(orphan, monkeypatch, age_basis):
    home, delegation_id, row = orphan
    helpers._set(home, delegation_id, completed_at=0,
                 dispatched_at=row["dispatched_at"] if age_basis == "dispatch" else 0)
    monkeypatch.setattr(ad.time, "time", lambda: row["dispatched_at"] + ad._MAX_COMPLETION_REPLAY_AGE_S + 1)
    q = queue.Queue()
    with helpers._Home(home):
        assert ad.restore_undelivered_completions(q) == (age_basis == "absent")
    assert helpers._row(home, delegation_id)["delivery_state"] == ("dropped" if age_basis == "dispatch" else "pending")


@pytest.mark.parametrize("terminal", ["delivered", "dropped", "deleted"])
def test_offer_history_prunes_only_settled_home_rows(orphan, monkeypatch, terminal):
    home, delegation_id, row = orphan
    now = row["updated_at"] + ad._ORPHAN_STALE_S + 1
    monkeypatch.setattr(ad.time, "time", lambda: now)
    q = queue.Queue()
    with helpers._Home(home):
        assert ad.restore_undelivered_completions(q) == 1
        key = (ad.hermes_home_key(home), delegation_id)
        other = ("/other/home", delegation_id)
        ad._offered.add(other)
        ad._offered_at[other] = now
        ad._reoffered_at[key] = now
        # A queued pending copy retains its history beyond a claim lease.
        assert ad.sweep_orphaned_completions(q, now=now + ad._CLAIM_LEASE_S + 1) == 0
        assert key in ad._offered_at and key in ad._offered
        if terminal == "deleted":
            with ad._transaction() as conn:
                conn.execute("DELETE FROM async_delegations WHERE delegation_id=?", (delegation_id,))
        else:
            helpers._set(home, delegation_id, delivery_state=terminal)
        assert ad.sweep_orphaned_completions(q, now=now + ad._CLAIM_LEASE_S + 2) == 0
        assert key not in ad._offered_at and key not in ad._offered and key not in ad._reoffered_at
        assert other in ad._offered_at and other in ad._offered
        assert delegation_id in ad._startup_replayed  # still needed by the other home's copy
        ad._offered.remove(other)
        del ad._offered_at[other]
        # A new local stale history entry is removed along with its startup marker.
        ad._offered_at[key] = now
        assert ad.sweep_orphaned_completions(q, now=now + ad._CLAIM_LEASE_S + 3) == 0
        assert delegation_id not in ad._startup_replayed
