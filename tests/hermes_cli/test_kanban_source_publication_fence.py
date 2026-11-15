"""Selected artifacts: publication and receipt-consumption fences after ``portable`` returns.

``portable`` proves the selection did not change during its own transport. A later
authenticated selection (heartbeat/lifecycle stage) can still land before the
manifest RPC, during it, or before the ending's status UPDATE consumes the receipt.
None of those may let a superseded selection be reported portable.
"""
from __future__ import annotations

import json
import time

from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_source_artifacts as artifacts
from hermes_cli import kanban_source_board as board_calls
from hermes_cli import kanban_source_receipt as carrier
from tests.hermes_cli import test_kanban_source_publish as helpers
from tests.hermes_cli.test_kanban_source_resolver import commit

world = helpers.world
board = helpers.board
card = helpers.card

ONE = [{"attachment_uid": "artifact-one", "filename": "one.txt", "sha256": "a" * 64}]
TWO = [{"attachment_uid": "artifact-two", "filename": "two.txt", "sha256": "b" * 64}]


def _select(card, entries):
    with kbc.write_txn(card.conn):
        artifacts._setup(card.conn)
        card.conn.execute("INSERT INTO kanban_source_artifact_selection VALUES (?,?,?) "
                          "ON CONFLICT(task_id,run_uid) DO UPDATE SET entries=excluded.entries",
                          (card.id, card.run_uid, json.dumps(entries)))


def _portable(monkeypatch, after=None):
    """Transport stand-in that returns the current selection, then lets a racer act."""
    def portable(conn, task_id, prior, run_uid, *, bound=None):
        current = artifacts.selected(conn, task_id, run_uid)
        result = list(current if current is not None else (prior or {}).get("artifacts", []))
        if after:
            after()
        return result
    monkeypatch.setattr(artifacts, "portable", portable)


def test_selection_change_after_portable_refuses_before_manifest_rpc(card, board, monkeypatch):
    commit(card.path, "feature.txt", "work")
    _select(card, ONE)
    calls = []

    def racer():  # the preflight's own transport is first; publish() transports again right before the RPC
        calls.append(1)
        if len(calls) == 2:
            _select(card, TWO)

    _portable(monkeypatch, after=racer)
    receipt = card.receipt()
    assert len(calls) == 2
    assert receipt["state"] == "retained"
    assert "artifact_selection_changed" in receipt["reason"]
    assert "publish_source" not in board.actions()


def test_selection_change_during_manifest_rpc_is_not_portable(card, board, monkeypatch):
    commit(card.path, "feature.txt", "work")
    _select(card, ONE)
    _portable(monkeypatch)
    real = board_calls.publish

    def racing_publish(*a, **kw):
        out = real(*a, **kw)
        _select(card, TWO)
        return out

    monkeypatch.setattr(board_calls, "publish", racing_publish)
    receipt = card.receipt()
    assert receipt["state"] == "retained"
    assert "artifact_selection_changed" in receipt["reason"]
    # The Board's applied revision is still recorded, so a retry publishes on top of it.
    assert card.run().manifest["manifest_revision"] == 1
    assert card.run().manifest["artifacts"] == ONE
    monkeypatch.setattr(board_calls, "publish", real)
    retry = card.receipt()
    assert (retry["state"], retry["manifest_revision"]) == ("published", 2)
    assert card.run().manifest["artifacts"] == TWO


def test_publication_refused_when_run_ended_after_portable(card, board, monkeypatch):
    commit(card.path, "feature.txt", "work")
    _select(card, ONE)

    def end_run():
        with kbc.write_txn(card.conn):
            card.conn.execute("UPDATE task_runs SET ended_at=? WHERE id=?", (int(time.time()), card.run_id))

    _portable(monkeypatch, after=end_run)
    receipt = card.receipt()
    assert receipt["state"] == "retained"
    assert "publish_run_ended" in receipt["reason"]
    assert "publish_source" not in board.actions()


def _consumed(card, receipt, *, ends=True):
    seen = {}

    def update():
        row = card.conn.execute(f"SELECT receipt FROM {carrier.TABLE} WHERE task_id=?", (card.id,)).fetchone()
        seen["receipt"] = json.loads(row[0])
        if not ends:
            return 0  # a CAS miss: the ending did not happen
        return card.conn.execute("UPDATE tasks SET status='ready' WHERE id=? AND status='running'",
                                 (card.id,)).rowcount

    with kbc.write_txn(card.conn):
        carrier.ensure_schema(card.conn)
        carrier.with_source_receipt(card.conn, card.id, update, run_id=card.run_id, receipt=receipt)
    return seen["receipt"]


def _refusal_events(card):
    return [e[0] for e in card.conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind=?",
                                            (card.id, "workspace_source_publish_refused")).fetchall()
            if "artifact_selection_changed" in (e[0] or "")]


def test_consumption_keeps_receipt_when_selection_is_unchanged(card, monkeypatch):
    commit(card.path, "feature.txt", "work")
    _select(card, ONE)
    _portable(monkeypatch)
    receipt = card.receipt()
    assert receipt["state"] == "published"
    assert _consumed(card, receipt) == receipt


def test_consumption_downgrades_receipt_when_selection_changed_after_preflight(card, monkeypatch):
    commit(card.path, "feature.txt", "work")
    _select(card, ONE)
    _portable(monkeypatch)
    receipt = card.receipt()
    assert receipt["state"] == "published"
    _select(card, TWO)
    consumed = _consumed(card, receipt)
    assert consumed["state"] == "retained"
    assert consumed["run_uid"] == receipt["run_uid"]
    assert consumed["reason"] == "artifact_selection_changed"
    assert len(_refusal_events(card)) == 1


def test_consumption_cas_miss_records_no_refusal(card, monkeypatch):
    commit(card.path, "feature.txt", "work")
    _select(card, ONE)
    _portable(monkeypatch)
    receipt = card.receipt()
    _select(card, TWO)
    assert _consumed(card, receipt, ends=False)["state"] == "retained"
    assert _refusal_events(card) == []


def test_consumption_unreadable_selection_is_not_portable(card, monkeypatch):
    commit(card.path, "feature.txt", "work")
    _select(card, ONE)
    _portable(monkeypatch)
    receipt = card.receipt()
    with kbc.write_txn(card.conn):
        card.conn.execute("UPDATE kanban_source_artifact_selection SET entries='[{\"bad\": 1}]' "
                          "WHERE task_id=?", (card.id,))
    assert _consumed(card, receipt)["state"] == "retained"


def test_move_rolls_back_when_its_portable_receipt_goes_stale(card, monkeypatch):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_move as km
    from hermes_cli import kanban_source_move as ksm
    commit(card.path, "feature.txt", "work")
    _select(card, ONE)
    _portable(monkeypatch)

    def publish_for_move(conn, task_id):
        receipt = card.receipt(publish=True)
        assert receipt["state"] == "published"
        _select(card, TWO)  # an authenticated stage lands after the move's publish
        return receipt, (card.run().manifest, None), ""

    plan = {"runs_on": ["machine:worker-1"], "me": "hub", "previous": [], "move_no": 1, "forced": False,
            "matches": ["worker-1"], "host": {}}
    monkeypatch.setattr(ksm, "publish_for_move", publish_for_move)
    monkeypatch.setattr(km, "check", lambda *a, **kw: plan)
    monkeypatch.setattr("hermes_cli.kanban_hub_admission_placement.gate_move", lambda *a, **kw: None)
    res = km.move_task(card.conn, card.id, runs_on=["machine:worker-1"], reason="r", checkpoint="c")
    assert (res["ok"], res["reason_code"]) == (False, "artifact_selection_changed")
    task = kb.get_task(card.conn, card.id)
    assert task is not None
    assert (task.status, task.current_run_id) == ("running", card.run_id)
    assert not card.conn.execute("SELECT 1 FROM task_events WHERE task_id=? AND kind=?",
                                 (card.id, km.MOVED_EVENT)).fetchone()


def test_consumption_downgrades_unchanged_receipt_too(card, monkeypatch):
    commit(card.path, "feature.txt", "work")
    _select(card, ONE)
    _portable(monkeypatch)
    assert card.receipt()["state"] == "published"
    receipt = card.receipt()
    assert receipt["state"] == "unchanged"
    _select(card, [])
    assert _consumed(card, receipt)["state"] == "retained"
