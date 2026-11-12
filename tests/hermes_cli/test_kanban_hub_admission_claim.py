"""The hub judges its own pre-gate cards at claim time: no hub slot goes to an unjudged card. [daveey]

The hub admission gate (11-12 10:58Z) judges cards where they are created, moved or landed, and foreign
pickups. Cards created on the hub before it carried no verdict, and the dispatcher's claim backstop
(``kanban_hub_admission_pickup.refusals``) only covers foreign copies with ``runs_on``: on 11-12 the hourly
pin sweep moved 7 such hub-local cards off the hub by hand after their runs (all judge DENY), and 26 more
were waiting. The hub dispatcher now judges a ready, label-less hub card with no verdict before it claims it:
admitted runs here, denied is re-placed on the worker queue (``moved``, never claimed), a denied card that
must stay (host workspace, host_repo, a hub worktree or hub-local wake_check) runs here with ``kept``, and an
unavailable judge fails open (claimed, no verdict, judged next time).
"""
from __future__ import annotations

import json
import logging
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_hub_admission as judge
from hermes_cli import kanban_hub_admission_claim as hac
from tests.hermes_cli import hub_judge_support
from tests.hermes_cli import test_kanban_hub_admission_pickup as pickup_suite
from tests.hermes_cli.test_kanban_worker_node import BOARD, HUB, WORKER

hub_judge = hub_judge_support.hub_judge
gws = pickup_suite.gws
POOL = pickup_suite.POOL


def _events(conn, tid, kind):
    return pickup_suite._events(conn, tid, kind)


def _holder(svc, tid):
    return (svc.board(BOARD)["leases"].get(tid) or {}).get("holder")


def _hub_card(hub, *, title="refactor the dashboard table", body="Portable repo work: edit, test, PR.", **kw):
    """A card created on the hub before the admission gate: no runs_on, no placement policy, no verdict."""
    hub.enter()
    return kb.create_task(hub.conn, title=title, body=body, assignee="default", **kw)


def _tick(hub):
    hub.round()
    return hub.tick()


def test_a_denied_pre_gate_hub_card_moves_to_the_worker_queue_and_is_not_claimed(gws, hub_judge):
    hub, worker, svc = gws
    hub_judge.admit_next = False
    tid = _hub_card(hub)
    _tick(hub)
    assert _holder(svc, tid) != HUB
    assert kb.get_task(hub.conn, tid).status == "ready"
    assert [c["card"]["title"] for c in hub_judge.calls] == ["refactor the dashboard table"]
    [verdict] = _events(hub.conn, tid, "hub_admission")
    assert verdict["admit"] is False and verdict["entry"] == "claim" and "kept" not in verdict
    [moved] = _events(hub.conn, tid, "moved")
    assert moved["runs_on"][0] == POOL and moved["previous_runs_on"] is None and moved["queued"] is True
    assert not any(label.startswith("machine:") for label in moved["runs_on"])
    assert moved["reason"].startswith("hub admission denied at claim: fake judge denies")
    assert moved["hub_admission"]["entry"] == "claim"
    assert json.loads(hub.conn.execute("SELECT runs_on FROM tasks WHERE id = ?", (tid,)).fetchone()[0]) == (
        moved["runs_on"])
    # The Board carries the pool placement: a pool worker runs it, and the hub never judges it again.
    hub.round()
    worker.round()
    worker.tick()
    assert _holder(svc, tid) == WORKER
    _tick(hub)
    assert len(hub_judge.calls) == 1


def test_an_admitted_pre_gate_hub_card_is_claimed_with_its_verdict(gws, hub_judge):
    hub, _worker, svc = gws
    tid = _hub_card(hub, title="restart the hub gateway", body="needs the hub's live process")
    _tick(hub)
    assert _holder(svc, tid) == HUB
    [verdict] = _events(hub.conn, tid, "hub_admission")
    assert verdict["admit"] is True and verdict["entry"] == "claim"
    assert _events(hub.conn, tid, "moved") == []
    ids = [row[0] for row in hub.conn.execute(
        "SELECT kind FROM task_events WHERE task_id = ? AND kind IN ('hub_admission', 'claimed') ORDER BY id",
        (tid,))]
    assert ids[0] == "hub_admission" and "claimed" in ids  # judged before the claim


KEEP_CASES = {
    # A config that carries host_repo, so only the explicit host_repo rule keeps it.
    "host_repo": ({"requires": ["terminal", "host_repo"]}, "requires host_repo",
                  {"carried_requires": ["terminal", "host_repo"]}),
    "uncarried_requirement": ({"requires": ["terminal", "host_repo"]}, "uncarried requirement", {}),
    "dir_workspace": ({"workspace_kind": "dir"}, "host workspace", {}),
    "hub_worktree_body": ({"body": "Finish the commit in ~/code/_wt/claim-fix and push it."},
                          "hub worktree path in the brief", {}),
    "dot_worktrees_body": ({"body": "The branch lives in ~/code/hermes/.worktrees/t_1234 only."},
                           "hub worktree path in the brief", {}),
    "wake_check_path": ({}, "wake_check reads a hub-local path", {}),
}


@pytest.mark.parametrize("case", sorted(KEEP_CASES))
def test_a_denied_card_that_must_stay_is_claimed_and_kept_recorded(gws, hub_judge, tmp_path, monkeypatch, case):
    from hermes_cli import kanban_placement as kp
    hub, _worker, svc = gws
    hub_judge.admit_next = False
    kw, reason, extra_cfg = KEEP_CASES[case]
    monkeypatch.setattr(kp, "config", lambda: {**pickup_suite.CFG, **extra_cfg})
    kw = {**kw, **({"workspace_path": str(tmp_path / "ws")} if kw.get("workspace_kind") == "dir" else {})}
    tid = _hub_card(hub, **kw)
    if case == "wake_check_path":
        with kb.write_txn(hub.conn):
            kb._append_event(hub.conn, tid, "scheduled", {"wake_check": "test -f ~/.hermes/cache/ready.flag"})
    _tick(hub)
    assert _holder(svc, tid) == HUB, case
    [verdict] = _events(hub.conn, tid, "hub_admission")
    assert verdict["admit"] is False and verdict["entry"] == "claim" and verdict["kept"] == reason, case
    assert _events(hub.conn, tid, "moved") == []


@pytest.mark.parametrize("probe", ["git -C ~/code/hermes merge-base --is-ancestor abc HEAD",
                                   "gh api repos/o/r/tags --jq '.[].name' | grep -q v2"])
def test_a_portable_wake_check_does_not_keep_a_denied_card(gws, hub_judge, probe):
    hub, _worker, svc = gws
    hub_judge.admit_next = False
    tid = _hub_card(hub)
    with kb.write_txn(hub.conn):
        kb._append_event(hub.conn, tid, "scheduled", {"wake_check": probe})
    _tick(hub)
    assert _holder(svc, tid) != HUB and len(_events(hub.conn, tid, "moved")) == 1


def test_the_newest_wake_check_decides(gws, hub_judge):
    """An old hub-local probe superseded by a portable one does not keep the card."""
    hub, _worker, svc = gws
    hub_judge.admit_next = False
    tid = _hub_card(hub)
    with kb.write_txn(hub.conn):
        kb._append_event(hub.conn, tid, "scheduled", {"wake_check": "test -f ~/.hermes/cache/x"})
        kb._append_event(hub.conn, tid, "scheduled", {"wake_check": "gh pr view 1 --json state"})
    _tick(hub)
    assert _holder(svc, tid) != HUB and len(_events(hub.conn, tid, "moved")) == 1


def test_a_deny_with_no_worker_queue_is_kept_and_claimed(gws, hub_judge, monkeypatch):
    from hermes_cli import kanban_placement as kp
    hub, _worker, svc = gws
    monkeypatch.setattr(kp, "config", lambda: {"worker_pools": {HUB: [WORKER]}})  # no worker_queue label
    hub_judge.admit_next = False
    tid = _hub_card(hub)
    _tick(hub)
    assert _holder(svc, tid) == HUB  # never stranded
    [verdict] = _events(hub.conn, tid, "hub_admission")
    assert verdict["kept"] == hac.NO_QUEUE and _events(hub.conn, tid, "moved") == []


def test_an_unavailable_judge_fails_open_once_per_tick(gws, monkeypatch, caplog):
    hub, _worker, svc = gws
    calls = []

    def broken(card, hub_reason=None):
        calls.append(card)
        raise RuntimeError("judge down")
    monkeypatch.setattr(judge, "admit", broken)
    tids = [_hub_card(hub, title=f"card {n}") for n in range(3)]
    with caplog.at_level(logging.INFO, logger=hac.__name__):
        _tick(hub)
    assert len(calls) == 1  # one outage per tick, not one judge timeout per card
    for tid in tids:
        assert _holder(svc, tid) == HUB, tid  # fail open: a judge outage never freezes the hub
        assert _events(hub.conn, tid, "hub_admission") == []  # no verdict: judged at its next ready
    assert "hub admission: claim screen judged=0 denied=0 moved=0 kept=0 unavailable=3" in caplog.text


def test_an_unavailable_verdict_is_not_recorded(gws, monkeypatch):
    hub, _worker, svc = gws
    monkeypatch.setattr(judge, "admit", lambda card, hub_reason=None: judge.Verdict())
    tid = _hub_card(hub)
    _tick(hub)
    assert _holder(svc, tid) == HUB and _events(hub.conn, tid, "hub_admission") == []


@pytest.mark.parametrize("prior", [
    {"admit": False, "entry": "create", "why": "denied", "forced": True},
    {"admit": True, "entry": "move", "need": "hub_chrome", "evidence": "x", "why": "ok"},
    {"admit": False, "entry": "claim", "why": "denied", "kept": "host workspace"},
])
def test_a_card_with_any_verdict_makes_no_model_call(gws, hub_judge, prior):
    hub, _worker, svc = gws
    tid = _hub_card(hub)
    with kb.write_txn(hub.conn):
        kb._append_event(hub.conn, tid, "hub_admission", prior)
    _tick(hub)
    assert hub_judge.calls == [] and _holder(svc, tid) == HUB


def test_a_creation_time_verdict_makes_no_model_call(gws, hub_judge):
    hub, _worker, svc = gws
    tid = _hub_card(hub, placement_audit={"hub_admission": {"admit": False, "why": "skipped", "entry": "create"}})
    _tick(hub)
    assert hub_judge.calls == [] and _holder(svc, tid) == HUB


def test_a_non_hub_gateway_makes_no_model_call(gws, hub_judge):
    _hub, worker, svc = gws
    worker.enter()
    tid = kb.create_task(worker.conn, title="worker's own card", body="b", assignee="default")
    worker.round()
    worker.tick()
    assert hub_judge.calls == [] and _holder(svc, tid) == WORKER


def test_labelled_and_policy_cards_are_not_screened(gws, hub_judge):
    hub, _worker, _svc = gws
    _hub_card(hub, runs_on=["linux"])
    hub.enter()
    assert hac.screen(hub.conn) == hac.Counts()
    assert hub_judge.calls == []


def test_screen_makes_no_model_call_inside_a_write_transaction(gws, hub_judge):
    hub, _worker, _svc = gws
    _hub_card(hub)
    hub.enter()
    hub.conn.execute("BEGIN IMMEDIATE")
    try:
        counts = hac.screen(hub.conn)
    finally:
        hub.conn.execute("ROLLBACK")
    assert hub_judge.calls == [] and counts == hac.Counts()


def test_a_card_claimed_or_relabelled_while_judged_is_not_moved(gws, monkeypatch):
    hub, _worker, _svc = gws
    tid = _hub_card(hub)

    def deny_after_a_relabel(card, hub_reason=None):
        hub.conn.execute("UPDATE tasks SET runs_on = ? WHERE id = ?", (json.dumps(["linux"]), tid))
        hub.conn.commit()
        return judge.Verdict(admit=False, why="fake judge denies")
    monkeypatch.setattr(judge, "admit", deny_after_a_relabel)
    hub.enter()
    counts = hac.screen(hub.conn)
    assert counts.moved == 0 and _events(hub.conn, tid, "moved") == []
    assert json.loads(hub.conn.execute("SELECT runs_on FROM tasks WHERE id = ?", (tid,)).fetchone()[0]) == ["linux"]


def test_unjudged_claims_counts_hub_claims_without_a_prior_verdict(gws, hub_judge, monkeypatch):
    hub, _worker, _svc = gws
    judged = _hub_card(hub, title="judged")
    _tick(hub)
    assert hac.unjudged_claims(hub.conn, HUB) == []
    monkeypatch.setattr(judge, "admit", lambda card, hub_reason=None: judge.Verdict())  # outage: fail open
    unjudged = _hub_card(hub, title="unjudged")
    _tick(hub)
    hub.round()  # the Board's lease ``claimed`` event (holder=hub) arrives with the next feed
    assert [tid for tid, _at in hac.unjudged_claims(hub.conn, HUB)] == [unjudged]
    assert hac.unjudged_claims(hub.conn, HUB, now=time.time() + hac.WINDOW_SECONDS + 60) == []  # aged out
    assert judged != unjudged
    assert hac.unjudged_claims(hub.conn, WORKER) == []  # only this gateway's own claims


def test_doctor_flags_unjudged_hub_claims(gws, monkeypatch, capsys):
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    from hermes_cli import doctor_state
    hub, _worker, _svc = gws
    monkeypatch.setattr(judge, "admit", hub_judge_support.FakeJudge())
    _hub_card(hub, title="judged first")  # the board's first verdict marks the gate
    _tick(hub)
    hub.enter()
    assert doctor_state._check_hub_admission_claims(False).manual_issues == []
    assert "0 hub claims of unjudged cards" in capsys.readouterr().out
    monkeypatch.setattr(judge, "admit", lambda card, hub_reason=None: judge.Verdict())
    tid = _hub_card(hub, title="claimed unjudged")
    _tick(hub)
    hub.round()
    hub.enter()
    found = doctor_state._check_hub_admission_claims(False)
    assert len(found.manual_issues) == 1 and tid in found.manual_issues[0], found.manual_issues
    assert "1 hub claim(s) of unjudged cards" in capsys.readouterr().out


def test_doctor_hub_claim_check_is_silent_off_the_hub(gws, monkeypatch, capsys):
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    from hermes_cli import doctor_state
    _hub, worker, _svc = gws
    worker.enter()
    assert doctor_state._check_hub_admission_claims(False).manual_issues == []
    assert capsys.readouterr().out == ""
