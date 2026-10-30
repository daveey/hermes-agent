"""Backend boot builds the session-list kanban annotation before the Desktop's first poll burst.

A cold annotation made every session read, GET /api/sessions/<id> included, wait on one pass that took 4-10 s
inside a reconnect burst on hermes-hub (t_01a18b4b710b722e). [daveey]
"""

from __future__ import annotations

import threading

from fastapi.testclient import TestClient

from hermes_cli import kanban_annotation_prewarm as prewarm
from hermes_cli import kanban_session_activity as ksa
from hermes_cli import web_server
from tests.hermes_cli.test_serve_unloaded_sweeper import _quiet_boot


def test_prewarm_builds_the_annotation_snapshot(monkeypatch):
    calls = []
    monkeypatch.setattr(ksa, "_annotation_snapshot", lambda: calls.append("snap") or ("parts",))
    assert prewarm.prewarm_annotation() is True
    assert calls == ["snap"]


def test_prewarm_never_raises(monkeypatch):
    def boom():
        raise RuntimeError("board unreadable")

    monkeypatch.setattr(ksa, "_annotation_snapshot", boom)
    assert prewarm.prewarm_annotation() is False


def test_prewarm_first_reader_gets_the_warm_snapshot_without_a_second_pass(monkeypatch):
    """The point of the prewarm: a later reader is served the published parts, it does not re-read."""
    reads = []
    monkeypatch.setattr(ksa, "_origin_scope", lambda: ("prewarm-test-scope",))
    monkeypatch.setattr(ksa, "_read_annotation_parts", lambda: reads.append(1) or ("warm",))
    monkeypatch.setattr(ksa, "_last_annotation", {})
    assert prewarm.prewarm_annotation() is True
    # A reader arriving while a refresh holds the lock gets the snapshot at once instead of waiting.
    assert ksa._annotate_refresh_lock.acquire(blocking=False)
    try:
        assert ksa._annotation_snapshot() == ("warm",)
    finally:
        ksa._annotate_refresh_lock.release()
    assert reads == [1]


def test_start_skips_under_test_isolation(monkeypatch):
    monkeypatch.setenv("HERMES_TEST_ISOLATION", "1")
    assert prewarm.start_annotation_prewarm() is None


def test_start_runs_prewarm_on_a_daemon_thread(monkeypatch):
    monkeypatch.delenv("HERMES_TEST_ISOLATION", raising=False)
    ran = threading.Event()
    monkeypatch.setattr(prewarm, "prewarm_annotation", lambda: ran.set() or True)
    thread = prewarm.start_annotation_prewarm()
    assert thread is not None and thread.daemon and thread.name == "kanban-annotation-prewarm"
    thread.join(5)
    assert ran.is_set()


def test_serve_lifespan_starts_the_annotation_prewarm(monkeypatch):
    _quiet_boot(monkeypatch)
    calls = []
    monkeypatch.setattr(prewarm, "start_annotation_prewarm", lambda: calls.append("start"))
    with TestClient(web_server.app, raise_server_exceptions=False):
        pass
    assert calls == ["start"]
