"""The dispatcher's auto-decompose sweep leaves fresh triage cards alone for a grace window.

Agents create triage cards meant as holds (test fixtures, "park it for later" cards) and park them
with ``kanban_schedule`` right after. Before ``kanban.auto_decompose_min_age_seconds`` the sweep
specified such a card 6-16 s after creation and a worker claimed it seconds later (defect
kanban-76c77018a6: fixture t_01a1949d723b06ac ran 3x). [daveey]
"""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb, kanban_db_connect as kbc
from gateway.kanban_watchers_dispatcher import _KanbanDispatcher, _resolve_dispatcher_settings


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path))
    monkeypatch.delenv("HERMES_KANBAN_DB", raising=False)
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    kb.create_board("default")


@pytest.fixture
def board(env, monkeypatch):
    dispatcher = _KanbanDispatcher(kb, _resolve_dispatcher_settings({}, kb))
    monkeypatch.setattr(dispatcher, "_board_slugs", lambda: ["default"])
    from hermes_cli import kanban_decompose as decomp

    specified: list[str] = []

    def fake_decompose(tid, *, author=None, **_kw):
        specified.append(tid)
        return decomp.DecomposeOutcome(tid, True, "ok")

    monkeypatch.setattr(decomp, "decompose_task", fake_decompose)
    return dispatcher, specified


def _triage_card(title="fixture: park me", age_seconds=0):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title=title, triage=True)
        if age_seconds:
            with kb.write_txn(conn):
                conn.execute("UPDATE tasks SET created_at = created_at - ? WHERE id = ?", (age_seconds, tid))
    return tid


def test_fresh_triage_card_waits_out_the_window_then_is_specified(board):
    dispatcher, specified = board
    fresh = _triage_card()
    dispatcher.auto_decompose_tick(3)
    assert fresh not in specified, "a triage card seconds old must not be auto-specified"
    with kbc.connect_closing() as conn, kb.write_txn(conn):
        conn.execute("UPDATE tasks SET created_at = created_at - 121 WHERE id = ?", (fresh,))
    dispatcher.auto_decompose_tick(3)
    assert specified == [fresh]


def test_triage_card_scheduled_inside_the_window_stays_scheduled(board):
    dispatcher, specified = board
    tid = _triage_card()
    with kbc.connect_closing() as conn:
        assert kb.schedule_task(conn, tid, reason="SCHEDULED_UNTIL=2099-01-01T00:00:00Z fixture hold",
                                clock_only=True, awaiting={"kind": "clock", "why": "fixture hold"})
    for _ in range(3):
        dispatcher.auto_decompose_tick(3)
    with kbc.connect_closing() as conn, kb.write_txn(conn):
        # Even once old, a scheduled card is not a triage candidate.
        conn.execute("UPDATE tasks SET created_at = created_at - 600 WHERE id = ?", (tid,))
    dispatcher.auto_decompose_tick(3)
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "scheduled"
    assert specified == []


def test_window_is_configurable_and_zero_restores_immediate_sweep(board, monkeypatch):
    dispatcher, specified = board
    from hermes_cli import config as hcfg

    monkeypatch.setattr(hcfg, "load_config", lambda: {"kanban": {"auto_decompose_min_age_seconds": 0}})
    fresh = _triage_card()
    dispatcher.auto_decompose_tick(3)
    assert specified == [fresh]


def test_list_triage_ids_min_age_filter_and_manual_default(board):
    from hermes_cli import kanban_decompose as decomp

    fresh = _triage_card("fresh")
    old = _triage_card("old", age_seconds=300)
    # Manual sweeps (`decompose --all`) and the default keep every candidate.
    assert set(decomp.list_triage_ids()) >= {fresh, old}
    aged = decomp.list_triage_ids(min_age_seconds=120)
    assert old in aged and fresh not in aged


def test_manual_decompose_still_specifies_a_fresh_card(env, monkeypatch):
    """`hermes kanban decompose <id>` / Specify is not gated by the sweep's window."""
    from hermes_cli import kanban_decompose as decomp

    calls = []
    monkeypatch.setattr(decomp, "_call_aux", lambda *a, **k: calls.append(a) or (None, "no aux"))
    tid = _triage_card("manual")
    decomp.decompose_task(tid, author="david")
    assert len(calls) == 1, "manual decompose of a seconds-old triage card must reach the specifier"


def test_window_default_and_tool_text_warn_triage_is_not_a_hold():
    from hermes_cli.config_defaults import DEFAULT_CONFIG
    from gateway.kanban_watchers_common import AUTO_DECOMPOSE_MIN_AGE_DEFAULT
    from tools import kanban_tools_schemas as schemas

    assert DEFAULT_CONFIG["kanban"]["auto_decompose_min_age_seconds"] == AUTO_DECOMPOSE_MIN_AGE_DEFAULT == 120
    assert "Triage is not a hold" in schemas.KANBAN_CREATE_SCHEMA["parameters"]["properties"]["triage"]["description"]
