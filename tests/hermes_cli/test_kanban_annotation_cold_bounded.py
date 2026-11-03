"""Cold annotation work must not strand otherwise readable sessions."""
from concurrent.futures import ThreadPoolExecutor
from contextvars import ContextVar
import threading

from hermes_cli import kanban_session_activity as activity
import pytest


@pytest.mark.parametrize("foreign", [False, True])
def test_cold_polls_finish_while_refresh_is_descheduled(monkeypatch, foreign):
    activity._reset_cache_for_tests()
    scope = ContextVar("test_annotation_scope", default="owner")
    monkeypatch.setattr(activity, "_origin_scope", lambda: (scope.get(),))
    monkeypatch.setattr(activity._annotation_copy, "seed", lambda *args: None)
    monkeypatch.setattr(activity._annotation_copy, "published", lambda *args: None)
    entered, release = threading.Event(), threading.Event()
    calls = []

    def read():
        calls.append(1)
        entered.set()
        assert release.wait(10), "test did not release cold refresh"
        return activity._identity.freeze_annotation(({"target": 3}, {}, {}, {}, {}, {}, {}, {}, {}, {}, {}))

    monkeypatch.setattr(activity, "_read_annotation_parts", read)

    def poll(name):
        scope.set(name)
        return activity.annotate_kanban_activity([{"id": "target"}])[0]

    try:
        with ThreadPoolExecutor(max_workers=5) as pool:
            owner = pool.submit(poll, "owner")
            try:
                assert entered.wait(5)
                followers = [pool.submit(poll, "foreign" if foreign else "owner") for _ in range(4)]
                for future in [owner, *followers]:
                    row = future.result(timeout=4)
                    assert row["id"] == "target"
                    assert row["kanban_unknown"] is True
                    assert "kanban_active" not in row
                assert calls == [1]
            finally:
                release.set()
        assert activity._annotate_refresh_lock.acquire(timeout=5)
        activity._annotate_refresh_lock.release()
        assert activity._last_annotation[("owner",)][0] == {"target": 3}
        # Recovery publishes actual counts, not the temporary unknown response.
        assert poll("owner")["kanban_active"] == 3
        assert poll("foreign")["kanban_active"] == 3
    finally:
        release.set()
        assert activity._annotate_refresh_lock.acquire(timeout=5)
        activity._annotate_refresh_lock.release()
        activity._reset_cache_for_tests()
