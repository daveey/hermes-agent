"""A sync client whose core is older than the board's schema must not push. [daveey]

Incident (2026-11-02, hermes-hub): the askeve-reviewer gateway ran a pinned core (1f926111) that predates
``placement_policy`` in ``SYNCED_COLUMNS["tasks"]`` (#461). It won the host-wide ``.sync.lock`` and pushed every
hub card. ``_wire_fields`` drops a column the client does not declare, so the Board row of every worker-first card
lacked ``placement_policy``; the pull then wrote NULL back locally and the cards ran on the hub instead of a pool
worker. Nothing failed loudly.

The guard: a board whose tables hold a column this core neither syncs nor keeps local was migrated by a newer core.
``push`` refuses (``SchemaAhead``), leaves the outbox pending for a core that knows the column, and
``run_forever`` gives the sync role up so a current gateway can take it.
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_sync_client as ksc
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli._placement_helpers import seen_now
from tests.hermes_cli.kanban_sync_fake_board import FakeService

ME, BOARD = "hub", "default"


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    for var in ("HERMES_SESSION_ID", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID"):
        monkeypatch.delenv(var, raising=False)
    return support.kanban_home(tmp_path, monkeypatch)


@pytest.fixture
def svc():
    return FakeService()


@pytest.fixture
def live(kanban_home):
    support.write_sync_config(kanban_home, True)
    conn = kbc.connect(kanban_home / "kanban.db")
    ksc.ensure_identity(conn, BOARD, ME)
    with kbc.write_txn(conn):
        conn.execute("UPDATE kanban_sync_state SET mode = 'primary'")
    try:
        yield conn
    finally:
        conn.close()


def _old_core(monkeypatch) -> None:
    """This process now declares the columns of a core from before #461 (no ``placement_policy``)."""
    monkeypatch.setitem(sync.SYNCED_COLUMNS, "tasks", sync.SYNCED_COLUMNS["tasks"] - {"placement_policy"})


def _board_row(svc, tid):
    return svc.board(BOARD)["rows"].get("tasks", {}).get(tid)


def _pending(conn):
    return conn.execute("SELECT COUNT(*) FROM kanban_sync_outbox WHERE state = 'pending'").fetchone()[0]


def test_current_core_pushes_placement_policy_to_the_board(live, svc):
    tid = kb.create_task(live, title="Summarize public data", assignee="default",
                         placement_policy="worker-first-v1")
    ksc.push(live, svc.hub(ME), BOARD, ME, primary=True)
    row = _board_row(svc, tid)
    assert row["placement_policy"] == "worker-first-v1"
    assert "placement_policy" in row["field_hlc"]
    assert _pending(live) == 0


def test_tool_created_worker_first_card_keeps_placement_policy_on_the_board(kanban_home, live, svc, monkeypatch):
    """The live path: ``kanban_create`` with the default placement on a gateway with a worker pool."""
    from tests.tools.test_kanban_create_overlap import register_chat
    monkeypatch.setenv("HERMES_SESSION_ID", register_chat(kanban_home))
    (kanban_home / "config.yaml").write_text(
        "kanban:\n  sync:\n    enabled: true\n  placement:\n    worker_pools:\n      hub: [worker-1]\n")
    from hermes_cli import kanban_capability_registry as reg
    monkeypatch.setattr(reg, "self_gateway", lambda: ME)
    from tools import kanban_tools as kt
    out = json.loads(kt._handle_create({"title": "Summarize public data", "assignee": "default",
                                        "requires": ["terminal"]}))
    assert out.get("placement", {}).get("placement_policy") == "worker-first-v1", out
    tid = out["task_id"]
    assert live.execute("SELECT placement_policy FROM tasks WHERE id = ?", (tid,)).fetchone()[0] == "worker-first-v1"
    ksc.push(live, svc.hub(ME), BOARD, ME, primary=True)
    assert _board_row(svc, tid)["placement_policy"] == "worker-first-v1"


def test_schema_ahead_names_the_unknown_columns(live, monkeypatch):
    assert ksc.schema_ahead(live) == {}
    _old_core(monkeypatch)
    assert ksc.schema_ahead(live) == {"tasks": {"placement_policy"}}


def test_older_core_refuses_to_push_and_keeps_the_ops(live, svc, monkeypatch):
    tid = kb.create_task(live, title="Summarize public data", assignee="default",
                         placement_policy="worker-first-v1")
    _old_core(monkeypatch)
    with pytest.raises(ksc.SchemaAhead) as err:
        ksc.push(live, svc.hub(ME), BOARD, ME, primary=True)
    assert "placement_policy" in str(err.value)
    assert _board_row(svc, tid) is None  # nothing reached the Board without the column
    assert _pending(live) >= 1  # kept for a core that declares it
    monkeypatch.undo()
    ksc.push(live, svc.hub(ME), BOARD, ME, primary=True)
    assert _board_row(svc, tid)["placement_policy"] == "worker-first-v1"


def test_older_core_round_reports_the_refusal(kanban_home, live, svc, monkeypatch):
    kb.create_task(live, title="x", assignee="default", placement_policy="worker-first-v1")
    _old_core(monkeypatch)
    results = ksc.sync_all_once(svc.hub(ME), boards=[(BOARD, kanban_home / "kanban.db")])
    assert results[0].error and results[0].error.startswith("SchemaAhead")


def test_run_forever_gives_up_the_sync_role_on_an_older_core(kanban_home, live, svc, monkeypatch, caplog):
    _old_core(monkeypatch)
    hub = svc.hub(ME)
    monkeypatch.setattr(ksc, "load_identity", lambda: hub.ident)
    monkeypatch.setattr(ksc, "Hub", lambda ident: hub)
    monkeypatch.setattr(ksc, "board_paths", lambda: [(BOARD, kanban_home / "kanban.db")])
    monkeypatch.setattr(ksc, "FeedWatcher", lambda *a, **k: pytest.fail("feed started on an older core"))
    ticks = iter(range(3))
    with caplog.at_level("ERROR", logger=ksc._log.name):
        ksc.run_forever(lambda: next(ticks, None) is not None, sleep=lambda _s: None)
    assert any("placement_policy" in r.getMessage() and r.levelname == "ERROR" for r in caplog.records)


# Incident (2026-11-02, MacBook): the guard read the two columns a pre-#20410 core created as "a newer core
# migrated this board". The rename migration is ADD-first (``kanban_db_connect._RENAMED_TASK_COLUMNS``): it adds
# ``consecutive_failures`` / ``last_failure_error``, fills them from ``spawn_failures`` / ``last_spawn_error`` and
# keeps those. So every board older than the rename holds them, and the current core's gateway logged
# "kanban sync: not starting: board default: ... (tasks: last_spawn_error, spawn_failures)", leaving the MacBook's
# outbox unpushed and its dispatcher's lease claims "not_found".
_PRE_RENAME_SCHEMA = """
CREATE TABLE tasks (
    id TEXT PRIMARY KEY, title TEXT NOT NULL, body TEXT, assignee TEXT, status TEXT NOT NULL,
    priority INTEGER DEFAULT 0, created_by TEXT, created_at INTEGER NOT NULL, started_at INTEGER,
    completed_at INTEGER, workspace_kind TEXT NOT NULL DEFAULT 'scratch', workspace_path TEXT, claim_lock TEXT,
    claim_expires INTEGER, tenant TEXT, result TEXT, idempotency_key TEXT,
    spawn_failures INTEGER NOT NULL DEFAULT 0, worker_pid INTEGER, last_spawn_error TEXT
);
CREATE TABLE task_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, kind TEXT NOT NULL, payload TEXT,
    created_at INTEGER NOT NULL
);
"""


@pytest.fixture
def pre_rename_live(kanban_home):
    """``live`` on a board a pre-#20410 core created, opened (and migrated) by this core."""
    raw = sqlite3.connect(str(kanban_home / "kanban.db"))
    raw.executescript(_PRE_RENAME_SCHEMA)
    raw.close()
    support.write_sync_config(kanban_home, True)
    conn = kbc.connect(kanban_home / "kanban.db")
    ksc.ensure_identity(conn, BOARD, ME)
    with kbc.write_txn(conn):
        conn.execute("UPDATE kanban_sync_state SET mode = 'primary'")
    try:
        yield conn
    finally:
        conn.close()


def test_board_migrated_from_before_the_rename_is_not_schema_ahead(pre_rename_live):
    cols = {r[1] for r in pre_rename_live.execute("PRAGMA table_info(tasks)")}
    assert {"spawn_failures", "last_spawn_error", "consecutive_failures", "last_failure_error"} <= cols
    assert ksc.schema_ahead(pre_rename_live) == {}


def test_board_migrated_from_before_the_rename_pushes(pre_rename_live, svc):
    tid = kb.create_task(pre_rename_live, title="Summarize public data", assignee="default")
    ksc.push(pre_rename_live, svc.hub(ME), BOARD, ME, primary=True)
    row = _board_row(svc, tid)
    assert row is not None and row["title"] == "Summarize public data"
    assert "spawn_failures" not in row and "last_spawn_error" not in row  # retired: never travel
    assert _pending(pre_rename_live) == 0


def test_run_forever_starts_on_a_board_migrated_from_before_the_rename(kanban_home, pre_rename_live, svc,
                                                                       monkeypatch, caplog):
    """The entry path the MacBook gateway refused at: run_forever's start check, then a real round."""
    from hermes_cli import machine_metrics

    hub = svc.hub(ME)
    monkeypatch.setattr(ksc, "load_identity", lambda: hub.ident)
    monkeypatch.setattr(ksc, "Hub", lambda ident: hub)
    monkeypatch.setattr(ksc.sync, "set_lease_client", lambda value: None)  # no process-global lease client leaks
    monkeypatch.setattr(ksc, "board_paths", lambda: [(BOARD, kanban_home / "kanban.db")])
    monkeypatch.setattr(ksc, "reconcile_links", lambda *args, **kwargs: None)
    monkeypatch.setattr(machine_metrics, "Reporter", lambda *args: type("R", (), {"report_due": lambda self: None, "pending_wake_in": lambda self: None})())
    monkeypatch.setattr(hub, "report_metrics", lambda *args: None, raising=False)
    feeds = []

    class Feed:
        def __init__(self, ident, slug, *args, **kwargs):
            feeds.append(slug)

        def run(self):
            return None

    monkeypatch.setattr(ksc, "FeedWatcher", Feed)
    running = [True]
    monkeypatch.setattr(ksc, "wait_for_work", lambda *args: running.__setitem__(0, False))
    tid = kb.create_task(pre_rename_live, title="x", assignee="default")
    with caplog.at_level("ERROR", logger=ksc._log.name):
        ksc.run_forever(lambda: running[0], interval=1)
    assert [r.getMessage() for r in caplog.records if r.levelname == "ERROR"] == []
    assert feeds == [BOARD]
    assert _board_row(svc, tid) is not None


def test_retired_columns_do_not_hide_a_newer_cores_column(pre_rename_live):
    with kbc.write_txn(pre_rename_live):
        pre_rename_live.execute("ALTER TABLE tasks ADD COLUMN upstream_new_field TEXT")
    assert ksc.schema_ahead(pre_rename_live) == {"tasks": {"upstream_new_field"}}


def test_every_rename_source_column_is_declared_retired():
    """A rename migration that keeps its old column must declare it retired: otherwise every board older than the
    rename reads as schema-ahead and its gateway stops syncing."""
    sources = {src for _new, _ddl, src, _copy in kbc._RENAMED_TASK_COLUMNS if src}
    assert sources and sources == sync.RETIRED_COLUMNS["tasks"]
    assert not sync.RETIRED_COLUMNS["tasks"] & (sync.SYNCED_COLUMNS["tasks"] | sync.LOCAL_COLUMNS["tasks"])


# Incident (2026-11-02, MacBook), the detection half: after that refusal the loop never respun, every claim went
# through the Board lease, and the board stalled (52 outbox ops unpushed, 15 ready, 0 running). The stuck warning
# named only ``claim_deferred`` counts, ``sync status`` printed ``last_error: None`` and ``kanban_create`` kept
# returning local ``runs_on`` rows. ``run_forever`` now records why it is not running (kanban_sync_runstate).

def _runstate():
    from hermes_cli import kanban_sync_runstate
    return kanban_sync_runstate


def _run_sync_loop(kanban_home, svc, monkeypatch, rounds=1):
    """``run_forever`` against the fake Board for ``rounds`` completed rounds (or its start refusal). Returns the
    recorded sync state seen while the loop was still running, one per round."""
    from hermes_cli import machine_metrics

    hub = svc.hub(ME)
    monkeypatch.setattr(ksc, "load_identity", lambda: hub.ident)
    monkeypatch.setattr(ksc, "Hub", lambda ident: hub)
    monkeypatch.setattr(ksc.sync, "set_lease_client", lambda value: None)
    monkeypatch.setattr(ksc, "board_paths", lambda: [(BOARD, kanban_home / "kanban.db")])
    monkeypatch.setattr(ksc, "reconcile_links", lambda *args, **kwargs: None)
    monkeypatch.setattr(machine_metrics, "Reporter", lambda *args: type("R", (), {"report_due": lambda self: None, "pending_wake_in": lambda self: None})())
    monkeypatch.setattr(hub, "report_metrics", lambda *args: None, raising=False)

    class Feed:
        def __init__(self, *args, **kwargs):
            pass

        def run(self):
            return None

    monkeypatch.setattr(ksc, "FeedWatcher", Feed)
    left = [rounds]
    seen = []

    def wait(*args):
        seen.append(_runstate().describe())
        left[0] -= 1

    monkeypatch.setattr(ksc, "wait_for_work", wait)
    ksc.run_forever(lambda: left[0] > 0, interval=1)
    return seen


def _deferred_tick():
    """The stuck line of a tick shaped like the MacBook's (claims deferred, cards not leasable here)."""
    from hermes_cli import kanban_db_dispatch as kbd
    res = kbd.DispatchResult()
    res.claim_deferred.append(("t_a", "not_found"))
    res.claim_deferred.append(("t_b", "not_claimable"))
    res.skipped_ineligible.append(("t_c", "labels_mismatch"))
    return kbd.stuck_warning(6, [res])


def _refuse_start(kanban_home, pre_rename_live, svc, monkeypatch):
    with kbc.write_txn(pre_rename_live):
        pre_rename_live.execute("ALTER TABLE tasks ADD COLUMN upstream_new_field TEXT")
    assert _run_sync_loop(kanban_home, svc, monkeypatch) == []  # refused: no round ran


def test_refused_sync_start_is_named_by_the_stuck_dispatcher_warning(kanban_home, pre_rename_live, svc, monkeypatch):
    _refuse_start(kanban_home, pre_rename_live, svc, monkeypatch)
    msg = _deferred_tick()
    assert "claim_deferred(not_found)=1" in msg
    assert "kanban sync not running since " in msg
    assert "upstream_new_field" in msg and "board default" in msg, msg


def test_refused_sync_start_is_named_by_sync_status(kanban_home, pre_rename_live, svc, monkeypatch, capsys):
    import argparse
    from hermes_cli import kanban_sync_cli
    _refuse_start(kanban_home, pre_rename_live, svc, monkeypatch)
    assert ksc.status(pre_rename_live)["running"].startswith("no: kanban sync not running since ")
    assert kanban_sync_cli._status(argparse.Namespace(board=BOARD, json=False)) == 0
    out = capsys.readouterr().out
    assert "running: no: kanban sync not running since" in out and "upstream_new_field" in out, out


def test_healthy_round_clears_the_sync_state(kanban_home, pre_rename_live, svc, monkeypatch):
    _refuse_start(kanban_home, pre_rename_live, svc, monkeypatch)
    assert "kanban sync not running" in _deferred_tick()
    with kbc.write_txn(pre_rename_live):
        pre_rename_live.execute("ALTER TABLE tasks DROP COLUMN upstream_new_field")  # the newer core's column gone
    assert _run_sync_loop(kanban_home, svc, monkeypatch) == [""]  # cleared by the round, while the loop runs
    _runstate().clear()  # drop the loop's own exit record (covered below): this asserts the running state
    msg = _deferred_tick()
    assert "kanban sync not running" not in msg and "runs_on / placement" in msg
    assert ksc.status(pre_rename_live)["running"].startswith("yes")


def test_sync_loop_exit_while_enabled_is_recorded(kanban_home, pre_rename_live, svc, monkeypatch):
    _run_sync_loop(kanban_home, svc, monkeypatch)
    assert "stopped" in _runstate().describe()


def test_sync_state_is_ignored_when_sync_is_off(kanban_home, pre_rename_live, svc, monkeypatch):
    _refuse_start(kanban_home, pre_rename_live, svc, monkeypatch)
    support.write_sync_config(kanban_home, False)
    assert _runstate().describe() == ""
    assert "kanban sync not running" not in _deferred_tick()


@pytest.fixture
def machine_pin_create(kanban_home, pre_rename_live, monkeypatch):
    """A primary board whose registry lists worker-1 as a machine that picks up other gateways' cards."""
    from tests.tools.test_kanban_create_overlap import register_chat
    monkeypatch.setenv("HERMES_SESSION_ID", register_chat(kanban_home))
    from hermes_cli import kanban_capability_registry as reg
    from hermes_cli import kanban_worker_node as wn
    monkeypatch.setattr(reg, "self_gateway", lambda: ME)
    wn.set_registry_fetch(lambda: {"me": ME, "machines": seen_now([
        {"name": "worker-1", "state": "active", "labels": ["linux"], "pickup": "all"}])})
    monkeypatch.setenv("HERMES_KANBAN_DB", str(kanban_home / "kanban.db"))
    monkeypatch.setenv("HERMES_KANBAN_BOARD", BOARD)
    monkeypatch.delenv("HERMES_DELEGATED_CHILD_CONTEXT", raising=False)
    from tools import kanban_tools as kt
    try:
        yield kt
    finally:
        wn.set_registry_fetch(None)


def _runs_on_rows(conn):
    return conn.execute("SELECT id, runs_on FROM tasks WHERE runs_on IS NOT NULL").fetchall()


def test_machine_pin_create_with_sync_running_makes_a_runs_on_row(machine_pin_create, pre_rename_live):
    out = json.loads(machine_pin_create._handle_create({"title": "Summarize public data", "assignee": "default",
                                                        "placement": "worker-1", "requires": []}))
    assert out.get("runs_on") == ["machine:worker-1"], out


@pytest.mark.parametrize("pin", [{"placement": "worker-1"}, {"runs_on": ["machine:worker-1"]}])
def test_machine_pin_create_while_sync_is_down_never_returns_a_stranded_row(machine_pin_create, pre_rename_live,
                                                                            pin):
    _runstate().record_down("not starting: board default: SchemaAhead(test)")
    out = json.loads(machine_pin_create._handle_create({"title": "Summarize public data", "assignee": "default",
                                                        "requires": [], **pin}))
    assert _runs_on_rows(pre_rename_live) == [], out
    assert "error" in out, out


def test_machine_pin_create_while_sync_is_down_takes_the_gateway_relay(machine_pin_create, pre_rename_live,
                                                                      monkeypatch):
    """A name that is both a gateway and an enrolled machine (``hub``) goes through the placement relay."""
    from hermes_cli import kanban_placement as kp
    sent = []
    monkeypatch.setattr(kp, "target_live", lambda target, **kw: None)  # the gateway answers the relay
    monkeypatch.setattr(kp, "create_remote", lambda view, **kw: sent.append(kw["target"]) or
                        {"ok": True, "request_id": "r_1", "queued_only": True})
    _runstate().record_down("not starting: board default: SchemaAhead(test)")
    out = json.loads(machine_pin_create._handle_create({"title": "Summarize public data", "assignee": "default",
                                                        "requires": [], "placement": "worker-1"}))
    assert sent == ["worker-1"] and out.get("status") == "queued", out
    assert _runs_on_rows(pre_rename_live) == []


# #779 follow-up: each test below kills a mutant of the sync-down paths that the tests above let survive. [daveey]

def test_sync_start_refused_for_no_identity_is_recorded(kanban_home, pre_rename_live, monkeypatch):
    """``load_identity`` → ``SyncUnavailable``: the start refusal names the reason (mutant: no record)."""
    def no_identity():
        raise ksc.SyncUnavailable("kanban sync has no Board key on this host")

    monkeypatch.setattr(ksc, "load_identity", no_identity)
    monkeypatch.setattr(ksc, "FeedWatcher", lambda *a, **k: pytest.fail("feed started without an identity"))
    ksc.run_forever(lambda: True, interval=1)
    described = _runstate().describe()
    assert "not starting: kanban sync has no Board key on this host" in described, described
    assert "not starting: kanban sync has no Board key on this host" in _deferred_tick()


def test_mid_loop_schema_ahead_give_up_is_recorded_and_not_overwritten(kanban_home, pre_rename_live, svc,
                                                                       monkeypatch):
    """A round that returns ``SchemaAhead`` mid-loop gives the role up; the record says so, not ``stopped``."""
    from types import SimpleNamespace
    rounds = []
    monkeypatch.setattr(ksc, "sync_all_once", lambda hub, boards: rounds.append(boards) or [
        SimpleNamespace(board=BOARD, error="SchemaAhead: tasks: upstream_new_field", push=None, mode="primary",
                        pulled=0)])
    assert _run_sync_loop(kanban_home, svc, monkeypatch, rounds=3) == []  # gave up after the first round
    assert len(rounds) == 1
    described = _runstate().describe()
    assert "gave up the sync role: board default: SchemaAhead: tasks: upstream_new_field" in described, described
    assert "stopped" not in described


@pytest.mark.parametrize("failure", [None, RuntimeError, KeyboardInterrupt])
def test_loop_cpu_early_exit_labels_and_partitions(kanban_home, pre_rename_live, svc, monkeypatch, failure):
    from hermes_cli import kanban_sync_cpu as cpu
    from types import SimpleNamespace

    clock, records = [0.0], []
    monkeypatch.setattr(cpu.time, "thread_time", lambda: clock[0])
    monkeypatch.setattr(cpu.loop_accounting, "record", lambda name, **kw: records.append((name, kw)))
    boom = failure("round failed") if failure else None

    @cpu.board_call
    def board_call():
        clock[0] += 3

    def round_once(hub, boards):
        clock[0] += 2
        board_call()
        if boom is not None:
            raise boom
        return [SimpleNamespace(board=BOARD, error="SchemaAhead: tasks: newer_field")]

    monkeypatch.setattr(ksc, "sync_all_once", round_once)
    if failure:
        with pytest.raises(failure) as raised:
            _run_sync_loop(kanban_home, svc, monkeypatch, rounds=3)
        assert raised.value is boom
    else:
        assert _run_sync_loop(kanban_home, svc, monkeypatch, rounds=3) == []
    loop = [(name, data) for name, data in records if name.endswith("/loop")]
    assert len(loop) == 3  # exactly one completed run in each partition
    assert {name: data["cpu_s"] for name, data in loop} == {
        "sync.cpu.local/loop": 2, "sync.cpu.board_call/loop": 3, "sync.cpu.total/loop": 5}
    assert all(data["error"] == bool(failure) for _, data in loop)
    assert all(data["error_kind"] == (failure.__name__ if failure else None) for _, data in loop)
    assert cpu._local.frames == ()
    assert not cpu._local.in_call


def test_sync_loop_crash_is_recorded_and_reraised(kanban_home, pre_rename_live, svc, monkeypatch):
    boom = RuntimeError("board exploded")

    def crash(hub, boards):
        raise boom

    monkeypatch.setattr(ksc, "sync_all_once", crash)
    with pytest.raises(RuntimeError) as err:
        _run_sync_loop(kanban_home, svc, monkeypatch)
    assert err.value is boom
    described = _runstate().describe()
    assert "crashed: RuntimeError: board exploded" in described, described


def _heavy_create(kt, monkeypatch):
    """A heavy-marker card with no placement on a routeless gateway whose ``heavy_route`` names worker-1."""
    from hermes_cli import kanban_placement as kp
    monkeypatch.setattr(kp, "config", lambda: {"heavy_route": {ME: "worker-1"}})
    return json.loads(kt._handle_create({"title": "Run the pytest regression suite", "assignee": "default",
                                         "requires": []}))


def test_heavy_create_with_sync_running_makes_a_runs_on_row(machine_pin_create, pre_rename_live, monkeypatch):
    out = _heavy_create(machine_pin_create, monkeypatch)
    assert out.get("runs_on") == ["machine:worker-1"], out
    assert len(_runs_on_rows(pre_rename_live)) == 1


def test_heavy_create_while_sync_is_down_never_returns_a_runs_on_row(machine_pin_create, pre_rename_live,
                                                                     monkeypatch):
    """``decide`` → ``_routeless_heavy`` → ``runs_on=[machine:worker-1]``: both the machine check and the stranded
    check after ``_local_placement`` must keep that row off a board whose sync is down."""
    _runstate().record_down("not starting: board default: SchemaAhead(test)")
    out = _heavy_create(machine_pin_create, monkeypatch)
    assert _runs_on_rows(pre_rename_live) == [], out
    assert not out.get("runs_on"), out


def _courier_intent():
    return {"request_id": "request-sync-down-001",
            "owner": {"gateway": ME, "profile": "default", "board": BOARD},
            "origin": {"gateway": "origin", "profile": "work", "session": "s_origin"}}


def test_courier_runs_on_create_while_sync_is_down_is_created(machine_pin_create, pre_rename_live):
    """A courier create keeps its own retry, so a stopped sync does not refuse it."""
    _runstate().record_down("not starting: board default: SchemaAhead(test)")
    args = {"title": "Summarize public data", "assignee": "default", "requires": [],
            "runs_on": ["machine:worker-1"]}
    out = json.loads(machine_pin_create._handle_create(args, origin_intent=_courier_intent()))
    assert out.get("task_id"), out
    assert [json.loads(r[1]) for r in _runs_on_rows(pre_rename_live)] == [["machine:worker-1"]], out


def test_non_courier_runs_on_create_while_sync_is_down_is_refused(machine_pin_create, pre_rename_live):
    _runstate().record_down("not starting: board default: SchemaAhead(test)")
    out = json.loads(machine_pin_create._handle_create({"title": "Summarize public data", "assignee": "default",
                                                        "requires": [], "runs_on": ["machine:worker-1"]}))
    assert out.get("rejection") == "sync_not_running", out
    assert "kanban sync not running since" in out.get("error", ""), out
    assert _runs_on_rows(pre_rename_live) == []


def test_record_down_keeps_since_for_the_same_reason_and_resets_it_for_a_new_one(kanban_home, pre_rename_live,
                                                                                 monkeypatch):
    import time as _time
    rs = _runstate()
    now = [1000.0]
    monkeypatch.setattr(_time, "time", lambda: now[0])

    def since():
        return json.loads(rs.path().read_text())["since"]

    rs.record_down("not starting: board default: SchemaAhead(a)")
    assert since() == 1000.0
    now[0] = 2000.0
    rs.record_down("not starting: board default: SchemaAhead(a)")  # a respun refusal: same reason
    assert since() == 1000.0
    now[0] = 3000.0
    rs.record_down("not starting: board default: SchemaAhead(b)")  # a different reason
    assert since() == 3000.0
