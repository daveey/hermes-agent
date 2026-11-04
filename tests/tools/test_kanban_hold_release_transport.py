"""Release receipt transport and cross-board tool entry paths."""
import json
import time

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_db_dispatch as dispatch
from hermes_cli.kanban_hold_release import release
from tests.hermes_cli import test_kanban_dependency_review_sync as sync_tests
from tests.tools import test_kanban_owner_wake_hold_release as cases
from tests.tools import test_kanban_owner_wake_requeue as wake

boards = sync_tests.boards
board = wake.board


def test_receipt_survives_remapped_ids_and_is_consumed(boards, monkeypatch):
    source, target = boards
    task, _, sup = cases.held(source, monkeypatch)
    now = int(time.time()) + 2
    monkeypatch.setattr(time, "time", lambda: now)
    release(source, task, supervisor=sup, supervisor_board="default", precondition=lambda _: None)
    sync_tests.transport(source, target)
    src, dst = sync_tests.checkpoint_row(source, task), sync_tests.checkpoint_row(target, task)
    assert src["run_id"] != dst["run_id"]
    assert dispatch.check_respawn_guard(target, task) is None
    assert kb.claim_task(target, task) is not None
    assert dispatch.check_respawn_guard(target, task) == "active_pr"


def test_cross_board_ready_release(board, monkeypatch):
    from tools import kanban_tools as kt
    kb.create_board("ops")
    with kbc.connect_closing() as own, kbc.connect_closing(board="ops") as target_conn:
        task, _, _ = cases.held(target_conn, monkeypatch)
        # Rebind to the real caller on default; target owns its review history.
        monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
        monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
        sup = wake._supervisor(own, kb, [wake._line(f"ops/{task}")])
        monkeypatch.setenv("HERMES_KANBAN_DB", str(kb.kanban_db_path()))
        wake._as_worker(monkeypatch, own, kb, sup)
        now = int(time.time()) + 2
        monkeypatch.setattr(time, "time", lambda: now)
        assert not json.loads(kt._handle_unblock({"task_id": task, "board": "default"})).get("ok")
        out = json.loads(kt._handle_unblock({"task_id": task, "board": "ops"}))
        assert out.get("ok"), out
        assert out["via"] == "owner-wake-hold-release"
        assert dispatch.check_respawn_guard(target_conn, task) is None
        assert not any(e.kind == "hold_released" for e in kb.list_events(own, sup))
