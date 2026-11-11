"""`kanban edit` can rewrite a completed handoff but never its admitted receipt.

The receipt rides the completed event's admission marker; an edit that swapped it would be
projected as admitted without its owners ever being checked. Anonymous synthetic fixtures.
"""
from __future__ import annotations

import pytest

from hermes_cli import kanban_db as kb
from hermes_cli.kanban_completion_receipt import ReceiptEditRefused
from tests.hermes_cli import test_kanban_completion_receipt as fixtures
from tests.hermes_cli.test_kanban_completion_receipt_projection import read
from tests.hermes_state.test_completion_receipt import receipt

conn = fixtures.conn


def _forged():
    value = receipt("delivered")
    value["items"][0]["remaining"] = [{"scope": "rollout", "state": "pending",
                                       "owner": "@task:default@nowhere/t_fake", "reason": "Unchecked."}]
    return value


def test_edit_cannot_swap_an_admitted_receipt(conn):
    tid = fixtures.task(conn)
    admitted = receipt("rejected")
    assert kb.complete_task(conn, tid, summary="done", metadata={"completion_receipt": admitted})
    with pytest.raises(ReceiptEditRefused):
        kb.edit_task(conn, tid, result="edited", metadata={"completion_receipt": _forged()})
    assert read(conn, tid)["completion_receipt"] == admitted


def test_edit_without_receipt_keeps_the_admitted_one(conn):
    tid = fixtures.task(conn)
    admitted = receipt("contained")
    assert kb.complete_task(conn, tid, summary="done", metadata={"completion_receipt": admitted})
    assert kb.edit_task(conn, tid, result="edited", metadata={"note": "backfilled"})
    assert read(conn, tid)["completion_receipt"] == admitted
    assert kb.edit_task(conn, tid, result="again", metadata={"completion_receipt": admitted})
    assert read(conn, tid)["completion_receipt"] == admitted
