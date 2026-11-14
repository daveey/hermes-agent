"""No producer, no park: a timed park must name who produces its condition. [daveey]

Replays of the four 10-26/27 orphan waits (``~/.hermes/kanban/evidence/orphan-waits-20261027/
5whys.md``; reasons verbatim from the parking events and the state-branch fixtures in
``tests/test_session_idle_orphan_wait.py``) must be refused with a typed error, and the controls
from those fixtures (a running card, an in-progress CI run, a pure clock wait, a wait on a card
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
                              "human_request": {"action": "Send draft 180. Unblock = proceed; archive = drop it.", "why_human": "your account",
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
    "No permissions changes needed; waiting for nightly release slot.",
    "Earlier run hit permission denied on cache; fixed in PR 12. Waiting for CI rerun.",
    "Per David's decision yesterday, deploy at the release slot.",
    "Following David's approval in chat, rollout is live; cooldown.",
    "David's decision recorded 23:06 (design A); waiting for TestFlight processing build 412.",
])
def test_ordinary_park_prose_is_not_a_wall(kanban_home, monkeypatch, reason) -> None:
    # clock_only: an opaque "exit 1" probe with no producer is now refused on its own (opaque_probe_wait).
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _as_worker(monkeypatch, conn, tid)
        out = _tool("schedule", {"reason": reason, "clock_only": True})
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
    "SCHEDULED_UNTIL=2099-01-01T00:00:00Z \u2014 release slot.",
    "No David decision needed; waiting on cooldown. SCHEDULED_UNTIL=2099-01-01T00:00:00Z",
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
    monkeypatch.setattr(kaw, "_supervisor_profile", lambda task: None)
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
        # A single-profile installation must have a dispatchable producer, not unassigned triage.
        assert card.status == "ready" and card.assignee == "worker" and "parked 3x" in card.title
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


def test_shared_clear_wakes_do_not_exhaust_repark_budget(kanban_home, monkeypatch) -> None:
    from hermes_cli import kanban_shared_blockers as ksb
    awaiting = {"kind": "external", "ref": "vendor ticket 4711", "why": "vendor response pending"}
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        for n in range(4):
            _as_worker(monkeypatch, conn, tid)
            out = _tool("schedule", {"reason": "waiting for vendor", "wake_check": "exit 1", "awaiting": awaiting})
            assert out.get("status") == "scheduled", (n, out)
            if n < 3:
                with kb.write_txn(conn):
                    assert kwc._wake(conn, tid, ksb.CLEARED_EVENT, {"blocker_id": "github-rest-quota"})
                assert kb.claim_task(conn, tid, claimer="worker")
        assert not [e for e in kb.list_events(conn, tid) if e.kind == kaw.ESCALATED_EVENT]


@pytest.mark.parametrize("status", [None, "scheduled"])
def test_clear_annotation_does_not_reset_repark_budget(kanban_home, status):
    from hermes_cli import kanban_shared_blockers as ksb
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        with kb.write_txn(conn):
            for _ in range(2):
                kb._append_event(conn, tid, "scheduled", {"awaiting": {"premise": "same"}})
            kb._append_event(conn, tid, ksb.CLEARED_EVENT, {"status": status})
        assert kaw.prior_same_premise(conn, tid, "same") == 2


@pytest.mark.parametrize("payload", [{"woke": True}, {"woke": True, "status": None}, {"status": "todo"}])
def test_clear_wake_provenance_resets_repark_budget(kanban_home, payload):
    """A blocked-branch wake (``woke``, no status) or a parent-gated ``todo`` wake ends the streak."""
    from hermes_cli import kanban_shared_blockers as ksb
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        with kb.write_txn(conn):
            for _ in range(2):
                kb._append_event(conn, tid, "scheduled", {"awaiting": {"premise": "same"}})
            kb._append_event(conn, tid, ksb.CLEARED_EVENT, payload)
        assert kaw.prior_same_premise(conn, tid, "same") == 0


def test_escalation_without_any_assignee_uses_default(kanban_home, monkeypatch):
    monkeypatch.setattr(kaw, "_supervisor_profile", lambda task: None)
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET assignee=NULL WHERE id=?", (tid,))
            owner = kaw.escalate(conn, tid, record={"premise": "same"}, reason="stuck")
        card = kb.get_task(conn, owner)
        assert (card.status, card.assignee) == ("ready", "default")
        assert kb.claim_task(conn, owner)


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


# PR #1806 (t_01a1d19e0179a093): red CI -> fix -> new head -> park again, three rounds on one PR URL with no
# per-head ``why``. Each park named its head only in the wake_check, so all three hashed to one premise and
# the third fix round escalated. The head SHA is part of a ci/deploy premise; a stuck head still escalates.
CI_PR = "https://github.com/softmax-universe/daveey-hermes/pull/1806"
CI_HEADS = ("b07a0bd43d" + "1" * 30, "efeeea0fe5" + "2" * 30, "06d1730d94" + "3" * 30)


def _ci_parks(conn, monkeypatch, heads, kind="ci"):
    tid = _running(conn)
    return [_repark(conn, monkeypatch, tid, "schedule", {
        "reason": f"waiting for required checks, round {n}",
        "wake_check": f"gh api repos/softmax-universe/daveey-hermes/commits/{head}/check-runs | grep -q completed",
        "awaiting": {"kind": kind, "ref": CI_PR}}) for n, head in enumerate(heads)]


@pytest.mark.parametrize("kind", ["ci", "deploy"])
def test_ci_parks_on_new_heads_are_new_premises(kanban_home, monkeypatch, kind) -> None:
    with kbc.connect_closing() as conn:
        outs = _ci_parks(conn, monkeypatch, CI_HEADS, kind)
        assert [o.get("status") for o in outs] == ["scheduled"] * 3, outs


@pytest.mark.parametrize("kind", ["ci", "deploy"])
def test_ci_parks_on_one_head_still_escalate(kanban_home, monkeypatch, kind) -> None:
    with kbc.connect_closing() as conn:
        outs = _ci_parks(conn, monkeypatch, (CI_HEADS[0],) * 2 + (CI_HEADS[0].upper(),), kind)
        assert [o.get("status") for o in outs[:2]] == ["scheduled", "scheduled"], outs
        assert outs[2].get("code") == kaw.ESCALATED_EVENT and outs[2].get("status") == "todo", outs[2]


def test_ci_premise_hashes_heads_only_for_run_kinds() -> None:
    head = CI_HEADS[0]
    probe = f"wait_for_ci.py {head} --once"
    for kind in ("ci", "deploy"):
        aw = {"kind": kind, "ref": CI_PR}
        assert kaw.premise_hash(aw, "r", probe) != kaw.premise_hash(aw, "r", probe.replace(head, CI_HEADS[1]))
        ref_aw = {"kind": kind, "ref": f"run for {head}"}
        assert kaw.premise_hash(ref_aw, "r") == kaw.premise_hash(ref_aw, "r", f"probe {head.upper()}")
        assert kaw.premise_hash(ref_aw, "r") != kaw.premise_hash(ref_aw, "r", f"probe {CI_HEADS[1]}")
        # No full SHA (short ones are not heads): the premise is unchanged from before.
        assert kaw.premise_hash(aw, "r", "wait_for_ci.py b07a0bd43d") == kaw.premise_hash(aw, "r")
    for aw in ({"kind": "external", "ref": CI_PR}, {"kind": kaw.RESOURCE, "ref": "pool"},
               {"kind": "ci", "ref": CI_PR, "inferred": True}):
        assert kaw.premise_hash(aw, "r", probe) == kaw.premise_hash(aw, "r", probe.replace(head, CI_HEADS[1]))


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
        for why in ("release slot A", "release slot Monday", "cooldown after B", "cooldown after C"):
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


# -- opaque probes: a probe alone is not a producer (kanban-92e96c16fd) ------------------------------

# macbook t_3b031dd1, run 5175 (10-27 17:33 UTC): parked 9 h on a probe nobody would ever make pass. The
# agent Chrome is a launchd KeepAlive job in active use, and the plan forbids closing unknown tabs.
CHROME_PROBE_REASON = ("SCHEDULED_UNTIL=2099-01-01T00:00:00Z \u2014 tab-ownership + idle-shutdown code is shipped; "
                       "the legacy agent Chrome (KeepAlive supervisor 85897, browser 85920) still holds 26 pages. "
                       "Wakes when the exact agent browser and both listeners (9333/9334) are absent.")
CHROME_PROBE = ("python3 -c \"import socket,sys; sys.exit(0 if all(socket.socket().connect_ex(('127.0.0.1', p)) "
                "for p in (9333, 9334)) else 1)\"")


@pytest.mark.parametrize("op,args", [
    ("schedule", {"reason": CHROME_PROBE_REASON, "wake_check": CHROME_PROBE}),
    ("block", {"reason": CHROME_PROBE_REASON, "kind": "time", "wake_check": CHROME_PROBE}),
    ("continue", {"checkpoint": CHROME_PROBE_REASON, "resume_after_seconds": 86400, "wake_check": CHROME_PROBE}),
    ("schedule", {"reason": "Waiting for the queue file to change.",
                  "wake_check": "test \"$(sha256sum QUEUE.md | cut -d ' ' -f 1)\" != ec94660442"}),
])
def test_opaque_probe_with_no_producer_is_refused(kanban_home, monkeypatch, op, args) -> None:
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _as_worker(monkeypatch, conn, tid)
        out = _tool(op, args)
        assert "opaque_probe_wait" in (out.get("code"), out.get("rejection")), out
        # The refusal says how to name the producer, and what to do when nobody else produces it.
        assert "awaiting={kind:'external'" in out["error"] and "owner card" in out["error"], out
        assert "needs_input" in out["error"], out
        assert kb.get_task(conn, tid).status == "running" and kwc.get_state(conn, tid) is None


def test_opaque_probe_with_a_declared_external_producer_parks(kanban_home, monkeypatch) -> None:
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _as_worker(monkeypatch, conn, tid)
        out = _tool("schedule", {"reason": CHROME_PROBE_REASON, "wake_check": CHROME_PROBE, "awaiting": {
            "kind": "external", "ref": "launchd com.example.agent-chrome",
            "why": "the 02:00 maintenance job unloads it nightly (log line 'agent-chrome unloaded')"}})
        assert out.get("status") == "scheduled", out
        rec = _park_payload(conn, tid)["awaiting"]
        assert rec["kind"] == "external" and not rec.get("inferred")
        assert kwc.get_state(conn, tid)["cmd"] == CHROME_PROBE


@pytest.mark.parametrize("op,args", [
    ("schedule", {"reason": "Waiting for CI run 40443505012 to finish.",
                  "wake_check": "gh run view 40443505012 --json status | grep -q completed"}),
    ("schedule", {"reason": "Owner card builds the image.", "wake_check": "hermes kanban wait-check task {owner}"}),
    ("continue", {"checkpoint": "Build still running; check again.", "resume_after_seconds": 600,
                  "wake_check": "! ps -p 3966721 >/dev/null"}),
    ("block", {"reason": "SCHEDULED_UNTIL=2099-01-01T00:00:00Z \u2014 release slot.", "kind": "time",
               "clock_only": True}),
])
def test_named_producers_and_short_continues_still_park(kanban_home, monkeypatch, op, args) -> None:
    with kbc.connect_closing() as conn:
        owner, tid = _running(conn), _running(conn)
        _as_worker(monkeypatch, conn, tid)
        args = {k: (v.format(owner=owner) if k == "wake_check" else v) for k, v in args.items()}
        out = _tool(op, args)
        assert out.get("status") == "scheduled", out


def test_same_probe_reparked_with_new_wording_escalates(kanban_home, monkeypatch) -> None:
    """The premise was a hash of the reason text, so a re-worded re-park on the same probe never counted."""
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        outs = []
        for n, why in enumerate(("listeners still up", "browser 85920 still alive, 26 tabs", "KeepAlive respawned")):
            outs.append(_repark(conn, monkeypatch, tid, "schedule", {
                "reason": f"park {n}: {why}", "wake_check": CHROME_PROBE,
                "awaiting": {"kind": "external", "ref": "agent Chrome on the MacBook", "why": why}}))
        assert [o.get("status") for o in outs[:2]] == ["scheduled", "scheduled"], outs
        assert outs[2].get("code") == kaw.ESCALATED_EVENT and outs[2].get("status") == "todo", outs[2]
        assert kb.get_task(conn, outs[2]["owner_task_id"]) is not None


def test_worker_cli_park_on_its_own_card_is_refused_but_an_operator_park_is_not(kanban_home, monkeypatch,
                                                                               capsys) -> None:
    """Entry paths: the worker's own `hermes kanban schedule` (CLI) and an operator (dashboard/terminal,
    no worker env). The operator's opaque park still records an inferred wait, so the checker flags it."""
    from hermes_cli import kanban as kcli

    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    kcli.build_parser(parser.add_subparsers(dest="command"))

    def run(*argv):
        return kcli.kanban_command(parser.parse_args(["kanban", *argv]))

    with kbc.connect_closing() as conn:
        mine, other = _running(conn), _running(conn)
    monkeypatch.setenv("HERMES_KANBAN_TASK", mine)
    assert run("schedule", mine, CHROME_PROBE_REASON, "--wake-check", CHROME_PROBE) != 0
    assert kaw.OPAQUE_PROBE_WAIT in capsys.readouterr().err
    monkeypatch.delenv("HERMES_KANBAN_TASK")
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, mine).status == "running" and not kb.list_comments(conn, mine)
        assert kb.schedule_task(conn, other, reason=CHROME_PROBE_REASON, wake_check=CHROME_PROBE)
        rec = _park_payload(conn, other)["awaiting"]
        assert rec["kind"] == "external" and rec["inferred"] is True


# -- own children are not producers (t_01a180e1189d7ec3) ------------------------------------

def _linked_child(conn, parent: str) -> str:
    return kb.create_task(conn, title="child", assignee="worker", parents=[parent])


def test_continue_does_not_infer_awaiting_from_own_child(kanban_home, monkeypatch) -> None:
    """10-27: a continue whose checkpoint named its own child inferred awaiting=card on that
    child. The child stays todo until the parent finishes, so the wait cycles."""
    with kbc.connect_closing() as conn:
        parent = _running(conn)
        child = _linked_child(conn, parent)
        grand = _linked_child(conn, child)
        _as_worker(monkeypatch, conn, parent)
        out = _tool("continue", {"checkpoint": f"child {child} still building; grandchild {grand} queued",
                                 "resume_after_seconds": 300})
        assert out.get("status") == "scheduled", out
        awaiting = _park_payload(conn, parent)["awaiting"]
        assert awaiting == {"kind": "clock", "inferred": True}, awaiting
        assert kb.get_task(conn, child).status == "todo"


def test_long_continue_on_a_descendant_does_not_install_a_child_probe(kanban_home, monkeypatch) -> None:
    """A long continue used to infer the child and install `wait-check task <child>` as the probe,
    which is the edge session-idle walks into a cycle. clock_only parks as a clock instead."""
    with kbc.connect_closing() as conn:
        parent = _running(conn)
        child = _linked_child(conn, parent)
        _as_worker(monkeypatch, conn, parent)
        out = _tool("continue", {"checkpoint": f"waiting on child {child} to finish the build",
                                 "resume_after_seconds": 20000, "clock_only": True})
        assert out.get("status") == "scheduled", out
        payload = _park_payload(conn, parent)
        assert payload["awaiting"]["kind"] == "clock"
        assert child not in json.dumps(payload.get("wake_check") or "")


def test_continue_still_infers_an_unrelated_live_card(kanban_home, monkeypatch) -> None:
    with kbc.connect_closing() as conn:
        parent = _running(conn)
        child = _linked_child(conn, parent)
        other = _running(conn)
        _as_worker(monkeypatch, conn, parent)
        out = _tool("continue", {"checkpoint": f"child {child} is todo; producer is {other}",
                                 "resume_after_seconds": 300})
        assert out.get("status") == "scheduled", out
        awaiting = _park_payload(conn, parent)["awaiting"]
        assert awaiting == {"kind": "card", "ref": other, "inferred": True}, awaiting


def test_declared_wait_on_a_descendant_is_refused(kanban_home, monkeypatch) -> None:
    with kbc.connect_closing() as conn:
        parent = _running(conn)
        child = _linked_child(conn, parent)
        _as_worker(monkeypatch, conn, parent)
        out = _tool("continue", {"checkpoint": "park on the child", "resume_after_seconds": 300,
                                 "awaiting": {"kind": "card", "ref": child}})
        assert out.get("rejection") == kaw.INVALID_AWAITING or out.get("code") == kaw.INVALID_AWAITING, out
        assert "descendant" in (out.get("error") or "")
        assert kb.get_task(conn, parent).status == "running"


def test_wake_probe_naming_a_descendant_is_refused(kanban_home, monkeypatch) -> None:
    with kbc.connect_closing() as conn:
        parent = _running(conn)
        child = _linked_child(conn, parent)
        _as_worker(monkeypatch, conn, parent)
        out = _tool("schedule", {"reason": "release slot", "clock_only": True,
                                 "wake_check": f"hermes kanban wait-check task {child}",
                                 "awaiting": {"kind": "clock", "why": "release slot"}})
        assert out.get("rejection") == kaw.INVALID_AWAITING or out.get("code") == kaw.INVALID_AWAITING, out
        assert "descendant" in (out.get("error") or "")
        assert kb.get_task(conn, parent).status == "running"


def test_mutual_card_wait_is_refused(kanban_home, monkeypatch) -> None:
    with kbc.connect_closing() as conn:
        a, b = _running(conn), _running(conn)
        _as_worker(monkeypatch, conn, b)
        out = _tool("schedule", {"reason": f"waiting on {a}", "awaiting": {"kind": "card", "ref": a}})
        assert out.get("status") == "scheduled", out
        _as_worker(monkeypatch, conn, a)
        out = _tool("schedule", {"reason": f"waiting on {b}", "awaiting": {"kind": "card", "ref": b}})
        assert (out.get("rejection") == kaw.INVALID_AWAITING or out.get("code") == kaw.INVALID_AWAITING), out
        assert "cycles" in (out.get("error") or "")
        assert kb.get_task(conn, a).status == "running"



# -- 10-28: a wait on David must not pass as kind='external' because its ref carries a number ---------

P4B_AWAITING = {"kind": "external", "ref": "PR 1777 approval",
                "why": "merge policy red tier needs David's approval on head 46754d080"}
MERGE_QUEUE_WHY = ("needs daveey's approving review (or his OK on Discord, Slack or the Inbox) "
                   "before the red tier merges")


@pytest.mark.parametrize("awaiting", [
    P4B_AWAITING,
    {"kind": "external", "ref": "https://github.com/o/r/pull/1777", "why": MERGE_QUEUE_WHY},
])
def test_external_park_naming_a_human_wait_is_refused_whatever_the_ref(kanban_home, monkeypatch,
                                                                        awaiting) -> None:
    """Hub t_b1fdca0b parked on 10-28 04:55 with the first awaiting; the ticket-number exemption let it pass."""
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _as_worker(monkeypatch, conn, tid)
        out = _tool("schedule", {"reason": "Upkeep P4b: waiting for the PR to merge.",
                                 "wake_check": "gh pr view 1777 --json state | grep -q MERGED",
                                 "awaiting": awaiting})
        assert kaw.HUMAN_WAIT in out.get("error", ""), out
        assert "needs_input" in out["error"] and "approval_reconciliation" in out["error"], out
        assert kb.get_task(conn, tid).status == "running"


@pytest.mark.parametrize("awaiting", [
    {"kind": "external", "ref": "AWS support case 4711", "why": "AWS support to grant the quota increase"},
    {"kind": "external", "ref": "https://status.example.com/incidents/123",
     "why": "upstream API returns 403 Forbidden while their maintenance runs"},
])
def test_external_park_on_a_vendor_ticket_still_parks(kanban_home, monkeypatch, awaiting) -> None:
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _as_worker(monkeypatch, conn, tid)
        out = _tool("schedule", {"reason": "Waiting on the vendor.", "clock_only": True, "awaiting": awaiting})
        assert out.get("status") == "scheduled", out


def test_vendor_ticket_ref_still_excuses_an_agent_wall_why(kanban_home, monkeypatch) -> None:
    """The ticket exemption keeps covering agent walls: a vendor holds the grant, not an agent."""
    with kbc.connect_closing() as conn:
        tid = _running(conn)
        _as_worker(monkeypatch, conn, tid)
        out = _tool("schedule", {"reason": "Waiting on the vendor.", "clock_only": True, "awaiting": {
            "kind": "external", "ref": "Vendor ticket 55821",
            "why": "their tenant lacks the role our integration needs; they apply it"}})
        assert out.get("status") == "scheduled", out


@pytest.mark.parametrize("text", [
    MERGE_QUEUE_WHY,
    "PR 1777 needs daveey's approving review",
    "parked until his OK on Discord",
    "waiting on daveey to merge",
])
def test_merge_queue_wording_is_a_human_wait(text) -> None:
    assert kaw.text_wall(text)[0] == kaw.HUMAN_WAIT


def test_inferred_wait_on_a_running_card_does_not_excuse_a_human_wait(kanban_home, monkeypatch) -> None:
    """Only a producer card that carries the visible ask excuses a human wait named in prose."""
    with kbc.connect_closing() as conn:
        other, tid = _running(conn), _running(conn)
        _as_worker(monkeypatch, conn, tid)
        out = _tool("schedule", {"reason": f"Waiting on David's approval; {other} merges after.",
                                 "wake_check": f"hermes kanban wait-check task {other}"})
        assert kaw.HUMAN_WAIT in out.get("error", ""), out


# -- bb-gate Claude-pool holds: a stated quota window never escalates (bb t_01a19f847b7f5abb) ---------

# The mini's bb_gate.py parks ready bb-queue cards as an operator (`hermes kanban schedule`, no worker env)
# on this probe. On 11-03 three such parks of one card in 40 min escalated twice into Opus "relieve the
# pool" cards, which burned the last Opus account the pool waits on.
BB_GATE_PROBE = ("env BB_DEV_HOME=/Users/daveey/.bb-hermes/bb-dev BB_HERMES_HOME=/Users/daveey/.bb-hermes "
                 "'/Users/daveey/Applications/Hermes Python.app/Contents/MacOS/python3' "
                 "/Users/daveey/code/hermes/hosts/daveey-mini/bb-dev/bb_gate.py wake-ready")


def _cli():
    from hermes_cli import kanban as kcli
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    kcli.build_parser(parser.add_subparsers(dest="command"))
    return lambda *argv: kcli.kanban_command(parser.parse_args(["kanban", *argv]))


def _bb_gate_parks(conn, tid, holds_reasons, awaiting=None, why=None) -> list[str]:
    """Park ``tid`` once per reason the way bb_gate.Env.park does, waking it (unblock) in between."""
    run = _cli()
    for reason in holds_reasons:
        extra = ["--awaiting", json.dumps(awaiting), "--awaiting-why", why] if awaiting else []
        assert run("schedule", tid, reason, "--wake-check", BB_GATE_PROBE, "--check-every", "600", *extra) == 0
        if kb.get_task(conn, tid).status == "scheduled":
            assert kb.unblock_task(conn, tid)
    return [e.kind for e in kb.list_events(conn, tid)]


def _ready(conn) -> str:
    tid = kb.create_task(conn, title="BB-1 fix", assignee="bb-queue")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='ready' WHERE id=?", (tid,))
    return tid


def test_bb_gate_pool_parks_with_a_stated_reset_never_escalate(kanban_home, monkeypatch) -> None:
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    reset = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 9 * 3600))
    awaiting = {"kind": "clock", "ref": f"claude-pool weekly reset {reset}"}
    why = "Claude pool at 89% of 6 accounts' weekly quota (self-fix holds at 75%)"
    reasons = ["bb-gate: governor: Claude pool at 88% (self-fix waits at 75%; talking to BB is unaffected)",
               "bb-gate: governor: memory pressure warn; Claude pool at 89% (self-fix waits at 75%; talking "
               "to BB is unaffected)"] * 2
    with kbc.connect_closing() as conn:
        tid = _ready(conn)
        kinds = _bb_gate_parks(conn, tid, reasons, awaiting, why)
        assert kaw.ESCALATED_EVENT not in kinds and kinds.count("scheduled") == 4, kinds
        rec = _park_payload(conn, tid)["awaiting"]
        assert rec["kind"] == "clock" and rec["ref"] == f"claude-pool weekly reset {reset}" and rec["why"] == why
        assert "probe" not in rec and not conn.execute(
            "SELECT 1 FROM tasks WHERE title LIKE 'Unstick%'").fetchone()
        # Then the pool drops and a memory-only hold persists: that premise counts from its own first park.
        mem = "bb-gate: governor: memory pressure critical"
        kinds = _bb_gate_parks(conn, tid, [mem, mem])
        assert kaw.ESCALATED_EVENT not in kinds, kinds
        assert _bb_gate_parks(conn, tid, [mem])[-1] == kaw.ESCALATED_EVENT


@pytest.mark.parametrize("reason", [
    "bb-gate: governor: memory pressure critical",
    "bb-gate: governor: 5-min load 4.8/core",
    "bb-gate: governor: free disk 8 GB < 15.0 GB",
])
def test_undeclared_probe_parks_still_escalate_on_the_third_park(kanban_home, monkeypatch, reason) -> None:
    # A park that declares nothing is a genuine inferred external wait: the repeat-park detector still fires.
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    with kbc.connect_closing() as conn:
        tid = _ready(conn)
        kinds = _bb_gate_parks(conn, tid, [reason] * 3)
        assert kinds[-1] == kaw.ESCALATED_EVENT and kinds.count("scheduled") == 2, kinds
        rec = [e for e in kb.list_events(conn, tid) if e.kind == kaw.ESCALATED_EVENT][-1].payload["awaiting"]
        # The inferred ref names the script, not an absolute path cut mid-word (".../hosts/daveey-min").
        assert rec["inferred"] is True and rec["ref"] == "bb_gate.py wake-ready", rec


def test_a_past_or_far_clock_window_still_escalates(kanban_home, monkeypatch) -> None:
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    for offset in (-3600, 30 * 86400):
        when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + offset))
        with kbc.connect_closing() as conn:
            tid = _ready(conn)
            kinds = _bb_gate_parks(conn, tid, ["bb-gate: governor: Claude pool at 88%"] * 3,
                                   {"kind": "clock", "ref": f"claude-pool weekly reset {when}"}, "pool 88%")
            assert kinds[-1] == kaw.ESCALATED_EVENT, (offset, kinds)


@pytest.mark.parametrize("cmd,ref", [
    (BB_GATE_PROBE, "bb_gate.py wake-ready"),
    ("python3 -B /x/y/probe.py --card t_1 'a b'", "probe.py --card t_1 'a b'"),
    ("bash /home/u/ws/check.sh", "check.sh"),
    ("test -f /tmp/marker", "test -f /tmp/marker"),
    ("python3 -c 'import sys; sys.exit(1)'", "python3 -c 'import sys; sys.exit(1)'"),
    ("echo 'unterminated", "echo 'unterminated"),
])
def test_inferred_probe_ref_names_the_script(cmd, ref) -> None:
    assert kaw.probe_ref(cmd) == ref


# -- governor/pool gates are an internal resource wait (bb t_01a19f5cca30ed12) ----------------------------
# t_01a18aa302529d91 parked 3x on "governor: memory pressure warn; Claude pool at 87%", recorded as an inferred
# external wait on the host launch command, and escalated into an Unstick card. A declared ``resource`` wait
# resumes on its probe, never escalates, and a repeat backs off with the gate named in the reason.
GOV = {"kind": "resource", "ref": "bb-gate governor: memory,pool"}


def test_gate_denial_parks_as_a_resource_wait_then_auto_resumes(kanban_home, monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    hold = tmp_path / "hold"
    hold.write_text("governor")
    probe = f"test ! -e {hold}"
    run = _cli()
    with kbc.connect_closing() as conn:
        tid = _ready(conn)
        assert run("schedule", tid, "bb-gate: governor: memory pressure warn; Claude pool at 87%",
                   "--wake-check", probe, "--check-every", "600", "--awaiting", json.dumps(GOV),
                   "--awaiting-why", "Claude pool at 87% (holds at 75%)") == 0
        task = kb.get_task(conn, tid)
        assert task.status == "scheduled"
        rec = _park_payload(conn, tid)["awaiting"]
        assert rec["kind"] == "resource" and rec["ref"] == GOV["ref"] and not rec.get("inferred"), rec
        assert "probe" not in rec and "repeat" not in rec
        from hermes_cli import kanban_wait_info as kwi
        assert kwi.LIVE_PRODUCER_KINDS.count("resource") == 1
        # The pool drops below threshold: the gate's own probe passes and the card resumes.
        # While the hold stands the probe fails and the card stays parked.
        kwc.run_due_wake_checks(conn, now=int(time.time()) + 601, wait=True)
        assert kb.get_task(conn, tid).status == "scheduled"
        hold.unlink()
        kwc.run_due_wake_checks(conn, now=int(time.time()) + 1202, wait=True)
        assert kb.get_task(conn, tid).status == "ready"


def test_resource_wait_needs_its_probe(kanban_home, monkeypatch) -> None:
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    with kbc.connect_closing() as conn:
        tid = _ready(conn)
        with pytest.raises(kaw.AwaitingError, match="resource.*wake_check"):
            kaw.resolve(conn, tid, awaiting=GOV, reason="bb-gate: governor: Claude pool at 87%")
        with pytest.raises(kaw.AwaitingError, match="needs ref=.*why="):
            kaw.resolve(conn, tid, awaiting=GOV, reason="bb-gate: governor: Claude pool at 87%", wake_check="false")


def test_a_worker_cannot_dodge_escalation_by_calling_its_wall_a_resource(kanban_home, monkeypatch) -> None:
    with kbc.connect_closing() as conn:
        tid = _ready(conn)
        monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
        with pytest.raises(kaw.AwaitingError, match="admission gate"):
            kaw.resolve(conn, tid, awaiting={**GOV, "why": "vendor response"}, reason="waiting", wake_check="false")


def test_governor_resource_parks_never_escalate_and_back_off(kanban_home, monkeypatch) -> None:
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    reasons = [f"bb-gate: governor: memory pressure warn; Claude pool at {p}% (self-fix waits at 75%)"
               for p in (84, 87, 87, 88, 90)]
    with kbc.connect_closing() as conn:
        tid = _ready(conn)
        kinds = _bb_gate_parks(conn, tid, reasons, GOV, "Claude pool at 87% (holds at 75%)")
        assert kaw.ESCALATED_EVENT not in kinds and kinds.count("scheduled") == 5, kinds
        assert not conn.execute("SELECT 1 FROM tasks WHERE title LIKE 'Unstick%'").fetchone()
        parks = [e.payload for e in kb.list_events(conn, tid) if e.kind == "scheduled"]
        # Never an external premise inferred from the host launch command.
        for p in parks:
            assert p["awaiting"]["kind"] == "resource" and not p["awaiting"].get("inferred"), p
            assert "daveey-min" not in p["awaiting"].get("ref", "") and "probe" not in p["awaiting"]
        assert len({p["awaiting"]["premise"] for p in parks}) == 1  # the reading changed, the gate did not
        assert [p["awaiting"].get("repeat", 0) for p in parks] == [0, 1, 2, 3, 4]
        assert [p["check_every"] for p in parks] == [600, 1200, 2400, 3600, 3600]
        assert "resource wait on bb-gate governor: memory,pool: park 3 in a row" in parks[2]["reason"]
        assert "Gate: Claude pool at 87% (holds at 75%)" in parks[2]["reason"]
        assert parks[0]["reason"] == reasons[0]


def test_repeat_detector_still_fires_for_a_genuine_external_premise_after_resource_parks(
        kanban_home, monkeypatch) -> None:
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    ext = {"kind": "external", "ref": "AWS support case 12345"}
    with kbc.connect_closing() as conn:
        tid = _ready(conn)
        _bb_gate_parks(conn, tid, ["bb-gate: governor: Claude pool at 88%"] * 3, GOV, "pool 88%")
        kinds = _bb_gate_parks(conn, tid, ["waiting on the vendor quota raise"] * 3, ext, "case open, ETA 2d")
        assert kinds[-1] == kaw.ESCALATED_EVENT and kinds.count(kaw.ESCALATED_EVENT) == 1, kinds


def test_ci_premise_ignores_head_order() -> None:
    """A park naming the same heads in another order is the same premise (run_heads sorts)."""
    a, b = CI_HEADS[0], CI_HEADS[1]
    for kind in ("ci", "deploy"):
        aw = {"kind": kind, "ref": CI_PR}
        assert kaw.premise_hash(aw, "r", f"wait {a} {b}") == kaw.premise_hash(aw, "r", f"wait {b} {a}")
