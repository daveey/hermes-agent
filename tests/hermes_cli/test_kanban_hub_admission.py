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
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_queue_reason as queue
from hermes_cli import machine_cpu_metrics as cm
from hermes_cli import machine_metrics as mm
from tests.tools import test_kanban_tools as tool_helpers

pytestmark = pytest.mark.platforms("posix")


@pytest.fixture
def board(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    conn = kbc.connect(tmp_path / "kanban.db")
    try:
        yield conn, tmp_path
    finally:
        conn.close()


def configure(home, limit=50, cap=None):
    lines = ["kanban:"]
    if cap is not None:
        lines.append(f"  max_in_progress: {cap}")
    if limit is not None:
        lines += ["  admission:", f"    cpu_busy_below_pct: {limit}"]
    (home / "config.yaml").write_text("\n".join(lines) + "\n")


def advisory(home, pct=10.0, age_ms=0):
    mm.record_hub_cpu_sample({"cpu_busy_pct": pct, "cpu_busy_window_ms": 1000,
                              "cpu_busy_sample_end_ms": int(time.time() * 1000) - age_ms}, root=home)


def task(conn, review=False, title="gate"):
    t = kb.create_task(conn, title=title, assignee="default")
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status=? WHERE id=?", ("review" if review else "ready", t))
    return t


def claim(conn, t, review):
    return (kb.claim_review_task if review else kb.claim_task)(conn, t)


def fake_sample(pct, calls=None, check=None):
    def sample(*a, **k):
        if check:
            check()
        if calls is not None:
            calls.append(time.monotonic())
        if pct is None:
            return None
        return {"cpu_busy_pct": pct, "cpu_busy_window_ms": 1000, "cpu_busy_sample_end_ms": int(time.time() * 1000)}
    return sample


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
    monkeypatch.setattr(cm, "sample", fake_sample(10))
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
    monkeypatch.setattr(cm, "sample", fake_sample(10))
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
    monkeypatch.setattr(cm, "sample", fake_sample(10))
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
    monkeypatch.setattr(cm, "sample", fake_sample(10))
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
    monkeypatch.setattr(cm, "sample", fake_sample(70))
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
    monkeypatch.setattr(cm, "sample", fake_sample(10))
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
    monkeypatch.setattr(cm, "sample", fake_sample(70))
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


def test_pool_census_uses_one_board_snapshot(board, monkeypatch):
    from hermes_cli import kanban_admission_pools as pools
    conn, home = board
    tid = task(conn)
    original = pools.board_pools

    def concurrent_claim(db, **kwargs):
        with closing(kbc.connect(home / "kanban.db")) as writer:
            with kb.write_txn(writer):
                writer.execute("UPDATE tasks SET status='running' WHERE id=?", (tid,))
        return original(db, **kwargs)

    monkeypatch.setattr(pools, "board_pools", concurrent_claim)
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


def runs(conn):
    return conn.execute("SELECT count(*) FROM task_runs").fetchone()[0]


def refused(conn, t, review, reason):
    assert kb.get_task(conn, t).status == ("review" if review else "ready")
    assert runs(conn) == 0
    assert sync.pop_claim_deferral(t) == reason


@pytest.mark.parametrize("review", [False, True])
def test_gate_absent_admits_without_sampling(board, monkeypatch, review):
    conn, _home = board
    monkeypatch.setattr(cm, "sample", lambda *a, **k: pytest.fail("no root limit: must not sample"))
    t = task(conn, review)
    assert claim(conn, t, review) is not None


@pytest.mark.parametrize("review", [False, True])
def test_below_limit_admits_and_claimed_event_carries_consumed_sample(board, monkeypatch, review):
    conn, home = board
    configure(home)
    advisory(home)
    monkeypatch.setattr(cm, "sample", fake_sample(49.999))
    t = task(conn, review)
    assert claim(conn, t, review) is not None
    payload = json.loads(conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='claimed'",
                                      (t,)).fetchone()[0])
    assert payload["hub_cpu"] == {"cpu_busy_pct": 49.999, "cpu_busy_window_ms": 1000,
                                  "cpu_busy_sample_end_ms": payload["hub_cpu"]["cpu_busy_sample_end_ms"],
                                  "limit_pct": 50.0}


@pytest.mark.parametrize("review", [False, True])
@pytest.mark.parametrize("pct,reason", [(50.0, "hub_cpu_at_or_above_limit"), (50.001, "hub_cpu_at_or_above_limit"),
                                        (None, "hub_cpu_unknown")])
def test_at_or_above_limit_or_unknown_refuses_with_no_local_run(board, monkeypatch, review, pct, reason):
    conn, home = board
    configure(home)
    advisory(home)
    monkeypatch.setattr(cm, "sample", fake_sample(pct))
    t = task(conn, review)
    assert claim(conn, t, review) is None
    refused(conn, t, review, reason)


@pytest.mark.parametrize("review", [False, True])
def test_invalid_root_limit_refuses_without_sampling(board, monkeypatch, review):
    conn, home = board
    configure(home, limit='"fifty"')  # type: ignore[arg-type]
    monkeypatch.setattr(cm, "sample", lambda *a, **k: pytest.fail("invalid limit: refuse before sampling"))
    t = task(conn, review)
    assert claim(conn, t, review) is None
    refused(conn, t, review, "hub_cpu_unknown")


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
    monkeypatch.setattr(cm, "sample", fake_sample(80.0))
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
    monkeypatch.setattr(cm, "sample", fake_sample(10.0, check=check))
    t = task(conn, review)
    assert claim(conn, t, review) is not None
    assert seen == ["held"]


@pytest.mark.parametrize("review", [False, True])
@pytest.mark.parametrize("advice,reason", [(80.0, "hub_cpu_at_or_above_limit"), ("stale", "hub_cpu_unknown"),
                                           ("missing", "hub_cpu_unknown"), ("future", "hub_cpu_unknown")])
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
    monkeypatch.setattr(cm, "sample", lambda *a, **k: pytest.fail("advisory refusal must not sample"))
    t = task(conn, review)
    assert claim(conn, t, review) is None
    refused(conn, t, review, reason)


def test_advisory_age_boundary_15000_ms_is_fresh(board, monkeypatch):
    conn, home = board
    configure(home)
    advisory(home, age_ms=14_900)
    monkeypatch.setattr(cm, "sample", fake_sample(10.0))
    t = task(conn)
    assert kb.claim_task(conn, t) is not None


@pytest.mark.parametrize("review", [False, True])
def test_whole_host_cap_is_rechecked_under_lock(board, monkeypatch, review):
    conn, home = board
    configure(home, cap=2)
    advisory(home)
    monkeypatch.setattr(cm, "sample", fake_sample(10.0))
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
    monkeypatch.setattr(cm, "sample", fake_sample(10.0, check=lambda: monkeypatch.setattr(
        ka, "_now", lambda: real_time() + 10 ** 7)))
    t = task(conn)
    assert kb.claim_task(conn, t) is None
    refused(conn, t, False, "hub_cpu_unknown")


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
    monkeypatch.setattr(cm, "sample", fake_sample(10.0, check=lambda: time.sleep(0.2)))  # health: allow HX013 -- stands in for the ~1 s sample held under the lock
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


@pytest.mark.skipif(not cm.sys.platform.startswith("linux"), reason="real /proc/stat sample")
def test_real_sample_is_entirely_under_lock_bounded_and_measures_chat_turn_cost(board):
    """Frozen contract: an independent FULL real ~1 s /proc/stat sample is taken entirely while admission.lock is held
    (held at its first and last read), the hold stays <= ~2 s, and a chat/notification turn reserving the lock with
    BUSY_WAIT_S (3.0 s) during the sample is measured (its outcome must match the measured hold interval)."""
    from hermes_cli import backend_retirement

    conn, home = board
    configure(home, limit=100)  # the real measured value admits unless the host is 100% busy
    advisory(home)
    t = task(conn)
    m = {}
    started = threading.Event()

    def chat_turn():
        started.wait(5)
        time.sleep(0.2)  # health: allow HX013 -- positions the chat turn inside the claim's real ~1 s sample
        m["chat_try"] = time.monotonic()
        try:
            with af.admission_lock(home, timeout=backend_retirement.BUSY_WAIT_S):
                m["chat"] = "acquired"
        except af.AdmissionBusy:
            m["chat"] = "busy"
        m["chat_done"] = time.monotonic()
    real_sample = cm.sample

    def sample(*a, **k):
        m["lock_at_sample_start"] = _lock_state(home)
        m["sample_start"] = time.monotonic()
        started.set()
        out = real_sample(*a, **k)
        m["sample_end"] = time.monotonic()
        m["lock_at_sample_end"] = _lock_state(home)
        m["sample"] = out
        return out
    cm_sample, cm.sample = cm.sample, sample
    try:
        th = threading.Thread(target=chat_turn)
        th.start()
        t0 = time.monotonic()
        got = kb.claim_task(conn, t)
        m["claim_end"] = time.monotonic()
        th.join(5)
    finally:
        cm.sample = cm_sample
    claim_s = m["claim_end"] - t0
    sample_s = m["sample_end"] - m["sample_start"]
    record = {"claim_s": round(claim_s, 4), "sample_s": round(sample_s, 4),
              "lock_at_sample_start": m["lock_at_sample_start"], "lock_at_sample_end": m["lock_at_sample_end"],
              "sample": m["sample"], "chat": m["chat"], "chat_wait_s": round(m["chat_done"] - m["chat_try"], 4),
              "chat_try_offset_s": round(m["chat_try"] - m["sample_start"], 4),
              "busy_wait_s": backend_retirement.BUSY_WAIT_S}
    print("HUB_ADMISSION_LOCK_MEASURE " + json.dumps(record))
    assert got is not None
    # The whole independent sample ran with admission.lock held (first and last /proc/stat read inside the hold).
    assert m["lock_at_sample_start"] == "held" and m["lock_at_sample_end"] == "held"
    assert m["sample"] is not None and cm.MIN_WINDOW_MS <= m["sample"]["cpu_busy_window_ms"] <= cm.MAX_WINDOW_MS
    assert sample_s >= 1.0
    assert claim_s <= 2.5  # frozen bound: one ~1 s sample (window <= 2 s) plus the commit
    # The chat turn's outcome follows from the measured hold: lock held over [sample_start, claim_end].
    wait_end = m["chat_try"] + backend_retirement.BUSY_WAIT_S
    if m["sample_start"] <= m["chat_try"] and wait_end <= m["sample_end"]:
        assert m["chat"] == "busy" and m["chat_done"] - m["chat_try"] >= backend_retirement.BUSY_WAIT_S
    elif m["chat"] == "acquired":
        assert m["chat_done"] >= m["sample_end"]  # only once the claim's hold had ended
    assert m["chat_done"] - m["chat_try"] <= 2.5


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
    monkeypatch.setattr(cm, "sample", lambda *a, **k: pytest.fail("at the cap: refuse before sampling"))
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
    monkeypatch.setattr(cm, "sample", lambda *a, **k: pytest.fail("deadline passed: refuse before sampling"))
    t = task(conn)
    assert kb.claim_task(conn, t) is None
    refused(conn, t, False, "hub_cpu_unknown")


def _shrink_hold_budget(monkeypatch, budget_s):
    """The designed hold is CLAIM_MAX_HOLD_S - COMMIT_RESERVE_S; shrink it so an overrun is cheap to provoke."""
    monkeypatch.setattr(ka, "COMMIT_RESERVE_S", af.CLAIM_MAX_HOLD_S - budget_s)


@pytest.mark.parametrize("review", [False, True])
def test_hold_that_overruns_its_budget_refuses_instead_of_committing(board, monkeypatch, review):
    """Final review N1: a sample + recount that outlast the designed hold must not commit (turn budget would lose)."""
    conn, home = board
    configure(home)
    advisory(home)
    _shrink_hold_budget(monkeypatch, 0.2)
    monkeypatch.setattr(cm, "sample", fake_sample(10.0, check=lambda: time.sleep(0.3)))  # health: allow HX013 -- a sample that overruns the shrunken hold
    t = task(conn, review)
    assert claim(conn, t, review) is None
    refused(conn, t, review, "hub_cpu_unknown")


@pytest.mark.parametrize("review", [False, True])
def test_slow_pre_sample_checks_leave_no_room_so_it_refuses_before_sampling(board, monkeypatch, review):
    """Final review N1: when the cheap checks already ate the room a nominal 1 s sample needs, never start sampling."""
    conn, home = board
    configure(home)
    advisory(home)
    _shrink_hold_budget(monkeypatch, cm.WINDOW_S + 0.2)

    def slow_count():
        time.sleep(0.3)  # health: allow HX013 -- a slow board scan before the sample
        return 0
    monkeypatch.setattr(mm, "active_card_runs", slow_count)
    monkeypatch.setattr(cm, "sample", lambda *a, **k: pytest.fail("no room for the sample: refuse before sampling"))
    t = task(conn, review)
    assert claim(conn, t, review) is None
    refused(conn, t, review, "hub_cpu_unknown")


def test_claim_inside_the_hold_budget_still_admits(board, monkeypatch):
    """Control for the N1 bound: the default budget admits a normal sample (the bound is not a blanket refusal)."""
    conn, home = board
    configure(home)
    advisory(home)
    assert ka._hold_budget_s() == pytest.approx(af.CLAIM_MAX_HOLD_S - ka.COMMIT_RESERVE_S)
    assert ka._hold_budget_s() >= cm.WINDOW_S + 1.0
    monkeypatch.setattr(cm, "sample", fake_sample(10.0, check=lambda: time.sleep(0.1)))  # health: allow HX013 -- a short stand-in sample
    t = task(conn)
    assert kb.claim_task(conn, t) is not None
