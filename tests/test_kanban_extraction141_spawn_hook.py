"""E5/N3: the generic tier-fault branch's in-memory task is what spawn AND the worker-spawned hook observe.

Recording-fixture evidence only (dispatch_once with spawn_fn); not live canonical dispatcher proof.
The stored row stays author evidence; the review fault branch is covered by the copied readers suite.
"""
from __future__ import annotations

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from tests.test_kanban_subset130_readers import RAW_MODEL, RAW_PROVIDER, _fault, _legacy, _row, _spawn_once
from tests.test_kanban_subset130_readers import board as board  # noqa: PLC0414 -- fixture re-export
from tests.test_kanban_subset130_readers import wallfile as wallfile  # noqa: PLC0414 -- fixture re-export


def _record_hook(monkeypatch) -> dict:
    seen: dict = {}

    def hook(conn, task, workspace_path, pid, *, board=None):
        seen[task.id] = (task.model_override, task.provider_override, getattr(task, "model_pin_mandatory", None))
    monkeypatch.setattr(kb, "_fire_worker_spawned_hook", hook)
    return seen


def test_fault_hook_sees_effective_selector_and_cleared_mandatory(board, wallfile, monkeypatch):
    conn = kbc.connect()
    tid = kb.create_task(conn, title="legacy", assignee="default")
    _legacy(conn, tid, mandatory=1)
    _fault(monkeypatch)
    hooked = _record_hook(monkeypatch)
    spawned = _spawn_once(conn)
    assert spawned[tid] == hooked[tid] == ("class:standard", "auto", None)
    assert _row(conn, tid) == (RAW_MODEL, RAW_PROVIDER, 1)  # stored author evidence untouched


def test_fault_hook_on_neutral_card_matches_spawn(board, wallfile, monkeypatch):
    """N3: neutral (no-override) cards also become class:standard/auto in memory on the fault branch."""
    conn = kbc.connect()
    tid = kb.create_task(conn, title="neutral", assignee="default")
    _fault(monkeypatch)
    hooked = _record_hook(monkeypatch)
    spawned = _spawn_once(conn)
    assert spawned[tid] == hooked[tid] == ("class:standard", "auto", None)
    assert _row(conn, tid) == (None, None, None)
