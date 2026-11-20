"""The owner hears of a quarantine: one hub card per receipt and a queue reason on the held card (review r3, item 4)."""
from __future__ import annotations

import logging

from hermes_cli import kanban_containment_connections as connections
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_queue_position as kqp
from hermes_cli import kanban_sync_quarantine as quarantine
from hermes_cli import kanban_sync_quarantine_notice as notice
from tests.hermes_cli import kanban_quarantine_support as support
from tests.hermes_cli import test_kanban_containment_replica as fixtures

kanban_home = fixtures.kanban_home
synced_conn = fixtures.synced_conn


class Courier:
    def __init__(self, rc=0):
        self.calls, self.rc = [], rc

    def __call__(self, argv):
        self.calls.append(argv)
        return self.rc, "", "courier down" if self.rc else ""


def _arg(argv, flag):
    return argv[argv.index(flag) + 1]


def test_one_hub_card_per_receipt_naming_board_tasks_kind_and_release_command(synced_conn):
    conn = synced_conn
    case = support.conflict(conn, both=True)
    courier = Courier()
    filed = notice.notify(conn, "fixture", runner=courier, hub_cli="hermes-hub", host="worker-1", now=1000.0)
    at = {e["receipt"]: e["quarantined_at"] for e in quarantine.entries(conn)}
    keys = {f"containment-quarantine:fixture:{uid}:worker-1:{at[uid]}" for uid in (case.uid, case.release_uid)}
    assert set(filed) == keys and len(courier.calls) == 2
    for argv in courier.calls:
        assert argv[:3] == ["hermes-hub", "create-task", "hub"]
        assert _arg(argv, "--id") in keys and f"idempotency_key=\"{_arg(argv, '--id')}\"" in argv
        body = _arg(argv, "--body")
        receipt = _arg(argv, "--id").split(":")[2]
        assert "fixture" in body and case.target in body and "identity_changed" in body
        assert f"hermes kanban --board fixture sync quarantine --release {receipt} --reason" in body
    assert notice.notify(conn, "fixture", runner=courier, hub_cli="hermes-hub", host="worker-1", now=1001.0) == []
    assert len(courier.calls) == 2  # once per receipt


def test_hub_host_logs_once_and_files_no_card(synced_conn, caplog):
    conn = synced_conn
    case = support.conflict(conn)
    courier = Courier()
    caplog.clear()  # the quarantine's own log line is not the notice
    with caplog.at_level(logging.ERROR, logger=notice.log.name):
        notice.notify(conn, "fixture", runner=courier, hub_cli="hermes-hub", host="hub", now=1000.0)
        notice.notify(conn, "fixture", runner=courier, hub_cli="hermes-hub", host="hub", now=1001.0)
    assert courier.calls == []
    assert sum(case.uid in r.getMessage() for r in caplog.records) == 1
    [entry] = quarantine.entries(conn)
    key = f"containment-quarantine:fixture:{case.uid}:hub:{entry['quarantined_at']}"
    assert notice._read_state()["filed"][key]["hub"] is True


def test_courier_failure_backs_off_ten_minutes(synced_conn):
    conn = synced_conn
    support.conflict(conn)
    down = Courier(rc=1)
    assert notice.notify(conn, "fixture", runner=down, hub_cli="hermes-hub", host="worker-1", now=1000.0) == []
    assert len(down.calls) == 1 and "courier down" in notice._read_state()["last_error"]
    up = Courier()
    assert notice.notify(conn, "fixture", runner=up, hub_cli="hermes-hub", host="worker-1", now=1000.0 + 599) == []
    assert up.calls == []
    assert len(notice.notify(conn, "fixture", runner=up, hub_cli="hermes-hub", host="worker-1", now=1000.0 + 601)) == 1
    assert "last_error" not in notice._read_state()


def test_held_card_queue_reason_and_refusal_name_the_receipt_and_its_release(synced_conn):
    conn = synced_conn
    case = support.conflict(conn)
    command = f"hermes kanban --board fixture sync quarantine --release {case.uid} --reason"
    place = kqp.position(conn, kb.get_task(conn, case.target))
    assert place["lane"] == "sync_quarantine"
    note = kqp.describe(place)
    assert f"held by sync quarantine of receipt {case.uid}" in note and command in note
    for task in (case.target, case.child):
        text = connections.refusal(conn, task)
        assert "resume-contained required" not in text and case.uid in text and command in text
