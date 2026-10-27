"""Regression from independent review: publication is not external visibility."""
import threading

import pytest

from hermes_cli import admission_fence as af, backend_retirement
from tui_gateway import server

pytestmark = pytest.mark.platforms("posix")


def test_unregistered_accepted_turn_stays_busy_until_worker_handoff(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    monkeypatch.setattr(backend_retirement, "retirement", backend_retirement.RetirementFence())
    session = {"profile_home": str(tmp_path), "history_lock": threading.RLock(), "running": False}
    try:
        with server._session_turn_admission(session) as admitted:
            assert admitted
            session["running"] = True
        # The record is not yet in server._sessions (session.start ordering).
        # There is no externally readable running ledger, so the reservation
        # must survive publication and an updater hold arriving before launch.
        assert af.set_hold(tmp_path)["reservations"] == 1
    finally:
        session["running"] = False
        server._clear_inflight_turn(session)


@pytest.fixture
def admitted(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    fence = backend_retirement.RetirementFence()
    monkeypatch.setattr(backend_retirement, "retirement", fence)
    session = {"profile_home": str(tmp_path), "history_lock": threading.RLock(), "running": False}
    with server._session_turn_admission(session) as accepted:
        assert accepted
        session["running"] = True
        session["inflight_turn"] = {"user": "hello", "assistant": ""}
    yield session, fence, tmp_path
    with session["history_lock"]:
        session["running"] = False
        server._clear_inflight_turn(session)


def test_adopt_under_hold_and_retain_through_cleanup(admitted):
    session, fence, home = admitted
    assert af.set_hold(home)["reservations"] == 1
    cleared, finish = threading.Event(), threading.Event()
    def run():
        with session["history_lock"]:
            session["running"] = False
            server._clear_inflight_turn(session)
        cleared.set()
        assert finish.wait(5)
    thread = server._start_session_work(run, name="handoff-test", session=session)
    try:
        assert thread is not None
        assert cleared.wait(5)
        assert fence.active_count() == 1
        assert af.set_hold(home)["reservations"] == 1
        assert server._start_session_work(lambda: None, name="fresh-test", home=home) is None
    finally:
        finish.set()
        thread.join(5)
    assert not thread.is_alive()
    assert fence.active_count() == 0
    assert af.set_hold(home)["reservations"] == 0


def test_failure_snapshot_releases_session_but_not_worker(admitted):
    session, fence, _ = admitted
    failed, finish = threading.Event(), threading.Event()
    def run():
        with session["history_lock"]:
            server._fail_inflight_turn(session, "build failed")
        failed.set()
        assert finish.wait(5)
    thread = server._start_session_work(run, name="failure-test", session=session)
    try:
        assert failed.wait(5)
        assert session["inflight_turn"]["error"] == "build failed"
        assert fence.active_count() == 1
    finally:
        finish.set()
        thread.join(5)
    assert fence.active_count() == 0


def test_nested_wait_worker_handoff_does_not_release_main(admitted):
    session, fence, home = admitted
    from tui_gateway.turn_admission import retained_callback
    build, cancel = retained_callback(session, lambda: None)
    entered, finish = threading.Event(), threading.Event()
    threads = []
    def main():
        entered.set()
        assert finish.wait(5)
    def wait():
        threads.append(server._start_session_work(main, name="main-test", session=session))
    af.set_hold(home)
    waiter = server._start_session_work(wait, name="wait-test", session=session)
    try:
        waiter.join(5)
        assert entered.wait(5)
        assert session["running"]
        assert fence.active_count() == 1
        finish.set()
        threads[0].join(5)
        # A cancelled/timed-out wait does not imply that the build unwound.
        assert fence.active_count() == 1
        build()
        cancel()  # idempotent failure cleanup
        assert fence.active_count() == 0
    finally:
        finish.set()
        cancel()


@pytest.mark.parametrize("during_start", [True, False])
def test_worker_exception_releases_all_refs(admitted, monkeypatch, during_start):
    import agent.memory_provider
    session, fence, _ = admitted
    queued = []
    class Inline:
        def __init__(self, run):
            self.run = run
        def start(self):
            if during_start:
                self.run()
            else:
                queued.append(self.run)
    monkeypatch.setattr(agent.memory_provider, "spawn_context_thread", lambda run, **kw: Inline(run))
    def fail():
        raise RuntimeError("worker prologue")
    with pytest.raises(RuntimeError, match="worker prologue"):
        server._start_session_work(fail, name="broken", session=session)
        if queued:
            queued[0]()
    assert not session["running"]
    assert fence.active_count() == 0


def test_spawn_failure_releases_all_refs(admitted, monkeypatch):
    import agent.memory_provider
    session, fence, _ = admitted
    class Broken:
        def start(self):
            raise RuntimeError("cannot start")
    monkeypatch.setattr(agent.memory_provider, "spawn_context_thread", lambda *a, **kw: Broken())
    with pytest.raises(RuntimeError, match="cannot start"):
        server._start_session_work(lambda: None, name="broken", session=session)
    assert not session["running"]
    assert fence.active_count() == 0


def test_old_worker_cannot_clear_new_turn(admitted):
    session, fence, _ = admitted
    cleared, finish = threading.Event(), threading.Event()
    def old():
        with session["history_lock"]:
            session["running"] = False
            server._clear_inflight_turn(session)
        cleared.set()
        assert finish.wait(5)
        with session["history_lock"]:
            server._clear_inflight_turn(session)
            server._fail_inflight_turn(session, "old failure")
    thread = server._start_session_work(old, name="old-test", session=session)
    try:
        assert cleared.wait(5)
        with server._session_turn_admission(session) as accepted:
            assert accepted
            session["running"] = True
            session["inflight_turn"] = {"user": "new"}
        assert fence.active_count() == 2
    finally:
        finish.set()
        thread.join(5)
    assert session["running"]
    assert session["inflight_turn"] == {"user": "new"}
    assert fence.active_count() == 1


def test_admission_and_stage_exceptions_release(admitted):
    from tui_gateway.turn_admission import stage
    session, fence, _ = admitted
    with pytest.raises(ValueError):
        with stage(session):
            raise ValueError("build scheduling failed")
    assert fence.active_count() == 0
    with pytest.raises(ValueError):
        with server._session_turn_admission(session) as accepted:
            assert accepted
            session["running"] = True
            raise ValueError("publication failed")
    assert fence.active_count() == 0


@pytest.mark.parametrize("terminal_type", ["turn.done", "turn.error"])
def test_compute_completion_retains_until_callback_cleanup(admitted, monkeypatch, terminal_type):
    from types import SimpleNamespace
    session, fence, home = admitted
    callbacks = []
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", dict)
    monkeypatch.setattr(server, "_compute_host_turn_frame", lambda *a, **kw: {})
    supervisor = SimpleNamespace(submit_turn=lambda frame, on_complete: callbacks.append(on_complete))
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda *a: supervisor)
    for name in ("_compute_host_adopt_frame_meta", "_apply_compute_host_metadata_mirror",
                 "_release_deferred_active_session_lease", "_emit"):
        monkeypatch.setattr(server, name, lambda *a, **kw: None)
    monkeypatch.setattr(server, "_compute_host_session_info", lambda *a: {})
    seen = []
    monkeypatch.setattr(server, "_drain_queued_prompt", lambda *a: seen.append(fence.active_count()))
    af.set_hold(home)
    result = server._submit_prompt_to_compute_host("r", "s", session, "hello")
    assert "error" not in result
    assert fence.active_count() == 1
    callbacks[0]({"type": terminal_type})
    assert seen == [1]
    assert not session["running"]
    assert fence.active_count() == 0
    callbacks[0]({"type": terminal_type})  # duplicate completion cannot underflow
    assert fence.active_count() == 0


def test_compute_send_failure_preserves_inline_handoff(admitted, monkeypatch):
    from types import SimpleNamespace
    session, fence, home = admitted
    monkeypatch.setattr(server, "_load_dashboard_process_isolation_config", dict)
    monkeypatch.setattr(server, "_compute_host_turn_frame", lambda *a, **kw: {})
    def submit(frame, on_complete):
        on_complete({"reason": "send_failed"})
        raise OSError("pipe closed")
    monkeypatch.setattr(server, "_get_compute_host_supervisor", lambda *a: SimpleNamespace(submit_turn=submit))
    af.set_hold(home)
    assert "error" in server._submit_prompt_to_compute_host("r", "s", session, "hello")
    assert fence.active_count() == 1
    thread = server._start_session_work(lambda: None, name="inline-fallback", session=session)
    thread.join(5)
    assert fence.active_count() == 0


def test_real_wait_failure_and_build_cleanup(admitted, monkeypatch):
    from tui_gateway.turn_admission import start_build_work
    session, fence, home = admitted
    building, finish = threading.Event(), threading.Event()
    def build():
        building.set()
        assert finish.wait(5)
    build_thread = start_build_work(session, build)
    assert building.wait(5)
    monkeypatch.setattr(server, "_wait_agent_for_prompt", lambda *a: {"error": {"message": "build failed"}})
    monkeypatch.setattr(server, "_emit", lambda *a, **kw: None)
    monkeypatch.setattr(server, "_session_info", lambda *a, **kw: {})
    monkeypatch.setattr(server, "_emit_terminal_turn_error", lambda sid, record, error, **kw: server._fail_inflight_turn(record, error))
    af.set_hold(home)
    try:
        waiter = server._start_session_work(
            lambda: server._run_after_agent_ready("r", "s", session, "hello", None, None, None),
            name="failed-build-wait", session=session)
        waiter.join(5)
        assert session["inflight_turn"]["error"] == "build failed"
        assert not session["running"]
        assert fence.active_count() == 1
    finally:
        finish.set()
        build_thread.join(5)
    assert fence.active_count() == 0

