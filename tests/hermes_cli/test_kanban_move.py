"""kanban_move: a running card moves itself to machines with other runs_on labels (hermes_cli.kanban_move). [daveey]

One move = checkpoint + end this run as ``continued`` + new ``runs_on`` + a ``moved`` event, in one transaction; the
Board ends the old lease when the card leaves ``running`` and admits only a machine with the new labels. Entry paths:
the core API, the agent tool (``kanban_move``) and the CLI (``hermes kanban move``), each tested here, plus a
two-gateway run on the fake Board (placement and lease-end on) proving the card completes once, on the target.
"""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_move as km
from hermes_cli import kanban_worker_node as wn
from tests.hermes_cli.kanban_sync_fake_board import FakeService
from tests.hermes_cli.test_kanban_worker_node import BOARD, HUB, MACHINES, WORKER, Gateway, _runs

# kanban_move_pool: a machine:<x> target needs a host_reason naming state on x (t_01a1d95c41620e6c).
HR = "the card's worktree is on that host"


@pytest.fixture
def svc():
    return FakeService(lease_runs=True, machines=MACHINES)


@pytest.fixture
def gws(tmp_path, monkeypatch, svc):
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)
    sync.consume_dispatcher_wake()
    hub = Gateway(tmp_path, HUB, svc, monkeypatch, pickup=False)
    worker = Gateway(tmp_path, WORKER, svc, monkeypatch, pickup="true")
    yield hub, worker
    for g in (hub, worker):
        g.conn.close()
    sync.set_lease_client(None)
    wn.set_registry_fetch(None)


def _running_hub_card(hub, **kw):
    hub.enter()
    tid = kb.create_task(hub.conn, title=kw.pop("title", "work"), assignee="default",
                         runs_on=kw.pop("runs_on", ["machine:hub"]), **kw)
    hub.round()
    hub.tick()
    assert kb.get_task(hub.conn, tid).status == "running"
    hub.round()
    return tid, hub.conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (tid,)).fetchone()[0]


def _events(conn, tid, kind=km.MOVED_EVENT):
    return [json.loads(p or "{}") for (p,) in conn.execute(
        "SELECT payload FROM task_events WHERE task_id = ? AND kind = ? ORDER BY id", (tid, kind))]


# --- end to end: hub -> worker-1, one completion -----------------------------------------------------------------

def test_a_hub_card_moves_itself_to_the_worker_and_completes_there_once(gws, svc):
    hub, worker = gws
    tid, run_id = _running_hub_card(hub)
    res = km.move_task(hub.conn, tid, runs_on=["machine:worker-1"], host_reason=HR, reason="needs the worker's GPU",
                       checkpoint="step 1 done (commit abc); next: run the bench", expected_run_id=run_id)
    assert res["ok"] and res["matches"] == [WORKER] and res["move_no"] == 1 and res["status"] == "ready", res
    t = hub.conn.execute("SELECT status, runs_on, claim_lock FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert (t["status"], json.loads(t["runs_on"]), t["claim_lock"]) == ("ready", ["machine:worker-1"], None)
    [ev] = _events(hub.conn, tid)
    assert ev["from_machine"] == HUB and ev["previous_runs_on"] == ["machine:hub"] and ev["reason"]
    hub.round()
    assert tid not in svc.board(BOARD)["leases"]  # the Board ended the hub's lease when the card left running
    # The hub's own dispatcher cannot take it back: the Board's grant checks runs_on for the creator too.
    hub.tick()
    assert kb.get_task(hub.conn, tid).status != "running" or \
        hub.conn.execute("SELECT lease_holder FROM tasks WHERE id = ?", (tid,)).fetchone()[0] != HUB
    hub.round()
    assert tid not in svc.board(BOARD)["leases"] or svc.board(BOARD)["leases"][tid]["holder"] != HUB
    # worker-1 claims it, runs it with the checkpoint, completes it.
    worker.round()
    worker.tick()
    assert svc.board(BOARD)["leases"][tid]["holder"] == WORKER
    worker.enter()
    wrun = worker.conn.execute("SELECT current_run_id FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    assert kb.complete_task(worker.conn, tid, result="bench done on worker-1", expected_run_id=wrun)
    worker.round()
    hub.round()
    t = kb.get_task(hub.conn, tid)
    assert t.status == "done" and t.result == "bench done on worker-1"
    outcomes = sorted(r["outcome"] for r in svc.board(BOARD)["rows"]["task_runs"].values())
    assert outcomes.count("completed") == 1 and "continued" in outcomes, outcomes  # one completion, no double run


# --- guards ---------------------------------------------------------------------------------------------------------

def test_move_refuses_unknown_held_and_self_only_targets(gws):
    hub, _ = gws
    tid, run_id = _running_hub_card(hub)
    for runs_on in (["machine:nowhere"], ["machine:hub"], ["gpu"]):
        res = km.move_task(hub.conn, tid, runs_on=runs_on, reason="r", checkpoint="c", expected_run_id=run_id)
        assert not res["ok"] and res["reason_code"] == "no_machine", (runs_on, res)
    held = {**MACHINES, WORKER: {**MACHINES[WORKER], "hold": True}}
    res = km.move_task(hub.conn, tid, runs_on=["machine:worker-1"], host_reason=HR, reason="r", checkpoint="c",
                       expected_run_id=run_id, machines=held)
    assert res["reason_code"] == "no_machine"
    assert kb.get_task(hub.conn, tid).status == "running"  # a refused move leaves the run alone


def test_move_needs_a_reason_and_a_running_card(gws):
    hub, _ = gws
    tid, run_id = _running_hub_card(hub)
    assert km.move_task(hub.conn, tid, runs_on=["machine:worker-1"], host_reason=HR, reason=" ", checkpoint="c",
                        expected_run_id=run_id)["reason_code"] == "reason_required"
    assert km.move_task(hub.conn, tid, runs_on=["machine:worker-1"], host_reason=HR, reason="r", checkpoint="c",
                        expected_run_id=run_id + 99)["ok"] is False  # not this run's card


def test_workspace_bound_cards_never_move(gws):
    hub, _ = gws
    hub.enter()
    tid = kb.create_task(hub.conn, title="wt", assignee="default", workspace_kind="dir", workspace_path="/tmp/x")
    res = km.move_queued(hub.conn, tid, runs_on=["machine:worker-1"], host_reason=HR, reason="r")
    assert res["reason_code"] == "workspace_bound"


def test_max_moves_and_ping_pong(gws):
    hub, _ = gws
    hub.enter()
    tid = kb.create_task(hub.conn, title="q", assignee="default")
    # Every target advertises pickup: a machine:<x> move to a machine without it is refused (placement_unavailable).
    machines = {**MACHINES, HUB: {**MACHINES[HUB], "pickup": "explicit"},
                "w2": {"name": "w2", "state": "active", "labels": ["linux"], "hold": False, "pickup": "all"}}
    # hub -> worker-1 -> w2: each move leaves a machine.
    assert km.move_queued(hub.conn, tid, runs_on=["machine:worker-1"], host_reason=HR, reason="1", machines=machines, me=HUB)["ok"]
    assert km.move_queued(hub.conn, tid, runs_on=["machine:w2"], host_reason=HR, reason="2", machines=machines, me=WORKER)["ok"]
    # w2 -> hub: the card already left the hub -> ping-pong, unless forced (and a forced move still counts).
    res = km.move_queued(hub.conn, tid, runs_on=["machine:hub"], host_reason=HR, reason="3", machines=machines, me="w2")
    assert res["reason_code"] == "ping_pong"
    res = km.move_queued(hub.conn, tid, runs_on=["machine:hub"], host_reason=HR, reason="3", machines=machines, me="w2", force=True)
    assert res["ok"] and res["move_no"] == 3 and _events(hub.conn, tid)[-1]["forced"] is True
    res = km.move_queued(hub.conn, tid, runs_on=["machine:worker-1"], host_reason=HR, reason="4", machines=machines, me=HUB,
                         force=True)
    assert res["reason_code"] == "move_limit"  # default kanban.move.max_moves = 3
    assert [e["reason"] for e in _events(hub.conn, tid)] == ["1", "2", "3"]  # the audit log
    assert km.max_moves({"max_moves": 5}) == 5 and km.max_moves({"max_moves": "x"}) == km.DEFAULT_MAX_MOVES


def _capped_card_pinned_to(hub, machine):
    """A queued card that spent the default 3-move budget, then pinned to ``machine:<machine>`` (as a card whose
    pinned worker was retired after its last move: t_01a1a1dea9c64d6a on worker-6)."""
    hub.enter()
    tid = kb.create_task(hub.conn, title="q", assignee="default")
    machines = {**MACHINES, HUB: {**MACHINES[HUB], "pickup": "explicit"},
                "w2": {"name": "w2", "state": "active", "labels": ["linux"], "hold": False, "pickup": "all"}}
    for runs_on, me, force in (("machine:worker-1", HUB, False), ("machine:w2", WORKER, False),
                               ("machine:hub", "w2", True)):
        assert km.move_queued(hub.conn, tid, runs_on=[runs_on], host_reason=HR, reason="r", machines=machines,
                              me=me, force=force)["ok"]
    with kb.write_txn(hub.conn):
        wn.set_runs_on(hub.conn, tid, [f"machine:{machine}"])
    return tid


# The registry after worker-6 was revoked: GET /v2/machines lists active gateways only (hermes-hub mailbox.ts
# ``machines()``: ``WHERE state = 'active'``), so a revoked machine is absent, not a row with state "revoked".
LIVE = {**MACHINES, "w3": {"name": "w3", "state": "active", "labels": ["linux"], "hold": False, "pickup": "all"}}


@pytest.mark.parametrize("registry", [
    LIVE,  # absent from a successful registry read: no machine can lease machine:worker-6
    {**LIVE, "worker-6": {"name": "worker-6", "state": "revoked", "labels": ["linux"], "hold": False}},
], ids=["absent", "revoked"])
def test_a_capped_move_off_a_retired_pinned_machine_is_allowed_and_audited(gws, registry):
    hub, _ = gws
    tid = _capped_card_pinned_to(hub, "worker-6")
    origin = hub.conn.execute("SELECT created_on FROM tasks WHERE id = ?", (tid,)).fetchone()[0]
    res = km.move_queued(hub.conn, tid, runs_on=["linux"], reason="worker-6 was retired", checkpoint="step 2 next",
                         machines=registry, me=HUB)
    assert res["ok"] and res["move_no"] == 4, res
    ev = _events(hub.conn, tid)[-1]
    assert ev["dead_pin"] == "worker-6" and ev["previous_runs_on"] == ["machine:worker-6"] and not ev["forced"]
    assert ev["checkpoint"] == "step 2 next"
    t = hub.conn.execute("SELECT runs_on, created_on FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert json.loads(t["runs_on"]) == ["linux"] and t["created_on"] == origin  # the origin is not rehomed
    # The rescue spent the budget like any move: a later healthy move is still capped.
    res = km.move_queued(hub.conn, tid, runs_on=["machine:w3"], host_reason=HR, reason="again", machines=registry,
                         me=HUB)
    assert res["reason_code"] == "move_limit", res


@pytest.mark.parametrize("entry", [
    {"state": "active", "hold": True},  # held: drains and comes back
    {"state": "active", "hold": False, "last_seen": 1},  # stale telemetry: maybe asleep, not retired
    {"state": "active", "hold": False, "max_card_runs": 1},  # busy: a queue, not a dead pin
], ids=["held", "stale", "busy"])
def test_a_capped_move_off_a_live_pinned_machine_is_still_refused(gws, entry):
    hub, _ = gws
    tid = _capped_card_pinned_to(hub, "worker-6")
    registry = {**LIVE, "worker-6": {"name": "worker-6", "labels": ["linux"], "pickup": "all", **entry}}
    res = km.move_queued(hub.conn, tid, runs_on=["linux"], reason="r", machines=registry, me=HUB)
    assert res["reason_code"] == "move_limit", res


def test_a_capped_dead_pin_needs_affirmative_registry_evidence_and_a_queued_card(gws):
    hub, _ = gws
    tid = _capped_card_pinned_to(hub, "worker-6")
    # An unreadable registry refuses before the budget (registry_unavailable); an empty read proves nothing.
    assert km.dead_pin(["machine:worker-6"], {}, me=HUB) is None
    assert km.dead_pin(["machine:worker-6", "machine:hub"], LIVE, me=HUB) is None  # not exactly one pin
    assert km.dead_pin(["pool:worker"], LIVE, me=HUB) is None  # an ordinary capped move stays capped
    assert km.dead_pin(["machine:hub"], {"w3": LIVE["w3"]}, me=HUB) is None  # the origin's own pin is never dead
    assert km.dead_pin(["machine:worker-6"], LIVE, me=HUB) == "worker-6"
    # A live claim is refused: a running card is not stranded, whatever its labels say.
    with kb.write_txn(hub.conn):
        hub.conn.execute("UPDATE tasks SET status = 'running' WHERE id = ?", (tid,))
    with pytest.raises(km.MoveRefused) as exc:
        km.check(hub.conn, tid, ["linux"], machines=LIVE, me=HUB, reason="r")
    assert exc.value.code == "move_limit"


def test_labels_matching_a_machine_it_never_left_are_not_ping_pong(gws):
    hub, _ = gws
    hub.enter()
    tid = kb.create_task(hub.conn, title="q", assignee="default")
    machines = {**MACHINES, "w2": {"name": "w2", "state": "active", "labels": ["linux"], "hold": False}}
    assert km.move_queued(hub.conn, tid, runs_on=["machine:worker-1"], host_reason=HR, reason="1", machines=machines, me=HUB)["ok"]
    # From worker-1, ["linux"] matches the hub (left) and w2 (never visited): allowed.
    assert km.move_queued(hub.conn, tid, runs_on=["linux"], reason="2", machines=machines, me=WORKER)["ok"]


# --- entry path: agent tool -------------------------------------------------------------------------------------------

def test_tool_kanban_move_moves_the_workers_own_card(gws, monkeypatch):
    hub, _ = gws
    tid, run_id = _running_hub_card(hub)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_move({"runs_on": ["machine:worker-1"], "host_reason": HR, "reason": "needs linux box",
                                      "checkpoint": "done: A; next: B"}))
    assert out.get("ok", True) and out["runs_on"] == ["machine:worker-1"] and out["matches"] == [WORKER], out
    assert "End your turn" in out["next"]
    assert json.loads(hub.conn.execute("SELECT runs_on FROM tasks WHERE id = ?", (tid,)).fetchone()[0]) == [
        "machine:worker-1"]
    assert _events(hub.conn, tid)[0]["reason"] == "needs linux box"
    out = json.loads(kt._handle_move({"runs_on": ["machine:worker-1"], "host_reason": HR, "reason": "again", "checkpoint": "x"}))
    assert out.get("ok") is False or "error" in out  # the run is over: no second move from it


def test_tool_kanban_move_requires_its_arguments(gws, monkeypatch):
    hub, _ = gws
    tid, run_id = _running_hub_card(hub)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    from tools import kanban_tools as kt
    for args in ({"reason": "r", "checkpoint": "c"}, {"runs_on": ["machine:worker-1"], "checkpoint": "c"},
                 {"runs_on": ["machine:worker-1"], "reason": "r"}, {"runs_on": [], "reason": "r", "checkpoint": "c"}):
        out = json.loads(kt._handle_move(args))
        assert "error" in out, (args, out)
    assert kb.get_task(hub.conn, tid).status == "running"


def test_kanban_move_is_a_registered_terminal_tool():
    from agent.kanban_stop import _TERMINAL_KANBAN_TOOLS
    from tools import kanban_tools as kt
    from tools.registry import registry
    assert "kanban_move" in _TERMINAL_KANBAN_TOOLS and "kanban_move" in kt._RUN_LIFECYCLE_TOOLS
    assert registry.get_entry("kanban_move") is not None if hasattr(registry, "get_entry") else True


# --- entry path: CLI ----------------------------------------------------------------------------------------------------

def _cli(line):
    from hermes_cli import kanban as kcli
    return kcli.run_slash(line)


def test_cli_move_relabels_a_queued_card(gws):
    hub, _ = gws
    hub.enter()
    tid = kb.create_task(hub.conn, title="queued", assignee="default")
    out = json.loads(_cli(f'move {tid} --runs-on machine:worker-1 --host-reason worktree --reason "needs the worker" --json'))
    assert out["ok"] and out["runs_on"] == ["machine:worker-1"], out
    assert _events(hub.conn, tid)[0]["queued"] is True
    out = json.loads(_cli(f'move {tid} --runs-on machine:nowhere --host-reason worktree --reason x --json'))
    assert out["reason_code"] == "no_machine"


def test_cli_move_of_a_running_card_needs_reclaim(gws, monkeypatch):
    hub, _ = gws
    tid, _ = _running_hub_card(hub)
    # The fixture's spawner records this test process as the worker pid; reclaim must not SIGTERM it.
    stopped = []
    monkeypatch.setattr(kb, "_terminate_reclaimed_worker", lambda pid, lock, **kw: stopped.append(pid) or {
        "prev_pid": pid, "host_local": True, "termination_attempted": True, "terminated": True, "sigkill": False})
    out = json.loads(_cli(f"move {tid} --runs-on machine:worker-1 --host-reason worktree --reason x --json"))
    assert out["reason_code"] == "running" and kb.get_task(hub.conn, tid).status == "running"
    out = json.loads(_cli(f'move {tid} --runs-on machine:worker-1 --host-reason worktree --reason "stuck here" --reclaim --json'))
    assert out["ok"], out
    t = hub.conn.execute("SELECT status, runs_on, claim_lock FROM tasks WHERE id = ?", (tid,)).fetchone()
    assert t["status"] != "running" and t["claim_lock"] is None and json.loads(t["runs_on"]) == ["machine:worker-1"]
    assert _events(hub.conn, tid)[0]["reclaimed"] is True and stopped  # the old run's worker was stopped


def test_cli_move_by_the_cards_own_worker_ends_its_run(gws, monkeypatch):
    hub, _ = gws
    tid, run_id = _running_hub_card(hub)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    out = json.loads(_cli(f'move {tid} --runs-on machine:worker-1 --host-reason worktree --reason "needs linux" --json'))
    assert out["reason_code"] == "checkpoint_required"  # refused before touching the card
    assert kb.get_task(hub.conn, tid).status == "running"
    out = json.loads(_cli(f'move {tid} --runs-on machine:worker-1 --host-reason worktree --reason "needs linux" '
                          f'--checkpoint "did A; next B" --json'))
    assert out["ok"] and out["status"] == "ready", out
    run = hub.conn.execute("SELECT outcome FROM task_runs WHERE id = ?", (run_id,)).fetchone()
    assert run["outcome"] == "continued"
