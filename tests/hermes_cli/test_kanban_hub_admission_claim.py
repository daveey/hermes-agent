"""The hub judges its own pre-gate cards at claim time: no hub slot goes to an unjudged card. [daveey]

The hub admission gate (11-12 10:58Z) judges cards where they are created, moved or landed, and foreign
pickups. Cards created on the hub before it carried no verdict, and the dispatcher's claim backstop
(``kanban_hub_admission_pickup.refusals``) only covers foreign copies with ``runs_on``: on 11-12 the hourly
pin sweep moved 7 such hub-local cards off the hub by hand after their runs (all judge DENY), and 26 more
were waiting. The hub dispatcher now judges a ready, label-less hub card with no verdict before it claims it:
admitted runs here, denied is re-placed on the worker queue (``moved``, never claimed), a denied card that
must stay (host workspace, host_repo, a hub worktree or hub-local wake_check) runs here with ``kept``, and an
unavailable judge defers unjudged cards until a later tick.
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


@pytest.fixture(autouse=True)
def _claim_screen_first(request, monkeypatch):
    """These tests drive the claim screen, so the Board serves no promotion unless a test is about it.

    Push-before-pull (t_01a1d36ddcea59fe) acks a hub-created card in the round's first push, and that round's feed
    echo stamps ``created_on``, so the same round's reevaluate already promotes a portable legacy card through
    placement recovery, before the tick's claim screen. The ``test_promotion_*`` cases cover that path."""
    if "promotion" not in request.node.name:
        _no_board_promotion(monkeypatch)

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


def _recovered(svc, tid):
    return [body for kind, _board, body in svc.calls if kind == "recover" and body["task_id"] == tid]


@pytest.mark.parametrize("admit", [False, True])
def test_promotion_keeps_a_card_with_a_hub_local_wake_check_on_the_hub(gws, hub_judge, admit):
    """The Board serves recover(mode=promote): the sync round's reevaluate reaches the legacy-routed card before
    the claim screen does, and must apply the screen's keep reasons (t_01a1d4125c1130dc)."""
    hub, _worker, svc = gws
    hub_judge.admit_next = admit
    tid = _hub_card(hub)
    with kb.write_txn(hub.conn):
        kb._append_event(hub.conn, tid, "scheduled", {"wake_check": "test -f ~/.hermes/cache/ready.flag"})
    hub.round()  # push: the Board acks the hub-created card
    _tick(hub)  # the feed echo stamps created_on, so this round's reevaluate sees it; then the claim screen
    assert _recovered(svc, tid) == [] and _events(hub.conn, tid, "moved") == []
    assert _holder(svc, tid) == HUB
    [verdict] = _events(hub.conn, tid, "hub_admission")
    assert verdict["admit"] is admit and verdict.get("kept") == (None if admit else "wake_check reads a hub-local path")


def test_promotion_still_moves_a_portable_hub_card_to_the_pool(gws, hub_judge):
    hub, _worker, svc = gws
    tid = _hub_card(hub)
    hub.round()
    hub.round()  # the echo stamps created_on; reevaluate promotes, and the feed carries the Board's placement
    assert [b["mode"] for b in _recovered(svc, tid)] == ["promote"]
    assert json.loads(svc.board(BOARD)["rows"]["tasks"][tid]["runs_on"]) == [f"machine:{WORKER}"]
    hub.tick()
    assert json.loads(hub.conn.execute("SELECT runs_on FROM tasks WHERE id = ?", (tid,)).fetchone()[0]) == [
        f"machine:{WORKER}"]  # the round's feed pull applied the Board's placement
    assert _holder(svc, tid) != HUB and hub_judge.calls == []  # labelled: never screened, never claimed here


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


def test_a_deny_with_no_worker_queue_is_deferred(gws, hub_judge, monkeypatch):
    from hermes_cli import kanban_placement as kp
    hub, _worker, svc = gws
    monkeypatch.setattr(kp, "config", lambda: {"worker_pools": {HUB: [WORKER]}})  # no worker_queue label
    hub_judge.admit_next = False
    tid = _hub_card(hub)
    _tick(hub)
    assert _holder(svc, tid) != HUB  # queue absence is not structural permission
    [verdict] = _events(hub.conn, tid, "hub_admission")
    assert "kept" not in verdict and _events(hub.conn, tid, "moved") == []
    _tick(hub)
    assert len(hub_judge.calls) == 1 and _holder(svc, tid) != HUB


def test_a_deny_no_worker_can_claim_is_kept_home_with_the_action_reason(gws, hub_judge, caplog):
    """The home route (the pickup claim gate refuses the text on every worker) records its own reason and count,
    not the generic no-queue one: it overrides the judge's deny and spends a hub slot."""
    from hermes_cli import kanban_placement_coordination as coord
    hub, _worker, svc = gws
    hub_judge.admit_next = False
    tid = _hub_card(hub, title="claim-stall incident", body="Read each request with `hermes-hub outcomes --id <id>`.")
    with caplog.at_level(logging.INFO, logger=hac.__name__):
        _tick(hub)
    assert _holder(svc, tid) == HUB
    [verdict] = _events(hub.conn, tid, "hub_admission")
    assert verdict["kept"] == coord.ACTION_REASON and verdict["home"] == coord.ACTION_REASON
    assert _events(hub.conn, tid, "moved") == []
    assert "kept=1 home=1" in caplog.text


def test_an_unavailable_judge_defers_once_per_tick(gws, monkeypatch, caplog):
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
        assert _holder(svc, tid) != HUB, tid  # outage is not an admission disposition
        assert _events(hub.conn, tid, "hub_admission") == []  # no verdict: judged at its next ready
    assert "hub admission: claim screen judged=0 denied=0 moved=0 kept=0 home=0 unavailable=3" in caplog.text


def test_an_unavailable_verdict_is_not_recorded(gws, monkeypatch):
    hub, _worker, svc = gws
    monkeypatch.setattr(judge, "admit", lambda card, hub_reason=None: judge.Verdict())
    tid = _hub_card(hub)
    _tick(hub)
    assert _holder(svc, tid) != HUB and _events(hub.conn, tid, "hub_admission") == []


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
    assert hub_judge.calls == []
    assert (_holder(svc, tid) == HUB) is bool(prior.get("admit") or prior.get("forced"))


def test_a_creation_time_verdict_makes_no_model_call(gws, hub_judge):
    hub, _worker, svc = gws
    tid = _hub_card(hub, placement_audit={"hub_admission": {"admit": False, "why": "skipped", "entry": "create"}})
    _tick(hub)
    assert hub_judge.calls == [] and _holder(svc, tid) != HUB


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
    policy = _hub_card(hub)
    hub.conn.execute("UPDATE tasks SET placement_policy = 'worker-first-v1' WHERE id = ?", (policy,))
    hub.conn.commit()
    hub.enter()
    assert hac.screen(hub.conn) == hac.Counts()
    assert hub_judge.calls == []


def test_an_empty_label_list_is_screened_like_no_labels(gws, hub_judge):
    hub, _worker, _svc = gws
    hub_judge.admit_next = False
    tid = _hub_card(hub)
    hub.conn.execute("UPDATE tasks SET runs_on = '[]' WHERE id = ?", (tid,))
    hub.conn.commit()
    hub.enter()
    assert hac.screen(hub.conn).moved == 1
    [moved] = _events(hub.conn, tid, "moved")
    assert moved["runs_on"][0] == POOL
    assert moved["previous_runs_on"] == []  # the card's real prior labels, not None


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


@pytest.mark.parametrize("change", [
    "runs_on = '[\"linux\"]'", "placement_policy = 'worker-first-v1'", "status = 'blocked'",
    "claim_lock = 'hermes-hub:1'", "body = 'needs signed-in hub Chrome'",
    "title = 'new brief'", "workspace_kind = 'dir'", "requires = '[\"host_repo\"]'",
    "assignee = 'other'", "workspace_path = '/home/example/work'", "created_on = 'other'"])
def test_a_card_changed_while_judged_is_left_to_its_new_owner(gws, monkeypatch, change):
    hub, _worker, _svc = gws
    tid = _hub_card(hub)
    before = hub.conn.execute("SELECT runs_on FROM tasks WHERE id = ?", (tid,)).fetchone()[0]

    def deny_after_a_change(card, hub_reason=None):
        hub.conn.execute(f"UPDATE tasks SET {change} WHERE id = ?", (tid,))
        hub.conn.commit()
        return judge.Verdict(admit=False, why="fake judge denies")
    monkeypatch.setattr(judge, "admit", deny_after_a_change)
    hub.enter()
    counts = hac.screen(hub.conn)
    assert counts.moved == 0 and counts.judged == 1, change
    assert _events(hub.conn, tid, "moved") == [] and _events(hub.conn, tid, "hub_admission") == [], change
    after = hub.conn.execute("SELECT runs_on FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    assert after == (before if "runs_on" not in change else '["linux"]'), change


def _no_board_promotion(monkeypatch):
    """A Board without the recovery endpoint: a deferred legacy card stays a hub candidate between ticks (the
    fake Board has no ``recover``; with it, the sync round may promote a portable card to the worker queue)."""
    from hermes_cli import kanban_placement_recovery as recovery
    monkeypatch.setattr(recovery, "endpoint_available", lambda *a: False)


def test_a_card_edited_while_judged_is_not_claimed_this_tick_and_is_judged_next_tick(gws, hub_judge, monkeypatch):
    """The skipped verdict must not leave the card claimable unjudged in the same tick (review of #1637)."""
    hub, _worker, svc = gws
    _no_board_promotion(monkeypatch)
    tid = _hub_card(hub)
    fake = judge.admit

    def edit_during_the_call(card, hub_reason=None):
        if not hub_judge.calls:  # only the first call races an edit
            hub.conn.execute("UPDATE tasks SET body = 'edited while judged' WHERE id = ?", (tid,))
            hub.conn.commit()
        return fake(card, hub_reason=hub_reason)
    monkeypatch.setattr(judge, "admit", edit_during_the_call)
    _tick(hub)
    assert _holder(svc, tid) != HUB and kb.get_task(hub.conn, tid).status == "ready"
    assert _events(hub.conn, tid, "hub_admission") == []  # no verdict for the stale snapshot
    _tick(hub)  # judged on its new body, then claimed
    assert [c["card"]["body"] for c in hub_judge.calls] == ["Portable repo work: edit, test, PR.",
                                                            "edited while judged"]
    [verdict] = _events(hub.conn, tid, "hub_admission")
    assert verdict["admit"] is True and verdict["entry"] == "claim"
    assert _holder(svc, tid) == HUB
    hub.round()
    assert tid not in [t for t, _at in hac.unjudged_claims(hub.conn, HUB)]


def test_judge_calls_are_capped_per_tick_and_the_rest_wait_unclaimed(gws, hub_judge, monkeypatch):
    hub, _worker, svc = gws
    _no_board_promotion(monkeypatch)
    n = hac.MAX_JUDGED_PER_TICK + 2
    tids = [_hub_card(hub, title=f"backlog card {i}") for i in range(n)]
    _tick(hub)
    assert len(hub_judge.calls) == hac.MAX_JUDGED_PER_TICK
    judged = {tid for tid in tids if _events(hub.conn, tid, "hub_admission")}
    assert len(judged) == hac.MAX_JUDGED_PER_TICK
    for tid in set(tids) - judged:  # over the budget: deferred, never claimed unjudged
        assert _holder(svc, tid) != HUB and kb.get_task(hub.conn, tid).status == "ready", tid
    _tick(hub)  # the deferred cards are judged on the next tick
    assert len(hub_judge.calls) == n
    assert all(_events(hub.conn, tid, "hub_admission") for tid in tids)
    hub.round()
    assert hac.unjudged_claims(hub.conn, HUB) == []


def test_screen_reports_deferred_ids(gws, hub_judge):
    hub, _worker, _svc = gws
    tids = [_hub_card(hub, title=f"card {i}") for i in range(3)]
    hub.enter()
    counts = hac.screen(hub.conn, budget=1)
    assert len(hub_judge.calls) == 1 and counts.judged == 1
    assert counts.deferred == 2 and counts.deferred_ids == frozenset(tids[1:])
    assert "deferred=2" in counts.line()
    assert all(tid in counts.line() for tid in tids[1:]) and tids[0] not in counts.line()


def test_deferred_ids_appear_in_the_dispatch_result(gws, hub_judge, monkeypatch, caplog):
    """A card deferred every tick (kept edited, or a long backlog) must be visible in the tick's diagnostics:
    ``lane_candidates`` leaves it out, so the result and the log line name it (review of #1811)."""
    from hermes_cli.kanban_db_dispatch_stuck import held_cards
    hub, _worker, _svc = gws
    _no_board_promotion(monkeypatch)
    tids = [_hub_card(hub, title=f"backlog card {i}") for i in range(hac.MAX_JUDGED_PER_TICK + 2)]
    with caplog.at_level(logging.INFO, logger=hac.__name__):
        result = _tick(hub)
    deferred = [tid for tid in tids if not _events(hub.conn, tid, "hub_admission")]
    assert len(deferred) == 2 and sorted(result.admission_deferred) == sorted(deferred)
    assert all(f"{tid}=admission_deferred" in held_cards([result]) for tid in deferred)
    [line] = [r.getMessage() for r in caplog.records if "claim screen" in r.getMessage()]
    assert "deferred=2" in line and all(tid in line for tid in deferred)
    assert _tick(hub).admission_deferred == []  # judged on the next tick: nothing left deferred


def test_print_deferred_names_the_cards(capsys):
    hac.print_deferred([])
    assert capsys.readouterr().out == ""
    hac.print_deferred(["t_a", "t_b"])
    assert capsys.readouterr().out == "Deferred by the hub claim screen (unjudged, judged next tick): t_a, t_b\n"


def test_a_write_to_a_later_card_during_an_earlier_judge_call_does_not_defer_it(gws, hub_judge, monkeypatch):
    """Each card's row is read when its turn comes, not once before the first judge call: a write to card 2
    during card 1's (up to 30 s) call no longer makes ``_apply`` defer card 2 and waste its judge call."""
    hub, _worker, _svc = gws
    first, second = _hub_card(hub, title="card 1"), _hub_card(hub, title="card 2")
    fake = judge.admit

    def write_card_2_during_card_1(card, hub_reason=None):
        if not hub_judge.calls:
            hub.conn.execute("UPDATE tasks SET body = 'edited before its turn' WHERE id = ?", (second,))
            hub.conn.commit()
        return fake(card, hub_reason=hub_reason)
    monkeypatch.setattr(judge, "admit", write_card_2_during_card_1)
    hub.enter()
    counts = hac.screen(hub.conn)
    assert counts.deferred == 0 and counts.judged == 2
    assert [c["card"]["body"] for c in hub_judge.calls] == ["Portable repo work: edit, test, PR.",
                                                            "edited before its turn"]
    assert _events(hub.conn, first, "hub_admission") and _events(hub.conn, second, "hub_admission")


def test_a_later_card_that_stops_being_screenable_is_not_judged(gws, hub_judge, monkeypatch):
    hub, _worker, _svc = gws
    _first, second = _hub_card(hub, title="card 1"), _hub_card(hub, title="card 2")
    fake = judge.admit

    def label_card_2_during_card_1(card, hub_reason=None):
        hub.conn.execute("UPDATE tasks SET runs_on = '[\"linux\"]' WHERE id = ?", (second,))
        hub.conn.commit()
        return fake(card, hub_reason=hub_reason)
    monkeypatch.setattr(judge, "admit", label_card_2_during_card_1)
    hub.enter()
    counts = hac.screen(hub.conn)
    assert [c["card"]["title"] for c in hub_judge.calls] == ["card 1"]
    assert counts.judged == 1 and counts.deferred == 0 and _events(hub.conn, second, "hub_admission") == []


def test_the_judge_budget_follows_dispatch_priority_not_creation_order(gws, hub_judge):
    hub, _worker, _svc = gws
    low = _hub_card(hub, title="older low-priority card", priority=0)
    high = _hub_card(hub, title="newer urgent card", priority=900)
    hub.enter()
    counts = hac.screen(hub.conn, budget=1)
    # The claim pass reaches the urgent card first; deferring it would let the older card jump it this tick.
    assert counts.deferred_ids == frozenset({low})
    assert _events(hub.conn, high, "hub_admission") and not _events(hub.conn, low, "hub_admission")


@pytest.mark.parametrize("kind,payload", [
    ("scheduled", {"wake_check": "test -f ~/.hermes/cache/new-probe"}),
    ("hub_admission", {"admit": True, "entry": "move", "why": "new decision"}),
])
def test_event_change_before_apply_invalidates_the_decision(gws, hub_judge, monkeypatch, kind, payload):
    hub, _worker, _svc = gws
    hub_judge.admit_next = False
    tid = _hub_card(hub)
    apply = hac._apply

    def changed(*args, **kwargs):
        with kb.write_txn(hub.conn):
            kb._append_event(hub.conn, tid, kind, payload)
        return apply(*args, **kwargs)

    monkeypatch.setattr(hac, "_apply", changed)
    hub.enter()
    counts = hac.screen(hub.conn)
    assert counts.moved == 0 and counts.kept == 0
    assert _events(hub.conn, tid, "moved") == []
    assert _events(hub.conn, tid, "hub_admission") == ([payload] if kind == "hub_admission" else [])


def test_unjudged_claims_is_empty_before_the_gate_and_ignores_later_verdicts(gws, monkeypatch):
    hub, _worker, _svc = gws
    early = _hub_card(hub, title="claimed before any verdict existed")
    with kb.write_txn(hub.conn):
        kb._append_event(hub.conn, early, "claimed", {"holder": HUB})
    assert hac.unjudged_claims(hub.conn, HUB) == []  # no verdict on this board yet: the gate is not live
    with kb.write_txn(hub.conn):  # a verdict recorded after the claim does not make that claim judged
        kb._append_event(hub.conn, early, "hub_admission", {"admit": True, "entry": "move"})
        # Same second as the claim: the gate opens no later than the claim (else the claim predates the gate
        # and is rightly ignored), but the verdict still follows it by event id. The claim is the Board's
        # holder-tagged one, stamped at grant time: the local lock claim lands after it and can fall in the next
        # second, which would open the gate after the claim.
        hub.conn.execute(
            "UPDATE task_events SET created_at = (SELECT MAX(created_at) FROM task_events WHERE task_id = ? "
            "AND kind = 'claimed' AND json_extract(payload, '$.holder') = ?) "
            "WHERE task_id = ? AND kind = 'hub_admission'", (early, HUB, early))
    assert [tid for tid, _at in hac.unjudged_claims(hub.conn, HUB)] == [early]
    audited = _hub_card(hub, title="admitted at creation",
                        placement_audit={"hub_admission": {"admit": True, "entry": "create", "why": "ok"}})
    _tick(hub)
    hub.round()
    assert audited not in [tid for tid, _at in hac.unjudged_claims(hub.conn, HUB)]


def test_unjudged_claims_accepts_a_creation_time_verdict_without_an_admission_event(gws):
    """A card whose only verdict rides its ``created`` payload (no ``hub_admission`` event) was judged."""
    hub, _worker, _svc = gws
    gate = _hub_card(hub, title="gate card")
    audited = _hub_card(hub, title="admitted at creation")
    bare = _hub_card(hub, title="never judged")
    with kb.write_txn(hub.conn):  # a synced creation: the audit rides the payload, no admission event
        hub.conn.execute(
            "UPDATE task_events SET payload = json_set(COALESCE(payload, '{}'), '$.hub_admission', json(?)) "
            "WHERE task_id = ? AND kind = 'created'",
            (json.dumps({"admit": True, "entry": "create", "why": "ok"}), audited))
        kb._append_event(hub.conn, gate, "hub_admission", {"admit": True, "entry": "move"})
        for tid in (audited, bare):
            kb._append_event(hub.conn, tid, "claimed", {"holder": HUB})
    assert _events(hub.conn, audited, "hub_admission") == []
    assert [tid for tid, _at in hac.unjudged_claims(hub.conn, HUB)] == [bare]


def test_unjudged_claims_gate_opens_on_a_creation_payload_verdict(gws):
    """A board whose only verdicts ride ``created`` payloads (synced creations) has a live gate: a later claim of
    an unjudged card is reported, not hidden behind a missing ``hub_admission`` event."""
    hub, _worker, _svc = gws
    audited = _hub_card(hub, title="admitted at creation")
    bare = _hub_card(hub, title="never judged")
    with kb.write_txn(hub.conn):
        hub.conn.execute(
            "UPDATE task_events SET payload = json_set(COALESCE(payload, '{}'), '$.hub_admission', json(?)) "
            "WHERE task_id = ? AND kind = 'created'",
            (json.dumps({"admit": True, "entry": "create", "why": "ok"}), audited))
        kb._append_event(hub.conn, bare, "claimed", {"holder": HUB})
    assert hub.conn.execute("SELECT COUNT(*) FROM task_events WHERE kind = 'hub_admission'").fetchone()[0] == 0
    assert [tid for tid, _at in hac.unjudged_claims(hub.conn, HUB)] == [bare]
    with kb.write_txn(hub.conn):  # a claim before that creation-time verdict predates the gate
        hub.conn.execute("UPDATE task_events SET created_at = created_at - 60 WHERE task_id = ? AND kind = 'claimed'",
                         (bare,))
    assert hac.unjudged_claims(hub.conn, HUB) == []


def test_unjudged_claims_counts_hub_claims_without_a_prior_verdict(gws, hub_judge, monkeypatch):
    hub, _worker, _svc = gws
    judged = _hub_card(hub, title="judged")
    _tick(hub)
    assert hac.unjudged_claims(hub.conn, HUB) == []
    unjudged = _hub_card(hub, title="unjudged")
    with kb.write_txn(hub.conn):  # historical bypass, independent of the fail-closed dispatcher
        kb._append_event(hub.conn, unjudged, "claimed", {"holder": HUB})
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
    assert "0 hub claims without an admission disposition" in capsys.readouterr().out
    tid = _hub_card(hub, title="claimed unjudged")
    with kb.write_txn(hub.conn):  # historical claim bypass, not a supported outage route
        kb._append_event(hub.conn, tid, "claimed", {"holder": HUB})
    hub.enter()
    found = doctor_state._check_hub_admission_claims(False)
    assert len(found.manual_issues) == 1 and tid in found.manual_issues[0], found.manual_issues
    assert "1 hub claim(s) without an admission disposition" in capsys.readouterr().out


def test_doctor_hub_claim_check_is_silent_off_the_hub(gws, monkeypatch, capsys):
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    from hermes_cli import doctor_state
    _hub, worker, _svc = gws
    worker.enter()
    assert doctor_state._check_hub_admission_claims(False).manual_issues == []
    assert capsys.readouterr().out == ""
