"""Shared harness for SessionState v2: a real board (``HERMES_KANBAN_HOME``), a real state.db and an isolated
process registry, with an injectable clock. [daveey]"""
from __future__ import annotations

import pytest

import hermes_state_outcome as so
from hermes_cli import kanban_session_activity as ksa
from hermes_cli.kanban_db_connect import connect
from hermes_session_state_store import SessionStateStore, install
from hermes_state import SessionDB
from tools import process_registry as registry_module
from tools import process_registry_results as results

HUMAN_REQUEST = {
    "action": "Approve the production deploy?", "why_human": "Only David can approve a deploy.",
    "interaction": "approval", "scope": "deploy", "evidence": "CI is green.",
    "choices": [{"label": "Ship it", "value": "yes"}, {"label": "Hold", "value": "no"}],
}


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


@pytest.fixture
def kanban_root(tmp_path, monkeypatch):
    from hermes_cli import kanban_session_activity_busy as busy

    root = tmp_path / "kanban-home"
    root.mkdir()
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(root))
    monkeypatch.setenv("HERMES_HUB_STATE", str(tmp_path / "hub-state"))
    monkeypatch.setattr(ksa, "_hub_db_path", lambda: None)
    ksa._reset_cache_for_tests()
    busy.reset()
    yield root
    ksa._reset_cache_for_tests()
    busy.reset()


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setattr(results, "get_hermes_home", lambda: tmp_path)
    monkeypatch.setattr(results, "_owns_result", lambda owner, parent: owner == parent)
    reg = registry_module.process_registry
    monkeypatch.setattr(reg, "_running", {})
    monkeypatch.setattr(reg, "_finished", {})
    (tmp_path / "logs" / "process-results").mkdir(parents=True)
    return reg


@pytest.fixture
def board(kanban_root):
    conn = connect(kanban_root / "kanban.db")
    yield conn
    conn.close()


@pytest.fixture
def db(tmp_path, kanban_root, registry):
    so._reset_hooks_for_tests()
    handle = SessionDB(db_path=tmp_path / "state.db")
    handle.create_session("chat", source="desktop")
    yield handle
    handle.close()
    so._reset_hooks_for_tests()


@pytest.fixture
def clock():
    import time

    return Clock(time.time())


@pytest.fixture
def store(db, clock, tmp_path):
    made = SessionStateStore(db, clock=clock, divergence_path=tmp_path / "logs" / "divergence.jsonl")
    previous = install(made)
    pushed: list[dict] = []
    made.subscribe(pushed.append)
    made.pushed = pushed  # type: ignore[attr-defined]
    yield made
    made.stop()
    install(previous)
    ksa._reset_cache_for_tests()


def drain(store, rounds: int = 3) -> int:
    """Run worker passes until nothing is pending (tests drive the worker synchronously)."""
    total = 0
    for _ in range(rounds):
        ksa._reset_cache_for_tests()
        total += store.run_once()
    return total
