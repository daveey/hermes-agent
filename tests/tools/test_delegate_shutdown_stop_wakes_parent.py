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


@pytest.mark.parametrize("extra,flagged", [
    ({}, True),
    ({"type": "completion"}, False),
    ({"task_failure_notice": True}, False),
    ({"role": "cron_run"}, False),
])
def test_requested_stop_is_flagged_only_for_final_delegations(caplog, extra, flagged):
    """The metadata step cannot know whether a viewer will be attached when the admission runs, so it only
    flags the requested stop; the executor logs it if it becomes a note (test_unviewed_delegation_failure_turn)."""
    import logging
    from gateway.config import Platform
    from gateway.run_notifications import _watch_notification_metadata

    caplog.set_level(logging.WARNING)
    evt = {"type": "async_delegation", "delegation_id": "deleg_warning", "status": "interrupted",
           "parent_session_id": "parent-warning", **extra}
    metadata = _watch_notification_metadata(evt, SimpleNamespace(platform=Platform.LOCAL))
    assert not metadata.get("completion_wakes_parent")
    assert ("completion_requested_stop" in metadata) is flagged, metadata
    if flagged:
        assert metadata["completion_requested_stop"] == ["interrupted"]
    assert not [r.getMessage() for r in caplog.records if "not woken" in r.getMessage()]


@pytest.mark.parametrize("value,ok", [
    (["interrupted"], True), (["cancelled", "interrupted", "cancelled"], True),
    ([], False), ("interrupted", False), (["failed"], False), ([None], False), (True, False),
])
def test_requested_stop_admission_metadata_is_validated(value, ok):
    from gateway.session_automation import requested_stop_metadata
    from hermes_state_runtime import RuntimeStoreError

    assert requested_stop_metadata({}) == {}
    if ok:
        assert requested_stop_metadata({"completion_requested_stop": value}) == {
            "requested_stop": sorted(set(value))}
    else:
        with pytest.raises(RuntimeStoreError):
            requested_stop_metadata({"completion_requested_stop": value})


def _dispatch_held_batch(stopped: threading.Event, release: threading.Event, **kwargs):
    """A unit whose runner unwinds only on *release*: a stop sets *stopped* but leaves it active (running),
    the window between a stop request and the unit's finalize."""
    def runner():
        assert release.wait(timeout=10)
        return {"results": [{"task_index": 0, "status": "interrupted", "summary": "partial"}]}
    res = ad.dispatch_async_delegation_batch(
        goals=["wait on CI"], context=None, toolsets=None, role="leaf", model="m", session_key="", runner=runner,
        interrupt_fn=stopped.set, max_async_children=1, parent_session_id="parent-1", **kwargs)
    assert res["status"] == "dispatched", res
    return res["delegation_id"]


_REQUESTED_STOPS = {
    "slash-stop": lambda: ad.interrupt_all(reason="/stop"),
    "gateway-stop-command": lambda: ad.interrupt_for_session(parent_session_id="parent-1", reason="stop_command"),
}


@pytest.mark.parametrize("first", [*_REQUESTED_STOPS, None], ids=[*_REQUESTED_STOPS, "shutdown-only"])
def test_shutdown_before_finalize_keeps_the_first_stops_intent(first):
    """A user stopped the unit, then the gateway shut down before its runner unwound: the shutdown sweep
    still reaches the active unit, but the stop was asked for, so the parent is not woken. With no earlier
    stop the sweep's stop is the only one and wakes the parent (the control)."""
    stopped, release = threading.Event(), threading.Event()
    delegation_id = _dispatch_held_batch(stopped, release)
    if first is not None:
        assert _REQUESTED_STOPS[first]() == 1
        assert stopped.is_set()
        stopped.clear()
    assert ad.interrupt_all(reason="gateway shutdown (restart)", by_shutdown=True) == 1, "still active"
    assert stopped.is_set(), "the sweep still stops it"
    release.set()
    evt = _drain_for(delegation_id, timeout=5.0)
    assert evt is not None and [r["status"] for r in evt["results"]] == ["interrupted"], evt
    if first is None:
        assert evt.get("interrupted_by") == "gateway shutdown (restart)", evt
        assert delegation_wakes_parent(evt)
    else:
        assert "interrupted_by" not in evt, evt
        assert not delegation_wakes_parent(evt)
        assert "not by you or the user" not in (format_process_notification(evt) or "")


@pytest.mark.parametrize("then", list(_REQUESTED_STOPS))
def test_shutdown_stop_stays_stamped_when_a_requested_stop_follows(then):
    """The first stop decides: work the shutdown already stopped still wakes its parent."""
    stopped, release = threading.Event(), threading.Event()
    delegation_id = _dispatch_held_batch(stopped, release)
    assert ad.interrupt_all(reason="gateway shutdown (restart)", by_shutdown=True) == 1
    assert _REQUESTED_STOPS[then]() == 1
    release.set()
    evt = _drain_for(delegation_id, timeout=5.0)
    assert evt is not None and evt.get("interrupted_by") == "gateway shutdown (restart)", evt
    assert delegation_wakes_parent(evt)


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
