"""Hub-admission-denied cards queue for the worker pool, even across a gateway restart. [daveey]

Incident (11-13, hermes-hub): ``kanban_create`` reported t_01a1d2a8ea3dfbdb as placed on the pool after hub admission
denied it, but stored it worker-first with ``runs_on`` NULL. It sat ready 36 min while pool workers claimed 63
other cards, until a manual ``hermes kanban move --pool`` gave it ``[pool:worker, capability:hub-write]``. Cause: every
gateway restart records the sync loop "stopped", and the new loop cleared that only after its first round (20 min
on the hub after a restart). In that window the tool's machine check read the queue as unreachable. Cards created
00:56-02:21 (each just after a restart) were all worker-first; the ones before and after queued.

Entry paths: ``kanban_create`` during a restarted loop's first round (``run_forever`` against the fake Board), and
the dispatcher's self-check (``kanban_worker_placement.reevaluate``) for a card that is already stranded.
"""
from __future__ import annotations

import json
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_move as km
from hermes_cli import kanban_placement as placement
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import kanban_sync_runstate as runstate
from hermes_cli import kanban_worker_node as wn
from hermes_cli import kanban_worker_placement as wp
from tests.hermes_cli import hub_judge_support
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli._placement_helpers import seen_now
from tests.hermes_cli.kanban_sync_fake_board import FakeService
from tools import kanban_tools as kt

hub_judge = hub_judge_support.hub_judge

ME, BOARD = "hub", "default"
WORKER = {"name": "worker-1", "state": "active", "labels": ["pool:worker", "capability:hub-write"], "pickup": "all",
          "hold": False, "pinned_only": False, "max_card_runs": 12}
CONFIG = ("kanban:\n  sync:\n    enabled: true\n  placement:\n    worker_pools:\n      hub: [worker-1]\n"
          "    worker_queue:\n      hub: pool:worker\n")
# Hub-state wording: the deny route adds hub:write, so the queue needs capability:hub-write.
TITLE = "Read ~/.hermes/logs/agent.log on the hub and fix the self-heal lane"
QUEUE = {"pool:worker", "capability:hub-write"}
CFG = {"worker_pools": {ME: ["worker-1"]}, "worker_queue": {ME: "pool:worker"}}


@pytest.fixture
def hub(tmp_path, monkeypatch, hub_judge):
    for var in ("HERMES_SESSION_ID", "HERMES_KANBAN_TASK", "HERMES_KANBAN_WORKER_TASK", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(var, raising=False)
    home = support.kanban_home(tmp_path, monkeypatch)
    from tests.tools.test_kanban_create_overlap import register_chat
    monkeypatch.setenv("HERMES_SESSION_ID", register_chat(home, "20261123_120000_deny_owner"))
    (home / "config.yaml").write_text(CONFIG)
    kb.init_db()
    conn = kbc.connect(home / "kanban.db")
    ksc.ensure_identity(conn, BOARD, ME)
    sync.set_mode(conn, "primary")
    monkeypatch.setattr(placement.reg, "self_gateway", lambda: ME)
    monkeypatch.setattr(kt, "_persisted_identity", lambda: "default")
    wn.set_registry_fetch(lambda: {"me": ME, "machines": seen_now([WORKER])})
    hub_judge.admit_next = False
    try:
        yield home, conn
    finally:
        conn.close()
        wn.set_registry_fetch(None)
        sync.set_lease_client(None)


def _loop(home, svc, monkeypatch, during_round=None):
    """One ``run_forever`` round against the fake Board; ``during_round`` runs before that round syncs."""
    from hermes_cli import machine_metrics

    board_hub = svc.hub(ME)
    monkeypatch.setattr(ksc, "load_identity", lambda: board_hub.ident)
    monkeypatch.setattr(ksc, "Hub", lambda ident: board_hub)
    monkeypatch.setattr(ksc.sync, "set_lease_client", lambda value: None)
    monkeypatch.setattr(ksc, "board_paths", lambda: [(BOARD, home / "kanban.db")])
    monkeypatch.setattr(ksc, "reconcile_links", lambda *args, **kwargs: None)
    monkeypatch.setattr(machine_metrics, "Reporter", lambda *args: type(
        "R", (), {"report_due": lambda self: None, "pending_wake_in": lambda self: None})())
    monkeypatch.setattr(board_hub, "report_metrics", lambda *args: None, raising=False)

    class Feed:
        def __init__(self, *args, **kwargs):
            pass

        def run(self):
            return None

    monkeypatch.setattr(ksc, "FeedWatcher", Feed)
    real = ksc.sync_all_once
    rounds = []

    def round_(hub_, boards=None):
        if during_round is not None and not rounds:
            during_round()
        rounds.append(True)
        return real(hub_, boards=boards)

    monkeypatch.setattr(ksc, "sync_all_once", round_)
    monkeypatch.setattr(ksc, "wait_for_work", lambda *args: None)
    ksc.run_forever(lambda: not rounds, interval=1)


def test_denied_create_during_a_restarted_loops_first_round_joins_the_worker_queue(hub, monkeypatch):
    home, conn = hub
    svc = FakeService()
    _loop(home, svc, monkeypatch)  # the previous gateway's loop: it stops, recording "stopped"
    record = runstate.down()
    assert record is not None and record["reason"].startswith("stopped (gateway stopping")
    made = []

    def create():
        made.append(json.loads(kt._handle_create({"title": TITLE, "requires": ["terminal", "file", "git"]})))

    _loop(home, svc, monkeypatch, during_round=create)  # the restarted gateway: a card created mid first round
    [result] = made
    assert result.get("ok"), result
    tid = result["task_id"]
    assert result["placement"]["where"] == "pool" and set(result["placement"]["runs_on"]) == QUEUE, result
    row = conn.execute("SELECT placement_policy, runs_on, requires FROM tasks WHERE id=?", (tid,)).fetchone()
    assert row["placement_policy"] is None
    assert set(json.loads(row["runs_on"])) == QUEUE
    assert "hub:write" in json.loads(row["requires"])
    # The Board queue receives it on the loop's next push: a pool worker carrying both labels can lease it.
    ksc.sync_board(BOARD, home / "kanban.db", svc.hub(ME), ME)
    board_row = svc.board(BOARD)["rows"]["tasks"][tid]
    assert set(json.loads(board_row["runs_on"])) == QUEUE
    assert not board_row.get("placement_policy")


def test_a_crash_or_start_refusal_record_still_waits_for_a_completed_round(hub):
    for reason in ("kanban sync loop crashed: RuntimeError: x", "not starting: board default: schema ahead"):
        runstate.record_down(reason)
        runstate.started()
        record = runstate.down()
        assert record is not None and record["reason"] == reason
    runstate.record_down(runstate.STOPPED)
    runstate.started()
    assert runstate.down() is None


# --- dispatcher self-check -------------------------------------------------------------------------------------

class RecordingHub:
    def __init__(self, *, refuse_repin=None, refuse_status=403, refuse_detail=None):
        self.reserves, self.repins, self.refuse_repin = [], [], refuse_repin
        self.refuse_status, self.refuse_detail = refuse_status, refuse_detail

    def reserve(self, board, **body):
        self.reserves.append(body)
        return {"state": "queued", "reason": "waiting_for_worker_capacity"}

    def repin(self, board, **body):
        self.repins.append(body)
        if self.refuse_repin:
            raise ksc.HubError(self.refuse_status, self.refuse_detail or json.dumps({"error": self.refuse_repin}))
        return {"version": 9, "runs_on": body["runs_on"], "placement_policy": None}


@pytest.fixture
def no_pull(monkeypatch):
    monkeypatch.setattr(ksc, "pull", lambda *a, **kw: None)
    wp._refused.clear()
    wp._queued.clear()
    wp._selfcheck_refused.clear()
    wp._origin_refused.clear()
    yield
    wp._refused.clear()
    wp._queued.clear()
    wp._selfcheck_refused.clear()
    wp._origin_refused.clear()


def _stranded(conn, *, age, requires=("terminal", "file", "git", "hub:write")):
    """An acknowledged worker-first card with runs_on NULL, ready for ``age`` seconds."""
    tid = kb.create_task(conn, title="Self-heal lane fix", assignee="default", placement_policy=wp.POLICY,
                         requires=list(requires))
    created = int(time.time()) - age
    conn.execute("UPDATE kanban_sync_state SET applying=1")
    conn.execute("UPDATE tasks SET sync_version=4, created_on=?, created_at=? WHERE id=?", (ME, created, tid))
    conn.execute("UPDATE task_events SET created_at=? WHERE task_id=?", (created, tid))
    conn.execute("UPDATE kanban_sync_state SET applying=0")
    conn.execute("DELETE FROM kanban_sync_outbox")
    conn.commit()
    return tid


def test_self_check_repins_a_stranded_worker_first_card_to_the_pool(hub, no_pull, caplog):
    _home, conn = hub
    tid = _stranded(conn, age=36 * 60)
    board = RecordingHub()
    with caplog.at_level("WARNING", logger="hermes_cli.kanban_worker_first_selfcheck"):
        wp.reevaluate(conn, board, BOARD, ME, cfg=CFG)
    assert board.reserves == []
    assert [(r["task_id"], r["expected_revision"]) for r in board.repins] == [(tid, 4)]
    assert set(board.repins[0]["runs_on"]) == QUEUE and board.repins[0]["runs_on"][0] == "pool:worker"
    [moved] = [e.payload for e in kb.list_events(conn, tid) if e.kind == km.MOVED_EVENT]
    assert moved["repinned"] is True and "self-check" in moved["reason"] and "36 min" in moved["reason"]
    assert any(tid in r.getMessage() and "self-check" in r.getMessage() for r in caplog.records)
    # The applied repin is never replayed as a run's move request.
    assert wp.requested_move(conn, tid) is None


@pytest.mark.parametrize("case", ["young", "no_queue", "no_machine", "registry_down", "pinned"])
def test_self_check_leaves_other_worker_first_cards_to_the_reserve(hub, no_pull, case):
    _home, conn = hub
    tid = _stranded(conn, age=60 if case == "young" else 3600)
    cfg = {"worker_pools": CFG["worker_pools"]} if case == "no_queue" else CFG
    if case == "no_machine":
        wn.set_registry_fetch(lambda: {"me": ME, "machines": seen_now([{**WORKER, "labels": ["pool:worker"]}])})
    if case == "registry_down":
        wn.set_registry_fetch(lambda: (_ for _ in ()).throw(OSError("board unreachable")))
    if case == "pinned":  # the Board already assigned it: runs_on machine:worker-N is not the bug state
        conn.execute("UPDATE kanban_sync_state SET applying=1")
        conn.execute("UPDATE tasks SET runs_on=? WHERE id=?", (json.dumps(["machine:worker-1"]), tid))
        conn.execute("UPDATE kanban_sync_state SET applying=0")
        conn.commit()
    board = RecordingHub()
    wp.reevaluate(conn, board, BOARD, ME, cfg=cfg)
    assert board.repins == []
    assert [r["task_id"] for r in board.reserves] == [tid]


def _bump(conn, tid, version):
    conn.execute("UPDATE kanban_sync_state SET applying=1")
    conn.execute("UPDATE tasks SET sync_version=? WHERE id=?", (version, tid))
    conn.execute("UPDATE kanban_sync_state SET applying=0")
    conn.commit()


def test_a_refused_self_check_repin_falls_back_to_the_reserve_until_the_revision_moves(hub, no_pull):
    """A Board refusal other than not_claimable (e.g. forbidden) must not hold the card on a repin re-asked every
    tick: at the refused revision it goes back to the reserve, and a new revision asks the repin again."""
    _home, conn = hub
    tid = _stranded(conn, age=36 * 60)
    board = RecordingHub(refuse_repin="forbidden")
    wp.reevaluate(conn, board, BOARD, ME, cfg=CFG)
    assert [r["expected_revision"] for r in board.repins] == [4] and board.reserves == []
    assert wp.requested_move(conn, tid) is None  # nothing was recorded as applied
    wp.reevaluate(conn, board, BOARD, ME, cfg=CFG)
    assert len(board.repins) == 1, "the refused self-check repin was re-asked at the same revision"
    assert [(r["task_id"], r["expected_revision"]) for r in board.reserves] == [(tid, 4)]
    _bump(conn, tid, 5)
    board.refuse_repin = None
    wp.reevaluate(conn, board, BOARD, ME, cfg=CFG)
    assert [r["expected_revision"] for r in board.repins] == [4, 5]
    assert len(board.reserves) == 1
    assert (BOARD, tid) not in wp._selfcheck_refused


@pytest.mark.parametrize("status,body", [(503, "upstream unavailable"), (502, '{"error": "bad_gateway"}'),
                                         (429, '{"error": "rate_limited"}')])
def test_a_transient_board_error_on_a_self_check_repin_is_retried_next_tick(hub, no_pull, status, body):
    """A 5xx/429 is an outage, not the Board's answer: the next tick asks the repin again instead of reserving."""
    _home, conn = hub
    tid = _stranded(conn, age=36 * 60)
    board = RecordingHub(refuse_repin="transient", refuse_status=status, refuse_detail=body)
    for _ in range(2):
        wp.reevaluate(conn, board, BOARD, ME, cfg=CFG)
    assert [(r["task_id"], r["expected_revision"]) for r in board.repins] == [(tid, 4)] * 2
    assert board.reserves == [] and not wp._selfcheck_refused


@pytest.mark.parametrize("status,body", [(404, "<html><body>404 Not Found</body></html>"),
                                         (405, "Method Not Allowed"), (501, "<html>Not Implemented</html>"),
                                         (400, "Bad Request")])
def test_an_uncoded_persistent_refusal_of_a_self_check_repin_reserves(hub, no_pull, status, body):
    """A Board or proxy without the repin route answers an uncoded 404/405/501 every tick: that is a persistent
    refusal, so the card goes back to the reserve at that revision instead of re-asking the repin forever."""
    _home, conn = hub
    tid = _stranded(conn, age=36 * 60)
    board = RecordingHub(refuse_repin="missing", refuse_status=status, refuse_detail=body)
    for _ in range(2):
        wp.reevaluate(conn, board, BOARD, ME, cfg=CFG)
    assert len(board.repins) == 1, "the refused self-check repin was re-asked at the same revision"
    assert [(r["task_id"], r["expected_revision"]) for r in board.reserves] == [(tid, 4)]
    assert wp._selfcheck_refused == {(BOARD, tid): 4}


@pytest.mark.parametrize("status", [408, 425])
def test_an_uncoded_retryable_4xx_on_a_self_check_repin_is_retried(hub, no_pull, status):
    _home, conn = hub
    tid = _stranded(conn, age=36 * 60)
    board = RecordingHub(refuse_repin="transient", refuse_status=status, refuse_detail="try again")
    for _ in range(2):
        wp.reevaluate(conn, board, BOARD, ME, cfg=CFG)
    assert [(r["task_id"], r["expected_revision"]) for r in board.repins] == [(tid, 4)] * 2
    assert board.reserves == [] and not wp._selfcheck_refused


def test_a_forbidden_self_check_repin_still_reserves_at_the_refused_revision(hub, no_pull):
    _home, conn = hub
    tid = _stranded(conn, age=36 * 60)
    board = RecordingHub(refuse_repin="forbidden", refuse_status=403)
    for _ in range(2):
        wp.reevaluate(conn, board, BOARD, ME, cfg=CFG)
    assert len(board.repins) == 1 and [r["task_id"] for r in board.reserves] == [tid]


def test_a_refused_explicit_move_repin_is_not_downgraded_to_a_reserve(hub, no_pull):
    """Only the self-check falls back: a run's kanban_move must never be handed back to the pool by a reserve."""
    _home, conn = hub
    tid = _stranded(conn, age=36 * 60)
    kb._append_event(conn, tid, km.MOVED_EVENT, {"from_machine": "worker-1", "runs_on": ["machine:hub"],
                                                 "reason": "r", "move_no": 1, "forced": False, "matches": [ME]})
    conn.commit()
    board = RecordingHub(refuse_repin="forbidden")
    for _ in range(2):
        wp.reevaluate(conn, board, BOARD, ME, cfg=CFG)
    assert [r["runs_on"] for r in board.repins] == [["machine:hub"]] * 2
    assert board.reserves == [] and not wp._selfcheck_refused
