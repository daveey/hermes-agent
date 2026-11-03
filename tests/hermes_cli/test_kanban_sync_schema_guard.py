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
    monkeypatch.setattr(machine_metrics, "Reporter", lambda *args: type("R", (), {"report_due": lambda self: None})())
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
