"""A pre-retirement wall booking must not strand the successor for days."""
import json

import pytest

from hermes_cli import host_loop_lease
from tests.tui_gateway import test_notice_guarantee as helpers
from tui_gateway import automation_replay as ar
from tui_gateway import notice_guarantee as ng
from tui_gateway import notice_retry as nr


db = helpers.db


def test_successor_recovers_pre_retirement_wall_booking(db, monkeypatch):
    now = 2_000_000_000.0
    reset = now + 4 * 86400
    monkeypatch.setattr(ng.time, "time", lambda: now)
    monkeypatch.setattr(host_loop_lease, "_core_sha", lambda: "a" * 40)
    monkeypatch.setattr(host_loop_lease, "host_loops_retired", lambda: None)
    from tui_gateway import server
    monkeypatch.setattr(server, "_find_live_session_by_key", lambda sid: (
        sid, {"agent_failure": {"failure_resets_at": reset}}))
    row = helpers._notice(db, "done")
    assert helpers._run(db, verdict="error", now=now)[1] == [(helpers.CHAT, row)]
    assert tuple(helpers._closeout_meta(db, row)) == ("deferred", reset + ng.DEFER_SLACK_S, 1)
    short = now + ar.REPLAY_MIN_DELAY_S + ng.DEFER_SLACK_S
    # Same code (including a same-code process restart) still trusts the wall.
    assert helpers._run(db, now=short) == ([], [])
    # The process-local replay is gone; only the stored row survives the handoff.
    ar.reset()
    ng._reset_refreshed_for_tests()
    monkeypatch.setattr(host_loop_lease, "_core_sha", lambda: "b" * 40)
    assert helpers._run(db, now=short - 1) == ([], [])
    assert helpers._run(db, now=short)[1] == [(helpers.CHAT, row)]
    assert helpers._closeout_meta(db, row)[2] == 2
    assert helpers._run(db, now=reset + 9999) == ([], [])


@pytest.mark.parametrize("booking,current", [(None, "b" * 40), ("a" * 40, None),
                                             ("a" * 40, "a" * 40)])
def test_unknown_or_same_code_keeps_durable_reset(db, monkeypatch, booking, current):
    now = 2_000_000_000.0
    monkeypatch.setattr(ng.time, "time", lambda: now)
    monkeypatch.setattr(host_loop_lease, "_core_sha", lambda: booking)
    monkeypatch.setattr(ng, "_retry_at", lambda sid, t: t + 86400)
    row = helpers._notice(db, "done")
    helpers._run(db, verdict="error", now=now)
    monkeypatch.setattr(host_loop_lease, "_core_sha", lambda: current)
    assert helpers._run(db, now=now + 1000) == ([], [])
    assert helpers._run(db, now=now + 86400)[1] == [(helpers.CHAT, row)]


def test_successor_does_not_shorten_busy_backoff(db, monkeypatch):
    now = 2_000_000_000.0
    monkeypatch.setattr(ng.time, "time", lambda: now)
    monkeypatch.setattr(host_loop_lease, "_core_sha", lambda: "b" * 40)
    row = helpers._notice(db, "done", closeout={
        "state": "deferred", "retry_at": now + 3600, "attempts": 5, "busy_attempts": 5})
    assert helpers._run(db, now=now + 1000) == ([], [])
    assert helpers._run(db, now=now + 3600)[1] == [(helpers.CHAT, row)]


def test_legacy_wall_booking_keeps_deadline(db, monkeypatch):
    now = 2_000_000_000.0
    monkeypatch.setattr(ng.time, "time", lambda: now)
    monkeypatch.setattr(host_loop_lease, "_core_sha", lambda: "b" * 40)
    row = helpers._notice(db, "done", closeout={
        "state": "deferred", "retry_at": now + 86400, "attempts": 1})
    assert helpers._run(db, now=now + 1000) == ([], [])
    raw = db._read_one("SELECT display_metadata FROM messages WHERE id = ?", (row,))[0]
    assert json.loads(raw)["closeout"]["retry_at"] == now + 86400


@pytest.mark.parametrize("field,value", [
    ("retry_code_sha", ""), ("retry_code_sha", 7),
    ("retry_short_at", None), ("retry_short_at", True),
    ("retry_short_at", "100"), ("retry_short_at", float("nan")),
    ("retry_short_at", float("inf")), ("retry_at", None),
    ("retry_at", True), ("retry_at", "200"),
])
def test_malformed_provenance_keeps_original(field, value, monkeypatch):
    monkeypatch.setattr(host_loop_lease, "_core_sha", lambda: "b" * 40)
    closeout = {"retry_code_sha": "a" * 40, "retry_at": 200, "retry_short_at": 100, field: value}
    assert nr.effective_due(closeout) == closeout["retry_at"]


def test_booking_only_marks_extended_delays_and_never_extends_due(monkeypatch):
    monkeypatch.setattr(host_loop_lease, "_core_sha", lambda: "a" * 40)
    assert nr.booking(100, 100) == {}
    assert nr.booking(99, 100) == {}
    assert nr.booking(101, 100) == {"retry_code_sha": "a" * 40, "retry_short_at": 100}
    monkeypatch.setattr(host_loop_lease, "_core_sha", lambda: None)
    assert nr.booking(101, 100) == {}
    monkeypatch.setattr(host_loop_lease, "_core_sha", lambda: "b" * 40)
    assert nr.effective_due({"retry_code_sha": "a" * 40, "retry_at": 50, "retry_short_at": 100}) == 50


def test_claim_cas_still_fences_successor_and_new_failure_rebooks(db, monkeypatch):
    now = 2_000_000_000.0
    monkeypatch.setattr(ng.time, "time", lambda: now)
    monkeypatch.setattr(host_loop_lease, "_core_sha", lambda: "b" * 40)
    monkeypatch.setattr(ng, "_retry_at", lambda sid, t: t + 86400)
    row = helpers._notice(db, "done", closeout={
        "state": "deferred", "retry_at": now + 86400, "attempts": 1,
        "retry_code_sha": "a" * 40, "retry_short_at": now})
    stale = ng.candidates(db, now)[0]
    assert helpers._run(db, verdict="error", now=now)[1] == [(helpers.CHAT, row)]
    assert not ng._claim(db, stale, now)
    assert helpers._closeout_meta(db, row)[2] == 2
    assert helpers._run(db, now=now + 1000) == ([], [])
