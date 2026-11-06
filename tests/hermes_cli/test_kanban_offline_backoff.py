"""Offline cooldown receipts remain scoped and bounded, including legacy events."""
import json
import sqlite3

import pytest

from hermes_cli.kanban_offline_backoff import recently_escalated


@pytest.mark.parametrize("change, expected", [
    ({}, True),
    ({"task_id": "different"}, False),
    ({"kind": "condition_met"}, False),
    ({"created_at": 900}, False),
    ({"created_at": 1001}, False),
    ({"created_at": 901}, True),
    ({"payload": "bad json"}, False),
    ({"payload": "[]"}, False),
    ({"payload": None}, False),
    ({"reason": "different"}, False),
    ({"wake_check": "different"}, False),
    ({"gateway": "different"}, False),
])
def test_receipt_identity_and_deadline(change, expected):
    payload = {"reason": "gateway_offline", "wake_check": "probe", "gateway": "other"}
    payload.update({k: v for k, v in change.items() if k in payload})
    row = {"task_id": "waiter", "kind": "wake_check_broken", "created_at": 999,
           "payload": json.dumps(payload)}
    row.update({k: v for k, v in change.items() if k in row})
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE task_events(task_id TEXT, kind TEXT, created_at INTEGER, payload TEXT)")
        conn.execute("INSERT INTO task_events VALUES(:task_id, :kind, :created_at, :payload)", row)
        assert recently_escalated(conn, "waiter", "probe", "other", 1000, 100) is expected


@pytest.mark.parametrize("reason, reasons, expected", [
    ("gateway_scope_unknown", None, False),  # default family is offline only (pre-existing callers)
    ("gateway_scope_unknown", ("gateway_offline", "gateway_scope_unknown"), True),
    ("gateway_offline", ("gateway_offline", "gateway_scope_unknown"), True),
    (None, ("gateway_offline", "gateway_scope_unknown"), False),  # a broken-probe wake is never a receipt
    ("other", ("gateway_offline", "gateway_scope_unknown"), False),
])
def test_receipt_reason_family(reason, reasons, expected):
    payload = {"reason": reason, "wake_check": "probe", "gateway": "other"}
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE task_events(task_id TEXT, kind TEXT, created_at INTEGER, payload TEXT)")
        conn.execute("INSERT INTO task_events VALUES('waiter', 'wake_check_broken', 999, ?)", (json.dumps(payload),))
        kwargs = {} if reasons is None else {"reasons": reasons}
        assert recently_escalated(conn, "waiter", "probe", "other", 1000, 100, **kwargs) is expected
