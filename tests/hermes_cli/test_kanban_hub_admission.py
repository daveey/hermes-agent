"""Final hub CPU/cap gate: an independent full CPU sample taken UNDER admission.lock, ready and review paths."""
import json
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing

import pytest

from hermes_cli import admission_fence as af
from hermes_cli import kanban_admission as ka
from hermes_cli import kanban_admission_score as score
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_queue_reason as queue
from hermes_cli import machine_cpu_metrics as cm
from hermes_cli import machine_metrics as mm
from tests.hermes_cli import hub_admission_support as support
from tests.hermes_cli.hub_admission_support import advisory, claim, configure, fake_sample, locked, refused, runs, task
from tests.tools import test_kanban_tools as tool_helpers

pytestmark = pytest.mark.platforms("posix")


@pytest.fixture
def board(tmp_path, monkeypatch):
    yield from support.open_board(tmp_path, monkeypatch)


tool_worker_env = tool_helpers.worker_env


def test_tool_create_preserves_admission_metadata(tool_worker_env):
    from tools import kanban_tools as kt
    result = json.loads(kt._handle_create({
        "title": "Restore runner headroom", "assignee": "peer",
        "admission_class": "capacity", "admission_frees": "frees: runner slots",
    }))
    assert result.get("ok"), result
    with closing(kbc.connect()) as conn:
        stored = kb.get_task(conn, result["task_id"])
        assert stored is not None
        assert stored.admission_class == "capacity"
        assert stored.admission_frees == "frees: runner slots"
    assert result["admission_class"] == "capacity"
    assert result["admission_frees"] == "frees: runner slots"



def test_tool_invalid_capacity_refuses_before_placement(tool_worker_env, monkeypatch):
    from tools import kanban_tools as kt
    monkeypatch.setattr(kt, "_place_remote", lambda *a, **kw: pytest.fail("invalid metadata reached placement"))
    result = json.loads(kt._handle_create({
        "title": "Missing claim", "assignee": "peer", "admission_class": "capacity",
    }))
    assert result["rejection"] == "invalid_args"
    assert result["error"] == "capacity requires a one-line frees: claim"


def test_admission_metadata_survives_remote_option_encoding():
    from hermes_cli import kanban_placement as placement
    args = {"admission_class": "capacity", "admission_frees": "frees: worker slots"}
    assert placement.carried_options(args) == args


@pytest.mark.parametrize("field,value", [("admission_class", "capacity"),
                                         ("admission_frees", "frees: worker slots")])
def test_legacy_courier_cannot_silently_drop_admission_metadata(field, value):
    from hermes_cli import kanban_placement as placement
    assert placement._uncarried({field: value}, placement._Cfg({}), False) == [field]


def test_explicit_normal_class_is_the_default_and_needs_no_courier_probe(monkeypatch):
    from hermes_cli import kanban_placement as placement
    monkeypatch.setattr(placement, "hub_carries_options", lambda *a: pytest.fail("probed for a default class"))
    assert placement.carried_options({"admission_class": "normal"}) == {}
    assert placement._uncarried({"admission_class": "normal"}, placement._Cfg({}), None) == []


def test_claim_persists_immutable_regular_receipt(board):
    conn, home = board
    configure(home, limit=None)
    task_id = task(conn)
    kb.edit_task(conn, task_id, admission_class="capacity", admission_frees="frees: worker slots")
    assert kb.claim_task(conn, task_id)
    run = conn.execute("SELECT admission_pool, admission_class FROM task_runs WHERE task_id=?", (task_id,)).fetchone()
    assert tuple(run) == ("regular", "capacity")
    kb.edit_task(conn, task_id, admission_class="normal")
    assert tuple(conn.execute("SELECT admission_pool, admission_class FROM task_runs WHERE task_id=?",
                              (task_id,)).fetchone()) == ("regular", "capacity")


@pytest.mark.parametrize("kind,expected", [("capacity", 2), ("urgent", 3)])
def test_concurrent_cross_board_claims_obey_origin_and_pool_caps(board, monkeypatch, kind, expected):
    conn, home = board
    configure(home, limit=None)
    for index in range(7):
        assert kb.claim_task(conn, task(conn, title=f"regular {index}"))
    configure(home, cap=7)
    with (home / "config.yaml").open("a") as config:
        config.write("    priority_slots: 3\n")
    advisory(home)
    locked(monkeypatch, fake_sample(10))
    candidates = []
    for index in range(4):
        folder = kb.boards_root() / f"pool-{index}"
        folder.mkdir(parents=True)
        (folder / "board.json").write_text("{}")
        path = folder / "kanban.db"
        with closing(kbc.connect(path)) as other:
            tid = task(other)
            kb.edit_task(other, tid, admission_class=kind, admission_frees="frees: worker slots")
            candidates.append((path, tid))

    def attempt(candidate):
        path, tid = candidate
        with closing(kbc.connect(path)) as other:
            return kb.claim_task(other, tid)

    for _ in range(4):
        with ThreadPoolExecutor(max_workers=4) as executor:
            list(executor.map(attempt, candidates))
    # Concurrent rounds may admit fewer than the free slots (losers of admission.lock defer as busy and retry next
    # tick); a final uncontended pass admits every remaining admissible card, so only the caps limit the result.
    for candidate in candidates:
        attempt(candidate)
    detail = {"pools": {"regular": 0, "priority": 0}, "capacity_origins": {}}
    assert mm.active_card_runs(detail=detail) == 7 + expected
    assert detail["pools"] == {"regular": 7, "priority": expected}
    if kind == "capacity":
        assert list(detail["capacity_origins"].values()) == [2]


def test_queue_note_reports_pools_and_marked_lane_order(board, monkeypatch):
    from hermes_cli import kanban_queue_position as qp
    conn, home = board
    configure(home, limit=None)
    assert kb.claim_task(conn, task(conn, title="regular running"))
    configure(home, cap=7)
    with (home / "config.yaml").open("a") as config:
        config.write("    priority_slots: 3\n")
    advisory(home)
    locked(monkeypatch, fake_sample(10))
    active = task(conn, title="marked running")
    kb.edit_task(conn, active, admission_class="urgent")
    assert kb.claim_task(conn, active)
    ordinary = task(conn, title="ordinary waiting")
    kb.edit_task(conn, ordinary, priority=1000)
    marked = task(conn, title="marked waiting")
    kb.edit_task(conn, marked, admission_class="capacity", admission_frees="frees: runner slots")
    monkeypatch.setattr(qp, "_running_and_limit", lambda *a: (2, 7))
    place = qp.read(conn, kb.get_task(conn, marked))
    assert place["ahead"] == 0
    assert place["admission_pools"] == {"regular": 1, "priority": 1, "regular_cap": 7, "priority_cap": 3}
    note = qp.describe(place)
    assert "regular 1/7, priority 1/3" in note
    assert "class capacity" in note
    assert "bump" not in note
    assert qp.read(conn, kb.get_task(conn, ordinary))["ahead"] == 1


@pytest.mark.parametrize("enabled,slots", [(False, 3), (True, 0)])
def test_queue_pool_details_do_not_change_legacy_workers(board, monkeypatch, enabled, slots):
    from hermes_cli import kanban_queue_position as qp
    conn, home = board
    configure(home, cap=7, limit=50 if enabled else None)
    if enabled:
        with (home / "config.yaml").open("a") as config:
            config.write(f"    priority_slots: {slots}\n")
    else:
        with (home / "config.yaml").open("a") as config:
            config.write(f"  admission:\n    priority_slots: {slots}\n")
    ordinary = task(conn)
    kb.edit_task(conn, ordinary, priority=1000)
    marked = task(conn)
    kb.edit_task(conn, marked, admission_class="urgent")
    monkeypatch.setattr(qp, "_running_and_limit", lambda *a: (0, 7))
    place = qp.read(conn, kb.get_task(conn, marked))
    assert place["ahead"] == 1
    assert "admission_pools" not in place


def test_queue_unknown_pool_census_does_not_look_empty(board, monkeypatch):
    from hermes_cli import kanban_queue_position as qp
    conn, home = board
    configure(home, cap=7)
    with (home / "config.yaml").open("a") as config:
        config.write("    priority_slots: 3\n")
    monkeypatch.setattr(qp, "_running_and_limit", lambda *a: (None, 7))
    monkeypatch.setattr(mm, "active_card_runs", lambda **kw: None)
    place = qp.read(conn, kb.get_task(conn, task(conn)))
    assert place["admission_pools"]["regular"] is None
    assert place["admission_pools"]["priority"] is None
    assert "regular ?/7, priority ?/3" in qp.describe(place)


def test_unsupported_fence_resets_prior_priority_receipt(board, monkeypatch):
    conn, home = board
    configure(home, cap=7)
    with (home / "config.yaml").open("a") as config:
        config.write("    priority_slots: 3\n")
    advisory(home)
    locked(monkeypatch, fake_sample(10))
    marked = task(conn)
    kb.edit_task(conn, marked, admission_class="urgent")
    assert kb.claim_task(conn, marked)
    assert conn.execute("SELECT admission_pool FROM task_runs WHERE task_id=?", (marked,)).fetchone()[0] == "priority"
    # A later unsupported-platform claim must not inherit thread-local evidence.
    configure(home, limit=None)
    monkeypatch.setattr(af, "SUPPORTED", False)
    ordinary = task(conn)
    assert kb.claim_task(conn, ordinary)
    assert conn.execute("SELECT admission_pool FROM task_runs WHERE task_id=?", (ordinary,)).fetchone()[0] == "regular"


def test_claim_captures_created_origin_not_mutable_shepherd(board):
    conn, home = board
    configure(home, limit=None)
    tid = task(conn)
    conn.executescript(sync.SYNC_SCHEMA_SQL)
    with kb.write_txn(conn):
        for kind, session in [("created", "original"), ("shepherd", "operator")]:
            conn.execute("INSERT INTO task_chat_links "
                         "(uid,task_id,chat_gateway,chat_profile,chat_session,kind,released,created_at) "
                         "VALUES (?,?, 'source', 'default', ?, ?, 0, 1)", (kind, tid, session, kind))
    assert kb.claim_task(conn, tid)
    origin = conn.execute("SELECT admission_origin FROM task_runs WHERE task_id=?", (tid,)).fetchone()[0]
    assert json.loads(origin) == ["chat", "source", "default", "original"]
    with kb.write_txn(conn):
        conn.execute("UPDATE task_chat_links SET released=1 WHERE task_id=?", (tid,))
    assert conn.execute("SELECT admission_origin FROM task_runs WHERE task_id=?", (tid,)).fetchone()[0] == origin


def test_capacity_origin_cap_survives_task_class_edits(board, monkeypatch):
    conn, home = board
    configure(home, cap=7)
    with (home / "config.yaml").open("a") as config:
        config.write("    priority_slots: 3\n")
    advisory(home)
    locked(monkeypatch, fake_sample(10))
    for index in range(2):
        tid = task(conn, title=f"capacity {index}")
        kb.edit_task(conn, tid, admission_class="capacity", admission_frees="frees: worker slots")
        assert kb.claim_task(conn, tid)
        kb.edit_task(conn, tid, admission_class="normal")
    candidate = task(conn)
    kb.edit_task(conn, candidate, admission_class="capacity", admission_frees="frees: runner slots")
    assert kb.claim_task(conn, candidate) is None
    # Incident recovery is not subject to the capacity-origin limit.
    kb.edit_task(conn, candidate, admission_class="urgent")
    assert kb.claim_task(conn, candidate)


@pytest.mark.parametrize("text", ["[]\n", "kanban: [bad]\n", "kanban:\n  admission: [bad]\n"])
def test_malformed_pool_config_refuses_cleanly(board, text):
    from hermes_cli import kanban_admission_pools as pools
    _conn, home = board
    (home / "config.yaml").write_text(text)
    with pytest.raises(ValueError, match="^admission configuration must be mappings$"):
        pools.root_settings()


def test_dispatch_invalid_priority_setting_defers_without_crashing(board):
    from hermes_cli import kanban_db_dispatch as dispatch
    conn, home = board
    configure(home, cap=7)
    with (home / "config.yaml").open("a") as config:
        config.write("    priority_slots: wrong\n")
    assert dispatch._tick_spawn_budget(conn, dispatch.DispatchResult(), max_spawn=None,
                                       max_in_progress=7, board=None) == (False, None)


def test_pool_receipt_is_local_only():
    fields = {"admission_pool", "admission_class", "admission_origin"}
    assert fields <= sync.LOCAL_COLUMNS["task_runs"]
    assert not fields & sync.SYNCED_COLUMNS["task_runs"]


@pytest.mark.parametrize("admission_class,regular,priority,expected", [
    ("capacity", 7, 0, "priority"),
    ("urgent", 7, 2, "priority"),
    ("capacity", 6, 3, "regular"),
    ("capacity", 7, 3, None),
    ("normal", 7, 0, None),
    ("normal", 6, 0, "regular"),
])
def test_pool_selection_preserves_separate_caps(admission_class, regular, priority, expected):
    from hermes_cli import kanban_admission_pools as pools
    assert pools.select_pool(admission_class, regular=regular, priority=priority,
                             regular_cap=7, priority_cap=3) == expected


def test_whole_host_pool_count_uses_receipts_not_current_class(board):
    conn, _home = board
    marked = task(conn, title="marked")
    regular = task(conn, title="regular")
    assert kb.claim_task(conn, marked)
    assert kb.claim_task(conn, regular)
    with kb.write_txn(conn):
        conn.execute("UPDATE task_runs SET admission_pool='priority', admission_class='capacity' WHERE task_id=?",
                     (marked,))
    kb.edit_task(conn, marked, admission_class="normal")
    detail = {"pools": {"regular": 0, "priority": 0}}
    assert mm.active_card_runs(detail=detail) == 2
    assert detail["pools"] == {"regular": 1, "priority": 1}


@pytest.mark.parametrize("review", [False, True])
def test_urgent_claim_uses_extra_pool_with_regular_pool_full(board, monkeypatch, review):
    conn, home = board
    configure(home, cap=7)
    with (home / "config.yaml").open("a") as config:
        config.write("    priority_slots: 3\n    priority_cpu_busy_below_pct: 85\n")
    for index in range(7):
        tid = task(conn, title=f"legacy regular {index}")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
    candidate = task(conn, review=review, title="Incident recovery")
    kb.edit_task(conn, candidate, admission_class="urgent")
    advisory(home, pct=70)
    locked(monkeypatch, fake_sample(70))
    assert claim(conn, candidate, review) is not None
    receipt = conn.execute("SELECT admission_pool FROM task_runs WHERE task_id=?", (candidate,)).fetchone()
    assert receipt[0] == "priority"
    event = next(event for event in kb.list_events(conn, candidate) if event.kind == "claimed")
    assert event.payload["hub_admission"]["pool"] == "priority"
    assert event.payload["hub_admission"]["pools"] == {"regular": 7, "priority": 0}
    assert event.payload["hub_cpu"]["limit_pct"] == 85


@pytest.mark.parametrize("bad", ["", "unrecognized"])
def test_unknown_pool_receipt_invalidates_census(board, bad):
    conn, _home = board
    task_id = task(conn)
    assert kb.claim_task(conn, task_id)
    with kb.write_txn(conn):
        conn.execute("UPDATE task_runs SET admission_pool=? WHERE task_id=?", (bad, task_id))
    assert mm.active_card_runs(detail={"pools": {"regular": 0, "priority": 0}}) is None


@pytest.mark.parametrize("pressure,expected", [("normal", (True, 3)), ("elevated", (True, 1)),
                                               ("critical", (False, None))])
def test_dispatch_budget_reaches_extra_pool_at_regular_cap(board, monkeypatch, pressure, expected):
    from hermes_cli import kanban_db_dispatch as dispatch
    conn, home = board
    configure(home, cap=7)
    with (home / "config.yaml").open("a") as config:
        config.write("    priority_slots: 3\n")
    for index in range(7):
        tid = task(conn, title=f"regular {index}")
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
    monkeypatch.setattr(dispatch, "_memory_pressure_level", lambda: pressure)
    result = dispatch.DispatchResult()
    assert dispatch._tick_spawn_budget(conn, result, max_spawn=None, max_in_progress=7,
                                       board=None) == expected
    if pressure in ("elevated", "critical"):
        assert result.memory_pressure == pressure


@pytest.mark.parametrize("review", [False, True])
def test_dispatch_marked_candidate_precedes_normal_in_same_lane(
    board, monkeypatch, all_assignees_spawnable, review,
):
    from hermes_cli import kanban_db_dispatch as dispatch
    conn, home = board
    configure(home, cap=7)
    with (home / "config.yaml").open("a") as config:
        config.write("    priority_slots: 3\n")
    advisory(home)
    locked(monkeypatch, fake_sample(10))
    monkeypatch.setattr(dispatch, "review_dispatch_enabled", lambda: True)
    monkeypatch.setattr(dispatch, "_memory_pressure_level", lambda: "normal")
    normal = task(conn, review=review, title="higher numeric priority")
    kb.edit_task(conn, normal, priority=1000)
    marked = task(conn, review=review, title="restore capacity")
    kb.edit_task(conn, marked, admission_class="capacity", admission_frees="frees: worker slots")
    spawned = []

    def spawn(candidate, workspace, board=None):
        spawned.append(candidate.id)

    result = dispatch.dispatch_once(conn, spawn_fn=spawn, max_spawn=1, max_in_progress=7)
    assert spawned == [marked], result
    assert kb.get_task(conn, normal).status == ("review" if review else "ready")
    receipt = conn.execute("SELECT admission_pool FROM task_runs WHERE task_id=?", (marked,)).fetchone()
    assert receipt[0] == "priority"


@pytest.mark.parametrize("enabled", [False, True])
def test_replica_waiter_ranking_cannot_undo_marked_lane_order(board, monkeypatch, all_assignees_spawnable, enabled):
    """A remote waiter donating priority to a normal row reorders within a class, never across marked-first."""
    from hermes_cli import kanban_db_dispatch as dispatch
    from hermes_cli import kanban_dispatch_priority as donation
    conn, home = board
    configure(home, limit=50 if enabled else None, cap=7)
    if enabled:
        with (home / "config.yaml").open("a") as config:
            config.write("    priority_slots: 3\n")
        advisory(home)
        locked(monkeypatch, fake_sample(10))
    monkeypatch.setattr(dispatch, "_memory_pressure_level", lambda: "normal")
    marked = task(conn, title="restore capacity")
    kb.edit_task(conn, marked, admission_class="urgent")
    donated = task(conn, title="remote waiter donates")
    monkeypatch.setattr(donation, "_order_with_remote_waiters",
                        lambda c, rows: sorted(rows, key=lambda r: r["id"] != donated))
    spawned = []

    def spawn(candidate, workspace, board=None):
        spawned.append(candidate.id)

    dispatch.dispatch_once(conn, spawn_fn=spawn, max_spawn=1, max_in_progress=7)
    assert spawned == ([marked] if enabled else [donated])


@pytest.mark.parametrize("enabled", [False, True])
def test_dispatch_pool_order_keeps_worker_priority(board, monkeypatch, all_assignees_spawnable, enabled):
    from hermes_cli import kanban_db_dispatch as dispatch
    conn, home = board
    configure(home, limit=None, cap=7)
    if enabled:
        with (home / "config.yaml").open("a") as config:
            config.write("  admission:\n    priority_slots: 3\n")
    monkeypatch.setattr(dispatch, "_memory_pressure_level", lambda: "normal")
    high = task(conn, title="high normal")
    kb.edit_task(conn, high, priority=1000)
    marked = task(conn, title="marked low")
    kb.edit_task(conn, marked, admission_class="urgent")
    spawned = []

    def spawn(candidate, workspace, board=None):
        spawned.append(candidate.id)

    dispatch.dispatch_once(conn, spawn_fn=spawn, max_spawn=1, max_in_progress=7)
    assert spawned == [high]


@pytest.mark.parametrize("review", [False, True])
@pytest.mark.parametrize("pressure,expected", [("normal", 2), ("elevated", 1), ("critical", 0)])
def test_dispatch_full_regular_pool_claims_marked_with_shared_memory_budget(
    board, monkeypatch, all_assignees_spawnable, review, pressure, expected,
):
    from hermes_cli import kanban_db_dispatch as dispatch
    conn, home = board
    configure(home, limit=None)
    for index in range(7):
        tid = task(conn, title=f"active {index}")
        assert kb.claim_task(conn, tid)
    configure(home, cap=7)
    with (home / "config.yaml").open("a") as config:
        config.write("    priority_slots: 3\n")
    normal = task(conn, review=not review, title="normal opposite lane")
    marked = [task(conn, review=review, title=f"incident {index}") for index in range(3)]
    for tid in marked:
        kb.edit_task(conn, tid, admission_class="urgent")
    advisory(home, pct=70)
    locked(monkeypatch, fake_sample(70))
    monkeypatch.setattr(dispatch, "review_dispatch_enabled", lambda: True)
    monkeypatch.setattr(dispatch, "_memory_pressure_level", lambda: pressure)
    spawned = []

    def spawn(candidate, workspace, board=None):
        spawned.append(candidate.id)

    dispatch.dispatch_once(conn, spawn_fn=spawn, max_spawn=9, max_in_progress=7, reconcile_orphans=False)
    assert len(spawned) == expected
    assert set(spawned) <= set(marked)
    assert normal not in spawned
    for tid in spawned:
        assert conn.execute("SELECT admission_pool FROM task_runs WHERE task_id=?", (tid,)).fetchone()[0] == "priority"
    detail = {"pools": {"regular": 0, "priority": 0}}
    assert mm.active_card_runs(detail=detail) == 7 + expected
    assert detail["pools"] == {"regular": 7, "priority": expected}


def _saturate(conn, home, monkeypatch, regular_active):
    """``regular_active`` real regular claims, then opt the hub in (cap 7 + 3 priority) with an idle CPU."""
    from hermes_cli import kanban_db_dispatch as dispatch
    configure(home, limit=None)
    for index in range(regular_active):
        assert kb.claim_task(conn, task(conn, title=f"active {index}"))
    configure(home, cap=7)
    with (home / "config.yaml").open("a") as config:
        config.write("    priority_slots: 3\n")
    advisory(home)
    locked(monkeypatch, fake_sample(10))
    monkeypatch.setattr(dispatch, "review_dispatch_enabled", lambda: True)
    monkeypatch.setattr(dispatch, "_memory_pressure_level", lambda: "normal")
    attempts, spawned = [], []
    original = dispatch._dispatch_lane_task

    def attempt(conn, row, *args, **kwargs):
        attempts.append(row["id"])
        return original(conn, row, *args, **kwargs)

    monkeypatch.setattr(dispatch, "_dispatch_lane_task", attempt)
    return attempts, spawned, lambda candidate, workspace, board=None: spawned.append(candidate.id)


@pytest.mark.parametrize("review", [False, True])
def test_full_regular_pool_never_attempts_normal_claims(board, monkeypatch, all_assignees_spawnable, review):
    """Regular 7/7 + priority 0/3: normal rows get no claim attempt (no lease, no locked census) and the
    tick still reports the regular pool as full; an idle priority pool is marked-only."""
    from hermes_cli import kanban_db_dispatch as dispatch
    from hermes_cli import kanban_queue_position as kqp
    conn, home = board
    attempts, spawned, spawn = _saturate(conn, home, monkeypatch, 7)
    normal = [task(conn, review=review, title=f"normal {index}") for index in range(3)]
    result = dispatch.dispatch_once(conn, spawn_fn=spawn, max_spawn=20, max_in_progress=7, reconcile_orphans=False)
    assert attempts == [] and spawned == []
    assert result.at_capacity == "regular pool max_in_progress=7/7 (priority 0/3 is marked-only)"
    for tid in normal:
        assert kb.get_task(conn, tid).status == ("review" if review else "ready")
        assert conn.execute("SELECT count(*) FROM task_runs WHERE task_id=?", (tid,)).fetchone()[0] == 0
    if not review:
        note = kqp.fields(kqp.position(conn, kb.get_task(conn, normal[0])))["queue_note"]
        assert "regular pool full (7/7)" in note, note


def test_partly_free_regular_pool_attempts_only_admissible_normal_rows(board, monkeypatch, all_assignees_spawnable):
    """Regular 6/7: the marked row takes a priority slot, ONE normal row the last regular slot; the other normal
    rows are not attempted although the combined budget (1 + 3) would cover them."""
    from hermes_cli import kanban_db_dispatch as dispatch
    conn, home = board
    attempts, spawned, spawn = _saturate(conn, home, monkeypatch, 6)
    normal = [task(conn, title=f"normal {index}") for index in range(3)]
    marked = task(conn, title="restore capacity")
    kb.edit_task(conn, marked, admission_class="urgent")
    dispatch.dispatch_once(conn, spawn_fn=spawn, max_spawn=20, max_in_progress=7, reconcile_orphans=False)
    assert len(attempts) == 2 and attempts[0] == marked and attempts[1] in normal
    assert spawned == attempts
    detail = {"pools": {"regular": 0, "priority": 0}}
    assert mm.active_card_runs(detail=detail) == 8
    assert detail["pools"] == {"regular": 7, "priority": 1}


@pytest.mark.parametrize("enabled", [False, True])
def test_worker_pickup_gets_pool_aware_budget(board, monkeypatch, all_assignees_spawnable, enabled):
    from hermes_cli import kanban_admission_pools as pools
    from hermes_cli import kanban_db_dispatch as dispatch
    conn, home = board
    if enabled:
        _saturate(conn, home, monkeypatch, 7)
    else:
        configure(home, limit=None, cap=7)
        monkeypatch.setattr(dispatch, "_memory_pressure_level", lambda: "normal")
    budgets = []
    monkeypatch.setattr(dispatch._kb._sync, "worker_pickup", lambda conn, budget, *rest: budgets.append(budget))
    # max_spawn is a per-board in-progress ceiling: 7 running + 2 more when enabled, 2 on the idle worker.
    dispatch.dispatch_once(conn, spawn_fn=lambda *a, **k: None, max_spawn=9 if enabled else 2, max_in_progress=7,
                           reconcile_orphans=False)
    if not enabled:
        assert budgets == [2]
        return
    [budget] = budgets
    assert isinstance(budget, pools.PoolBudget)
    assert not budget.admits("normal")
    assert budget.admits("capacity") and budget.admits("urgent")
    budget.take("capacity")
    budget.take("urgent")
    assert not budget.admits("capacity"), "max_spawn=2 still caps the total"


def test_pool_budget_charges_marked_to_priority_then_regular():
    from hermes_cli.kanban_admission_pools import PoolBudget
    budget = PoolBudget(1, 1)
    assert budget.admits("normal")
    budget.take("capacity")  # priority first
    assert budget.admits("normal")
    budget.take("urgent")  # priority full: falls to regular
    assert not budget.admits("normal") and not budget.admits("urgent")
    assert PoolBudget(0, 0, limit=None).admits("urgent") is False
    plain = PoolBudget(2, 0)
    plain.take("normal")
    assert plain.admits("normal")
    plain.take("normal")
    assert not plain.admits("capacity")


def test_row_without_admission_column_reads_as_normal():
    from hermes_cli.kanban_admission_pools import row_class
    with closing(sqlite3.connect(":memory:")) as db:
        db.row_factory = sqlite3.Row
        legacy = db.execute("SELECT 't_1' AS id").fetchone()
        marked = db.execute("SELECT 't_2' AS id, 'urgent' AS admission_class").fetchone()
        unset = db.execute("SELECT 't_3' AS id, NULL AS admission_class").fetchone()
    assert (row_class(legacy), row_class(marked), row_class(unset)) == ("normal", "urgent", "normal")


def test_pool_census_uses_one_board_snapshot(board, monkeypatch):
    """A claim landing between the board's running-row read and its pool count is in neither (one snapshot)."""
    from hermes_cli import kanban_admission_pools as pools
    conn, home = board
    tid = task(conn)
    original = pools.count_pools

    def concurrent_claim(*args, **kwargs):
        with closing(kbc.connect(home / "kanban.db")) as writer:
            with kb.write_txn(writer):
                writer.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
        return original(*args, **kwargs)

    monkeypatch.setattr(pools, "count_pools", concurrent_claim)
    detail = {"pools": {"regular": 0, "priority": 0}}
    total = mm.active_card_runs(detail=detail)
    assert total == 0
    assert detail["pools"] == {"regular": 0, "priority": 0}
    assert kb.get_task(conn, tid).status == "running"


def test_capacity_class_and_frees_claim_survive_task_readback(board):
    conn, _home = board
    task_id = kb.create_task(
        conn, title="Restore worker disk headroom", assignee="default",
        admission_class="capacity", admission_frees="frees: worker disk slots",
    )
    stored = kb.get_task(conn, task_id)
    assert stored.admission_class == "capacity"
    assert stored.admission_frees == "frees: worker disk slots"


@pytest.mark.parametrize("admission_class,frees,reason", [
    ("boosted", None, "admission_class must be normal, capacity or urgent"),
    ("capacity", None, "capacity requires a one-line frees: claim"),
    ("capacity", "disk", "capacity requires a one-line frees: claim"),
    ("capacity", "frees:   ", "capacity requires a one-line frees: claim"),
    ("capacity", "frees: disk\nother", "capacity requires a one-line frees: claim"),
])
def test_invalid_admission_metadata_refuses_before_insertion(board, admission_class, frees, reason):
    conn, _home = board
    before = conn.execute("SELECT count(*) FROM tasks").fetchone()[0]
    with pytest.raises(ValueError, match=f"^{reason}$"):
        kb.create_task(conn, title="Invalid class", admission_class=admission_class, admission_frees=frees)
    assert conn.execute("SELECT count(*) FROM tasks").fetchone()[0] == before


def test_admission_edit_validates_resulting_pair_atomically(board):
    conn, _home = board
    task_id = kb.create_task(conn, title="Disk headroom")
    assert kb.edit_task(conn, task_id, admission_class="capacity",
                        admission_frees="frees: worker disk slots")
    assert kb.get_task(conn, task_id).admission_class == "capacity"
    before = conn.execute("SELECT count(*) FROM task_events").fetchone()[0]
    with pytest.raises(ValueError, match="^capacity requires a one-line frees: claim$"):
        kb.edit_task(conn, task_id, title="Must roll back", admission_frees="")
    stored = kb.get_task(conn, task_id)
    assert stored.title == "Disk headroom"
    assert stored.admission_frees == "frees: worker disk slots"
    assert conn.execute("SELECT count(*) FROM task_events").fetchone()[0] == before
    assert kb.edit_task(conn, task_id, admission_class="normal", admission_frees="")
    assert kb.get_task(conn, task_id).admission_class == "normal"
    assert kb.get_task(conn, task_id).admission_frees == ""


def test_cli_edit_admission_metadata(board, monkeypatch):
    import argparse
    from hermes_cli import kanban as cli

    conn, home = board
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    task_id = kb.create_task(conn, title="Disk headroom")
    parser = argparse.ArgumentParser()
    cli.build_parser(parser.add_subparsers())
    args = parser.parse_args(["kanban", "edit", task_id, "--class", "capacity",
                              "--frees", "frees: worker disk slots"])
    assert cli._cmd_edit(args) == 0
    assert kb.get_task(conn, task_id).admission_class == "capacity"
    args = parser.parse_args(["kanban", "edit", task_id, "--frees", ""])
    assert cli._cmd_edit(args) != 0
    assert kb.get_task(conn, task_id).admission_frees == "frees: worker disk slots"


@pytest.mark.parametrize("options,reason", [
    (["--max-retries", "4"], "operator fields"),
    (["--completion-contract", "local-only", "--reason", "test"], "--completion-contract"),
    (["--requires", "terminal"], "--requires"),
])
def test_cli_admission_refuses_separate_transaction_edits(board, monkeypatch, capsys, options, reason):
    import argparse
    from hermes_cli import kanban as cli

    conn, home = board
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    task_id = kb.create_task(conn, title="Unchanged")
    before = tuple(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
    parser = argparse.ArgumentParser()
    cli.build_parser(parser.add_subparsers())
    args = parser.parse_args(["kanban", "edit", task_id, "--class", "urgent", *options])
    assert cli._cmd_edit(args) == 2
    assert f"admission metadata cannot be combined with {reason}" in capsys.readouterr().err
    assert tuple(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()) == before


def test_cli_frees_alone_refuses_requires_edit(board, monkeypatch, capsys):
    import argparse
    from hermes_cli import kanban as cli

    conn, home = board
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    task_id = kb.create_task(conn, title="Unchanged")
    before = tuple(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
    parser = argparse.ArgumentParser()
    cli.build_parser(parser.add_subparsers())
    args = parser.parse_args(["kanban", "edit", task_id, "--frees", "frees: worker slots", "--requires", "terminal"])
    assert cli._cmd_edit(args) == 2
    assert "admission metadata cannot be combined with --requires" in capsys.readouterr().err
    assert tuple(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()) == before


@pytest.mark.parametrize("changes,reason", [
    ({"admission_class": "capacity"}, "capacity requires a one-line frees: claim"),
    ({"admission_class": "unknown"}, "admission_class must be normal, capacity or urgent"),
    ({"admission_class": []}, "admission_class must be normal, capacity or urgent"),
])
def test_invalid_admission_edit_preserves_task_and_events(board, changes, reason):
    conn, _home = board
    task_id = kb.create_task(conn, title="Unchanged")
    before = tuple(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone())
    event_count = conn.execute("SELECT count(*) FROM task_events").fetchone()[0]
    with pytest.raises(ValueError, match=f"^{reason}$"):
        kb.edit_task(conn, task_id, title="Invalid edit", **changes)
    assert tuple(conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()) == before
    assert conn.execute("SELECT count(*) FROM task_events").fetchone()[0] == event_count


def test_cli_create_admission_metadata(board, monkeypatch):
    import argparse
    from hermes_cli import kanban as cli
    from hermes_cli import kanban_placement as placement

    conn, home = board
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    monkeypatch.setattr(placement, "config", dict)
    parser = argparse.ArgumentParser()
    cli.build_parser(parser.add_subparsers())
    args = parser.parse_args(["kanban", "create", "Restore disk", "--class", "capacity",
                              "--frees", "frees: worker disk slots", "--no-requires"])
    assert cli._cmd_create(args) == 0
    stored = kb.list_tasks(conn)
    assert len(stored) == 1
    assert stored[0].admission_class == "capacity"
    assert stored[0].admission_frees == "frees: worker disk slots"


def test_cli_create_capacity_without_frees_refuses_cleanly(board, monkeypatch, capsys):
    import argparse
    from hermes_cli import kanban as cli
    from hermes_cli import kanban_placement as placement

    conn, home = board
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "kanban.db"))
    monkeypatch.setattr(placement, "config", dict)
    parser = argparse.ArgumentParser()
    cli.build_parser(parser.add_subparsers())
    args = parser.parse_args(["kanban", "create", "Restore disk", "--class", "capacity", "--no-requires"])
    assert cli._cmd_create(args) == 2
    assert "capacity requires a one-line frees: claim" in capsys.readouterr().err
    assert kb.list_tasks(conn) == []


@pytest.mark.parametrize("review", [False, True])
def test_gate_absent_admits_without_sampling(board, monkeypatch, review):
    conn, _home = board
    locked(monkeypatch, lambda *a, **k: pytest.fail("no root limit: must not sample"))
    t = task(conn, review)
    assert claim(conn, t, review) is not None


@pytest.mark.parametrize("review", [False, True])
def test_below_limit_admits_and_claimed_event_carries_consumed_sample(board, monkeypatch, review):
    conn, home = board
    configure(home)
    advisory(home)
    locked(monkeypatch, fake_sample(49.999))
    t = task(conn, review)
    assert claim(conn, t, review) is not None
    payload = json.loads(conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='claimed'",
                                      (t,)).fetchone()[0])
    assert payload["hub_cpu"] == {"cpu_busy_pct": 49.999, "cpu_busy_window_ms": 1000,
                                  "cpu_busy_sample_end_ms": payload["hub_cpu"]["cpu_busy_sample_end_ms"],
                                  "limit_pct": 50.0}


@pytest.mark.parametrize("review", [False, True])
@pytest.mark.parametrize("pct,reason", [(50.0, "hub_cpu_at_or_above_limit"), (50.001, "hub_cpu_at_or_above_limit"),
                                        (None, "hub_cpu_sample_stale")])
def test_at_or_above_limit_or_unknown_refuses_with_no_local_run(board, monkeypatch, review, pct, reason):
    conn, home = board
    configure(home)
    advisory(home)
    locked(monkeypatch, fake_sample(pct))
    t = task(conn, review)
    assert claim(conn, t, review) is None
    refused(conn, t, review, reason)


@pytest.mark.parametrize("review", [False, True])
def test_invalid_root_limit_refuses_without_sampling(board, monkeypatch, review):
    conn, home = board
    configure(home, limit='"fifty"')  # type: ignore[arg-type]
    locked(monkeypatch, lambda *a, **k: pytest.fail("invalid limit: refuse before sampling"))
    t = task(conn, review)
    assert claim(conn, t, review) is None
    refused(conn, t, review, "hub_cpu_limit_unknown")


def test_profile_override_cannot_disable_root_gate(board, monkeypatch, tmp_path):
    conn, home = board
    configure(home)
    advisory(home)
    profile = home / "profiles" / "p"
    profile.mkdir(parents=True)
    (profile / "config.yaml").write_text("kanban:\n  admission:\n    cpu_busy_below_pct: null\n")
    monkeypatch.setenv("HERMES_HOME", str(profile))
    import hermes_constants
    monkeypatch.setattr(hermes_constants, "get_default_hermes_root", lambda *a, **k: home)
    locked(monkeypatch, fake_sample(80.0))
    t = task(conn)
    assert kb.claim_task(conn, t) is None
    refused(conn, t, False, "hub_cpu_at_or_above_limit")


@pytest.mark.parametrize("review", [False, True])
def test_sample_is_taken_while_admission_lock_is_held(board, monkeypatch, review):
    conn, home = board
    configure(home)
    advisory(home)
    seen = []

    def check():
        try:
            with af.admission_lock(home, timeout=0):
                seen.append("free")
        except af.AdmissionBusy:
            seen.append("held")
    locked(monkeypatch, fake_sample(10.0, check=check))
    t = task(conn, review)
    assert claim(conn, t, review) is not None
    assert seen == ["held"]


@pytest.mark.parametrize("review", [False, True])
@pytest.mark.parametrize("advice,reason", [(80.0, "hub_cpu_at_or_above_limit"), ("stale", "hub_cpu_unknown"),
                                           ("missing", "hub_cpu_unknown"), ("future", "hub_cpu_unknown"),
                                           (150.0, "hub_cpu_unknown"), ("text", "hub_cpu_unknown")])
def test_advisory_precheck_defers_before_any_lease_or_sample(board, monkeypatch, review, advice, reason):
    conn, home = board
    configure(home)
    if advice == "stale":
        advisory(home, age_ms=15_001)
    elif advice == "future":
        advisory(home, age_ms=-5_000)
    elif advice != "missing":
        advisory(home, pct=advice)
    monkeypatch.setattr(sync, "claim_gate", lambda *a, **k: pytest.fail("advisory refusal must not ask for a lease"))
    locked(monkeypatch, lambda *a, **k: pytest.fail("advisory refusal must not sample"))
    t = task(conn, review)
    assert claim(conn, t, review) is None
    refused(conn, t, review, reason)


def test_advisory_age_boundary_10000_ms_is_fresh_and_the_locked_read_keeps_15000(board, monkeypatch):
    """Pre-lease: 10 s (15 s minus the lease headroom). The locked gate reads the same file and accepts up to 15 s."""
    conn, home = board
    configure(home)
    # Freeze only the admission clock: task creation or runner descheduling must not
    # consume a 100 ms margin before the pre-lease read. Exercise the exact bounds.
    now = int(time.time())
    monkeypatch.setattr(ka, "_now", lambda: now)
    mm.record_hub_cpu_sample({"cpu_busy_pct": 10.0, "cpu_busy_window_ms": 1000,
                              "cpu_busy_sample_end_ms": now * 1000 - 10_000}, root=home)
    t = task(conn)
    assert kb.claim_task(conn, t) is not None
    assert ka.ADVISORY_MAX_AGE_MS - ka.PRELEASE_HEADROOM_MS == 10_000
    now += 0.001
    assert ka.sampler_sample(10_000) is None
    now = int(now) + 5  # same sample, exactly 15 s old: still fresh for the locked read
    assert ka.sampler_sample() is not None and ka.sampler_sample(10_000) is None
    now += 0.001
    assert ka.sampler_sample() is None


@pytest.mark.parametrize("review", [False, True])
def test_whole_host_cap_is_rechecked_under_lock(board, monkeypatch, review):
    conn, home = board
    configure(home, cap=2)
    advisory(home)
    locked(monkeypatch, fake_sample(10.0))
    monkeypatch.setattr(mm, "active_card_runs", lambda: 2)
    t = task(conn, review)
    assert claim(conn, t, review) is None
    refused(conn, t, review, "hub_capacity_full")
    monkeypatch.setattr(mm, "active_card_runs", lambda: None)  # an unreadable board fails closed
    assert claim(conn, t, review) is None
    refused(conn, t, review, "hub_capacity_full")
    monkeypatch.setattr(mm, "active_card_runs", lambda: 1)
    assert claim(conn, t, review) is not None


def test_lease_expiring_during_sample_cannot_start(board, monkeypatch):
    conn, home = board
    configure(home)
    advisory(home)
    real_time = time.time
    locked(monkeypatch, fake_sample(10.0, check=lambda: monkeypatch.setattr(
        ka, "_now", lambda: real_time() + 10 ** 7)))
    t = task(conn)
    assert kb.claim_task(conn, t) is None
    refused(conn, t, False, "hub_cpu_gate_expired_counting")


def test_simultaneous_claimers_at_6_of_7_start_at_most_one(board, monkeypatch):
    conn, home = board
    configure(home, cap=7)
    advisory(home)
    # Six runs elsewhere plus whatever this board has claimed, read from the DB like the real count:
    # a counter bumped after claim_task returns lags the lock release, so a busy runner let a second
    # claimer recount 6/7 before the first winner's bump landed.
    def active():
        with closing(sqlite3.connect(f"file:{home / 'kanban.db'}?mode=ro", uri=True, timeout=5)) as db:
            return 6 + db.execute("SELECT count(*) FROM tasks WHERE status='running'").fetchone()[0]
    monkeypatch.setattr(mm, "active_card_runs", active)
    locked(monkeypatch, fake_sample(10.0, check=lambda: time.sleep(0.2)))  # health: allow HX013 -- widens the locked census-to-commit window
    tasks = [task(conn, title=f"t{i}") for i in range(4)]

    def go(t):
        with kbc.connect(home / "kanban.db") as other:
            return kb.claim_task(other, t)
    for _ in range(3):  # contended rounds: losers defer (lock busy) or are refused at 7/7
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(go, tasks))
    assert runs(conn) == 1


def _lock_state(home):
    try:
        with af.admission_lock(home, timeout=0):
            return "free"
    except af.AdmissionBusy:
        return "held"


def test_real_claim_reads_the_sampler_file_under_the_lock_and_holds_it_briefly(board, monkeypatch):
    """Contract (replaces the 2026-11 independent ~1 s sample): the locked gate reads the sampler's own file while
    admission.lock is held, takes no /proc/stat sample, records that very triple on ``claimed``, and the whole claim
    finishes in under ``BUSY_WAIT_S - WINDOW_S`` (1.75 s), so it no longer holds the lock for a ~1 s sample."""
    from hermes_cli import backend_retirement

    conn, home = board
    configure(home)
    advisory(home, pct=23.0, age_ms=500)
    t = task(conn)
    monkeypatch.setattr(cm, "sample", lambda *a, **k: pytest.fail("no /proc/stat sample under the lock"))
    m = {}
    real = ka.sampler_sample

    def read(max_age_ms=None):
        if max_age_ms is None:
            m["lock_at_read"] = _lock_state(home)
        return real(max_age_ms)
    monkeypatch.setattr(ka, "sampler_sample", read)
    t0 = time.monotonic()
    got = kb.claim_task(conn, t)
    claim_s = time.monotonic() - t0
    print("HUB_ADMISSION_LOCK_MEASURE " + json.dumps({"claim_s": round(claim_s, 4)}))
    assert got is not None
    assert m["lock_at_read"] == "held"
    payload = json.loads(conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='claimed'",
                                      (t,)).fetchone()[0])
    assert {k: payload["hub_cpu"][k] for k in cm.FIELDS} == mm.read_hub_cpu_sample(home)
    assert claim_s < backend_retirement.BUSY_WAIT_S - cm.WINDOW_S  # no ~1 s sample inside the hold any more


def test_queue_reason_codes_cover_the_eight_overflow_reasons():
    assert {"workers_unknown", "no_eligible_worker", "workers_saturated", "hub_cpu_at_or_above_limit",
            "hub_cpu_unknown", "hub_capacity_full", "hub_sample_already_used", "hub_overflow_reserved"} <= queue.CODES


@pytest.mark.parametrize("review", [False, True])
def test_full_cap_refuses_before_the_sample_so_the_lock_is_not_held_for_it(board, monkeypatch, review):
    """Review F6: the cheap whole-host cap check runs first; a claim at the cap never holds admission.lock ~1 s."""
    conn, home = board
    configure(home, cap=2)
    advisory(home)
    monkeypatch.setattr(mm, "active_card_runs", lambda: 2)
    locked(monkeypatch, lambda *a, **k: pytest.fail("at the cap: refuse before sampling"))
    t = task(conn, review)
    assert claim(conn, t, review) is None
    refused(conn, t, review, "hub_capacity_full")


def test_deadline_already_passed_refuses_before_the_sample(board, monkeypatch):
    conn, home = board
    configure(home)
    advisory(home, age_ms=-10 ** 10)  # fresh against the shifted clock below, so the advisory admits
    real_time = time.time
    monkeypatch.setattr(sync, "claim_gate", lambda conn, task_id, expires, lane="ready": sync.ClaimGate(expires))
    monkeypatch.setattr(ka, "_now", lambda: real_time() + 10 ** 7)
    locked(monkeypatch, lambda *a, **k: pytest.fail("deadline passed: refuse before sampling"))
    t = task(conn)
    assert kb.claim_task(conn, t) is None
    refused(conn, t, False, "hub_cpu_gate_expired")


def _shrink_hold_budget(monkeypatch, budget_s):
    """The designed hold is CLAIM_MAX_HOLD_S - COMMIT_RESERVE_S; shrink it so an overrun is cheap to provoke."""
    monkeypatch.setattr(ka, "COMMIT_RESERVE_S", af.CLAIM_MAX_HOLD_S - budget_s)


@pytest.mark.parametrize("review", [False, True])
def test_hold_that_overruns_its_budget_refuses_instead_of_committing(board, monkeypatch, review):
    """Final review N1: a gate that outlasts the designed hold must not commit (turn budget would lose). It is named
    ``hub_admission_overrun``: an overrun is not a CPU reading (hub 2026-11-13 reported it as hub_cpu_unknown)."""
    conn, home = board
    configure(home)
    advisory(home)
    _shrink_hold_budget(monkeypatch, 0.2)
    locked(monkeypatch, fake_sample(10.0, check=lambda: time.sleep(0.3)))  # health: allow HX013 -- a read that overruns the shrunken hold
    t = task(conn, review)
    assert claim(conn, t, review) is None
    refused(conn, t, review, ka.OVERRUN)


@pytest.mark.parametrize("review", [False, True])
def test_slow_census_overrun_refuses_as_overrun(board, monkeypatch, review):
    conn, home = board
    configure(home)
    advisory(home)
    _shrink_hold_budget(monkeypatch, 0.2)

    def slow_count():
        time.sleep(0.3)  # health: allow HX013 -- a slow board scan under the lock
        return 0
    monkeypatch.setattr(mm, "active_card_runs", slow_count)
    t = task(conn, review)
    assert claim(conn, t, review) is None
    refused(conn, t, review, ka.OVERRUN)


def test_claim_inside_the_hold_budget_still_admits(board, monkeypatch):
    """Control for the N1 bound: the default budget admits a normal gate (the bound is not a blanket refusal)."""
    conn, home = board
    configure(home)
    advisory(home)
    assert ka._hold_budget_s() == pytest.approx(af.CLAIM_MAX_HOLD_S - ka.COMMIT_RESERVE_S)
    assert ka._hold_budget_s() >= cm.WINDOW_S + 1.0
    locked(monkeypatch, fake_sample(10.0, check=lambda: time.sleep(0.1)))  # health: allow HX013 -- a short stand-in read
    t = task(conn)
    assert kb.claim_task(conn, t) is not None


# --- Phase 2: deterministic admission score over the full replica ---------------------------------------

def ready(conn, title, priority=0, created_at=None, admission_class="normal", failures=0):
    tid = task(conn, title=title)
    fields = {"priority": priority}
    if admission_class != "normal":
        fields["admission_class"] = admission_class
        if admission_class == "capacity":
            fields["admission_frees"] = "frees: worker slots"
    kb.edit_task(conn, tid, **fields)
    with kb.write_txn(conn):
        if created_at is not None:
            conn.execute("UPDATE tasks SET created_at=? WHERE id=?", (created_at, tid))
            conn.execute("UPDATE task_events SET created_at=? WHERE task_id=?", (created_at, tid))
        conn.execute("UPDATE tasks SET consecutive_failures=? WHERE id=?", (failures, tid))
    return tid


def rows(conn, *ids):
    marks = ",".join("?" for _ in ids)
    return conn.execute(f"SELECT id, assignee, admission_class FROM tasks WHERE id IN ({marks})", ids).fetchall()


def order(conn, now_s, *ids, weights=None):
    return [row["id"] for row, _ in score.rank(conn, rows(conn, *ids), now_ms=now_s * 1000, weights=weights)]


def test_breakdown_sums_every_component_with_default_weights(board):
    conn, _home = board
    now = 1_800_000_000
    tid = ready(conn, "parent", priority=300, created_at=now - 35 * 60, admission_class="capacity", failures=1)
    for index in range(3):
        child = task(conn, title=f"child {index}")
        kb.link_tasks(conn, tid, child)
    (row, parts), = score.rank(conn, rows(conn, tid), now_ms=now * 1000)
    assert row["id"] == tid
    assert (parts.effective_priority, parts.class_rank, parts.descendants, parts.age_buckets, parts.failures) == \
        (300, 1, 3, 3, 1)
    assert parts.total == 300 + 500 + 25 * 3 + 25 * 3 - 100


def test_capacity_card_outranks_higher_priority_normal_card(board):
    conn, _home = board
    now = 1_800_000_000
    normal = ready(conn, "normal", priority=400, created_at=now)
    capacity = ready(conn, "capacity", priority=0, created_at=now, admission_class="capacity")
    assert order(conn, now, normal, capacity) == [capacity, normal]


def test_descendants_raise_score_and_are_capped(board):
    conn, _home = board
    now = 1_800_000_000
    plain = ready(conn, "plain", priority=100, created_at=now - 1)
    parent = ready(conn, "parent", priority=60, created_at=now)
    for index in range(25):
        kb.link_tasks(conn, parent, task(conn, title=f"child {index}"))
    assert order(conn, now, plain, parent) == [parent, plain]
    (_row, parts), = score.rank(conn, rows(conn, parent), now_ms=now * 1000)
    assert parts.descendants == 20


def test_aging_lifts_starved_card_within_bound(board):
    conn, _home = board
    now = 1_800_000_000
    starved = ready(conn, "starved", priority=0, created_at=now - 10 * 3600)
    fresh = ready(conn, "fresh", priority=1000, created_at=now)
    (_row, parts), = score.rank(conn, rows(conn, starved), now_ms=now * 1000)
    assert parts.age_buckets == 60
    # 60 buckets * 25 = 1500 > the 1000 priority cap: ten hours lift any card past fresh capped priority.
    assert order(conn, now, fresh, starved) == [starved, fresh]
    # The bound: aging never exceeds 60 buckets, so an older card gains nothing more.
    older = ready(conn, "older", priority=0, created_at=now - 100 * 3600)
    (_row, parts), = score.rank(conn, rows(conn, older), now_ms=now * 1000)
    assert parts.age_buckets == 60


def test_age_counts_current_ready_interval_not_card_age(board):
    conn, _home = board
    now = 1_800_000_000
    tid = ready(conn, "requeued", created_at=now - 10 * 3600)
    with kb.write_txn(conn):
        kb._append_event(conn, tid, "promoted", {})
        conn.execute("UPDATE task_events SET created_at=? WHERE task_id=? AND kind='promoted'", (now - 25 * 60, tid))
    (_row, parts), = score.rank(conn, rows(conn, tid), now_ms=now * 1000)
    assert parts.age_buckets == 2
    assert parts.ready_since == now - 25 * 60


def test_consecutive_failures_lower_the_score(board):
    conn, _home = board
    now = 1_800_000_000
    failing = ready(conn, "failing", priority=150, created_at=now - 1, failures=2)
    steady = ready(conn, "steady", priority=0, created_at=now)
    assert order(conn, now, failing, steady) == [steady, failing]
    (_row, parts), = score.rank(conn, rows(conn, failing), now_ms=now * 1000)
    assert (parts.failures, parts.total) == (2, 150 - 200)


def test_equal_scores_break_on_ready_start_then_id(board):
    conn, _home = board
    now = 1_800_000_000
    later = ready(conn, "later", created_at=now - 60)
    earlier = ready(conn, "earlier", created_at=now - 120)
    assert order(conn, now, later, earlier) == [earlier, later]
    twin_a, twin_b = ready(conn, "a", created_at=now - 60), ready(conn, "b", created_at=now - 60)
    assert order(conn, now, twin_b, twin_a) == sorted([twin_a, twin_b])


def test_configured_weights_are_validated_and_applied(board):
    conn, home = board
    now = 1_800_000_000
    normal = ready(conn, "normal", priority=400, created_at=now)
    capacity = ready(conn, "capacity", created_at=now, admission_class="capacity")
    configure(home, limit=None)
    with (home / "config.yaml").open("a") as config:
        config.write("  admission:\n    weights:\n      class: 100\n")
    assert score.configured_weights() == {**score.DEFAULT_WEIGHTS, "class": 100}
    assert [row["id"] for row, _ in score.rank(conn, rows(conn, normal, capacity), now_ms=now * 1000)] == \
        [normal, capacity]


@pytest.mark.parametrize("weights,reason", [
    ([], "admission weights must be a mapping"),
    ({"bump": 1}, "unknown admission weight(s): bump"),
    ({"age": -1}, "admission weight age must be a nonnegative number"),
    ({"age": True}, "admission weight age must be a nonnegative number"),
    ({"age": "25"}, "admission weight age must be a nonnegative number"),
    ({"age": float("inf")}, "admission weight age must be a nonnegative number"),
    ({"age": float("nan")}, "admission weight age must be a nonnegative number"),
])
def test_invalid_weights_refuse_with_their_own_reason(weights, reason):
    with pytest.raises(ValueError) as error:
        score.validate_weights(weights)
    assert str(error.value) == reason


@pytest.fixture
def replica(board, tmp_path, monkeypatch):
    from hermes_cli import kanban_worker_node as wn
    path = tmp_path / "replica.db"
    remote = kbc.connect(path)
    monkeypatch.setattr(sync, "board_mode", lambda conn: "primary")
    monkeypatch.setattr(wn, "open_replica", lambda conn: kbc.connect(path))
    try:
        yield remote
    finally:
        remote.close()


def _copy_task(remote, conn, tid):
    row = conn.execute("SELECT id, title, assignee, priority, status, created_at FROM tasks WHERE id=?",
                       (tid,)).fetchone()
    with kb.write_txn(remote):
        remote.execute("INSERT INTO tasks (id, title, assignee, priority, status, created_at) VALUES (?,?,?,?,?,?)",
                       tuple(row))


def test_replica_descendants_count_and_local_unlink_or_completion_wins(board, replica):
    conn, _home = board
    now = 1_800_000_000
    parent = ready(conn, "imported parent", priority=0, created_at=now)
    local_work = ready(conn, "local", priority=100, created_at=now)
    _copy_task(replica, conn, parent)
    waiter = kb.create_task(replica, title="remote waiter", assignee="default", priority=900, parents=[parent])
    assert order(conn, now, local_work, parent) == [parent, local_work]
    (_row, parts), = score.rank(conn, rows(conn, parent), now_ms=now * 1000)
    assert (parts.effective_priority, parts.descendants) == (900, 1)
    # A local edge set for the child overrides the replica edges: after a local unlink the waiter donates nothing.
    _copy_task(conn, replica, waiter)
    assert order(conn, now, local_work, parent) == [local_work, parent]
    # Re-linked locally, then completed locally: a done child is no longer an open descendant.
    kb.link_tasks(conn, parent, waiter)
    assert order(conn, now, local_work, parent) == [parent, local_work]
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='done' WHERE id=?", (waiter,))
    assert order(conn, now, local_work, parent) == [local_work, parent]
    assert conn.execute("SELECT priority FROM tasks WHERE id=?", (parent,)).fetchone()[0] == 0


def _enable(home):
    configure(home, cap=7)
    with (home / "config.yaml").open("a") as config:
        config.write("    priority_slots: 3\n")


def test_opted_in_hub_lane_order_uses_score_and_keeps_marked_first(board):
    from hermes_cli import kanban_admission_pools as pools
    conn, home = board
    now = int(time.time())
    starved = ready(conn, "starved", priority=0, created_at=now - 11 * 3600)
    fresh = ready(conn, "fresh", priority=1000, created_at=now)
    marked = ready(conn, "marked", priority=0, created_at=now, admission_class="capacity")
    _enable(home)
    assert [r["id"] for r in pools.lane_rows(conn, "ready")] == [marked, starved, fresh]


@pytest.mark.parametrize("enabled,slots", [(False, 3), (True, 0)])
def test_worker_lane_order_keeps_plain_priority(board, enabled, slots):
    from hermes_cli import kanban_admission_pools as pools
    conn, home = board
    now = int(time.time())
    starved = ready(conn, "starved", priority=0, created_at=now - 11 * 3600)
    fresh = ready(conn, "fresh", priority=1000, created_at=now)
    configure(home, cap=7, limit=50 if enabled else None)
    with (home / "config.yaml").open("a") as config:
        config.write(("" if enabled else "  admission:\n") + f"    priority_slots: {slots}\n")
    assert [r["id"] for r in pools.lane_rows(conn, "ready")] == [fresh, starved]


def _explain_board(conn, home, monkeypatch):
    now = int(time.time())
    configure(home, limit=None)
    assert kb.claim_task(conn, task(conn, title="regular running"))
    _enable(home)
    advisory(home)
    locked(monkeypatch, fake_sample(10))
    fresh = ready(conn, "fresh", priority=900, created_at=now)
    starved = ready(conn, "starved", priority=0, created_at=now - 11 * 3600)
    marked = ready(conn, "marked", priority=0, created_at=now - 25 * 60, admission_class="capacity")
    return fresh, starved, marked


def test_queue_note_carries_score_breakdown_on_opted_in_hub(board, monkeypatch):
    from hermes_cli import kanban_queue_position as qp
    conn, home = board
    fresh, _starved, marked = _explain_board(conn, home, monkeypatch)
    monkeypatch.setattr(qp, "_running_and_limit", lambda *a: (1, 7))
    place = qp.read(conn, kb.get_task(conn, fresh))
    assert place["ahead"] == 2
    assert place["admission_score"]["effective_priority"] == 900
    note = qp.describe(place)
    assert "score 900 (priority 900 + class 0 + unblock 0 + age 0 - failures 0)" in note
    assert "bump" not in note
    assert qp.read(conn, kb.get_task(conn, marked))["admission_score"]["class_rank"] == 1


def test_queue_explain_prints_breakdown_position_and_pools(board, monkeypatch):
    from hermes_cli import kanban as kc
    conn, home = board
    fresh, starved, marked = _explain_board(conn, home, monkeypatch)
    text = kc.run_slash(f"queue --explain {starved}")
    assert f"{starved}: ready lane position 2/3" in text
    assert "score 1500 = priority 0 + class 500*0 + unblock 25*0 + age 25*60 - failures 100*0" in text
    assert "pools: regular 1/7, priority 0/3" in text
    assert f"ahead: {marked}" in text
    listing = json.loads(kc.run_slash("queue --json"))
    assert [row["id"] for row in listing["ready"]] == [marked, starved, fresh]
    assert listing["ready"][0]["score"]["class_rank"] == 1
    assert listing["scored"] is True


def test_queue_explain_on_worker_reports_plain_priority(board):
    from hermes_cli import kanban as kc
    conn, home = board
    configure(home, limit=None)
    tid = ready(conn, "worker card", priority=5)
    text = kc.run_slash(f"queue --explain {tid}")
    assert "hub admission score not enabled here: plain priority order" in text
    assert f"{tid}: ready lane position 1/1" in text
    assert json.loads(kc.run_slash("queue --json"))["scored"] is False


def test_queue_explain_unknown_or_unqueued_card(board):
    from hermes_cli import kanban as kc
    conn, home = board
    configure(home, limit=None)
    blocked = task(conn)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='blocked' WHERE id=?", (blocked,))
    assert "not in a ready or review lane (status blocked)" in kc.run_slash(f"queue --explain {blocked}")
    assert "no such task: t_missing" in kc.run_slash("queue --explain t_missing")


def _claimed_run(conn, title, *, ready_at, started_at, admission_class="normal", origin=None, completed_at=None):
    tid = ready(conn, title, created_at=ready_at, admission_class=admission_class)
    with kb.write_txn(conn):
        conn.execute("INSERT INTO task_runs (task_id, profile, status, started_at, ended_at, outcome, "
                     "admission_pool, admission_class, admission_origin) VALUES (?, 'default', 'done', ?, ?, "
                     "'completed', ?, ?, ?)",
                     (tid, started_at, completed_at, "priority" if admission_class != "normal" else "regular",
                      admission_class, origin))
        if completed_at is not None:
            conn.execute("UPDATE tasks SET status='done', completed_at=? WHERE id=?", (completed_at, tid))
    return tid


def _waits(conn, around, before_wait, after_wait, n=3):
    for index in range(n):
        start = around - 3600 - index * 60
        _claimed_run(conn, f"before {index}", ready_at=start - before_wait, started_at=start)
        start = around + 3600 + index * 60
        _claimed_run(conn, f"after {index}", ready_at=start - after_wait, started_at=start)


def test_capacity_report_measures_ready_wait_p50_around_completion(board):
    from hermes_cli import kanban_admission_report as report
    conn, _home = board
    now = 1_800_000_000
    done_at = now - 2 * 86400
    moved = _claimed_run(conn, "add worker", ready_at=done_at - 7200, started_at=done_at - 3600,
                         admission_class="capacity", origin="chat:a", completed_at=done_at)
    _waits(conn, done_at, before_wait=3000, after_wait=600)
    data = report.capacity_report(conn, now_s=now, days=7)
    entry, = data["cards"]
    assert entry["id"] == moved
    assert entry["frees"] == "frees: worker slots"
    assert entry["origin"] == "chat:a"
    assert (entry["ready_wait_p50_before_s"], entry["ready_wait_p50_after_s"]) == (3000, 600)
    assert entry["effect"] == "moved"


def test_capacity_report_flags_repeat_no_effect_origin_and_unknown_is_not_failure(board):
    from hermes_cli import kanban_admission_report as report
    conn, _home = board
    now = 1_800_000_000
    for index, day in enumerate((2, 4)):
        done_at = now - day * 86400
        _claimed_run(conn, f"no effect {index}", ready_at=done_at - 7200, started_at=done_at - 3600,
                     admission_class="capacity", origin="chat:b", completed_at=done_at)
        _waits(conn, done_at, before_wait=600, after_wait=600)
    sparse_at = now - 6 * 86400
    _claimed_run(conn, "unmeasured", ready_at=sparse_at - 7200, started_at=sparse_at - 3600,
                 admission_class="capacity", origin="chat:c", completed_at=sparse_at)
    data = report.capacity_report(conn, now_s=now, days=7)
    effects = {card["origin"]: card["effect"] for card in data["cards"]}
    assert sorted(card["effect"] for card in data["cards"] if card["origin"] == "chat:b") == ["no_effect"] * 2
    assert effects["chat:c"] == "unknown"
    assert data["origins"]["chat:b"] == {"moved": 0, "no_effect": 2, "unknown": 0, "revoke_recommended": True}
    assert data["origins"]["chat:c"]["revoke_recommended"] is False
    text = report.render(data)
    assert "chat:b: 0 moved, 2 no effect, 0 unknown -> remove capacity class from new cards" in text


def test_capacity_report_cli_window_excludes_old_cards(board):
    from hermes_cli import kanban as kc
    conn, home = board
    configure(home, limit=None)
    old = int(time.time()) - 30 * 86400
    _claimed_run(conn, "old capacity", ready_at=old - 60, started_at=old, admission_class="capacity",
                 origin="chat:a", completed_at=old + 60)
    assert json.loads(kc.run_slash("queue --report --days 7 --json"))["cards"] == []
    assert len(json.loads(kc.run_slash("queue --report --days 60 --json"))["cards"]) == 1


# --- Mutation survivors (score-mutants-run1: S13, S16, R1, R4, R6) ------------------------------------------

def test_legacy_uncapped_priority_is_capped_in_the_score(board):
    """Rows stored before the PR1137 cap (hub had 30100) count as at most PRIORITY_CAP, so aging still lifts past them."""
    conn, _home = board
    now = 1_800_000_000
    legacy = ready(conn, "legacy bump", created_at=now)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET priority=30100 WHERE id=?", (legacy,))
    starved = ready(conn, "starved", priority=0, created_at=now - 10 * 3600)
    (_row, parts), = score.rank(conn, rows(conn, legacy), now_ms=now * 1000)
    assert (parts.effective_priority, parts.total) == (1000, 1000)
    assert order(conn, now, legacy, starved) == [starved, legacy]


def test_equal_scores_prefer_earlier_ready_start_over_smaller_id(board):
    conn, _home = board
    now = 1_800_000_000
    small, large = sorted([ready(conn, "x", created_at=now - 60), ready(conn, "y", created_at=now - 60)])
    with kb.write_txn(conn):
        conn.execute("UPDATE task_events SET created_at=? WHERE task_id=?", (now - 120, large))
        conn.execute("UPDATE tasks SET created_at=? WHERE id=?", (now - 120, large))
    (_r, a), = score.rank(conn, rows(conn, small), now_ms=now * 1000)
    (_r, b), = score.rank(conn, rows(conn, large), now_ms=now * 1000)
    assert a.total == b.total and b.ready_since < a.ready_since
    assert order(conn, now, small, large) == [large, small]


def test_capacity_report_too_few_samples_is_unknown(board):
    from hermes_cli import kanban_admission_report as report
    conn, _home = board
    now = 1_800_000_000
    done_at = now - 2 * 86400
    _claimed_run(conn, "thin evidence", ready_at=done_at - 7200, started_at=done_at - 3600,
                 admission_class="capacity", origin="chat:e", completed_at=done_at)
    _waits(conn, done_at, before_wait=3000, after_wait=600, n=report.MIN_SAMPLES - 1)
    entry, = report.capacity_report(conn, now_s=now, days=7)["cards"]
    assert (entry["ready_wait_p50_before_s"], entry["ready_wait_p50_after_s"], entry["effect"]) == \
        (None, None, "unknown")


def test_capacity_report_one_moved_card_keeps_origin_class(board):
    from hermes_cli import kanban_admission_report as report
    conn, _home = board
    now = 1_800_000_000
    for index, (day, before) in enumerate(((1, 3000), (3, 600), (5, 600))):
        done_at = now - day * 86400
        _claimed_run(conn, f"card {index}", ready_at=done_at - 7200, started_at=done_at - 3600,
                     admission_class="capacity", origin="chat:d", completed_at=done_at)
        _waits(conn, done_at, before_wait=before, after_wait=600)
    data = report.capacity_report(conn, now_s=now, days=7)
    assert data["origins"]["chat:d"] == {"moved": 1, "no_effect": 2, "unknown": 0, "revoke_recommended": False}


def test_capacity_report_wait_starts_at_latest_ready_event_not_creation(board):
    from hermes_cli import kanban_admission_report as report
    conn, _home = board
    now = 1_800_000_000
    done_at = now - 2 * 86400
    _claimed_run(conn, "add worker", ready_at=done_at - 7200, started_at=done_at - 3600,
                 admission_class="capacity", origin="chat:f", completed_at=done_at)
    _waits(conn, done_at, before_wait=600, after_wait=600)
    # Created long ago but promoted (became ready) only 600 s before each claim: the wait is 600, not 50 000.
    for tid, started in conn.execute("SELECT task_id, started_at FROM task_runs WHERE admission_class='normal'"
                                     ).fetchall():
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET created_at=? WHERE id=?", (started - 50_000, tid))
            conn.execute("UPDATE task_events SET created_at=? WHERE task_id=?", (started - 50_000, tid))
            kb._append_event(conn, tid, "promoted", {})
            conn.execute("UPDATE task_events SET created_at=? WHERE task_id=? AND kind='promoted'",
                         (started - 600, tid))
    entry, = report.capacity_report(conn, now_s=now, days=7)["cards"]
    assert (entry["ready_wait_p50_before_s"], entry["ready_wait_p50_after_s"]) == (600, 600)


_POOLS_FULL = {"regular": 7, "regular_cap": 7, "priority": 0, "priority_cap": 3}
_SCORE = {"total": 1100, "effective_priority": 1000, "class_rank": 0, "descendants": 0, "age_buckets": 1,
          "failures": 0, "weights": {"class": 500, "unblock": 25, "age": 100, "failure": 25}}


def test_pool_note_keeps_non_ordering_dispatcher_skip():
    """An opted-in hub's pool census must not swallow the recorded dispatcher skip (kanban-dd62aef001)."""
    from hermes_cli import kanban_dispatch_skip as skips
    from hermes_cli import kanban_queue_position as qp
    skip = {"reason": "pinned_quota_wall", "detail": "claude-opus-5-5 (anthropic)"}
    note = qp.describe({"lane": "local", "ahead": 0, "running": 7, "limit": 7, "skip": skip,
                        "admission_class": "normal", "admission_pools": dict(_POOLS_FULL),
                        "admission_score": dict(_SCORE)})
    assert note.startswith("last dispatcher skip of this ready episode: " + skips.words(skip)), note
    assert "regular 7/7, priority 0/3" in note, note
    assert "score 1100 (priority 1000" in note, note
    assert "regular pool full (7/7), a normal card waits for it" in note, note
    assert "class normal" in note, note
    assert note.endswith(skips.sentence_advice(skip)), note  # its own sentence after '. ' (PR #1846 review)
    assert "check class/placement" not in note, note


def test_pool_note_ordering_skip_keeps_pool_advice():
    """A slot-ordering skip still names itself, then the class/placement advice (never a bump)."""
    from hermes_cli import kanban_dispatch_skip as skips
    from hermes_cli import kanban_queue_position as qp
    skip = {"reason": "claim_deferred", "detail": "capacity"}
    note = qp.describe({"lane": "local", "ahead": 2, "skip": skip, "admission_class": "normal",
                        "admission_pools": dict(_POOLS_FULL)})
    assert note.startswith("last dispatcher skip of this ready episode: " + skips.words(skip)), note
    assert "regular pool full (7/7)" in note and "class normal" in note, note
    assert "check class/placement" in note and "bump" not in note, note


def test_pool_note_without_skip_is_unchanged():
    from hermes_cli import kanban_queue_position as qp
    note = qp.describe({"lane": "local", "ahead": 0, "admission_class": "capacity",
                        "admission_pools": dict(_POOLS_FULL)})
    assert note.startswith("waiting for a local slot: ~0 ahead, regular 7/7, priority 0/3; class capacity. "), note
    assert "last dispatcher skip" not in note and "pool full" not in note, note
