"""Host running count excludes pool-worker-leased cards from what the file says, in any process. [daveey]

hermes-hub 2026-11-12 10:35Z: from a Kanban worker process, ``hermes kanban show`` said "14/7 running" while the
gateway held 6 (default 5 + paintbot-tools 1). The other 8 were default-board cards leased to worker-1/3/4/6
(``lease_holder`` another gateway, ``claim_lock`` NULL). The worker's fenced ``mode=ro`` connection never installs
sync, so ``board_mode`` said ``off`` and ``held_elsewhere_sql`` excluded nothing. The count now reads the file's
``kanban_sync_state`` (mode + gateway_id) instead of this process's ``_SYNCED_PATHS``.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from urllib.parse import quote

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli import kanban_db_sync as sync
from hermes_cli import kanban_host_counts as khc
from hermes_cli import kanban_lockguard
from hermes_cli import kanban_queue_position as kqp
from tests.hermes_cli import kanban_sync_support as support

OWN = "gw_hub_test"

_ENV = ("HERMES_KANBAN_DB", "HERMES_KANBAN_BOARD", "HERMES_KANBAN_TASK", "HERMES_KANBAN_RUN_ID",
        "HERMES_KANBAN_CLAIM_LOCK", "HERMES_KANBAN_HOME", "HERMES_DELEGATED_CHILD_CONTEXT",
        "HERMES_KANBAN_WORKER_TASK")


@pytest.fixture
def home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    h = support.kanban_home(tmp_path, monkeypatch)
    monkeypatch.setenv("HERMES_PROFILE", "default")
    for key in _ENV:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(kbd, "configured_max_in_progress", lambda: 7)
    support.write_sync_config(h, True)
    kb._INITIALIZED_PATHS.clear()
    kb.init_db()
    kb.create_board("runners")
    monkeypatch.setattr(sync, "_SYNCED_PATHS", set(sync._SYNCED_PATHS))
    return h


def _seed(board: str | None, *, mode: str, own: int, leased: int) -> str:
    """``own`` running cards this gateway claims, ``leased`` running cards another gateway's lease holds, and one
    ready card (returned). Written through a synced connection, as the gateway writes them."""
    with kbc.connect_closing(board=board) as conn:
        sync.set_mode(conn, mode)
        conn.execute("UPDATE kanban_sync_state SET gateway_id = ?", (OWN,))
        for i in range(own):
            tid = kb.create_task(conn, title=f"own {i}", assignee="default")
            conn.execute("UPDATE tasks SET status='running', claim_lock='hub:1', lease_holder=? WHERE id=?",
                         (OWN, tid))
        for i in range(leased):
            tid = kb.create_task(conn, title=f"pool {i}", assignee="default")
            conn.execute("UPDATE tasks SET status='running', claim_lock=NULL, lease_holder=? WHERE id=?",
                         (f"worker-{i % 4 + 1}", tid))
        ready = kb.create_task(conn, title="waiting", assignee="default")
        conn.commit()
    return ready


def _ro(board: str | None) -> sqlite3.Connection:
    """The fenced worker's connection: ``mode=ro``, no init, so the file never joins ``_SYNCED_PATHS``."""
    path = kb.kanban_db_path(board=board).resolve()
    conn = kanban_lockguard.connect(f"file:{quote(str(path))}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _forget_sync() -> None:
    sync._SYNCED_PATHS.clear()


def test_worker_read_of_primary_board_counts_only_own_gateway_cards(home):
    ready = _seed(None, mode="primary", own=5, leased=8)
    _seed("runners", mode="primary", own=1, leased=0)
    _forget_sync()
    conn = _ro(None)
    try:
        assert sync.board_mode(conn) == "off"  # the precondition the incident had: this process never synced it
        assert khc.running_on_host(conn)["running"] == 6
        place = kqp.position(conn, kb.get_task(conn, ready))
    finally:
        conn.close()
    assert (place["running"], place["limit"]) == (6, 7)
    assert "6/7 running" in kqp.describe(place)


def test_other_board_file_read_excludes_pool_worker_leases(home):
    _seed(None, mode="primary", own=5, leased=8)
    ready = _seed("runners", mode="primary", own=1, leased=0)
    _forget_sync()
    conn = _ro("runners")
    try:
        assert khc._running_in_file(kb.kanban_db_path(board="default").resolve()) == 5
        assert khc.running_on_host(conn)["running"] == 6
        assert kqp.position(conn, kb.get_task(conn, ready))["running"] == 6
    finally:
        conn.close()


def test_gateway_process_with_sync_installed_counts_the_same(home):
    _seed(None, mode="primary", own=5, leased=8)
    with kbc.connect_closing() as conn:
        assert sync.board_mode(conn) == "primary"
        assert khc.running_on_host(conn)["running"] == 5


@pytest.mark.parametrize("mode", ["off", "shadow"])
def test_board_not_primary_counts_every_running_card(home, mode):
    _seed(None, mode=mode, own=5, leased=8)
    _forget_sync()
    conn = _ro(None)
    try:
        assert khc.running_on_host(conn)["running"] == 13
    finally:
        conn.close()


def test_board_without_sync_state_table_counts_every_running_card(home):
    _seed(None, mode="primary", own=5, leased=8)
    with kbc.connect_closing() as conn:
        conn.execute("DROP TABLE kanban_sync_state")
        conn.commit()
    _forget_sync()
    conn = _ro(None)
    try:
        assert khc.running_on_host(conn)["running"] == 13
    finally:
        conn.close()


def test_sync_state_without_a_row_counts_every_running_card(home):
    _seed(None, mode="primary", own=5, leased=8)
    with kbc.connect_closing() as conn:
        conn.execute("DELETE FROM kanban_sync_state")
        conn.commit()
    _forget_sync()
    conn = _ro(None)
    try:
        assert khc.running_on_host(conn)["running"] == 13
    finally:
        conn.close()


def test_unreadable_sync_state_makes_the_count_unknown(home):
    _seed(None, mode="primary", own=5, leased=8)
    with kbc.connect_closing() as conn:
        conn.execute("DROP TABLE kanban_sync_state")
        conn.execute("CREATE TABLE kanban_sync_state (board_id TEXT)")  # no mode/gateway_id columns
        conn.commit()
    _forget_sync()
    conn = _ro(None)
    try:
        assert khc.running_on_host(conn)["running"] is None
    finally:
        conn.close()


def test_primary_state_without_a_gateway_id_makes_the_count_unknown(home):
    _seed(None, mode="primary", own=5, leased=8)
    with kbc.connect_closing() as conn:
        conn.execute("UPDATE kanban_sync_state SET gateway_id = ''")
        conn.commit()
    _forget_sync()
    conn = _ro(None)
    try:
        assert khc.running_on_host(conn)["running"] is None
    finally:
        conn.close()


def test_a_leased_card_this_gateway_still_claims_is_a_local_slot(home):
    """``claim_lock`` set means a local worker runs it even when a foreign lease row lingers."""
    _seed(None, mode="primary", own=0, leased=0)
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="claimed here", assignee="default")
        conn.execute("UPDATE tasks SET status='running', claim_lock='hub:1', lease_holder='worker-1' WHERE id=?",
                     (tid,))
        tid2 = kb.create_task(conn, title="no lease", assignee="default")
        conn.execute("UPDATE tasks SET status='running', claim_lock=NULL, lease_holder=NULL WHERE id=?", (tid2,))
        conn.commit()
    _forget_sync()
    conn = _ro(None)
    try:
        assert khc.running_on_host(conn)["running"] == 2
    finally:
        conn.close()
