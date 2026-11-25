"""``run_route`` receipts name where the route came from: the stored card row, not the per-run allocation.

Hub review run of PR #4407 (2026-11-25): the card row held no selector and the tier reason was ``review_unpinned``,
yet the receipt said ``source: "pin"``. ``apply_model_tier`` sets the concrete route on the claimed ``Task`` object
only, and ``effective_route`` labelled any concrete in-memory route a pin.
"""
import pytest

from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_connect as kbc
from hermes_cli import kanban_model_tier as tier
from tests.hermes_cli.test_kanban_quota_episode import board as board  # noqa: PLC0414 -- the self-alias is load-bearing: it suppresses F811 for the pytest fixture parameter shadowing this import

MODEL, PROVIDER = "claude-opus-5-5", "anthropic"


def _receipt(conn, stored, allocated, reason):
    """Claim a card whose row holds ``stored``, give the claimed Task ``allocated``, return its ``run_route``."""
    tid = kb.create_task(conn, title="route", assignee="a")
    if stored != (None, None):  # a legacy or synced row: creation no longer accepts a concrete selector
        conn.execute("UPDATE tasks SET model_override=?, provider_override=? WHERE id=?", (*stored, tid))
        conn.commit()
    task = kb.claim_task(conn, tid, claimer=kb._claimer_id())
    assert task is not None
    task.model_override, task.provider_override = allocated
    tier.record_run_route(conn, task, lane="ready", reason=reason)
    row = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='run_route' ORDER BY id DESC LIMIT 1",
                       (tid,)).fetchone()
    return kb._json_dict(row[0])


@pytest.mark.parametrize("stored, allocated, reason, source", [
    ((None, None), (MODEL, PROVIDER), "review_unpinned", "dynamic"),
    ((None, None), (MODEL, None), "review_unpinned", "dynamic_provider_unknown"),
    (("class:deep", "auto"), (MODEL, PROVIDER), "default", "class"),
    (("class:deep", "auto"), ("class:deep", "auto"), "default", "class"),
    ((MODEL, PROVIDER), (MODEL, PROVIDER), "legacy_receipt", "pin"),
    ((MODEL, None), (MODEL, None), "legacy_receipt", "pin_provider_unknown"),
])
def test_receipt_source_follows_the_stored_card_row(board, stored, allocated, reason, source):
    with kbc.connect() as conn:
        receipt = _receipt(conn, stored, allocated, reason)
    assert receipt["source"] == source
    assert (receipt["model"], receipt["tier_reason"]) == (allocated[0], reason)  # the route itself is untouched


def test_unreadable_card_row_keeps_the_in_memory_label_and_still_writes_the_receipt(board, monkeypatch):
    def unreadable(_conn, _task):
        raise RuntimeError("row read failed")
    monkeypatch.setattr(tier, "_stored_selector", unreadable)
    with kbc.connect() as conn:
        receipt = _receipt(conn, (None, None), (MODEL, PROVIDER), "review_unpinned")
    assert receipt["source"] == "pin" and receipt["model"] == MODEL
