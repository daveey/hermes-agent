"""Native gateway watcher + real busy-queue admission; no external transport."""
import asyncio
import sys
from types import SimpleNamespace

import pytest
import pytest_asyncio

from gateway.config import GatewayConfig, Platform
from gateway.run import GatewayRunner, _profile_runtime_scope
from gateway.session import SessionSource
from gateway.session_context import scoped_current_session_id
from agent.subagent_lifecycle import bind_subagent_parent
from tests.gateway.test_completion_admission_compression_race import _QueuedTransport
from tests.tools.test_process_ownership_snapshot import capture
from tools.process_registry import ProcessRegistry


@pytest_asyncio.fixture
async def owned(tmp_path, monkeypatch):
    import tools.process_registry as pr
    registry = ProcessRegistry()
    monkeypatch.setattr(pr, "process_registry", registry)
    monkeypatch.setattr("hermes_cli.profiles.get_active_profile_name", lambda: "default")
    monkeypatch.setattr("hermes_cli.lifecycle.invoke_hook", lambda *a, **k: [])
    with _profile_runtime_scope(tmp_path):
        runner = GatewayRunner(GatewayConfig(sessions_dir=tmp_path / "sessions"))
        runner._running = True
        runner._gateway_loop = asyncio.get_running_loop()
        adapter = _QueuedTransport()
        adapter._running = True
        adapter.gateway_runner = runner
        runner.adapters[Platform.TELEGRAM] = adapter
        entry = runner.session_store.get_or_create_session(SessionSource(
            platform=Platform.TELEGRAM, chat_id="process-proof", chat_type="dm"))
        async def unused(event):
            raise AssertionError("busy admission must queue, not run a model")
        adapter.set_message_handler(unused)
        adapter._active_sessions[entry.session_key] = asyncio.Event()
        agent = SimpleNamespace(session_id=entry.session_id, model="example", _current_turn_id="turn")
        with bind_subagent_parent(agent), scoped_current_session_id(entry.session_id):
            uid = capture(agent)
            process = registry.spawn_local(
                f"exec {sys.executable} -c 'import threading; threading.Event().wait(60)'",
                session_key=entry.session_key)
            process.notify_on_complete = True
        db = runner.session_store._db
        db._conn.execute("UPDATE sessions SET profile_name='default' WHERE id=?", (entry.session_id,))
        db.append_message(entry.session_id, "user", "Repair example app", message_uid=uid)
        watcher = dict(session_id=process.id, session_key=entry.session_key,
                       parent_session_id=entry.session_id, check_interval=60,
                       platform="telegram", chat_id="process-proof", notify_on_complete=True)
        task = asyncio.create_task(runner._run_process_watcher(watcher))
        await asyncio.sleep(0)
        event = dict(type="completion", session_id="proc_0123456789ab", started_at=1,
                     session_key=entry.session_key, parent_session_id=entry.session_id)
        try:
            yield runner, adapter, registry, process, entry, event, task
        finally:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            process.notify_on_complete = False
            process.process.kill()
            process.process.wait(timeout=5)
            process._completion_event.wait(5)
            runner._running = False
            runner.session_store.close_all_db_handles()
            runner.close_all_session_db_handles()


def health(owned):
    from gateway.process_consumer_health import receipt_version
    return receipt_version(owned[2].ownership_snapshot(owned[3].id))


@pytest.mark.asyncio
async def test_gateway_requires_real_admission_then_revokes_on_stop(owned):
    runner, adapter, registry, process, entry, event, _task = owned
    assert registry.ownership_snapshot(process.id) is not None
    assert health(owned) is None
    assert await runner._inject_watch_notification("completed fixture", event) is True
    assert adapter.get_pending_message(entry.session_key).internal
    assert health(owned) is not None
    runner._invalidate_session_run_generation(entry.session_key, reason="stop")
    assert health(owned) is None
    assert await runner._inject_watch_notification("late completion", event) is True
    assert health(owned) is None  # old watcher cannot re-bootstrap after Stop


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["none", "exception", "watch", "foreign-parent"])
async def test_no_receipt_for_unsupported_or_refused_admission(owned, monkeypatch, failure):
    runner, adapter, _, _, _, event, _ = owned
    if failure in {"none", "exception"}:
        async def refuse(event):
            if failure == "exception":
                raise RuntimeError("fixture refusal")
        monkeypatch.setattr(adapter, "handle_message", refuse)
    elif failure == "watch":
        event["type"] = "watch_match"
    else:
        event["parent_session_id"] = "foreign"
    await runner._inject_watch_notification("fixture", event)
    assert health(owned) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("loss", ["stale", "shutdown", "adapter", "watcher", "route", "new-user"])
async def test_receipt_requires_live_unchanged_consumer(owned, monkeypatch, loss):
    from gateway import process_consumer_health as ph
    from gateway.platforms.event import MessageEvent, MessageType
    runner, adapter, _, _, entry, event, task = owned
    assert await runner._inject_watch_notification("fixture", event) is True
    assert health(owned) is not None
    if loss in {"stale", "shutdown", "adapter", "route"}:
        obj, name, value = {"stale": (ph, "MAX_AGE", -1), "shutdown": (runner, "_running", False),
                            "adapter": (adapter, "_running", False),
                            "route": (entry, "session_id", "replacement")}[loss]
        monkeypatch.setattr(obj, name, value)
    elif loss == "watcher":
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
    else:
        # Real base-adapter ingress revokes before this human message sits in its busy queue.
        adapter.get_pending_message(entry.session_key)
        await adapter.handle_message(MessageEvent(text="new instruction", message_type=MessageType.TEXT,
                                                  source=entry.origin))
    assert health(owned) is None


@pytest.mark.asyncio
async def test_late_watcher_cannot_restore_pre_stop_ownership(owned):
    runner, _, registry, process, entry, _event, task = owned
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)
    runner._invalidate_session_run_generation(entry.session_key, reason="stop")
    assert registry.ownership_snapshot(process.id) is None


@pytest.mark.asyncio
async def test_late_admission_cannot_resurrect_receipt(owned, monkeypatch):
    runner, adapter, _, _, entry, event, _ = owned
    original = adapter.handle_message
    async def stop_during_admission(message):
        await original(message)
        runner._invalidate_session_run_generation(entry.session_key, reason="stop")
    monkeypatch.setattr(adapter, "handle_message", stop_during_admission)
    assert await runner._inject_watch_notification("fixture", event) is True
    assert health(owned) is None


@pytest.mark.asyncio
async def test_failed_admission_clears_previous_success(owned, monkeypatch):
    runner, adapter, _, _, _, event, _ = owned
    assert await runner._inject_watch_notification("fixture", event) is True
    assert health(owned) is not None
    async def refuse(message):
        return None
    monkeypatch.setattr(adapter, "handle_message", refuse)
    assert await runner._inject_watch_notification("fixture", event) is False
    assert health(owned) is None


@pytest.mark.asyncio
async def test_readback_is_exact_bounded_and_nonconsuming(owned, monkeypatch):
    from dataclasses import replace
    from gateway import process_consumer_health as ph
    runner, adapter, registry, process, entry, event, _ = owned
    assert await runner._inject_watch_notification("fixture", event) is True
    snap = registry.ownership_snapshot(process.id)
    version = ph.receipt_version(snap)
    assert version is not None
    for field, value in {"home": "/foreign", "session_id": "foreign", "session_key": "foreign",
                         "message_uid": "foreign", "generation": "foreign", "process_id": "foreign"}.items():
        assert ph.receipt_version(replace(snap, **{field: value})) is None
    with ph._lock:
        assert ph.receipt_version(snap) is None
    with runner.session_store._lock:
        assert ph.receipt_version(snap) is None
    monkeypatch.setattr(registry, "drain_notifications", lambda *a, **k: pytest.fail("consuming read"))
    assert ph.receipt_version(snap, expected=version) == version
    assert adapter.get_pending_message(entry.session_key) is not None


@pytest.mark.asyncio
async def test_changed_admission_source_cannot_certify_old_route(owned):
    runner, adapter, _, _, entry, event, _ = owned
    assert await runner._inject_watch_notification("fixture", event) is True
    assert health(owned) is not None
    adapter.get_pending_message(entry.session_key).source.chat_id = "foreign-chat"
    assert health(owned) is None
