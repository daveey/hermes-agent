"""The embedded dispatcher's host cap follows config.yaml without a gateway restart. [daveey]

Regression: hermes-hub booted at max_in_progress=16 (04:18Z), config was lowered to 7 at 05:15Z,
and the gateway kept claiming to 16/16 because ``_DispatcherSettings`` froze the cap at boot.
"""
import os
import time

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_dispatch as kbd
from hermes_cli.kanban_db_dispatch import DispatchResult


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "home"
    root.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    return root


def _write_config(home, body: str) -> None:
    path = home / "config.yaml"
    old = path.stat().st_mtime_ns if path.exists() else 0
    path.write_text(body)
    # load_config_readonly is mtime-cached: make sure the edit is visible to it.
    os.utime(path, ns=(time.time_ns(), max(time.time_ns(), old + 2_000_000_000)))


def _dispatcher(boot_cap):
    from gateway.kanban_watchers_dispatcher import _DispatcherSettings, _KanbanDispatcher
    return _KanbanDispatcher(kb, _DispatcherSettings(60, None, boot_cap, 2, 0, True, None, None))


def _capture(monkeypatch):
    seen = []

    def fake_dispatch_once(conn, **kwargs):
        seen.append(kwargs.get("max_in_progress"))
        return DispatchResult()

    monkeypatch.setattr(kbd, "dispatch_once", fake_dispatch_once)
    return seen


def test_lowered_cap_applies_on_next_tick_without_restart(home, monkeypatch):
    _write_config(home, "kanban:\n  max_in_progress: 16\n")
    kb.create_board("sample")
    dispatcher = _dispatcher(16)
    seen = _capture(monkeypatch)

    dispatcher.tick_once_for_board("sample")
    _write_config(home, "kanban:\n  max_in_progress: 7\n")
    dispatcher.tick_once_for_board("sample")
    dispatcher.tick_once_for_board("sample", suppress_spawn=True)

    assert seen == [16, 7, 7]


def test_unset_cap_keeps_boot_value(home, monkeypatch):
    _write_config(home, "kanban:\n  max_in_progress: 7\n")
    kb.create_board("sample")
    dispatcher = _dispatcher(7)
    seen = _capture(monkeypatch)

    dispatcher.tick_once_for_board("sample")
    _write_config(home, "kanban:\n  dispatch_interval_seconds: 15\n")
    dispatcher.tick_once_for_board("sample")
    _write_config(home, "kanban:\n  max_in_progress: not-a-number\n")
    dispatcher.tick_once_for_board("sample")

    assert seen == [7, 7, 7]


def test_lowered_cap_refuses_claims_through_real_budget_gate(home, monkeypatch):
    """End to end through dispatch_once's real ``_tick_spawn_budget``: 3 running, config 16 -> 2."""
    _write_config(home, "kanban:\n  max_in_progress: 16\n")
    kb.create_board("sample")
    dispatcher = _dispatcher(16)
    monkeypatch.setattr(kbd, "count_running_tasks", lambda conn: 3)
    monkeypatch.setattr(kbd, "count_running_tasks_other_boards", lambda board=None: 0)
    monkeypatch.setattr(kbd, "_memory_pressure_level", lambda sample=None: "ok")
    budgets = []
    real_budget = kbd._tick_spawn_budget

    def spy(conn, result, **kwargs):
        out = real_budget(conn, result, **kwargs)
        budgets.append((kwargs["max_in_progress"], out, result.at_capacity))
        return out

    monkeypatch.setattr(kbd, "_tick_spawn_budget", spy)
    dispatcher.tick_once_for_board("sample")
    _write_config(home, "kanban:\n  max_in_progress: 2\n")
    dispatcher.tick_once_for_board("sample")

    assert budgets[0][:2] == (16, (True, 13))
    assert budgets[-1] == (2, (False, None), "max_in_progress=3/2")
