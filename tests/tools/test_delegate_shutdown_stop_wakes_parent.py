"""A background subagent stopped by a gateway shutdown wakes its parent; a stop someone asked for does not.

A core update restarted the gateway while a background subagent waited on CI. Shutdown stopped it with
the same cooperative stop ``/stop`` uses, so its result read ``interrupted`` like a requested stop: the
unviewed parent got a silent note, never ran a turn, and the user heard nothing for 35 minutes until he
asked. ``interrupt_all(..., by_shutdown=True)`` now stamps why the unit stopped onto its result, and a
stamped stop counts as a failure the parent must act on (re-dispatch or tell the user).
"""
import threading
from types import SimpleNamespace

import pytest

from tests.tools.test_async_delegation import _drain_for
from tools import async_delegation as ad
from tools.process_registry_notifications import delegation_wakes_parent, format_process_notification


def _dispatch_batch(interrupted: threading.Event, **kwargs):
    """A unit whose runner blocks until stopped, then returns its child as interrupted."""
    def runner():
        interrupted.wait(timeout=10)
        return {"results": [{"task_index": 0, "status": "interrupted", "summary": "partial"}]}
    res = ad.dispatch_async_delegation_batch(
        goals=["wait on CI"], context=None, toolsets=None, role="leaf", model="m", session_key="", runner=runner,
        interrupt_fn=interrupted.set, max_async_children=1, **kwargs)
    assert res["status"] == "dispatched", res
    return res["delegation_id"]


def _dispatch_single(interrupted: threading.Event, **kwargs):
    def runner():
        interrupted.wait(timeout=10)
        return {"status": "interrupted", "summary": "partial", "error": None, "exit_reason": "interrupted"}
    res = ad.dispatch_async_delegation(
        goal="wait on CI", context=None, toolsets=None, role="leaf", model="m", session_key="", runner=runner,
        interrupt_fn=interrupted.set, max_async_children=1, **kwargs)
    assert res["status"] == "dispatched", res
    return res["delegation_id"]


def setup_function(_fn):
    ad._reset_for_tests()


def teardown_function(_fn):
    ad._reset_for_tests()


def test_shutdown_stop_of_a_batch_is_stamped_and_wakes_the_parent():
    delegation_id = _dispatch_batch(threading.Event())
    assert ad.interrupt_all(reason="gateway shutdown (restart)", by_shutdown=True) == 1
    evt = _drain_for(delegation_id, timeout=5.0)
    assert evt is not None and [r["status"] for r in evt["results"]] == ["interrupted"]
    assert evt.get("interrupted_by") == "gateway shutdown (restart)", evt
    assert delegation_wakes_parent(evt)
    text = format_process_notification(evt) or ""
    assert "stopped by gateway shutdown (restart), not by you or the user" in text, text


def test_shutdown_stop_of_a_single_unit_is_stamped_and_wakes_the_parent():
    delegation_id = _dispatch_single(threading.Event())
    assert ad.interrupt_all(reason="gateway shutdown (restart)", by_shutdown=True) == 1
    evt = _drain_for(delegation_id, timeout=5.0)
    assert evt is not None and evt["status"] == "interrupted", evt
    assert evt.get("interrupted_by") == "gateway shutdown (restart)", evt
    assert delegation_wakes_parent(evt)
    assert "not by you or the user" in (format_process_notification(evt) or "")


@pytest.mark.parametrize("statuses", [("completed",), ("failed",), ("completed", "failed")])
def test_shutdown_stamp_without_stopped_children_has_no_stop_guidance(statuses):
    evt = {"type": "async_delegation", "is_batch": True, "delegation_id": "deleg_done",
           "interrupted_by": "gateway shutdown (restart)", "goals": ["work"] * len(statuses),
           "results": [{"task_index": i, "status": status, "summary": "result"}
                       for i, status in enumerate(statuses)]}
    text = format_process_notification(evt) or ""
    assert "deleg_done" in text
    assert "stopped by" not in text
    assert delegation_wakes_parent(evt) is ("failed" in statuses)


@pytest.mark.parametrize("extra,warns", [
    ({}, True),
    ({"type": "completion"}, False),
    ({"task_failure_notice": True}, False),
    ({"role": "cron_run"}, False),
])
def test_requested_stop_warning_only_for_final_delegations(caplog, extra, warns):
    from gateway.config import Platform
    from gateway.run_notifications import _watch_notification_metadata

    evt = {"type": "async_delegation", "delegation_id": "deleg_warning", "status": "interrupted",
           "parent_session_id": "parent-warning", **extra}
    metadata = _watch_notification_metadata(evt, SimpleNamespace(platform=Platform.LOCAL))
    assert not metadata.get("completion_wakes_parent")
    warnings = [record.getMessage() for record in caplog.records if "not woken" in record.getMessage()]
    assert bool(warnings) is warns, warnings
    if warns:
        assert "parent-warning" in warnings[0] and "interrupted" in warnings[0]


def test_requested_stop_stays_asleep():
    """``/stop`` (and any caller that does not say shutdown) is a stop someone asked for."""
    delegation_id = _dispatch_batch(threading.Event())
    assert ad.interrupt_all(reason="/stop") == 1
    evt = _drain_for(delegation_id, timeout=5.0)
    assert evt is not None and "interrupted_by" not in evt, evt
    assert not delegation_wakes_parent(evt)


def test_session_end_stop_stays_asleep():
    """The chat closed (exit, /new): that ended the session's work, so nothing is stamped."""
    delegation_id = _dispatch_batch(threading.Event(), parent_session_id="parent-1")
    assert ad.interrupt_for_session(parent_session_id="parent-1") == 1
    evt = _drain_for(delegation_id, timeout=5.0)
    assert evt is not None and "interrupted_by" not in evt, evt
    assert not delegation_wakes_parent(evt)
