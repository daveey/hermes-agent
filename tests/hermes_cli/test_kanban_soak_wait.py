"""Clock observation parks must run their checks now, not wait out a nominal soak."""
import pytest

from hermes_cli import kanban_awaiting as kaw
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_wake_check as kwc
from tests.hermes_cli import test_kanban_awaiting as helpers

kanban_home = helpers.kanban_home

SOAKS = [
    "24 h soak after PR #3051 (landed yesterday)",
    "48 h post-apply measurement window for the governor",
    "7-day soak of 141bcec62a",
    "one-week acceptance watch",
    "SCHEDULED_UNTIL=2099-01-01T00:00:00Z — 24h OOM soak window after deploy.",
    "No David decision needed; waiting on the 24h soak. SCHEDULED_UNTIL=2099-01-01T00:00:00Z",
    "No permissions changes needed; waiting for nightly soak.",
    "Per David's decision yesterday, deploy then soak 24h.",
    "Following David's approval in chat, rollout is live; 24h soak.",
    "1 h live check", "60-min observation", "0.5-day recount", "two-hour readback",
    "24 hours after merge", "1 hour after went live",
]


@pytest.mark.parametrize("reason", SOAKS)
@pytest.mark.parametrize("declared", [False, True])
def test_soak_refused_without_mutation(kanban_home, reason, declared):
    with kbc.connect_closing() as conn:
        tid = helpers._running(conn)
        with pytest.raises(kaw.AwaitingError, match="soak_wait"):
            kb.schedule_task(conn, tid, reason=reason, clock_only=True,
                             awaiting={"kind": "clock"} if declared else None)
        assert kb.get_task(conn, tid).status == "running"
        assert kwc.get_state(conn, tid) is None


@pytest.mark.parametrize("op", ["schedule", "block", "continue"])
def test_tool_entry_refusal(kanban_home, monkeypatch, op):
    with kbc.connect_closing() as conn:
        tid = helpers._running(conn)
        helpers._as_worker(monkeypatch, conn, tid)
        args = {"clock_only": True}
        if op == "continue":
            args.update(checkpoint=SOAKS[0], resume_after_seconds=7201)
        else:
            args["reason"] = SOAKS[0]
        if op == "block":
            args["kind"] = "time"
        out = helpers._tool(op, args)
        assert "soak_wait" in (out.get("code"), out.get("rejection")), out
        assert "real window" in out["error"]
        assert kb.get_task(conn, tid).status == "running"


def test_cli_schedule_refusal(kanban_home, capsys):
    with kbc.connect_closing() as conn:
        tid = helpers._running(conn)
    assert helpers._cli()("schedule", tid, SOAKS[0], "--clock-only") != 0
    assert "soak_wait" in capsys.readouterr().err


@pytest.mark.parametrize("seconds", [1800, 7200])
@pytest.mark.parametrize("declared", [False, True])
def test_short_continue_exempt(kanban_home, monkeypatch, seconds, declared):
    with kbc.connect_closing() as conn:
        tid = helpers._running(conn)
        helpers._as_worker(monkeypatch, conn, tid)
        out = helpers._tool("continue", {"checkpoint": SOAKS[0], "resume_after_seconds": seconds,
                                        "awaiting": {"kind": "clock", "why": "pacing the build"} if declared else None})
        assert out.get("status") == "scheduled", out


@pytest.mark.parametrize("location", ["body", "comment"])
def test_explicit_request_exempt(kanban_home, location):
    with kbc.connect_closing() as conn:
        tid = helpers._running(conn)
        marker = 'soak_requested_by: David — "wait a full day for this check"'
        if location == "body":
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET body=? WHERE id=?", (marker, tid))
        else:
            kb.add_comment(conn, tid, author="operator", body=marker)
        assert kb.schedule_task(conn, tid, reason=SOAKS[0], clock_only=True, awaiting={"kind": "clock", "why": "fixture window"})


@pytest.mark.parametrize("marker", [
    'soak_requested_by: David',
    'soak_requested_by: David — ""',
    'soak_requested_by: David — "   \t "',
    'example: soak_requested_by: David — "wait a day"',
    'soak_requested_by: Someone — "wait a day"',
    'soak_requested_by: David — "wait a day" (example only)',
])
def test_marker_must_be_a_complete_request_line(kanban_home, marker):
    with kbc.connect_closing() as conn:
        tid = helpers._running(conn)
        kb.add_comment(conn, tid, author="operator", body=marker)
        with pytest.raises(kaw.AwaitingError, match="soak_wait"):
            kaw.resolve(conn, tid, awaiting={"kind": "clock"}, reason=SOAKS[0])


@pytest.mark.parametrize("location", ["body", "comment"])
def test_foreign_card_request_does_not_exempt(kanban_home, location):
    with kbc.connect_closing() as conn:
        tid, foreign = helpers._running(conn), helpers._running(conn)
        marker = 'soak_requested_by: David — "wait a full day for this check"'
        if location == "body":
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET body=? WHERE id=?", (marker, foreign))
        else:
            kb.add_comment(conn, foreign, author="operator", body=marker)
        with pytest.raises(kaw.AwaitingError, match="soak_wait"):
            kaw.resolve(conn, tid, awaiting={"kind": "clock"}, reason=SOAKS[0])


def test_request_in_park_reason_is_not_card_opt_in(kanban_home):
    with kbc.connect_closing() as conn:
        tid = helpers._running(conn)
        reason = SOAKS[0] + '\nsoak_requested_by: David — "wait a full day"'
        with pytest.raises(kaw.AwaitingError, match="soak_wait"):
            kaw.resolve(conn, tid, awaiting={"kind": "clock"}, reason=reason)


@pytest.mark.parametrize("reason", [
    "30-min observation", "0.5 h live check", "release slot Monday", "cooldown for 3h",
    "quota-reset in 24h; monitoring window", "weekly quota reset; soak check deferred",
    "watch the release", "1 h build", "0-hour observation",
    "Await review and next window", "Brand watch", "expand observation",
])
def test_non_observation_and_quota_clocks(kanban_home, reason):
    with kbc.connect_closing() as conn:
        tid = helpers._running(conn)
        # A declared clock names its why (kanban_awaiting); here the same window text as the reason.
        assert kaw.resolve(conn, tid, awaiting={"kind": "clock", "why": reason}, reason=reason)["kind"] == "clock"


@pytest.mark.parametrize("kind", ["ci", "deploy", "external", "resource", "card"])
def test_non_clock_producers_exempt(kanban_home, monkeypatch, kind):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    with kbc.connect_closing() as conn:
        tid, owner = helpers._running(conn), helpers._running(conn)
        rec = {"kind": kind, "ref": owner if kind == "card" else "release 12345", "why": SOAKS[0]}
        assert kaw.resolve(conn, tid, awaiting=rec, reason=SOAKS[0], wake_check="exit 1")["kind"] == kind
        assert kaw.resolve(conn, tid, awaiting=None, reason=f"awaiting card {owner}")["kind"] == "card"


def test_why_is_checked_but_ref_is_not_observation_prose(kanban_home):
    with kbc.connect_closing() as conn:
        tid = helpers._running(conn)
        with pytest.raises(kaw.AwaitingError, match="soak_wait"):
            kaw.resolve(conn, tid, awaiting={"kind": "clock", "why": SOAKS[0]}, reason="pending")
        assert kaw.resolve(conn, tid, awaiting={"kind": "clock", "ref": "soak", "why": "the release slot"},
                           reason="release slot")
