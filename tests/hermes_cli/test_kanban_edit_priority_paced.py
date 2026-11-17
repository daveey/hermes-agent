"""``hermes kanban edit``: raising the priority of a card the LLM pacing governor holds needs ``--reason``.

Defect agent-6f3c6009b5 (hub, 2026-11-15): a chat ran ``kanban edit <id> --priority 1000`` on a ready card the
dispatcher was holding for ``llm_pacing``, although the stall notice it had just read said "priority changes do not
lift a pacing hold" and a coach hint said not to bump. Nothing at the edit refused it, so the prose was the only
guard. The CLI now refuses a raise (``--priority`` above the stored value, or ``--priority-bump``) while every
candidate pool of the card is paced or held (``kanban_llm_pacing_dispatch.paced_pools``, the read the stall notice
uses), unless ``--reason`` is given; the reason lands on the ``reprioritized`` event. A card the governor does not
hold, an unreadable governor and lowering/same-value edits behave as before. [daveey]
"""
from __future__ import annotations

import argparse

import pytest

from hermes_cli import kanban as kc
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_llm_pacing as lp
from hermes_cli import kanban_llm_pacing_dispatch as pd
from hermes_cli.kanban_parser import build_parser
from tests.hermes_cli import test_kanban_model_tier as model_tier
from tests.hermes_cli.test_kanban_notice_pacing_admission import (  # _pace_starts_mode: the autouse opt-in
    OPUS, _pace_starts_mode, _store)

board = model_tier.board  # one unsynced gateway: every card is its own to lease


def _main(argv: list[str]) -> int:
    root = argparse.ArgumentParser(prog="hermes")
    build_parser(root.add_subparsers(dest="command"))
    return kc.kanban_command(root.parse_args(["kanban", *argv]))


@pytest.fixture
def paced_card(board):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="paced", assignee="default", priority=900, model_override=OPUS,
                             provider_override="anthropic")
    _store(("anthropic", lp.PACED))
    with kbc.connect_closing() as conn:
        assert pd.paced_pools(conn, tid), "precondition: the governor holds the card"
    return tid


def _state(tid):
    with kbc.connect_closing() as conn:
        prio = int(conn.execute("SELECT priority FROM tasks WHERE id = ?", (tid,)).fetchone()[0])
        events = [e.payload or {} for e in kb.list_events(conn, tid) if e.kind == "reprioritized"]
    return prio, events


@pytest.mark.parametrize("flags", [["--priority", "1000"], ["--priority-bump", "100"]], ids=["absolute", "bump"])
def test_raise_on_paced_card_without_reason_is_refused(paced_card, capsys, flags):
    rc = _main(["edit", paced_card, *flags])
    err = capsys.readouterr().err
    print(f"rc={rc} state={_state(paced_card)} stderr={err.strip()!r}")
    assert rc == 1
    assert "llm_pacing" in err and "--reason" in err and "anthropic" in err
    assert _state(paced_card) == (900, [])


def test_refused_raise_leaves_operator_fields_unapplied(paced_card, capsys):
    assert _main(["edit", paced_card, "--priority", "1000", "--max-retries", "5"]) == 1
    with kbc.connect_closing() as conn:
        assert conn.execute("SELECT max_retries FROM tasks WHERE id = ?", (paced_card,)).fetchone()[0] is None


def test_raise_on_paced_card_with_reason_applies_and_records_it(paced_card, capsys):
    rc = _main(["edit", paced_card, "--priority", "1000", "--reason", "David asked to run it next"])
    assert rc == 0, capsys.readouterr().err
    assert _state(paced_card) == (1000, [{"priority": 1000, "previous_priority": 900,
                                          "reason": "David asked to run it next"}])


def test_lowering_and_same_value_on_paced_card_unchanged(paced_card, capsys):
    assert _main(["edit", paced_card, "--priority", "900"]) == 0
    assert _main(["edit", paced_card, "--priority", "10", "--reason", "superseded"]) == 0
    assert _state(paced_card)[0] == 10


def test_raise_on_unpaced_card_needs_no_reason(board, capsys):
    with kbc.connect_closing() as conn:
        tid = kb.create_task(conn, title="open", assignee="default", priority=900, model_override=OPUS,
                             provider_override="anthropic")
    _store(("anthropic", lp.OPEN))
    assert _main(["edit", tid, "--priority", "1000"]) == 0, capsys.readouterr().err
    assert _state(tid)[0] == 1000


def test_governor_read_failure_fails_open(paced_card, capsys, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("governor read failed")
    monkeypatch.setattr(pd, "paced_pools", boom)
    assert _main(["edit", paced_card, "--priority", "1000"]) == 0, capsys.readouterr().err
    assert _state(paced_card)[0] == 1000


def test_library_default_unchanged_for_dashboard_and_rest(paced_card):
    with kbc.connect_closing() as conn:
        assert kb.edit_task(conn, paced_card, priority=1000)
    assert _state(paced_card)[0] == 1000
