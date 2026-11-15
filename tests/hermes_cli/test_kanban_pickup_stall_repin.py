"""A pickup-stall alarm's run re-places exactly the cards it lists, off the stalled machine. [daveey]

t_01a1cea829121562 (2026-11-12): the alarm about machine M (here worker-1) runs elsewhere and says to re-place cards
that cannot wait for M, but its run had no supported way: worker shells are fenced from ``hermes kanban move`` and
``kanban_move(task_id=<other>)`` refused every card the run's own task did not create. The grant
(``kanban_pickup_stall_repin``) moves only a card the alarm lists, only while it is ready and pinned to M, and only to
one other machine eligible now. The end-to-end tests fail on the code before it (``not_creator_task``); the unit
tests kill one mutant per scope condition.
"""
from __future__ import annotations

import json

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_pickup_stall as kps
from hermes_cli import kanban_pickup_stall_repin as rp
from hermes_cli import kanban_worker_node as wn
from hermes_cli.kanban_db_connect import connect
from tests.hermes_cli import test_kanban_operator_call as oc
from tests.hermes_cli import test_kanban_pickup_alarm_placement as place
from tests.hermes_cli import test_kanban_pickup_stall as stall
from tests.hermes_cli import test_kanban_worker_node as support

M, X = "worker-1", "worker-2"
NOW = 1_800_000_000_000
# t_01a1d95c41620e6c: a move to one machine needs a host_reason naming state on it; capacity is never one.
HR = "the card's worktree and suite logs are on worker-2"
svc = support.svc
fleet = oc.fleet


def _worker(name, **kw):
    # Target-card helpers are not pooled by default; only _pooled opts into that lane.
    return place._worker(name, last_seen=NOW, labels=["linux", *place.NEED, "capability:hub-write"], **kw)


def _machines(**over):
    pool = {M: _worker(M, hold=True), X: _worker(X), "hub": place._hub(last_seen=NOW)}
    for name, change in over.items():
        name = name.replace("_", "-")
        pool[name] = None if change is None else {**pool.get(name, _worker(name)), **change}
    return {n: e for n, e in pool.items() if e is not None}


# --- unit: one test per scope condition ----------------------------------------------------------------------------

class Board:
    def __init__(self, tmp_path):
        self.conn = connect(tmp_path / "board.db")
        have = {r[1] for r in self.conn.execute("PRAGMA table_info(tasks)")}
        for col in ("created_on", "runs_on", "lease_holder"):  # the synced-board columns
            if col not in have:
                self.conn.execute(f"ALTER TABLE tasks ADD COLUMN {col} TEXT")
        self.conn.commit()
        self.target = self.card("stalled work", runs_on=[f"machine:{M}"])
        self.unlisted = self.card("unlisted work", runs_on=[f"machine:{M}"])
        self.alarm = self.card(f"Pickup held on {M}: oldest {self.target}", created_by=kps.DETECTOR, created_on=M,
                               key=f"{kps.KEY_PREFIX}{M}:hold:{NOW}",
                               body=f"1 card(s) pinned...\n\n- {self.target} (stalled work), ready 15 min, last skip hold")

    def card(self, title, *, runs_on=None, created_by="default", created_on="hub", key=None, body=None, **kw):
        tid = kb.create_task(self.conn, title=title, assignee="default", created_by=created_by, body=body,
                             idempotency_key=key, **kw)
        with kb.write_txn(self.conn):
            self.conn.execute("UPDATE tasks SET created_on = ?, runs_on = ? WHERE id = ?",
                              (created_on, json.dumps(runs_on) if runs_on else None, tid))
        return tid

    def set(self, tid, **cols):
        with kb.write_txn(self.conn):
            for col, value in cols.items():
                self.conn.execute(f"UPDATE tasks SET {col} = ? WHERE id = ?", (value, tid))

    def why(self, target=None, runs_on=(f"machine:{X}",), machines=None, alarm=None, host_reason=None):
        if host_reason is None and any(str(label).startswith("machine:") for label in runs_on):
            host_reason = HR
        return rp.refusal(self.conn, target or self.target, alarm or self.alarm, list(runs_on),
                          machines=_machines() if machines is None else machines, now_ms=NOW,
                          host_reason=host_reason or None)


@pytest.fixture
def board(tmp_path):
    b = Board(tmp_path)
    yield b
    b.conn.close()


def test_a_listed_ready_card_pinned_to_the_stalled_machine_may_go_to_an_eligible_machine(board):
    assert board.why() is None


@pytest.mark.parametrize("col,value", [
    ("created_by", "default"),                               # not the detector's card
    ("status", "done"), ("status", "archived"),              # a closed alarm grants nothing
    ("idempotency_key", f"repin-owner:{M}:hold:1"),          # not a pickup-stall key
    ("idempotency_key", f"pickup-stale:{M}:hold:1"),         # same length, not the prefix (M would parse)
    ("idempotency_key", f"{kps.KEY_PREFIX}:hold:1"),         # no machine in the key
    ("created_on", "worker-5"),                              # filed by another gateway than M
])
def test_only_an_open_alarm_the_detector_filed_on_the_stalled_machine_grants(board, col, value):
    board.set(board.alarm, **{col: value})
    assert board.why() is not None


def test_the_stalled_machine_comes_from_the_key_not_from_the_caller(board):
    # An alarm about worker-3 cannot move a card off worker-1 (the card is not pinned to its machine).
    board.set(board.alarm, idempotency_key=f"{kps.KEY_PREFIX}worker-3:hold:1", created_on="worker-3")
    assert "no longer pinned to machine:worker-3" in board.why()


def test_only_a_card_the_alarm_lists(board):
    assert "lists" in board.why(target=board.unlisted)


def test_a_per_card_alarm_lists_the_card_its_key_names(board):
    alarm = board.card(f"Pickup stalled: {board.unlisted}", created_by=kps.DETECTOR, created_on=M,
                       key=f"{kps.KEY_PREFIX}{M}:{board.unlisted}:{NOW}", body="Card ... has been ready")
    assert board.why(target=board.unlisted, alarm=alarm) is None
    assert "lists" in board.why(target=board.target, alarm=alarm)


def test_a_host_wide_alarm_lists_only_its_body_and_comment_lines(board):
    assert board.why(target=board.unlisted) is not None and board.why() is None


@pytest.mark.parametrize("author,origin,listed", [
    (kps.DETECTOR, None, True), (kps.DETECTOR, M, True),
    ("default", None, False),          # any other author (an agent) cannot add targets
    (kps.DETECTOR, "worker-5", False),  # a detector-named comment another gateway wrote
])
def test_a_later_target_is_listed_only_by_the_detectors_own_comment(board, author, origin, listed):
    with kb.write_txn(board.conn):  # as synced: the writing gateway, or NULL when the Board did not say
        board.conn.execute(
            "INSERT INTO task_comments (task_id, author, body, created_at, origin_gateway) VALUES (?, ?, ?, 0, ?)",
            (board.alarm, author, f"Additional targets for this pickup cause:\n\n- {board.unlisted} (unlisted "
                                  "work), ready 11 min", origin))
    assert (board.why(target=board.unlisted) is None) is listed


@pytest.mark.parametrize("cols", [
    {"status": "running"}, {"status": "blocked"}, {"status": "todo"},
    {"claim_lock": "host:1"}, {"current_run_id": 7},
])
def test_only_a_ready_unclaimed_card_without_a_run(board, cols):
    board.set(board.target, **cols)
    assert board.why() is not None


def test_a_card_with_a_lease_holder_is_not_moved(board):
    board.set(board.target, lease_holder=X)
    assert board.why() is not None


@pytest.mark.parametrize("pin", [None, '["machine:worker-3"]', '["linux"]'])
def test_only_a_card_still_pinned_to_the_stalled_machine(board, pin):
    board.set(board.target, runs_on=pin)
    assert board.why() is not None


@pytest.mark.parametrize("runs_on", [
    [f"machine:{M}"],                       # back onto M
    ["linux"],                              # no machine named: the Board could pick M again
    [f"machine:{X}", "linux"],              # exactly one machine label, nothing else
    [f"machine:{X}", "machine:worker-3"],
    [], ["Bad Label"],
])
def test_runs_on_is_exactly_one_other_machine(board, runs_on):
    assert board.why(runs_on=runs_on) is not None


@pytest.mark.parametrize("change", [
    None,                                   # not in the registry
    {"state": "retired"}, {"hold": True}, {"pickup": None},
    {"labels": ["linux", "capability:file"]},  # no profile:default
    {"metrics": {"active_card_runs": 0, "drain_active": 1}},
    {"last_seen": NOW - 301_000},
    {"metrics": {"active_card_runs": 10}},  # no free slot
])
def test_the_new_machine_must_be_eligible_now(board, change):
    assert board.why(machines=_machines(**{X: change})) is not None


# t_01a1d0214f3e63e8 (2026-11-12): every eligible machine was a pool worker, and a move to ``machine:<pool worker>``
# needs a host_reason no alarm path can pass (kanban_move_pool), so the alarm could re-place nothing. The pool queue
# is the alarm's way off M whenever another pool worker is eligible now.
POOL = "pool:worker"


def _pooled(**over):
    return _machines(**{X: {"labels": [*_worker(X)["labels"], POOL]}}, **over)


def test_the_pool_queue_is_a_valid_target_when_another_pool_worker_is_eligible(board):
    assert board.why(runs_on=[POOL], machines=_pooled()) is None


@pytest.mark.parametrize("machines", [
    lambda: _machines(),                                               # no machine carries the pool label
    lambda: _pooled(worker_2={"hold": True}),                          # the only other pool worker is held
    lambda: _pooled(worker_2={"state": "retired"}),                    # ... or retired
    lambda: _pooled(worker_2={"pickup": None}),                        # ... or takes no pickup
    lambda: _machines(worker_1={"hold": False, "labels": [*_worker(M)["labels"], POOL]}),  # only M is pooled
    lambda: None,                                                      # the registry is unreadable
])
def test_the_pool_queue_is_refused_when_no_other_pool_worker_can_take_it(board, machines):
    why = rp.refusal(board.conn, board.target, board.alarm, [POOL], machines=machines(), now_ms=NOW)
    assert why is not None and (POOL in why or "unreadable" in why), why


@pytest.mark.parametrize("change", [
    {"metrics": {"active_card_runs": 10}},  # full now: the card waits in the single queue for a slot
    {"metrics": {"active_card_runs": 0, "drain_active": 1}},
])
def test_the_pool_queue_does_not_need_a_free_slot_now(board, change):
    """t_01a1d95c41620e6c: requiring a free pool slot sent the alarm to ``machine:<X>`` pins (worker-1/-4/-8 on 11-13)
    that stalled again on X. The single queue waits for any worker's slot."""
    assert board.why(runs_on=[POOL], machines=_pooled(worker_2=change)) is None


# --- t_01a1d95c41620e6c: a machine:<X> target needs a host_reason naming state on X ---------------------------------

def test_an_alarm_move_to_one_machine_without_a_host_reason_is_refused_pointing_at_the_pool(board):
    why = board.why(host_reason="")
    assert why is not None and "host_reason" in why and POOL in why, why


@pytest.mark.parametrize("reason", ["worker-1 10/10, no pool worker eligible now", "worker-2 has free slots",
                                    "capacity on worker-2", "worker-1 is full"])
def test_an_alarm_move_to_one_machine_citing_capacity_is_refused(board, reason):
    why = board.why(host_reason=reason)
    assert why is not None and "capacity" in why and POOL in why, why


def test_the_same_alarm_move_to_the_pool_is_accepted(board):
    assert board.why(runs_on=[POOL], machines=_pooled(), host_reason="") is None


def test_an_alarm_move_naming_state_on_the_machine_is_accepted(board):
    assert board.why(host_reason="worker-2 holds the card's worktree ~/code/_wt/x with unpushed commits") is None


@pytest.mark.parametrize("runs_on", [[POOL, "linux"], [POOL, f"machine:{X}"], ["pool:gpu"]])
def test_the_pool_queue_is_exactly_the_pool_label(board, runs_on):
    assert board.why(runs_on=runs_on, machines=_pooled(worker_2={
        "labels": [*_worker(X)["labels"], POOL, "pool:gpu"]})) is not None


def test_the_pool_queue_still_needs_the_cards_capabilities(board):
    board.set(board.target, requires=json.dumps(["gpu"]))
    assert "capability:gpu" in board.why(runs_on=[POOL], machines=_pooled())
    assert board.why(runs_on=[POOL], machines=_pooled(
        worker_2={"labels": [*_worker(X)["labels"], POOL, "capability:gpu"]})) is None


def test_the_new_machine_needs_the_cards_required_capabilities(board):
    board.set(board.target, requires=json.dumps(["gpu"]))
    assert "capability:gpu" in board.why()
    assert board.why(machines=_machines(**{X: {"labels": [*_worker(X)["labels"], "capability:gpu"]}})) is None


def test_declared_hub_write_needs_the_hyphenated_label_a_worker_attests(board):
    """t_01a1d28d2ff68689: ``requires=["hub:write"]`` built ``capability:hub:write``, which no worker carries, so
    the pool move refused every worker that attests ``capability:hub-write``."""
    board.set(board.target, requires=json.dumps(["terminal", "hub:write"]))
    pooled = {"labels": ["linux", "capability:terminal", "capability:hub-write", "profile:default", POOL]}
    assert board.why(runs_on=[POOL], machines=_machines(**{X: pooled})) is None
    assert board.why(machines=_machines(**{X: pooled})) is None
    lacking = {"labels": ["linux", "capability:terminal", "profile:default", POOL]}
    assert "capability:hub-write" in board.why(runs_on=[POOL], machines=_machines(**{X: lacking}))


@pytest.mark.parametrize("access", ["ssh:mbp", "ssh:mini"])
def test_declared_ssh_access_refuses_the_pool_queue_with_the_board_reason(board, access):
    """t_01a1d2b4b091e158: workers attest ``capability:ssh-mbp``/``ssh-mini``, but the Board's pool selection cannot
    place ssh access, so a pool move would leave the card in task_requirement_unsupported."""
    from hermes_cli import kanban_placement_coordination as coord
    board.set(board.target, requires=json.dumps([access]))
    label = coord.requirement_label(access)
    machines = _pooled(worker_2={"labels": [*_worker(X)["labels"], POOL, label]})
    assert rp.pool_refusal(M, machines, rp._need(board.conn.execute(
        "SELECT * FROM tasks WHERE id = ?", (board.target,)).fetchone()), NOW) is None  # a worker qualifies
    why = board.why(runs_on=[POOL], machines=machines)
    assert why is not None and coord.BOARD_REASON in why, why


@pytest.mark.parametrize("stored", ['{"a":1}', '"ssh:mbp"', '["terminal", 3]', "null", "not json"])
def test_a_requires_the_board_cannot_read_refuses_the_pool_queue_only(board, stored):
    """t_01a1d2d611cfe71d: ``_requires`` maps a stored requires that is not a JSON list of strings to ``[]``, so the
    pool move looked eligible with only ``profile:<assignee>``; the Board's ``stringList`` answers
    ``task_metadata_invalid`` for that row and never pool-places it. A move to one machine is unchanged."""
    board.set(board.target, requires=stored)
    why = board.why(runs_on=[POOL], machines=_pooled())
    assert why is not None and "task_metadata_invalid" in why and "not a JSON list of strings" in why, why
    assert board.why(machines=_pooled()) is None  # machine:<X> still moves


@pytest.mark.parametrize("stored", [None, "[]", '["terminal"]'])
def test_a_requires_the_board_reads_still_takes_the_pool_queue(board, stored):
    board.set(board.target, requires=stored)
    machines = _pooled(worker_2={"labels": [*_worker(X)["labels"], POOL, "capability:terminal"]})
    assert board.why(runs_on=[POOL], machines=machines) is None


@pytest.mark.parametrize("stored", ['{"a":1}', '"kanban-worker"', '["kanban-worker", 3]', "null", "not json"])
def test_skills_the_board_cannot_read_refuse_the_pool_queue_only(board, stored):
    """t_01a1d2f66de283de: the Board's ``requiredLabels`` reads ``skills`` through the same ``stringList`` as
    ``requires`` and answers ``task_metadata_invalid`` for a row it cannot read, so a pool move would stall. A move to
    one machine is unchanged."""
    board.set(board.target, skills=stored)
    why = board.why(runs_on=[POOL], machines=_pooled())
    assert why is not None and "task_metadata_invalid" in why and "stored skills" in why, why
    assert board.why(machines=_pooled()) is None  # machine:<X> still moves


def test_the_pool_queue_needs_the_cards_skill_labels(board):
    """t_01a1d2f66de283de: the Board's ``requiredLabels`` adds ``skill:<s>``; a pool worker without it never takes the
    card, so the pool move is refused until one attests it. A move to one machine does not need the skill label."""
    board.set(board.target, skills=json.dumps(["kanban-worker", "github"]))
    why = board.why(runs_on=[POOL], machines=_pooled())
    assert why is not None and "skill:kanban-worker" in why and "skill:github" in why, why
    partial = _pooled(worker_2={"labels": [*_worker(X)["labels"], POOL, "skill:kanban-worker"]})
    assert board.why(runs_on=[POOL], machines=partial) is not None
    full = _pooled(worker_2={"labels": [*_worker(X)["labels"], POOL, "skill:kanban-worker", "skill:github"]})
    assert board.why(runs_on=[POOL], machines=full) is None
    assert board.why(machines=_pooled()) is None  # machine:<X> unchanged


@pytest.mark.parametrize("stored", [None, "[]"])
def test_no_skills_still_takes_the_pool_queue(board, stored):
    board.set(board.target, skills=stored)
    assert board.why(runs_on=[POOL], machines=_pooled()) is None


def test_a_row_without_a_skills_column_reads_as_no_skills():
    """An older replica row has no ``skills`` column: NULL, as the Board reads a missing value."""
    row = {"requires": None, "assignee": "default"}
    assert rp.malformed_skills(row) is None
    assert rp._pool_need(row) == rp._need(row) == {"profile:default"}


@pytest.mark.parametrize("name, label", [("hub:write", "capability:hub-write"), ("hub:read", "capability:hub-read"),
                                         ("ssh:mbp", "capability:ssh-mbp"), ("ssh:mini", "capability:ssh-mini"),
                                         ("terminal", "capability:terminal")])
def test_every_need_site_maps_access_names_to_their_attested_labels(name, label):
    from hermes_cli import kanban_placement_coordination as coord
    from hermes_cli import kanban_repin_notice as rn
    assert coord.requirement_label(name) == label
    assert label in rp._need({"requires": json.dumps([name]), "assignee": "default"})
    assert label in rn.required_labels({"requires": json.dumps([name]), "assignee": "default"})
    machines = {X: {**_worker(X), "labels": ["linux", label, "profile:default"]}}
    assert kps.alarm_placement(M, machines, assignee="default", requires=(name,), now_ms=NOW)[0] == sorted(
        {label, "profile:default", "capability:hub-write", POOL})


@pytest.mark.parametrize("assignee", [None, ""])
def test_a_card_with_no_assignee_needs_the_default_profile_like_the_board(assignee):
    """t_01a1d3123e9d25fd: the Board's ``requiredLabels`` reads ``assignee || "default"``; ``_need`` built
    ``profile:None`` / ``profile:``, which no worker carries, so the pool move looked ineligible."""
    from hermes_cli import kanban_repin_notice as rn
    row = {"requires": None, "assignee": assignee}
    assert rp._need(row) == {"profile:default"} == set(rn.required_labels(row))
    assert rp.pool_refusal(M, _pooled(), rp._need(row), NOW) is None


def test_the_hub_lane_without_hub_write_is_a_valid_target(board):
    assert board.why(runs_on=["machine:hub"]) is None  # hub-write is the alarm's need, not the card's


def test_an_unreadable_registry_refuses(board):
    assert "unreadable" in rp.refusal(board.conn, board.target, board.alarm, [f"machine:{X}"], machines=None,
                                      now_ms=NOW, host_reason=HR)


def test_a_card_not_on_this_board_or_only_a_pickup_copy_points_at_the_operator_call(board, monkeypatch):
    assert "kanban_supervisor_request" in board.why(target="t_absent")
    from hermes_cli import kanban_capability_routing as kcr
    monkeypatch.setattr(kcr, "card_home", lambda conn, tid: "hub")
    assert "kanban_supervisor_request" in board.why()


def test_the_scope_never_reads_or_writes_a_hold():
    import inspect
    src = inspect.getsource(rp)
    assert "set_registry" not in src and "\"hold\"" not in src and "'hold'" not in src


# --- end to end through the kanban_move tool (fails before: not_creator_task) --------------------------------------

@pytest.fixture
def held(tmp_path, monkeypatch, svc):
    """worker-1 is held; worker-3 runs its alarm; worker-2 is the eligible target."""
    from hermes_cli import kanban_db_dispatch as kbd
    from hermes_cli import kanban_db_sync as sync
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: None)
    sync.consume_dispatcher_wake()
    hub = support.Gateway(tmp_path, support.HUB, svc, monkeypatch, pickup="explicit")
    worker = support.Gateway(tmp_path, M, svc, monkeypatch, pickup="true")
    runner = support.Gateway(tmp_path, "worker-3", svc, monkeypatch, pickup="explicit")
    # Separate the capable alarm runner from the non-pooled target worker.
    place._registry(monkeypatch, held=True, other=False, hub=place._hub())
    monkeypatch.setitem(support.MACHINES, X, {**place._worker(X), "labels": ["linux", *place.NEED]})
    monkeypatch.setitem(support.MACHINES, "worker-3", place._worker("worker-3"))
    try:
        # Local-move coverage: the alarm runner owns these targets, though they are pinned to M.
        # Foreign-owned targets use the separately tested operator-call route below.
        a, b = stall._pinned(runner, title="a"), stall._pinned(runner, title="b")
        runner.round()
        worker.round()
        worker.enter()
        wn.pickup(worker.conn, budget=4)
        stall._age(worker, a, 900)
        stall._age(worker, b, 700)
        [alarm] = kps.sweep(worker.conn)
        assert place._pin(worker.conn, alarm) == place.QUEUE
        worker.round()
        runner.round()
        runner.tick()
        assert kb.get_task(runner.conn, alarm).status == "running"
        # Drain further pickup on the runner without interrupting its claimed alarm.
        # Only worker-2 may accept the target work in the move tests below.
        monkeypatch.setitem(support.MACHINES, "worker-3", {**support.MACHINES["worker-3"], "hold": True})
        unlisted = stall._pinned(runner, title="never listed")
        runner.round()
        runner.enter()
        monkeypatch.setenv("HERMES_KANBAN_TASK", alarm)
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(kb._current_run_id(runner.conn, alarm)))
        monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
        yield runner, worker, alarm, (a, b), unlisted
    finally:
        for g in (hub, worker, runner):
            g.conn.close()
        sync.set_lease_client(None)
        wn.set_registry_fetch(None)


def _move(task_id, runs_on, host_reason=None):
    from tools import kanban_tools as kt
    args = {"task_id": task_id, "runs_on": runs_on, "reason": f"{M} is on hold", "checkpoint": "n/a"}
    if host_reason:
        args["host_reason"] = host_reason
    return json.loads(kt._handle_move(args))


def test_the_alarms_run_re_places_each_listed_card_and_keeps_running(held):
    hub, _worker_gw, alarm, cards, _unlisted = held
    body = kb.get_task(hub.conn, alarm).body
    assert "kanban_move(task_id=" in body and "kanban_supervisor_request" in body  # the alarm names the command
    for tid in cards:
        out = _move(tid, [f"machine:{X}"])  # t_01a1d95c41620e6c: a capacity stall is no state on X
        assert out.get("ok") is not True and "host_reason" in out["error"], out
        assert place._pin(hub.conn, tid) == [f"machine:{M}"]
        out = _move(tid, [f"machine:{X}"], host_reason=HR)
        assert out.get("ok") is True, out
        assert place._pin(hub.conn, tid) == [f"machine:{X}"]
        [moved] = [json.loads(e[0]) for e in hub.conn.execute(
            "SELECT payload FROM task_events WHERE task_id = ? AND kind = 'moved'", (tid,))]
        assert moved["pickup_stall_alarm"] == alarm and moved["stalled_machine"] == M
    assert kb.get_task(hub.conn, alarm).status == "running"  # moving another card never ends this run
    assert support.MACHINES[M]["hold"] is True  # the hold is the operator's: never lifted or shortened


def test_the_alarms_run_re_places_a_listed_card_onto_the_pool_queue(held, monkeypatch):
    hub, _worker_gw, alarm, (a, b), _unlisted = held
    body = kb.get_task(hub.conn, alarm).body
    assert f'runs_on=["{POOL}"]' in body  # the alarm recommends the pool queue first
    out = _move(a, [POOL])  # worker-2 carries no pool label yet: nowhere on the pool can take it now
    assert out.get("ok") is not True and POOL in out["error"], out
    monkeypatch.setitem(support.MACHINES, X, {**support.MACHINES[X],
                                              "labels": [*support.MACHINES[X]["labels"], POOL]})
    hub.enter()  # re-read the registry
    for tid in (a, b):
        out = _move(tid, [POOL])
        assert out.get("ok") is True, out
        assert place._pin(hub.conn, tid) == [POOL]
    assert kb.get_task(hub.conn, alarm).status == "running"


def test_the_alarms_run_may_not_move_an_unlisted_card_or_back_onto_the_stalled_machine(held):
    hub, _worker_gw, _alarm, (a, _b), unlisted = held
    out = _move(unlisted, [f"machine:{X}"])
    assert out.get("ok") is not True and "lists" in out["error"], out
    out = _move(a, [f"machine:{M}"])
    assert out.get("ok") is not True and f"leave {M}" in out["error"], out
    assert place._pin(hub.conn, unlisted) == place._pin(hub.conn, a) == [f"machine:{M}"]


def test_the_alarms_pool_move_keeps_a_mandatory_pins_label(held, monkeypatch):
    # Review 5878972889 on #1622: the alarm may only ask for exactly [pool:worker]; the move must still carry
    # capability:mandatory-model-pin, or an old-core pool worker leases a pin it would run on another model.
    from hermes_cli import kanban_mandatory_pin as mp
    hub, _worker_gw, _alarm, (a, _b), _unlisted = held
    with kb.write_txn(hub.conn):
        hub.conn.execute("UPDATE tasks SET model_override = 'claude-opus-5-5', provider_override = 'anthropic', "
                         "model_pin_mandatory = 1 WHERE id = ?", (a,))
    pooled = {**support.MACHINES[X], "labels": [*support.MACHINES[X]["labels"], POOL]}
    monkeypatch.setitem(support.MACHINES, X, pooled)
    hub.enter()
    out = _move(a, [POOL])  # worker-2 is on the pool but does not attest the mandatory-pin refusal
    assert out.get("ok") is not True and mp.LABEL in out["error"], out
    assert place._pin(hub.conn, a) == [f"machine:{M}"]
    monkeypatch.setitem(support.MACHINES, X, {**pooled, "labels": [*pooled["labels"], mp.LABEL]})
    hub.enter()
    out = _move(a, [POOL])
    assert out.get("ok") is True, out
    assert place._pin(hub.conn, a) == sorted([POOL, mp.LABEL])


def test_the_alarms_run_leaves_a_card_whose_unpushed_tree_is_on_this_host(held, tmp_path, monkeypatch):
    """agent-e575f77d7d: the re-place is an ordinary queued move, so ``unpushed_state_here`` refuses it; the card
    stays pinned and the alarm's run goes on (a refusal, not an error that loops)."""
    import subprocess
    from pathlib import Path
    hub, _worker_gw, alarm, (a, b), _unlisted = held
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    tree = tmp_path / "home" / "code" / "_wt" / "stalled"
    git = ["git", "-c", "user.email=t@t", "-c", "user.name=t"]
    main = tmp_path / "stalled-main"
    subprocess.run([*git, "init", "-q", str(main)], check=True, capture_output=True)
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "base"], cwd=main, check=True, capture_output=True)
    subprocess.run([*git, "worktree", "add", "-q", "-b", "wt/stalled", str(tree)], cwd=main, check=True,
                   capture_output=True)
    gitdir = (tree / ".git").read_text().split(":", 1)[1].strip()
    (Path(gitdir) / "hermes-owner.json").write_text(json.dumps({"task_id": a}))
    subprocess.run([*git, "commit", "-q", "--allow-empty", "-m", "wip"], cwd=tree, check=True, capture_output=True)
    out = _move(a, [f"machine:{X}"], host_reason=HR)
    # The tool error carries move_queued's message, not its reason_code.
    assert out.get("ok") is not True and "unpushed commits in" in out["error"], out
    assert "another machine cannot resume it" in out["error"], out
    assert place._pin(hub.conn, a) == [f"machine:{M}"]
    assert not hub.conn.execute("SELECT 1 FROM task_events WHERE task_id = ? AND kind = 'moved'", (a,)).fetchone()
    assert _move(b, [f"machine:{X}"], host_reason=HR).get("ok") is True  # a card with no tree here still moves
    assert kb.get_task(hub.conn, alarm).status == "running"


def test_the_hold_alarm_text_names_the_supported_command():
    assert "kanban_move command below" in kps._HOST_GROUPS["hold"][2]
    text = rp.howto(M)
    assert "kanban_move(task_id=" in text and "\"action\": \"move\"" in text and f"never back to {M}" in text


# --- the operator-call route for a card another gateway created ----------------------------------------------------

def test_an_alarm_caller_gets_the_same_scope_on_an_operator_call_move(fleet, monkeypatch):
    from hermes_cli import kanban_supervisor_requests as ksr
    f = fleet
    for c in (f.h, f.rep):
        if "runs_on" not in {r[1] for r in c.execute("PRAGMA table_info(tasks)")}:
            c.execute("ALTER TABLE tasks ADD COLUMN runs_on TEXT")
            c.commit()
    with kb.write_txn(f.h):
        f.h.execute("UPDATE tasks SET runs_on = ? WHERE id = ?", (json.dumps([f"machine:{M}"]), f.target))
    f.board()
    with kb.write_txn(f.w):  # the caller's card is the alarm M's detector filed (a pickup copy on worker-5)
        f.w.execute("UPDATE tasks SET created_by = ?, created_on = ?, idempotency_key = ?, body = ? WHERE id = ?",
                    (kps.DETECTOR, M, f"{kps.KEY_PREFIX}{M}:hold:1", f"- {f.target} (hub-owned card), ready",
                     f.caller))
    monkeypatch.setattr(rp, "_registry_machines", lambda: _machines())

    def ask(runs_on, target=None, host_reason=None):
        op = {"op": "operator", "action": "move", "task_id": target or f.target, "runs_on": runs_on,
              "reason": f"{M} is on hold"}
        if host_reason:
            op["host_reason"] = host_reason
        out = ksr.submit(f.w, env_task=f.caller, env_run=str(f.run_id), dry_run=True, ops=[op])
        return out["ops"][0]

    # t_01a1d95c41620e6c: the 11-13 alarm moves (worker-7 -> worker-1/-4, worker-3 -> worker-8) carried no host state.
    assert ask([f"machine:{X}"]).get("code") == "pickup_stall_scope"
    assert ask([f"machine:{X}"], host_reason="worker-7 10/10, no pool worker eligible now").get(
        "code") == "pickup_stall_scope"
    assert ask([f"machine:{X}"], host_reason=HR)["verdict"] == "supervisor"
    assert ask([f"machine:{M}"], host_reason=HR).get("code") == "pickup_stall_scope"
    other = f.card(f.h, "not listed")
    with kb.write_txn(f.h):
        f.h.execute("UPDATE tasks SET runs_on = ? WHERE id = ?", (json.dumps([f"machine:{M}"]), other))
    f.board()
    assert ask([f"machine:{X}"], target=other, host_reason=HR).get("code") == "pickup_stall_scope"
    # The pool queue: granted while another pool worker is eligible now, refused when none is.
    assert ask([POOL]).get("code") == "pickup_stall_scope"
    monkeypatch.setattr(rp, "_registry_machines", lambda: _pooled())
    assert ask([POOL])["verdict"] == "supervisor"
    monkeypatch.setattr(rp, "_registry_machines", lambda: _pooled(worker_2={"hold": True}))
    assert ask([POOL]).get("code") == "pickup_stall_scope"
    # A caller that is no alarm keeps the plain operator-call rules.
    with kb.write_txn(f.w):
        f.w.execute("UPDATE tasks SET created_by = 'default' WHERE id = ?", (f.caller,))
    assert ask([f"machine:{M}"])["verdict"] == "supervisor"
