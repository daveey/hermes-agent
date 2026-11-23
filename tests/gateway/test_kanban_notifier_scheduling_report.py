"""Empty delivery results must not erase notification recovery obligations."""
import math

import pytest

from gateway import kanban_watchers_notifier as notifier
from gateway.kanban_notification_schedule import PassReport
from hermes_cli import kanban_db as kb
from hermes_cli import kanban_db_notify as notify
from tests.gateway import test_kanban_notifier_board_gate as helpers

board = helpers.board
routed = helpers.routed


def collector(runner):
    return notifier._Collector(runner, kb, notifier_profile="default", gc_due=False,
                               gc_retention_days=30)


def collect_board(work):
    with kb.pin_first_board_resolution():
        work.collect_board("default")


def test_empty_board_is_a_quiet_certificate(board):
    work = collector(helpers._runner("telegram"))
    collect_board(work)
    assert not work.report.pending
    assert math.isinf(work.report.next_due(10, 5))


@pytest.mark.parametrize("failure", ["connect", "recovery", "claim"])
def test_swallowed_failures_retain_retry(board, routed, monkeypatch, failure):
    task = kb.create_task(board, title="pending", assignee="default")
    notify.add_notify_sub(board, task_id=task, platform="telegram", chat_id="chat")
    helpers._event(board, task)
    work = collector(helpers._runner("telegram"))

    def fail(*args, **kwargs):
        raise RuntimeError("unavailable")

    if failure == "connect":
        monkeypatch.setattr(notifier._kbc(), "connect", fail)
    elif failure == "recovery":
        monkeypatch.setattr(notify, "recover_orphaned_notify_claims", fail)
    else:
        monkeypatch.setattr(work, "_claim_for_sub", fail)
    collect_board(work)
    assert work.report.pending
    assert work.report.next_due(10, 5) == 15


def test_unroutable_pending_and_claimed_work_both_retain_retry(board, monkeypatch):
    task = kb.create_task(board, title="pending", assignee="default")
    notify.add_notify_sub(board, task_id=task, platform="telegram", chat_id="chat")
    helpers._event(board, task)
    runner = helpers._runner("telegram")
    monkeypatch.setattr(notifier, "_adapter_for_subscription", lambda *args: None)
    work = collector(runner)
    collect_board(work)
    assert work.deliveries == []
    assert work.report.pending
    monkeypatch.setattr(notifier, "_adapter_for_subscription", lambda *args: object())
    work = collector(runner)
    collect_board(work)
    assert len(work.deliveries) == 1
    assert work.report.pending  # delivery/ACK is not yet a quiet proof


def test_failed_census_is_not_certified_by_empty_fallback(board, monkeypatch):
    def fail(**kwargs):
        raise OSError("census unavailable")

    monkeypatch.setattr(kb, "list_boards", fail)
    report = PassReport()
    result = notifier._notifier_collect(helpers._runner("telegram"), kb,
        notifier_profile="default", gc_due=False, gc_retention_days=30, report=report)
    assert result == []
    assert report.pending


def test_disconnected_adapters_retain_retry():
    work = collector(helpers._runner())
    assert work.collect() == []
    assert work.report.pending


def test_live_inflight_claim_retains_recovery_without_new_events(board, routed, monkeypatch):
    task = kb.create_task(board, title="held", assignee="default")
    notify.add_notify_sub(board, task_id=task, platform="telegram", chat_id="chat")
    helpers._event(board, task)
    runner = helpers._runner("telegram")
    work = collector(runner)
    collect_board(work)
    monkeypatch.setattr(notify, "_holder_alive", lambda row: True)
    work = collector(runner)
    collect_board(work)
    assert work.deliveries == []
    assert work.report.pending


def test_deadlines_and_retry_take_earliest_without_moving_receipts():
    report = PassReport()
    report.due_at(30)
    report.due_at(40)
    assert report.next_due(10, 5) == 30
    report.retry()
    assert report.next_due(10, 5) == 15
