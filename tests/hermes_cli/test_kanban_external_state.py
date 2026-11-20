"""Pure state machine of external-run cards (light blue / red). [daveey]

Plan cases 1, 3, 4, 5, 7, 8, 9 and 11 at the rule level: a fake clock and plain records, no database.
"""
from __future__ import annotations

import pytest

from hermes_cli import kanban_external_state as ks

EVERY = 300
T0 = 1_000_000.0


def _poll(job="running", service="ok", **kw):
    return {"job": job, "service": service, "detail": kw.pop("detail", ""), "url": kw.pop("url", None),
            "retry_after": kw.pop("retry_after", None), **kw}


def _rec(prev, poll, now):
    return ks.next_record(prev, poll, now=now, every=EVERY, adapter="github_run", scope="o/r", rand=0.5)


def _derive(rec, now, **kw):
    kw.setdefault("every", EVERY)
    return ks.derive(rec, now=now, **kw)


def test_running_job_with_a_fresh_poll_is_external_running():
    rec = _rec(None, _poll("queued"), T0)
    got = _derive(rec, T0 + 10)
    assert (got["state"], got["health"], got["job"], got["wake"]) == ("external_running", "ok", "queued", False)
    assert _derive(_rec(None, _poll("running"), T0), T0 + 10)["state"] == "external_running"


@pytest.mark.parametrize("job", ["succeeded", "failed", "cancelled"])
def test_terminal_job_wakes_and_is_never_down(job):
    got = _derive(_rec(None, _poll(job), T0), T0 + 1)
    assert got["wake"] is True
    assert got["state"] != "external_down"
    assert got["job"] == job


def test_one_5xx_is_unknown_then_recovers_without_wake():
    bad = _rec(None, _poll("unknown", "down", detail="HTTP 502"), T0)
    assert bad["consecutive_failures"] == 1
    got = _derive(bad, T0 + 5)
    assert (got["state"], got["health"]) == ("external_unknown", "down")
    ok = _rec(bad, _poll("running"), T0 + EVERY)
    assert ok["consecutive_failures"] == 0
    got = _derive(ok, T0 + EVERY + 1)
    assert (got["state"], got["wake"]) == ("external_running", False)


def test_two_consecutive_down_polls_are_red_within_two_intervals():
    one = _rec(None, _poll("unknown", "down"), T0)
    two = _rec(one, _poll("unknown", "unreachable"), T0 + EVERY)
    assert two["consecutive_failures"] == 2
    got = _derive(two, T0 + EVERY + 1)
    assert got["state"] == "external_down"
    assert got["health"] == "unreachable"
    assert T0 + EVERY + 1 - T0 <= 2 * EVERY + 1


def test_rate_limit_is_unknown_never_red_and_honours_retry_after():
    prev = None
    for i in range(5):
        prev = _rec(prev, _poll("unknown", "rate_limited", retry_after=700), T0 + i * 700)
        got = _derive(prev, T0 + i * 700 + 1)
        assert got["state"] == "external_unknown" and got["health"] == "rate_limited"
    assert prev["consecutive_failures"] == 0
    assert prev["next_due_at"] >= prev["observed_at"] + 700  # never earlier than retry_after


def test_rate_limited_polls_do_not_reset_an_existing_failure_streak_but_never_add():
    one = _rec(None, _poll("unknown", "down"), T0)
    limited = _rec(one, _poll("unknown", "rate_limited", retry_after=60), T0 + EVERY)
    assert limited["consecutive_failures"] == 1


def test_auth_failed_is_red_at_once():
    got = _derive(_rec(None, _poll("unknown", "auth_failed", detail="no broker credential"), T0), T0 + 1)
    assert (got["state"], got["health"]) == ("external_down", "auth_failed")
    assert "no broker credential" in got["reason"]


def test_silent_host_unknown_after_one_interval_red_after_two():
    rec = _rec(None, _poll("running"), T0)
    assert _derive(rec, T0 + EVERY * 0.9)["state"] == "external_running"
    got = _derive(rec, T0 + EVERY * 1.5)
    assert (got["state"], got["health"]) == ("external_unknown", "host_silent")
    got = _derive(rec, T0 + EVERY * 2.5)
    assert (got["state"], got["health"]) == ("external_down", "host_silent")


def test_silence_stays_grey_for_hosts_that_do_not_advertise():
    rec = _rec(None, _poll("running"), T0)
    assert _derive(rec, T0 + EVERY * 5, advertises=False) is None
    assert _derive(None, T0 + EVERY * 5, since=T0, advertises=False) is None


def test_no_poll_yet_is_unknown_then_silent_red():
    assert _derive(None, T0 + 30, since=T0)["state"] == "external_unknown"
    assert _derive(None, T0 + EVERY * 2.5, since=T0)["health"] == "host_silent"


def test_watch_missing_is_red():
    got = _derive(None, T0, watch_missing=True)
    assert (got["state"], got["health"]) == ("external_down", "watch_missing")


def test_probe_broken_is_red_after_two_failures():
    one = _rec(None, _poll("unknown", "probe_broken"), T0)
    assert _derive(one, T0 + 1)["state"] == "external_unknown"
    two = _rec(one, _poll("unknown", "probe_broken"), T0 + EVERY)
    got = _derive(two, T0 + EVERY + 1)
    assert (got["state"], got["health"]) == ("external_down", "probe_broken")


def test_cadence_jitter_floor_and_retry_after():
    lo = ks.next_record(None, _poll(), now=T0, every=EVERY, adapter="a", scope="s", rand=0.0)
    hi = ks.next_record(None, _poll(), now=T0, every=EVERY, adapter="a", scope="s", rand=1.0)
    assert lo["next_due_at"] - T0 == pytest.approx(EVERY * 0.9)
    assert hi["next_due_at"] - T0 == pytest.approx(EVERY * 1.1)
    floor = ks.next_record(None, _poll(), now=T0, every=10, adapter="a", scope="s", rand=0.0)
    assert floor["next_due_at"] - T0 >= ks.MIN_EVERY
    assert ks.clamp_every(5) == ks.MIN_EVERY and ks.clamp_every(None) == ks.DEFAULT_CI_EVERY
    assert ks.clamp_every("x") == ks.DEFAULT_CI_EVERY


def test_interface_fields_are_always_filled():
    got = _derive(_rec(None, _poll("running", url="https://x/y", detail="job tests"), T0), T0 + 5,
                  service_name="GitHub Actions")
    ext = got["external"]
    assert ext["service"] == "GitHub Actions"
    assert ext["job"] == "running" and ext["health"] == "ok"
    assert ext["reason"] and ext["url"] == "https://x/y"
    assert ext["observed_at"] == T0 and ext["next_due_at"] == pytest.approx(T0 + EVERY)
    for key in ("service", "adapter", "job", "health", "reason", "url", "observed_at", "next_due_at"):
        assert key in ext
    before_poll = _derive(None, T0 + 5, since=T0, service_name="GitHub Actions", adapter="github_run")["external"]
    assert before_poll["service"] == "GitHub Actions" and before_poll["observed_at"] is None
    assert before_poll["job"] == "unknown" and before_poll["health"] == "ok" and before_poll["reason"]
