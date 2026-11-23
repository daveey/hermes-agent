"""Session consumer lifecycle and canonical selector contracts. [daveey]"""
import asyncio
import inspect
import threading

import pytest
from fastapi import HTTPException, Query

from hermes_cli.sessions_fact_routes import SessionsFactRoutes, selector_for
from hermes_cli.web_read_coalescing import coalesced_read
from tests.waiting import async_wait_until


def test_selector_keeps_defaults_and_explicit_values():
    def route(limit=Query(20, ge=0), offset=Query(0, ge=0), full=False, source=None):
        pass

    signature = inspect.signature(route)
    assert selector_for(signature, (), {}) == dict(limit=20, offset=0, full=False, source=None)
    assert selector_for(signature, (), dict(offset=700, full=True, source="cron")) == dict(
        limit=20, offset=700, full=True, source="cron")


@pytest.mark.asyncio
async def test_uninstalled_binding_preserves_coalescing_and_signature():
    calls = []
    started = threading.Event()
    release = threading.Event()

    def scan(limit=20):
        calls.append(limit)
        started.set()
        assert release.wait(5)
        return {"sessions": [], "limit": limit}

    bridge = SessionsFactRoutes()
    endpoint = bridge.wrap("sessions_sidebar", coalesced_read(scan))
    assert inspect.signature(endpoint) == inspect.signature(scan)
    first = asyncio.create_task(endpoint(limit=10))
    await async_wait_until(started.is_set, timeout=5)
    second = asyncio.create_task(endpoint(limit=10))
    await async_wait_until(lambda: bridge._active == 2, timeout=5)
    release.set()
    assert await first == await second == {"sessions": [], "limit": 10}
    assert calls == [10]


@pytest.mark.asyncio
async def test_disconnect_does_not_finish_scan_drain():
    from types import SimpleNamespace
    from hermes_cli.fact_contract import Scope
    from hermes_cli.fact_fake import FakeStore

    started = threading.Event()
    release = threading.Event()
    transitioned = threading.Event()

    def scan():
        started.set()
        assert release.wait(5)
        return {}

    bridge = SessionsFactRoutes().install(
        SimpleNamespace(v1=FakeStore(), demand=lambda *args: None), Scope("install", "profile"))
    endpoint = bridge.wrap("sessions_sidebar", coalesced_read(scan))
    request = asyncio.create_task(endpoint())
    await async_wait_until(started.is_set, timeout=5)
    request.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request
    # The disconnected scan must drain before facts can replace its reader.
    def transition():
        bridge.on_mode("facts", 1)
        transitioned.set()

    worker = threading.Thread(target=transition)
    worker.start()
    try:
        await async_wait_until(lambda: bridge._mode == "transition", timeout=5)
        assert bridge._active == 1
        assert not transitioned.is_set()
        with pytest.raises(HTTPException) as error:
            await endpoint()
        assert error.value.status_code == 503
    finally:
        release.set()
        await async_wait_until(transitioned.is_set, timeout=5)
        worker.join(5)
    assert bridge._active == 0
    bridge.on_mode("legacy", 2)
    assert await endpoint() == {}
    bridge.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("key,value", [("archived", "bad"), ("automated", "bad"), ("order", "bad")])
async def test_invalid_list_selectors_never_enter_legacy(key, value):
    async def legacy(archived="exclude", automated="include", order="recent"):
        pytest.fail("invalid selector reached legacy reader")

    endpoint = SessionsFactRoutes().wrap("sessions", legacy)
    with pytest.raises(HTTPException) as error:
        await endpoint(**{key: value})
    assert error.value.status_code == 400
    assert error.value.detail.startswith(key + " must be one of:")


def test_uninstalled_facts_and_unknown_modes_refuse():
    bridge = SessionsFactRoutes()
    with pytest.raises(RuntimeError, match="not installed"):
        bridge.on_mode("facts", 1)
    with pytest.raises(ValueError, match="unsupported"):
        bridge.on_mode("shadow", 1)
    assert bridge._mode == "legacy"


@pytest.mark.asyncio
async def test_shared_latch_rollback_reuses_coalescer_once():
    from types import SimpleNamespace
    from hermes_cli.fact_contract import Fact, Scope
    from hermes_cli.fact_fake import FakeStore

    store = FakeStore()
    scope = Scope("install", "profile")
    bridge = SessionsFactRoutes().install(SimpleNamespace(v1=store, demand=lambda *args: None), scope)
    calls = []
    entered = threading.Event()
    release = threading.Event()

    def scan(limit=20):
        calls.append(limit)
        entered.set()
        assert release.wait(5)
        return {"legacy": limit}

    endpoint = bridge.wrap("sessions_sidebar", coalesced_read(scan))
    selector = {"limit": 20}
    store.publish([Fact(store.view_key(scope, "sessions_sidebar", selector),
                        {"encoded": b'{"fact":true}'}, health="fresh")])
    store.report_ready("sessions", scope)
    with pytest.raises(RuntimeError, match="not ready"):
        store.activate(scope)
    for name in ("panel", "annotations", "board", "notifications"):
        store.bind_consumer(name, scope, lambda mode, generation: None)
        store.report_ready(name, scope)
    try:
        store.activate(scope)
        for _ in range(3):
            assert (await endpoint()).body == b'{"fact":true}'
        assert calls == []
        with pytest.raises(RuntimeError, match="idle and legacy"):
            bridge.close()
        store.rollback(scope)
        store.rollback(scope)  # Repeated rollback must not install another reader.
        first = asyncio.create_task(endpoint())
        await async_wait_until(entered.is_set, timeout=5)
        second = asyncio.create_task(endpoint())
        await async_wait_until(lambda: bridge._active == 2, timeout=5)
        release.set()
        assert await first == await second == {"legacy": 20}
        assert calls == [20]
    finally:
        release.set()
        store.rollback(scope)
        bridge.close()
    # A replacement owner has its own generation, not the predecessor's fence.
    successor = FakeStore()
    bridge.install(SimpleNamespace(v1=successor, demand=lambda *args: None), scope)
    try:
        assert bridge._generation == 0
    finally:
        bridge.close()


@pytest.mark.asyncio
async def test_slow_scan_refuses_activation_without_stranding_legacy():
    from types import SimpleNamespace
    from hermes_cli.fact_contract import Scope
    from hermes_cli.fact_fake import FakeStore

    bridge = SessionsFactRoutes(drain_timeout=0).install(
        SimpleNamespace(v1=FakeStore(), demand=lambda *args: None), Scope("install", "profile"))
    bridge._begin()
    try:
        bridge.on_mode("legacy", 1)
        assert bridge._mode == "legacy"
        with pytest.raises(RuntimeError, match="off the event loop"):
            bridge.on_mode("facts", 2)
        with pytest.raises(TimeoutError, match="activation refused"):
            await asyncio.to_thread(bridge.on_mode, "facts", 2)
        assert bridge._mode == "legacy"
        assert bridge._generation == 1
        bridge.on_mode("legacy", 2)
        bridge.on_mode("facts", 1)
        assert bridge._mode == "legacy"
    finally:
        bridge._end()
        bridge.close()
