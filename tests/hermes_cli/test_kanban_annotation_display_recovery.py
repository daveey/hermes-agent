"""A completed host observation must beat a stale busy-refresh fallback. [daveey]"""
from __future__ import annotations

import threading
import time

import pytest

from hermes_cli import kanban_session_activity as ksa
from tests.hermes_cli import test_kanban_annotation_host as host_helpers
from tests.hermes_cli import test_kanban_annotation_refresh_bounded as helpers

scoped = helpers.scoped
world = host_helpers.world


def test_peek_does_not_overwrite_a_concurrently_published_local_read(scoped):
    previous = helpers._prime(scoped, 1)
    old_at = time.time() - 10
    entered, release = threading.Event(), threading.Event()
    results = []

    def read(scope):
        entered.set()
        assert release.wait(5)
        return helpers._parts(2), old_at, True

    scoped.setattr(ksa._host_copy, "_display_read", read)
    thread = threading.Thread(target=lambda: results.append(ksa._host_copy.peek_display(helpers.SCOPE, previous)))
    thread.start()
    assert entered.wait(5)
    try:
        with ksa._annotate_refresh_lock:
            ksa._refresh_annotation_locked(helpers.SCOPE)
        current = ksa._last_annotation[helpers.SCOPE]
        current_at = ksa.annotation_read_at(current)
    finally:
        release.set()
        thread.join(5)
    assert not thread.is_alive()
    assert ksa._last_annotation[helpers.SCOPE] is current
    assert ksa.annotation_read_at(current) == current_at
    assert ksa._annotate_flight_result is current
    assert ksa.annotation_read_at(results[0]) <= current_at


def test_peek_preserves_newer_previous_and_refuses_undeclared_sources(scoped):
    previous = helpers._prime(scoped, 1)
    scoped.setattr(ksa._host_copy, "_display_read", lambda scope: (helpers._parts(0), time.time() - 10, True))
    assert ksa._host_copy.peek_display(helpers.SCOPE, previous) is previous
    scoped.setattr(ksa._host_copy, "_display_read", lambda scope: (helpers._parts(0), time.time() + 1, False))
    scoped.setattr(ksa, "_extra_waiting", lambda: pytest.fail("unlocked waiting probe"))
    assert ksa._host_copy.peek_display(helpers.SCOPE, previous) is previous


def test_peek_timestamp_survives_later_scope_publication(scoped):
    scoped.setattr(ksa._host_copy, "_display_read", lambda scope: (helpers._parts(0), 1234.0, True))
    peeked = ksa._host_copy.peek_display(helpers.SCOPE, None)
    ksa._bounded.record(helpers.SCOPE, helpers._parts(1), time.time())
    assert ksa.annotation_read_at(peeked) == 1234.0


@pytest.mark.parametrize("display,age,unknown", [(True, 240, True), (True, 30, False), (False, 240, False)])
def test_observation_expiry_preserves_fresh_and_strict_controls(scoped, display, age, unknown):
    previous = helpers._prime(scoped, 1)
    ksa._bounded.record(helpers.SCOPE, previous, time.time() - age)
    scoped.setattr(ksa._host_copy, "adopt_display", lambda scope: None)
    with ksa._annotate_refresh_lock:
        row = ksa.annotate_kanban_activity([{"id": "s1"}], display=display)[0]
    assert bool(row.get("kanban_unknown")) is unknown
    assert row.get("kanban_active") == (None if unknown else 1)


def test_real_published_completion_beats_busy_local_positive(world):
    host_helpers._poll()
    stale = ksa._last_annotation[host_helpers.SCOPE]
    host_helpers._advance(world, 4)
    world["conn"].execute("UPDATE tasks SET status='done' WHERE session_id='s1'")
    with ksa._annotate_refresh_lock:
        fresh = ksa._refresh_annotation_locked(host_helpers.SCOPE)
    assert fresh[0].get("s1", 0) == 0
    # The publisher is another backend: this reader still has the old tuple.
    ksa._last_annotation[host_helpers.SCOPE] = stale
    with ksa._annotate_refresh_lock:
        row = ksa.annotate_kanban_activity([{"id": "s1"}], display=True)[0]
    assert row.get("kanban_active", 0) == 0
    assert not row.get("kanban_waiting_on")
    assert not row.get("kanban_unknown")


def test_display_adopts_completed_host_copy_while_refresh_lock_is_busy(scoped):
    previous = helpers._prime(scoped, 1)
    ksa._bounded.record(helpers.SCOPE, previous, time.time() - 240)
    completed = helpers._parts(0)
    adopted = []
    scoped.setattr(ksa._host_copy, "_display_read", lambda scope: adopted.append(scope) or (completed, time.time(), True))
    assert ksa._annotate_refresh_lock.acquire(blocking=False)
    try:
        row = ksa.annotate_kanban_activity([{"id": "s1"}], display=True)[0]
        assert adopted == [helpers.SCOPE]
        assert row.get("kanban_active", 0) == 0
        assert not row.get("kanban_unknown")
        assert row["kanban_observed_at"] > time.time() - 10
    finally:
        ksa._annotate_refresh_lock.release()


def test_expired_display_fallback_is_unknown_not_an_active_observation(scoped):
    previous = helpers._prime(scoped, 1)
    ksa._bounded.record(helpers.SCOPE, previous, time.time() - 240)
    scoped.setattr(ksa._host_copy, "adopt_display", lambda scope: None)
    assert ksa._annotate_refresh_lock.acquire(blocking=False)
    try:
        row = ksa.annotate_kanban_activity([{"id": "s1"}], display=True)[0]
        assert row.get("kanban_unknown") is True
        assert "kanban_active" not in row
    finally:
        ksa._annotate_refresh_lock.release()
