"""A retained (scope-stopped) triage card whose wait an open parent or recorded wait explains is agent work, not a
human ask; an acknowledged exited process leaves ``failures``; ``kanban schedule`` never prints success for a park
that changed nothing. [daveey]

t_01a1ee476b0f4c0b (2026-11-22): a ``decision_retained`` triage card with an open parent kept a chat red
(``human_ask``) because the stranded-park sweep had written its generic ask; ``kanban schedule`` printed "Scheduled"
and the card stayed triage; ``process_manage poll acknowledge=true`` did not clear the failed launch from ``failures``.
"""
from __future__ import annotations

import argparse
import json

import pytest

import hermes_state_outcome as so
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_session_activity as ksa
from hermes_cli import kanban_stranded_park as ksp
from tests.hermes_cli import test_kanban_awaiting as _awaiting_tests
from tests.hermes_cli.test_kanban_awaiting import _running

kanban_home = _awaiting_tests.kanban_home

# What the stranded-park sweep stores on a card with no live owner.
SWEEP_ASK = {
    "action": "Decide this parked card (x): release it, archive it, or re-scope it. Unblock = back to work.",
    "why_human": "Parked 3h with no live owner and no request (decision_retained)",
    "interaction": "approval", "scope": "This card only", "evidence": "retained"}


@pytest.fixture
def board(kanban_home, monkeypatch):
    path = kb.kanban_db_path()
    monkeypatch.setattr(ksa, "_board_db_paths", lambda: [path])
    monkeypatch.setattr(ksa, "_hub_db_path", lambda: None)
    ksa._reset_cache_for_tests()
    yield path
    ksa._reset_cache_for_tests()


def _retained(conn, *, parent=None, swept=True, request=SWEEP_ASK) -> str:
    tid = _running(conn)
    with kb.write_txn(conn):
        conn.execute("UPDATE tasks SET status='triage', session_id='s1', block_kind='needs_input', claim_lock=NULL, current_run_id=NULL, "
                     "human_request=? WHERE id=?", (json.dumps(request), tid))
        kb._append_event(conn, tid, "decision_retained", {"disposition": {"reason": "scope_stopped"}})
        if swept:
            kb._append_event(conn, tid, ksp.EVENT, {"request": True})
        if parent:
            conn.execute("INSERT INTO task_links (parent_id, child_id) VALUES (?, ?)", (parent, tid))
    return tid


def _items():
    ksa._reset_cache_for_tests()
    return so._read_card_items(["s1"])


def test_a_retained_card_with_an_open_parent_is_agent_work_not_a_human_ask(board) -> None:
    with kbc.connect_closing() as conn:
        tid = _retained(conn, parent=_running(conn))
    decisions, waits = _items()
    assert decisions == []
    assert [(w["id"], w["state"]) for w in waits] == [(tid, "triage")]


def test_a_retained_card_with_no_parent_and_no_wait_still_asks(board) -> None:
    with kbc.connect_closing() as conn:
        tid = _retained(conn)
    decisions, waits = _items()
    assert [d["card_id"] for d in decisions] == [tid]
    assert waits == []


def test_a_retained_card_whose_parent_is_done_still_asks(board) -> None:
    with kbc.connect_closing() as conn:
        parent = _running(conn)
        assert kb.complete_task(conn, parent, summary="done")
        tid = _retained(conn, parent=parent)
    decisions, _ = _items()
    assert [d["card_id"] for d in decisions] == [tid]


@pytest.mark.parametrize("awaiting", [{"kind": "clock", "why": "soak"}, "card"])
def test_a_retained_card_with_a_recorded_awaiting_card_or_clock_is_agent_work(board, awaiting) -> None:
    with kbc.connect_closing() as conn:
        tid = _retained(conn)
        if awaiting == "card":
            awaiting = {"kind": "card", "ref": _running(conn)}
        with kb.write_txn(conn):
            kb._append_event(conn, tid, "scheduled", {"reason": "waiting", "awaiting": awaiting})
    decisions, waits = _items()
    assert decisions == [] and [w["id"] for w in waits] == [tid]


def test_a_wait_recorded_before_the_retain_or_on_a_closed_card_does_not_explain_it(board) -> None:
    with kbc.connect_closing() as conn:
        closed = _running(conn)
        assert kb.complete_task(conn, closed, summary="done")
        early = _running(conn)
        with kb.write_txn(conn):
            kb._append_event(conn, early, "scheduled", {"awaiting": {"kind": "clock", "why": "old"}})
        tid = _retained(conn)
        with kb.write_txn(conn):
            conn.execute("UPDATE task_events SET task_id=? WHERE task_id=? AND kind='scheduled'", (tid, early))
            kb._append_event(conn, tid, "scheduled", {"awaiting": {"kind": "card", "ref": closed}})
    decisions, _ = _items()
    assert [d["card_id"] for d in decisions] == [tid]


def test_a_real_request_on_a_retained_card_still_asks_with_an_open_parent(board) -> None:
    """Not the sweep's generated ask (no ``stranded_park_escalated`` after the retain): somebody asked David."""
    real = dict(SWEEP_ASK, action="Approve the restart of the hub gateway. Unblock = restart; archive = skip.",
                why_human="Only David can approve a gateway restart")
    with kbc.connect_closing() as conn:
        tid = _retained(conn, parent=_running(conn), swept=False, request=real)
    decisions, _ = _items()
    assert [d["card_id"] for d in decisions] == [tid]


def test_the_sweep_writes_no_ask_on_a_retained_card_an_open_parent_explains(board) -> None:
    with kbc.connect_closing() as conn:
        tid = _retained(conn, parent=_running(conn), swept=False)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET human_request=NULL WHERE id=?", (tid,))
        row = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (tid,)).fetchone())
        assert ksp._excluded(conn, row, 10**10, age=10**6, defer=0)
        orphan = _retained(conn, swept=False)
        with kb.write_txn(conn):
            conn.execute("UPDATE tasks SET human_request=NULL WHERE id=?", (orphan,))
        row = dict(conn.execute("SELECT * FROM tasks WHERE id=?", (orphan,)).fetchone())
        assert not ksp._excluded(conn, row, 10**10, age=10**6, defer=0)


def _schedule(args):
    from hermes_cli import kanban as kcli
    parser = argparse.ArgumentParser(prog="hermes", add_help=False)
    kcli.build_parser(parser.add_subparsers(dest="command"))
    return kcli.kanban_command(parser.parse_args(["kanban", "schedule", *args]))


def test_schedule_on_a_triage_card_parks_it(kanban_home, capsys) -> None:
    with kbc.connect_closing() as conn:
        tid = _retained(conn, parent=_running(conn))
        parent = conn.execute("SELECT parent_id FROM task_links WHERE child_id=?", (tid,)).fetchone()[0]
    assert _schedule([tid, "SCHEDULED_UNTIL=2099-01-01T00:00:00Z waiting for the parent",
                      "--awaiting", f"card:{parent}"]) == 0
    assert f"Scheduled {tid}" in capsys.readouterr().out
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "scheduled"


def test_schedule_that_changes_nothing_is_an_error_not_scheduled(kanban_home, capsys, monkeypatch) -> None:
    """The reported symptom: the park call said yes, the card stayed triage."""
    with kbc.connect_closing() as conn:
        tid = _retained(conn, parent=_running(conn))
        parent = conn.execute("SELECT parent_id FROM task_links WHERE child_id=?", (tid,)).fetchone()[0]
    monkeypatch.setattr(kb, "schedule_task", lambda *a, **k: True)
    rc = _schedule([tid, "SCHEDULED_UNTIL=2099-01-01T00:00:00Z waiting for the parent", "--awaiting", f"card:{parent}"])
    out = capsys.readouterr()
    assert rc != 0
    assert "Scheduled" not in out.out
    assert "still triage" in out.err and "nothing changed" in out.err
    with kbc.connect_closing() as conn:
        assert kb.get_task(conn, tid).status == "triage"
