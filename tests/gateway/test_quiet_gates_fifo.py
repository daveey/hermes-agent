"""Claim-quiet gates the EXECUTION of a classified Kanban wake in the session FIFO, nothing else.

Real ``SessionAuthority._claim_next`` / ``claim_session_input`` with the real ``agent.claim_quiet`` lease
(the clock is the only injected collaborator, so expiry needs no waiting); only the finite execution is a fake.
"""
import asyncio
import contextlib
import json

import pytest

from agent import claim_quiet as cq
from gateway import session_authority, session_notifier_drain as drain
from hermes_state_runtime import admit_session_input, list_session_admissions
from tests.agent.test_claim_quiet import FakeClock
from tests.gateway.test_session_authority_cancel_settlement import REF, _authority, _submit
from tests.hermes_state.test_notifier_claim_eligibility import wake_payload
from tests.waiting import async_wait_until


@pytest.fixture
def clock(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    fake = FakeClock()
    monkeypatch.setattr(cq, "SystemClock", lambda: fake)  # every default-clock reader (eligible()) sees it
    monkeypatch.setattr(session_authority, "_CLAIM_RETRY_MIN_S", 0.02)  # the pump's own poll: resume path
    return fake


def _wake(db, authority, name="one"):
    identity = json.dumps(["kanban-wake", "route", name])
    return admit_session_input(db, epoch=authority.epoch, session_id="s", principal_id="automation:route",
                               request_id=identity, payload=wake_payload(identity, local=True))


def _statuses(db):
    return {r["request_id"]: r["status"] for r in list_session_admissions(db, session_id="s", pending_only=False)}


async def _stop(task):
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


def _count_claims(authority):
    """Log every ``_claim_next`` the pump makes (the real method still runs)."""
    claims, real = [], authority._claim_next

    async def claim_next(ref, live, attempted=None):
        claims.append(1)
        return await real(ref, live, attempted)
    authority._claim_next = claim_next
    return claims


def _fake_execute(monkeypatch, executed):
    from gateway import session_api_turn, session_finite
    # Route/adapter validation of an API-platform row needs a live adapter; it is not under test here.
    monkeypatch.setattr(session_api_turn, "check_api_turn", lambda authority, ref, payload: None)

    async def execute(authority_, ref, row):
        executed.append(row["request_id"] if row["principal_id"].startswith("automation:") else row["payload"]["text"])
        return "done"
    monkeypatch.setattr(session_finite, "execute_finite_admission", execute)


def test_eligible_only_changes_for_classified_wakes(clock):
    identity = json.dumps(["kanban-wake", "route", "x"])
    wake = {"principal_id": "automation:route", "request_id": identity, "payload": wake_payload(identity)}
    human = {"principal_id": "human", "request_id": "h", "payload": {"text": identity}}
    assert drain.eligible(wake) and drain.eligible(human)
    with cq.acquire(max_s=60, reason="r", clock=clock):
        assert not drain.eligible(wake)           # classified wake execution waits
        assert drain.eligible(human)              # human text with the same bytes does not
        assert drain.held() is False              # held() stays updater-only
        assert drain.held_noted() is False and drain.last_held_at() is None
    assert drain.eligible(wake)


@pytest.mark.asyncio
@pytest.mark.parametrize("end", ["release", "expiry"])
async def test_quiet_wake_stays_queued_then_resumes(tmp_path, monkeypatch, clock, end):
    from gateway.config import Platform
    db, authority = _authority(tmp_path, monkeypatch, platform=Platform.API_SERVER)
    executed = []
    _fake_execute(monkeypatch, executed)
    claims = _count_claims(authority)
    owner = cq.acquire(max_s=60, reason="measure", clock=clock)
    with db:
        authority._schedule = lambda ref: None
        wake = _wake(db, authority)
        del authority._schedule
        live = authority.sessions["s"]
        authority._schedule(REF)
        try:
            # parked: the receipt stays queued under its identity while the lease is live
            await async_wait_until(lambda: len(claims) >= 3, timeout=5, what="the pump re-read the held wake")
            assert executed == [] and not live.task.done()
            row = list_session_admissions(db, session_id="s")[0]
            assert row["admission_id"] == wake["admission_id"] and row["status"] == "queued"
            if end == "release":
                owner.close()
            else:
                clock.advance(481)  # lease deadline passes; nothing released explicitly
            await async_wait_until(lambda: executed, timeout=5, what="the same wake runs after the lease ends")
            assert executed == [wake["request_id"]]
            await async_wait_until(lambda: _statuses(db).get(wake["request_id"]) != "queued", timeout=5,
                                   what="the wake settles")
        finally:
            owner.close()
            await _stop(live.task)


@pytest.mark.asyncio
async def test_human_input_bypasses_a_quiet_held_wake(tmp_path, monkeypatch, clock):
    from gateway.config import Platform
    db, authority = _authority(tmp_path, monkeypatch, platform=Platform.API_SERVER)
    executed = []
    _fake_execute(monkeypatch, executed)
    claims = _count_claims(authority)
    owner = cq.acquire(max_s=60, reason="measure", clock=clock)
    with db:
        authority._schedule = lambda ref: None
        wake = _wake(db, authority)
        del authority._schedule
        live = authority.sessions["s"]
        authority._schedule(REF)
        try:
            await async_wait_until(lambda: len(claims) >= 3, timeout=5, what="the pump re-read the held wake")
            assert executed == []
            await _submit(authority, "hello")  # new human input sets claim_changed and is selected
            await async_wait_until(lambda: executed == ["hello"], timeout=5, what="human runs under quiet")
            assert _statuses(db)[wake["request_id"]] == "queued"  # the wake is still waiting, same identity
            owner.close()
            await async_wait_until(lambda: wake["request_id"] in executed, timeout=5, what="wake resumes")
            assert executed == ["hello", wake["request_id"]]
        finally:
            owner.close()
            await _stop(live.task)
