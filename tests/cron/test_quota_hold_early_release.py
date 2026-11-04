"""A quota hold releases early once its refusing pool serves again (cron/quota_hold.py).

Defect: x-reply-discovery was parked until 2026-11-04T15:07Z by a 11-02 22:07Z
``SubscriptionQuotaUnavailable openai-codex/gpt-6-astra`` refusal. The same pool served sibling
cron sessions all of 11-03, yet the hold was one-way: only a run of the SAME job reaching the model
cleared it, and the hold prevented that run, so ~26 in-window slots were skipped.

Contract: the hold stamps the refusing route; the due scan consults that route's local OAuth pool
(no network) and, when it is serving, clears the hold and moves ``next_run_at`` to the schedule's
NEXT natural occurrence (never an immediate catch-up fire). A pool still exhausted keeps the hold;
a hold that names no route (stamp absent and no persisted pool refusal) keeps today's behaviour.
"""

import logging
from datetime import UTC, datetime, timedelta

import pytest

from agent import credential_pool
import agent.model_routing_unavailable as mru
from agent.model_routing_unavailable import SubscriptionQuotaUnavailable
from cron import quota_hold as qh
from cron.jobs import create_job, get_due_jobs, get_job, mark_job_run, update_job

from tests.cron.test_quota_hold import _tick

ROUTE = {"provider": "openai-codex", "model": "gpt-6-astra"}
# 01:25Z on the incident night; the schedule is x-reply-discovery's own.
NOW = datetime(2026, 11, 4, 1, 25, tzinfo=UTC)
EXPR = "7,37 15-23,0-4 * * *"
NATURAL_NEXT = datetime(2026, 11, 4, 1, 37, tzinfo=UTC)
PARKED = datetime(2026, 11, 4, 15, 7, tzinfo=UTC)
LEGACY_ERROR = ("RuntimeError: Subscription quota unavailable for openai-codex/gpt-6-astra; "
                "resets at 1793774269.0")


def _get(job_id):
    j = get_job(job_id)
    assert j is not None
    return j


@pytest.fixture
def tmp_cron_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr("cron.jobs._hermes_now", lambda: NOW)
    monkeypatch.setattr(qh, "_hermes_now", lambda: NOW)
    return home


class _Pool:
    """The two reads ``pool_serving`` makes of a CredentialPool."""

    def __init__(self, provider, *, exhausted_until=None, available=True):
        self.provider = provider
        self.exhausted_until = exhausted_until
        self.available = available

    def has_available(self, *, model=None):
        return self.available


@pytest.fixture
def pool(monkeypatch):
    """The local pool state the hold consults, plus a record of every (provider, model) read."""
    state = {"exhausted_until": None, "available": True, "reads": []}

    def load_pool(provider, *, allowed_auth_types=None):
        assert allowed_auth_types == {"oauth"}
        return _Pool(provider, exhausted_until=state["exhausted_until"], available=state["available"])

    def quota_unavailable(p, model):
        state["reads"].append((p.provider, model))
        if p.exhausted_until is None:
            return None
        return SubscriptionQuotaUnavailable(p.provider, model, p.exhausted_until)

    monkeypatch.setattr(credential_pool, "load_pool", load_pool)
    monkeypatch.setattr(mru, "quota_unavailable", quota_unavailable)
    return state


def _held_job(*, route=ROUTE, last_error=LEGACY_ERROR, expr=EXPR):
    """A cron job parked by a pool refusal exactly as mark_job_run leaves it."""
    job = create_job("x-reply-discovery", expr)
    fields = {
        "next_run_at": PARKED.isoformat(),
        qh.STATE_KEY: PARKED.isoformat(),
        qh.SCHEDULE_EXPR_KEY: None,
        "last_status": "error",
        "last_error": last_error,
        "last_run_at": (NOW - timedelta(hours=27)).isoformat(),
    }
    if route is not None:
        fields[qh.ROUTE_KEY] = dict(route)
    update_job(job["id"], fields)
    held = _get(job["id"])
    assert held[qh.STATE_KEY] == PARKED.isoformat()
    return held["id"]


def test_pool_serving_releases_hold_to_next_natural_slot_without_firing(
        tmp_cron_home, pool, caplog, monkeypatch):
    """(a) Route stamped + pool serving: released to the next 7,37 slot, not fired this tick."""
    job_id = _held_job()
    with caplog.at_level(logging.INFO, logger="cron.scheduler"):
        due = get_due_jobs()
    assert job_id not in {d["id"] for d in due}, "a released hold must never catch-up fire"
    j = _get(job_id)
    assert datetime.fromisoformat(j["next_run_at"]) == NATURAL_NEXT
    for key in (qh.STATE_KEY, qh.SCHEDULE_EXPR_KEY, qh.ROUTE_KEY):
        assert key not in j
    assert pool["reads"] == [("openai-codex", "gpt-6-astra")]
    assert any("quota hold released early: pool serving" in r.getMessage() for r in caplog.records)

    # The released job then fires at that natural slot, like any healthy cron job.
    later = NATURAL_NEXT + timedelta(seconds=5)
    monkeypatch.setattr("cron.jobs._hermes_now", lambda: later)
    monkeypatch.setattr(qh, "_hermes_now", lambda: later)
    assert job_id in {d["id"] for d in get_due_jobs()}


def test_pool_still_exhausted_keeps_hold(tmp_cron_home, pool):
    """(b) Route stamped + pool still exhausted (or nothing available): stays parked."""
    job_id = _held_job()
    pool["exhausted_until"] = PARKED.timestamp()
    assert job_id not in {d["id"] for d in get_due_jobs()}
    j = _get(job_id)
    assert j["next_run_at"] == PARKED.isoformat()
    assert j[qh.STATE_KEY] == PARKED.isoformat()
    assert j[qh.ROUTE_KEY] == ROUTE

    # No cooldown recorded but no usable entry either: an empty pool is not "serving".
    pool["exhausted_until"] = None
    pool["available"] = False
    assert job_id not in {d["id"] for d in get_due_jobs()}
    assert _get(job_id)[qh.STATE_KEY] == PARKED.isoformat()


def test_pool_read_fault_keeps_hold(tmp_cron_home, monkeypatch):
    job_id = _held_job()

    def broken(provider, *, allowed_auth_types=None):
        raise OSError("auth.json unreadable")

    monkeypatch.setattr(credential_pool, "load_pool", broken)
    assert job_id not in {d["id"] for d in get_due_jobs()}
    assert _get(job_id)[qh.STATE_KEY] == PARKED.isoformat()


def test_legacy_hold_without_route_is_unchanged(tmp_cron_home, pool):
    """(c) No stamped route and no persisted pool refusal to name one: one-way as before, and the
    pool is never consulted."""
    job_id = _held_job(route=None, last_error="RuntimeError: Codex provider quota exhausted (429); "
                                              "retry after 123518s.")
    assert job_id not in {d["id"] for d in get_due_jobs()}
    j = _get(job_id)
    assert j["next_run_at"] == PARKED.isoformat()
    assert j[qh.STATE_KEY] == PARKED.isoformat()
    assert pool["reads"] == []


def test_pre_stamp_hold_from_pool_refusal_releases(tmp_cron_home, pool):
    """The live x-reply-discovery record: parked before the stamp existed, but its persisted
    last_error is the exact SubscriptionQuotaUnavailable refusal, which names the route. Read only
    to release after the pool check agrees; the fix (not a jobs.json edit) unsticks it."""
    job_id = _held_job(route=None)
    assert job_id not in {d["id"] for d in get_due_jobs()}
    j = _get(job_id)
    assert datetime.fromisoformat(j["next_run_at"]) == NATURAL_NEXT
    assert qh.STATE_KEY not in j
    assert pool["reads"] == [("openai-codex", "gpt-6-astra")]


def test_no_earlier_natural_slot_keeps_hold(tmp_cron_home, pool):
    """When the next natural occurrence is the parked instant itself (or later), nothing moves."""
    job_id = _held_job(expr="7 15 * * *")
    assert job_id not in {d["id"] for d in get_due_jobs()}
    assert _get(job_id)[qh.STATE_KEY] == PARKED.isoformat()
    assert pool["reads"] == []


def test_interval_hold_stays_one_way(tmp_cron_home, pool):
    """Interval holds are out of scope: a cleared marker on an errored interval job would let the
    stale-error re-arm fire it immediately."""
    job = create_job("interval job", "every 30m")
    update_job(job["id"], {"next_run_at": PARKED.isoformat(), qh.STATE_KEY: PARKED.isoformat(),
                           qh.ROUTE_KEY: dict(ROUTE), "last_status": "error",
                           "last_error": LEGACY_ERROR})
    assert job["id"] not in {d["id"] for d in get_due_jobs()}
    assert _get(job["id"])[qh.STATE_KEY] == PARKED.isoformat()
    assert pool["reads"] == []


def test_mark_job_run_stamps_route_and_model_reach_clears_it(tmp_cron_home):
    job = create_job("x-reply-discovery", EXPR)
    assert mark_job_run(job["id"], False, LEGACY_ERROR, quota_hold_seconds=13 * 3600,
                        quota_hold_route=dict(ROUTE))
    j = _get(job["id"])
    assert j[qh.STATE_KEY] and j[qh.ROUTE_KEY] == ROUTE
    assert mark_job_run(job["id"], False, "RuntimeError: model said no")
    j = _get(job["id"])
    assert qh.STATE_KEY not in j and qh.ROUTE_KEY not in j


def test_route_from_pool_refusal_and_fallback_chain():
    def wrapped(exc):
        try:
            raise RuntimeError(str(exc)) from exc
        except RuntimeError as w:
            return w

    refusal = SubscriptionQuotaUnavailable("openai-codex", "gpt-6-astra", NOW.timestamp() + 3600)
    assert qh.route_from_failure(wrapped(refusal)) == ROUTE
    assert qh.route_from_failure(RuntimeError(LEGACY_ERROR)) is None  # text alone never stamps


def test_scheduler_tick_on_pool_refusal_stamps_route(tmp_cron_home, cron_owner, monkeypatch):
    """The real run path: a resolve that raises the pool refusal parks the job AND stamps the
    refusing route, so the next due scan can release it."""
    real_now = datetime.now(UTC)
    monkeypatch.setattr("cron.jobs._hermes_now", lambda: real_now)
    monkeypatch.setattr(qh, "_hermes_now", lambda: real_now)
    job = create_job("x-reply-discovery", "every 30m")
    refusal = SubscriptionQuotaUnavailable("openai-codex", "gpt-6-astra", real_now.timestamp() + 13 * 3600)

    def resolve(**_kw):
        raise refusal

    _tick(_get(job["id"]), tmp_cron_home, [], resolve)
    j = _get(job["id"])
    assert j[qh.STATE_KEY], j.get("last_error")
    assert j[qh.ROUTE_KEY] == ROUTE
    assert "_quota_hold_route" not in j


def test_fallback_chain_stamps_terminal_route():
    from hermes_cli.fallback_errors import ProviderFallbackError

    reset = NOW.timestamp() + 3600
    chain = ProviderFallbackError([
        ("anthropic", "claude-opus-5-5", SubscriptionQuotaUnavailable("anthropic", "claude-opus-5-5", reset)),
        ("openai-codex", "gpt-6-astra", SubscriptionQuotaUnavailable("openai-codex", "gpt-6-astra", reset)),
    ])
    assert qh.route_from_failure(chain) == ROUTE
    assert qh.route_from_failure(ProviderFallbackError([])) is None


def test_timezone_pinned_job_releases_to_its_own_zones_next_slot(tmp_cron_home, pool):
    """01:25Z is 17:25 in Los Angeles (PST): the next 7,37 17-18 slot there is 17:37 PST = 01:37Z.
    Read in UTC instead it would be 17:07Z, after the parked 15:07Z, and nothing would move."""
    job_id = _held_job(expr="7,37 17-18 * * *")
    update_job(job_id, {"timezone": "America/Los_Angeles"})
    update_job(job_id, {"next_run_at": PARKED.isoformat(), qh.STATE_KEY: PARKED.isoformat()})
    assert job_id not in {d["id"] for d in get_due_jobs()}
    j = _get(job_id)
    assert datetime.fromisoformat(j["next_run_at"]) == NATURAL_NEXT
    assert qh.STATE_KEY not in j


def test_queued_run_now_keeps_hold_and_skips_pool_read(tmp_cron_home, pool):
    """A run-now queued during the hold waits for the boundary (extra_fire.reschedule owns it);
    the early release never rewrites next_run_at under it."""
    from cron import extra_fire
    from cron.jobs import trigger_job

    job_id = _held_job()
    assert trigger_job(job_id) is not None
    assert extra_fire.queued(_get(job_id))
    get_due_jobs()
    assert pool["reads"] == []
    assert _get(job_id).get(qh.STATE_KEY) == PARKED.isoformat()


def test_real_credential_pool_drives_pool_serving(tmp_path, monkeypatch):
    """No mocks on the pool: a temp HERMES_HOME auth store with every OAuth entry cooling down is
    not serving; one healthy entry is."""
    import time
    from pathlib import Path

    from agent.credential_pool import PooledCredential
    from hermes_cli.auth import write_credential_pool

    # The suite's hermetic HERMES_HOME holds the store; external CLI stores stay in tmp_path.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_SHARED_AUTH_DIR", str(tmp_path / "shared"))

    def rows(exhausted):
        now = time.time()
        out = []
        for i in range(2):
            row = dict(id=f"{'cool' if exhausted else 'mixed'}{i}", label=f"account{i}",
                       source="manual:device_code", auth_type="oauth",
                       access_token=f"fixture-access-{i}", refresh_token=f"fixture-refresh-{i}", priority=i)
            if exhausted or i == 0:
                row.update(last_status="exhausted", last_status_at=now, last_error_code=429,
                           last_error_reset_at=now + 3600)
            out.append(PooledCredential.from_dict("openai-codex", row).to_dict())
        return out

    write_credential_pool("openai-codex", rows(exhausted=True))
    assert qh.pool_serving(ROUTE) is False
    write_credential_pool("openai-codex", rows(exhausted=False))
    assert qh.pool_serving(ROUTE) is True
