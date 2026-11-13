"""Read budgets for the gateway's dispatcher tick, and two repair-pass edge cases. [daveey]

hermes-hub 2026-11-12 (12 boards, 600 s at 118% of a core): every board's tick listed every profile with its
gateway liveness twice (7.6% of samples) and opened every other board to count its running cards (6.4%).
"""
from __future__ import annotations

import os
import sqlite3
import time
from pathlib import Path

import pytest

from gateway.kanban_watchers_dispatcher import _KanbanDispatcher, _resolve_dispatcher_settings
from hermes_cli import kanban_board_share as share
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_origin
from hermes_cli import kanban_sync_client as ksc
from hermes_cli import profiles
from tests.hermes_cli import kanban_sync_support as support
from tests.hermes_cli.kanban_sync_fake_board import FakeService

BOARDS = ("default", "b1", "b2", "b3", "b4")


def _scope_open() -> bool:
    return getattr(getattr(share, "_tick", None), "counts", None) is not None


def _tick_scope():
    return share.tick_counts()


@pytest.fixture
def boards(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    for key in ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_HOME"):
        monkeypatch.delenv(key, raising=False)
    kb.init_db()
    for slug in BOARDS[1:]:
        kb.create_board(slug)
    for slug in BOARDS:
        _start(slug)
    return home


def _start(slug: str) -> None:
    with kbc.connect(board=slug) as conn:
        tid = kb.create_task(conn, title="busy", assignee="alice")
        assert kb.claim_task(conn, tid) is not None


def _count_connects(monkeypatch) -> list:
    real, opened = kbc.connect, []

    def counted(*args, **kwargs):
        opened.append(kwargs.get("board"))
        return real(*args, **kwargs)

    monkeypatch.setattr(kbc, "connect", counted)
    return opened


# --- _receipt_scope -------------------------------------------------------------------------------------------------


def test_receipt_scope_lists_names_without_probing_any_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(profiles, "_iter_named_profile_dirs", lambda **kw: [tmp_path / "alice", tmp_path / "bob"])
    probed = []
    monkeypatch.setattr(profiles, "_check_gateway_running", lambda path: probed.append(path) or False)
    monkeypatch.setattr(profiles, "_read_config_model", lambda path: probed.append(path) or (None, None))
    monkeypatch.setattr(kbd, "_profile_exists_fn", lambda: lambda name: name != "bob")
    monkeypatch.setattr(kbd, "review_dispatch_enabled", lambda: True)
    assert _KanbanDispatcher._receipt_scope() == ({"default", "alice"}, {"ready", "review"})
    assert probed == [], f"profile config/liveness read for a name-only scope: {probed}"


# --- other boards' running count ------------------------------------------------------------------------------------


def test_a_tick_reads_each_other_board_about_once_not_once_per_board(boards, monkeypatch):
    opened = _count_connects(monkeypatch)
    with _tick_scope():
        totals = [share.count_running_tasks_other_boards(slug) for slug in BOARDS]
    assert totals == [len(BOARDS) - 1] * len(BOARDS)
    budget = 2 * (len(BOARDS) - 1)
    assert len(opened) <= budget, f"{len(opened)} board opens in one tick over {len(BOARDS)} boards (budget {budget})"


def test_a_tick_counts_the_workers_earlier_boards_started_in_it(boards):
    with _tick_scope():
        assert share.count_running_tasks_other_boards("default") == 4
        _start("default")  # the default board's tick starts a worker
        assert share.count_running_tasks_other_boards("b1") == 5
        _start("b1")
        _start("b1")
        assert share.count_running_tasks_other_boards("b2") == 7
        assert share.count_running_tasks_other_boards("default") == 6


def test_outside_a_tick_every_call_reads_every_board(boards, monkeypatch):
    opened = _count_connects(monkeypatch)
    assert share.count_running_tasks_other_boards("default") == 4
    first = len(opened)
    assert first == len(BOARDS) - 1
    assert share.count_running_tasks_other_boards("default") == 4
    assert len(opened) == 2 * first
    with _tick_scope():
        share.count_running_tasks_other_boards("default")
    assert not _scope_open()
    before = len(opened)
    share.count_running_tasks_other_boards("default")
    assert len(opened) == before + first, "a count outlived its tick"


def test_the_gateway_tick_runs_every_board_inside_one_count_scope(boards, monkeypatch):
    dispatcher = _KanbanDispatcher(kb, _resolve_dispatcher_settings({}, kb))
    monkeypatch.setattr(dispatcher, "_board_slugs", lambda: list(BOARDS))
    scoped = []
    monkeypatch.setattr(dispatcher, "tick_once_for_board", lambda slug, **kw: scoped.append(_scope_open()))
    dispatcher.tick_once()
    dispatcher.tick_once(suppress_spawn=True)
    assert scoped == [True] * (2 * len(BOARDS))
    assert not _scope_open()


# --- gateway_names ---------------------------------------------------------------------------------------------------


def test_gateway_names_does_not_keep_the_answer_of_a_failed_hub_db_read(tmp_path, monkeypatch):
    state = tmp_path / "hub-state"
    state.mkdir()
    monkeypatch.setenv("HERMES_HUB_STATE", str(state))
    monkeypatch.setenv("HERMES_MAIL_CONFIG", str(tmp_path / "absent.json"))
    monkeypatch.setattr(kanban_origin, "_names_memo", None, raising=False)
    db = sqlite3.connect(state / "hub.db")
    db.execute("CREATE TABLE meta (k TEXT PRIMARY KEY, v TEXT)")
    db.execute("CREATE TABLE replica (gw TEXT)")
    db.execute("INSERT INTO meta VALUES ('gateway', 'hub')")
    db.execute("INSERT INTO replica VALUES ('macbook')")
    db.commit()
    db.close()
    old = time.time() - 60
    os.utime(state / "hub.db", (old, old))
    real, calls = kanban_origin._ro, []

    def flaky(path):
        calls.append(str(path))
        if len(calls) == 1:
            raise sqlite3.OperationalError("database is locked")
        return real(path)

    monkeypatch.setattr(kanban_origin, "_ro", flaky)
    assert kanban_origin.gateway_names() == ("", set())
    assert kanban_origin.gateway_names() == ("hub", {"hub", "macbook"}), "a failed read was remembered"
    assert kanban_origin.gateway_names() == ("hub", {"hub", "macbook"})
    assert len(calls) == 2


# --- divergence repair ----------------------------------------------------------------------------------------------


class _Rows(list):
    def fetchall(self):
        return list(self)


class _VanishingReplica:
    """The replica connection, losing one task row right after the pass's first read of its tasks."""

    def __init__(self, conn, task_id):
        self._conn, self._task_id, self._reads = conn, task_id, 0

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def execute(self, sql, *params):
        if self._reads == 0 and "FROM tasks" in sql:
            self._reads = 1
            rows = self._conn.execute(sql, *params).fetchall()
            self._conn.execute("DELETE FROM tasks WHERE id = ?", (self._task_id,))
            return _Rows(rows)
        return self._conn.execute(sql, *params)


@pytest.mark.parametrize("prefilter", [True, False])
def test_divergence_repair_skips_a_row_that_left_the_replica_mid_pass(tmp_path, monkeypatch, prefilter):
    if not prefilter:  # the full Python scan, used when the SQLite pre-filter cannot run
        from hermes_cli import kanban_sync_divergence
        monkeypatch.setattr(kanban_sync_divergence, "_differing_settled", lambda *_a: None)
    home = support.kanban_home(tmp_path, monkeypatch)
    support.write_sync_config(home, True)
    live = kbc.connect(home / "kanban.db")
    me, board = "macbook", "default"
    try:
        ksc.ensure_identity(live, board, me)
        svc = FakeService()
        ids = [kb.create_task(live, title=f"card {i}", assignee="default") for i in range(4)]
        ksc.sync_board(board, home / "kanban.db", svc.hub(me), me)
        real, passes = ksc.repair_divergence, []

        def vanishing(live_conn, shadow, gateway):
            passes.append(1)
            if len(passes) == 1:
                return real(live_conn, _VanishingReplica(shadow, ids[0]), gateway)
            return real(live_conn, shadow, gateway)

        monkeypatch.setattr(ksc, "repair_divergence", vanishing)
        with kbc.write_txn(live):
            sync.drop_triggers(live)
            live.execute("UPDATE tasks SET title = 'gap edit' WHERE id = ?", (ids[-1],))
        sync.install_triggers(live)
        ksc.sync_board(board, home / "kanban.db", svc.hub(me), me)
        assert passes, "the repair scan did not run"
        ksc.sync_board(board, home / "kanban.db", svc.hub(me), me)
        assert svc.board(board)["rows"]["tasks"][ids[-1]]["title"] == "gap edit", \
            "one vanished replica row stopped the pass before the uncaptured edit was healed"
    finally:
        live.close()
