"""No producer, no park: a timed park must name who produces its condition. [daveey]

Replays of the four 10-26/27 orphan waits (``~/.hermes/kanban/evidence/orphan-waits-20261027/
5whys.md``; reasons verbatim from the parking events and the state-branch fixtures in
``tests/test_session_idle_orphan_wait.py``) must be refused with a typed error, and the controls
from those fixtures (a running card, an in-progress CI run, a pure clock soak, a wait on a card
blocked on David with a visible ask) must still park.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import time
from pathlib import Path

import pytest

from hermes_cli import kanban_awaiting as kaw
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_wake_check as kwc

IAM_DESCRIBE = ("Waiting on AWS describe authority for hub role (denied, re-verified). Probe polls hub role ec2 "
                "describe_vpcs until it succeeds.")
IAM_MUTATION = ("SCHEDULED_UNTIL=2026-10-28T07:30:00Z \u2014 Read-only discovery works (inventory-20261027.txt), but "
                "hub role is denied EC2 mutation (DryRun RunInstances/CreateLaunchTemplate/CreateSecurityGroup "
                "UnauthorizedOperation). Needs the scoped hermes-asg-ci-* us-west-2 grant in mutation-permission-"
                "request.md applied by the supervisor (same route as the discovery fix); card wakes when a "
                "CreateSecurityGroup DryRun succeeds.")
SUPPORT_TICKET = ("Parked by supervisor on a REAL recovery signal (a hermes artifact exists), not the shared "
                  "github-actions-billing auto-clear. The human step stays unchanged: David's decision on sending "
                  "support draft attachment180, already asked in chat; don't re-ask. Wakes when artifacts "
                  "total_count>0 or on unblock.")
CONSOLE_OUTPUT = ("SCHEDULED_UNTIL=2026-10-27T14:00:00Z \u2014 waiting for supervisor t_4c3070a4 to grant "
                  "ec2:GetConsoleOutput (and rollback perms) for hermes-hub on Project=hermes-asg-ci; asks posted. "
                  "Wakes when GetConsoleOutput on the pilot instance stops being denied.")


@pytest.fixture
def kanban_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_WAKE_DRYRUN", "0")
    monkeypatch.setenv("ASK_GATE_LEDGER", str(home / "state" / "ask-ledger.sqlite"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    yield home
    kwc._shutdown_for_tests()


def _running(conn) -> str:
    tid = kb.create_task(conn, title="t", assignee="worker")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    assert kb.claim_task(conn, tid, claimer="worker") is not None
    return tid


def _as_worker(monkeypatch, conn, tid):
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(kb.get_task(conn, tid).current_run_id))


def _human_blocked(conn) -> str:
    tid = _running(conn)
    assert kb.block_task(conn, tid, reason="Approve the scoped restart", kind="needs_input", human_request={
        "action": "Approve restart", "why_human": "policy", "interaction": "approval",
        "scope": "hub gateway", "evidence": "drain plan attached"})
    return tid


def _park_payload(conn, tid) -> dict:
    return [e for e in kb.list_events(conn, tid) if e.kind in ("scheduled", "continued")][-1].payload


def _tool(name, args):
    from tools import kanban_tools as kt
    return json.loads(getattr(kt, f"_handle_{name}")(args))


# -- replays: refused with a typed error, or turned into a visible ask -------------------------------

@pytest.mark.parametrize("op,args", [
    ("schedule", {"reason": IAM_DESCRIBE, "wake_check": "python3 aws_hub_ec2_probe.py"}),
    ("block", {"reason": IAM_MUTATION, "kind": "time", "wake_check": "python3 dryrun.py"}),
    ("block", {"reason": IAM_MUTATION, "kind": "time", "clock_only": True}),
    ("schedule", {"reason": CONSOLE_OUTPUT, "wake_check": "python3 asg_console_probe.py"}),
    ("schedule", {"reason": IAM_MUTATION, "clock_only": True, "awaiting": {"kind": "clock", "why": "31h"}}),
    ("schedule", {"reason": CONSOLE_OUTPUT, "awaiting": {"kind": "card", "ref": "t_4c3070a4"}}),
])
def test_agent_action_waits_with_no_producer_are_refused(kanban_home, monkeypatch, op, args) -> None:
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _as_worker(monkeypatch, conn, tid)
        out = _tool(op, args)
        assert out.get("code") == kaw.AGENT_ACTION_WAIT or kaw.AGENT_ACTION_WAIT in out.get("error", ""), out
        assert "owner card" in out["error"] and "assignment" in out["error"]
        task = kb.get_task(conn, tid)
        assert task.status == "running" and kwc.get_state(conn, tid) is None


@pytest.mark.parametrize("reason,code", [
    ("Resume once David's approval lands.", kaw.HUMAN_WAIT),
    ("Park until after David's go-ahead on the migration.", kaw.HUMAN_WAIT),
    ("with David's approval pending, parking", kaw.HUMAN_WAIT),
    ("the supervisor was asked to apply the grant", kaw.AGENT_ACTION_WAIT),
    ("AccessDenied: the role was missing s3:PutObject permission", kaw.AGENT_ACTION_WAIT),
])
def test_pending_asks_and_walls_stay_refused(kanban_home, monkeypatch, reason, code) -> None:
    assert kaw.text_wall(reason)[0] == code
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _as_worker(monkeypatch, conn, tid)
        assert code in _tool("schedule", {"reason": reason, "wake_check": "exit 1"})["error"]


def test_hidden_human_ask_is_refused_as_a_park(kanban_home, monkeypatch) -> None:
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _as_worker(monkeypatch, conn, tid)
        out = _tool("schedule", {"reason": SUPPORT_TICKET, "wake_check": "gh api artifacts | grep -qv ^0$"})
        assert kaw.HUMAN_WAIT in out["error"] and "human_request" in out["error"], out
        out = _tool("schedule", {"reason": "need a call", "awaiting": {"kind": "human", "why": "approve"}})
        assert kaw.HUMAN_WAIT in out["error"]
        assert kb.get_task(conn, tid).status == "running"
        # The visible path still works: a needs_input block with a human_request.
        out = _tool("block", {"reason": "Send support draft attachment180?", "kind": "needs_input",
                              "human_request": {"action": "Send draft 180", "why_human": "your account",
                                                "interaction": "approval", "scope": "GitHub support",
                                                "evidence": "draft attached"}})
        assert out.get("status") == "blocked", out


def test_answered_ask_is_never_re_asked(kanban_home, monkeypatch) -> None:
    ledger = Path(kanban_home) / "state" / "ask-ledger.sqlite"
    ledger.parent.mkdir(parents=True)
    c = sqlite3.connect(ledger)
    c.execute("create table answered (id integer primary key, ts real, session text, topic text, answer text, "
              "card_id text)")
    c.execute("insert into answered(ts, session, topic, answer, card_id) values (?,?,?,?,?)",
              (time.time(), "s", "send support draft attachment180 about artifact quota",
               "will not be solved; drop it", ""))
    c.commit()
    c.close()
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _as_worker(monkeypatch, conn, tid)
        out = _tool("schedule", {"reason": SUPPORT_TICKET, "wake_check": "exit 1"})
        assert "already answered" in out["error"] and "will not be solved" in out["error"], out


def test_cli_refuses_the_same_parks(kanban_home, capsys) -> None:
    from hermes_cli import kanban as kcli

    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    kcli.build_parser(parser.add_subparsers(dest="command"))

    def run(*argv):
        return kcli.kanban_command(parser.parse_args(["kanban", *argv]))

    with kbc.connect_closing() as conn:
        a, b, ci = _running(conn), _running(conn), _running(conn)
    assert run("schedule", a, IAM_DESCRIBE, "--wake-check", "exit 1") != 0
    assert kaw.AGENT_ACTION_WAIT in capsys.readouterr().err
    assert run("block", b, IAM_MUTATION, "--kind", "time", "--clock-only") != 0
    assert kaw.AGENT_ACTION_WAIT in capsys.readouterr().err
    assert run("schedule", ci, "SCHEDULED_UNTIL=2099-01-01T00:00:00Z waiting for the release build",
               "--wake-check", "exit 1", "--awaiting", "ci:40443505012", "--awaiting-why", "in progress") == 0
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, a).status == "running" and kb.get_task(conn, b).status == "running"
        assert _park_payload(conn, ci)["awaiting"]["ref"] == "40443505012"
        # A refused park leaves no "SCHEDULED: ..." / "BLOCKED: ..." comment behind.
        assert not kb.list_comments(conn, a) and not kb.list_comments(conn, b)


# -- controls: real waits still park, with their producer recorded ------------------------------------

def test_wait_on_a_running_card_parks_with_a_derived_probe(kanban_home, monkeypatch) -> None:
    with kbc.connect_closing() as conn:
        owner = _running(conn)
        tid = _running(conn)
        _as_worker(monkeypatch, conn, tid)
        out = _tool("schedule", {"reason": "Waiting on scoped IAM grant from owner card (running).",
                                 "awaiting": {"kind": "card", "ref": owner}})
        assert out.get("status") == "scheduled", out
        rec = _park_payload(conn, tid)["awaiting"]
        assert rec["kind"] == "card" and rec["ref"] == owner and not rec.get("inferred")
        assert kwc.get_state(conn, tid)["cmd"] == f"hermes kanban wait-check task {owner}"


def test_wall_owned_by_a_card_only_named_in_prose_must_be_declared(kanban_home, monkeypatch) -> None:
    """A mentioned id is not a producer (a tracker card was named on 10-27); the owner is declared."""
    with kbc.connect_closing() as conn:
        owner = _running(conn)
        tid = _running(conn)
        _as_worker(monkeypatch, conn, tid)
        reason = f"Waiting on scoped IAM grant from owner card {owner} (running); hub role denied."
        out = _tool("schedule", {"reason": reason, "wake_check": "python3 dryrun.py"})
        assert kaw.AGENT_ACTION_WAIT in out["error"] and f"ref:'{owner}'" in out["error"], out
        out = _tool("schedule", {"reason": reason, "wake_check": "python3 dryrun.py",
                                 "awaiting": {"kind": "card", "ref": owner}})
        assert out.get("status") == "scheduled", out


@pytest.mark.parametrize("status", ["triage", "blocked"])
def test_replay_tracker_card_that_produces_nothing_is_refused(kanban_home, monkeypatch, status) -> None:
    """10-27 03:35: t_4c3070a4 existed, as a tracker that was itself idle."""
    with kbc.connect_closing() as conn:
        tracker, tid = _running(conn), _running(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status=?, current_run_id=NULL, block_kind='transient' WHERE id=?",
                         (status, tracker))
        _as_worker(monkeypatch, conn, tid)
        reason = CONSOLE_OUTPUT.replace("t_4c3070a4", tracker)
        for extra in ({}, {"awaiting": {"kind": "card", "ref": tracker}}):
            out = _tool("schedule", {"reason": reason, "wake_check": "exit 1", **extra})
            assert kaw.AGENT_ACTION_WAIT in out["error"], out
        assert kb.get_task(conn, tid).status == "running"


@pytest.mark.parametrize("reason", [
    "Waiting for AWS support to grant the quota increase (case 1234567).",
    "Upstream API returns 403 Forbidden while their maintenance runs; SCHEDULED_UNTIL=2099-01-01T00:00:00Z",
    "Rate limiter returned 401 unauthorized until the token rotates at 02:00.",
    "No permissions changes needed; waiting for nightly soak.",
    "Earlier run hit permission denied on cache; fixed in PR 12. Waiting for CI rerun.",
    "Per David's decision yesterday, deploy then soak 24h.",
    "Following David's approval in chat, rollout is live; 24h soak.",
    "David's decision recorded 23:06 (design A); waiting for TestFlight processing build 412.",
])
def test_ordinary_park_prose_is_not_a_wall(kanban_home, monkeypatch, reason) -> None:
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _as_worker(monkeypatch, conn, tid)
        out = _tool("schedule", {"reason": reason, "wake_check": "exit 1"})
        assert out.get("status") == "scheduled", out


def test_in_progress_ci_run_parks(kanban_home, monkeypatch) -> None:
    with kbc.connect_closing() as conn:
        a, b = _running(conn), _running(conn)
        _as_worker(monkeypatch, conn, a)
        out = _tool("schedule", {"reason": "Waiting for CI run 40443505012 on PR92 to finish.",
                                 "wake_check": "gh run view 40443505012 --json status | grep -q completed",
                                 "awaiting": {"kind": "ci", "ref": "40443505012", "why": "in progress"}})
        assert out.get("status") == "scheduled", out
        _as_worker(monkeypatch, conn, b)
        out = _tool("schedule", {"reason": "Waiting for CI run 40443505012 on PR92 to finish (in progress on a "
                                           "self-hosted runner).", "wake_check": "gh run view"})
        assert out.get("status") == "scheduled", out
        assert _park_payload(conn, b)["awaiting"]["inferred"] is True
        _as_worker(monkeypatch, conn, b)


def test_ci_wait_needs_a_run_id(kanban_home, monkeypatch) -> None:
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _as_worker(monkeypatch, conn, tid)
        out = _tool("schedule", {"reason": "CI", "wake_check": "exit 1", "awaiting": {"kind": "ci", "ref": "soon"}})
        assert kaw.INVALID_AWAITING in out["error"], out


@pytest.mark.parametrize("reason", [
    "SCHEDULED_UNTIL=2099-01-01T00:00:00Z \u2014 24h OOM soak window after deploy.",
    "No David decision needed; waiting on the 24h soak. SCHEDULED_UNTIL=2099-01-01T00:00:00Z",
    "Fixed file permissions on the cache dir; waiting for the nightly run at 02:00 to confirm.",
])
def test_pure_clock_waits_park(kanban_home, monkeypatch, reason) -> None:
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _as_worker(monkeypatch, conn, tid)
        out = _tool("block", {"reason": reason, "kind": "time", "clock_only": True})
        assert out.get("status") == "scheduled", out
        assert _park_payload(conn, tid)["awaiting"]["kind"] == "clock"


def test_wait_on_a_card_blocked_on_david_with_a_visible_ask_parks(kanban_home, monkeypatch) -> None:
    with kbc.connect_closing() as conn:
        ask = _human_blocked(conn)
        a, b = _running(conn), _running(conn)
        _as_worker(monkeypatch, conn, a)
        out = _tool("schedule", {"reason": f"Waiting on David's approval in {ask}.",
                                 "wake_check": f"hermes kanban wait-check task {ask}"})
        assert out.get("status") == "scheduled", out
        _as_worker(monkeypatch, conn, b)
        out = _tool("schedule", {"reason": "restart approval", "awaiting": {"kind": "card", "ref": ask}})
        assert out.get("status") == "scheduled", out


def test_card_producer_that_is_itself_an_orphan_is_refused(kanban_home, monkeypatch) -> None:
    with kbc.connect_closing() as conn:
        orphan, tid = _running(conn), _running(conn)
        # A legacy park (before awaiting existed) on an agent-only wall.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='scheduled', current_run_id=NULL WHERE id=?", (orphan,))
            kb._append_event(conn, orphan, "scheduled", {"reason": IAM_DESCRIBE})
        _as_worker(monkeypatch, conn, tid)
        out = _tool("schedule", {"reason": "tracker", "awaiting": {"kind": "card", "ref": orphan}})
        assert kaw.AGENT_ACTION_WAIT in out["error"] and orphan in out["error"], out


def test_unconfirmed_remote_card_is_refused_inside_the_txn(kanban_home) -> None:
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        with pytest.raises(kaw.AwaitingError, match="not confirmed"):
            kb.schedule_task(conn, tid, reason="x", wake_check="exit 1",
                             awaiting={"kind": "card", "ref": "t_00000000aaaaaaaa", "gateway": "mini"})
        assert kb.get_task(conn, tid).status == "running"


def test_cross_gateway_card_is_confirmed_with_wait_check(kanban_home, monkeypatch) -> None:
    from hermes_cli import kanban_wait_check as kwait

    calls = []
    monkeypatch.setattr(kwait, "check_remote", lambda gw, kind, ref, board=None, until=None: (
        calls.append((gw, ref)) or ((1, f"{ref} running") if ref == "t_01a17cee71315bb0" else (2, "error: not found"))))
    with kbc.connect_closing() as conn:
        a, b = _running(conn), _running(conn)
        _as_worker(monkeypatch, conn, a)
        out = _tool("schedule", {"reason": "MacBook owner card extends the hub role policy.",
                                 "awaiting": {"kind": "card", "ref": "t_01a17cee71315bb0", "gateway": "macbook"}})
        assert out.get("status") == "scheduled", out
        assert kwc.get_state(conn, a)["cmd"] == "hermes kanban wait-check task t_01a17cee71315bb0 --gateway macbook"
        _as_worker(monkeypatch, conn, b)
        out = _tool("schedule", {"reason": "x", "awaiting": {"kind": "card", "ref": "macbook:t_0000000000000000"}})
        assert kaw.INVALID_AWAITING in out["error"] and "cannot confirm" in out["error"], out
    assert calls == [("macbook", "t_01a17cee71315bb0"), ("macbook", "t_0000000000000000")]
    monkeypatch.setattr(kwait, "check_remote", lambda gw, kind, ref, board=None, until=None: (1, f"{ref} triage"))
    with kbc.connect_closing() as conn:
        c = _running(conn)
        _as_worker(monkeypatch, conn, c)
        out = _tool("schedule", {"reason": "x", "awaiting": {"kind": "card", "ref": "t_0000000000000001",
                                                            "gateway": "macbook"}})
        assert kaw.AGENT_ACTION_WAIT in out["error"] and "triage" in out["error"], out


def test_short_continue_is_not_judged_by_its_checkpoint_prose(kanban_home, monkeypatch) -> None:
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _as_worker(monkeypatch, conn, tid)
        out = _tool("continue", {"checkpoint": "Earlier the role was denied; fixed. Next: rerun tests.",
                                 "resume_after_seconds": 300})
        assert out.get("status") == "scheduled", out
        assert _park_payload(conn, tid)["awaiting"] == {"kind": "clock", "inferred": True}


def test_long_continue_on_an_agent_wall_is_refused(kanban_home, monkeypatch) -> None:
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _as_worker(monkeypatch, conn, tid)
        out = _tool("continue", {"checkpoint": IAM_MUTATION, "resume_after_seconds": 20000, "clock_only": True})
        assert kaw.AGENT_ACTION_WAIT in out["error"], out
        assert kb.get_task(conn, tid).status == "running"


# -- re-parks escalate --------------------------------------------------------------------------------

def test_third_park_on_the_same_premise_goes_to_the_supervisor(kanban_home, monkeypatch) -> None:
    reason = "Waiting for vendor ticket 4711 (AWS quota increase), evidence: case opened {n}"
    awaiting = {"kind": "external", "ref": "AWS support case 4711", "why": "quota increase pending"}
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        for n in range(2):
            _as_worker(monkeypatch, conn, tid)
            out = _tool("schedule", {"reason": reason.format(n=n), "wake_check": "exit 1", "awaiting": awaiting})
            assert out.get("status") == "scheduled", out
            assert kb.unblock_task(conn, tid)
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
            assert kb.claim_task(conn, tid, claimer="worker") is not None
        _as_worker(monkeypatch, conn, tid)
        out = _tool("schedule", {"reason": reason.format(n=9), "wake_check": "exit 1", "awaiting": awaiting})
        assert out.get("code") == "repark_escalated" and out.get("status") == "todo", out
        owner = out["owner_task_id"]
        card = kb.get_task(conn, owner)
        # No capable supervisor is configured here: the issue lands in triage, never on the parking worker.
        assert card.status == "triage" and card.assignee != "worker" and "parked 3x" in card.title
        assert "not a question for David" in card.body and card.human_request is None
        assert conn.execute("SELECT 1 FROM task_links WHERE parent_id=? AND child_id=?", (owner, tid)).fetchone()
        assert kwc.get_state(conn, tid) is None
        # A different premise is new evidence: it parks normally.
        other = _running(conn)
        _as_worker(monkeypatch, conn, other)
        assert _tool("schedule", {"reason": reason.format(n=1), "wake_check": "exit 1",
                                  "awaiting": awaiting}).get("status") == "scheduled"
        # The supervisor resolves the owner card: the count restarts, so the next park is a park.
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready', assignee='boss' WHERE id=?", (owner,))
        assert kb.claim_task(conn, owner, claimer="boss") is not None
        assert kb.complete_task(conn, owner, summary="quota raised")
        assert kb.get_task(conn, tid).status == "ready"
        assert kb.claim_task(conn, tid, claimer="worker") is not None
        _as_worker(monkeypatch, conn, tid)
        assert _tool("schedule", {"reason": "again", "wake_check": "exit 1",
                                  "awaiting": awaiting}).get("status") == "scheduled"


def test_escalation_goes_to_a_capable_supervisor(kanban_home, monkeypatch) -> None:
    monkeypatch.setattr(kaw, "_supervisor_profile", lambda task: "boss")
    awaiting = {"kind": "deploy", "ref": "release 93ed6b10", "why": "rollout"}
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        for n in range(3):
            _as_worker(monkeypatch, conn, tid)
            out = _tool("schedule", {"reason": f"rollout {n}", "wake_check": "exit 1", "awaiting": awaiting})
            if n < 2:
                assert kb.unblock_task(conn, tid)
                with kb.write_txn(conn):
                    conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
                assert kb.claim_task(conn, tid, claimer="worker") is not None
        card = kb.get_task(conn, out["owner_task_id"])
        assert (card.assignee, card.status) == ("boss", "ready")


def _repark(conn, monkeypatch, tid, name, args):
    _as_worker(monkeypatch, conn, tid)
    out = _tool(name, args)
    if out.get("status") == "scheduled":
        assert kb.unblock_task(conn, tid)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
        assert kb.claim_task(conn, tid, claimer="worker") is not None
    return out


def test_pacing_continues_and_distinct_clock_windows_never_escalate(kanban_home, monkeypatch) -> None:
    with kbc.connect_closing() as conn:
        a, b = _running(conn), _running(conn)
        for _ in range(4):
            out = _repark(conn, monkeypatch, a, "continue", {
                "checkpoint": "Build still running; check again.", "resume_after_seconds": 300})
            assert out.get("status") == "scheduled", out
        for why in ("24h soak after deploy A", "release slot Monday", "cooldown after B", "soak after C"):
            out = _repark(conn, monkeypatch, b, "schedule", {
                "reason": "window", "clock_only": True, "awaiting": {"kind": "clock", "why": why}})
            assert out.get("status") == "scheduled", out


def test_continue_refusal_carries_the_typed_code(kanban_home, monkeypatch) -> None:
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _as_worker(monkeypatch, conn, tid)
        out = _tool("continue", {"checkpoint": "x", "resume_after_seconds": 600,
                                 "awaiting": {"kind": "ci", "ref": "soon"}})
        assert out.get("rejection") == kaw.INVALID_AWAITING or out.get("code") == kaw.INVALID_AWAITING, out


def test_unrelated_ledger_topics_do_not_match(kanban_home) -> None:
    ledger = Path(kanban_home) / "state" / "ask-ledger.sqlite"
    ledger.parent.mkdir(parents=True)
    c = sqlite3.connect(ledger)
    c.execute("create table answered (id integer primary key, ts real, session text, topic text, answer text, "
              "card_id text)")
    c.execute("insert into answered(ts, session, topic, answer, card_id) values (?,?,?,?,?)",
              (time.time(), "s", "Live card status and whether it is blocked on David", "it is not", ""))
    c.commit()
    c.close()
    assert kaw.answered_ask("t_x", "Waiting on David's approval for the hub restart; card status blocked") is None
