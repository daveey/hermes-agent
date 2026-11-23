"""Authorized request-local bindings, not process-global profile state. [daveey]"""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from hermes_cli.fact_contract import Fact, Scope
from hermes_cli.fact_fake import FakeStore
from hermes_cli.sessions_fact_routes import ScopedSessionsRoutes, use


def installed(router, profile):
    store = FakeStore()
    scope = Scope("install", profile)
    binding = router.install(SimpleNamespace(v1=store, demand=lambda *args: None), scope)
    store.publish([Fact(store.view_key(scope, "sessions_sidebar", {}),
                        {"encoded": ('{"profile":"' + profile + '"}').encode()}, health="fresh")])
    for name in ("panel", "annotations", "board", "notifications"):
        store.bind_consumer(name, scope, lambda *args: None)
        store.report_ready(name, scope)
    store.report_ready("sessions", scope)
    store.activate(scope)
    return store, scope, binding


def close(item):
    store, scope, binding = item
    store.rollback(scope)
    binding.close()


@pytest.mark.asyncio
async def test_concurrent_scopes_pin_one_binding_and_restore_nested_context():
    router = ScopedSessionsRoutes()
    calls = []

    async def legacy():
        calls.append("legacy")
        return {"legacy": True}

    endpoint = router.wrap("sessions_sidebar", legacy)
    a, b = installed(router, "a"), installed(router, "b")
    barrier = asyncio.Barrier(2)

    async def request(own, other):
        with use(own[2]):
            await barrier.wait()
            first = (await endpoint()).body
            with use(other[2]):
                second = (await endpoint()).body
            await barrier.wait()
            assert (await endpoint()).body == first
            return first, second

    try:
        assert await asyncio.gather(request(a, b), request(b, a)) == [
            (b'{"profile":"a"}', b'{"profile":"b"}'),
            (b'{"profile":"b"}', b'{"profile":"a"}')]
        with pytest.raises(HTTPException) as error:
            await endpoint()
        assert error.value.status_code == 503
        assert calls == []
        # Only an explicit authorized nonparticipant may select legacy.
        with use(None):
            assert await endpoint() == {"legacy": True}
        assert calls == ["legacy"]
        # Rolling back one scope does not change another scope's binding.
        a[0].rollback(a[1])
        with use(a[2]):
            assert await endpoint() == {"legacy": True}
        with use(b[2]):
            assert (await endpoint()).body == b'{"profile":"b"}'
    finally:
        close(a)
        close(b)


@pytest.mark.asyncio
async def test_missing_foreign_and_closed_bindings_fail_without_legacy():
    router = ScopedSessionsRoutes()
    foreign_router = ScopedSessionsRoutes()
    own = installed(router, "a")
    foreign = installed(foreign_router, "b")

    async def legacy():
        pytest.fail("missing or closed binding fell through to I/O")

    endpoint = router.wrap("sessions_sidebar", legacy)
    try:
        for binding in (foreign[2], object()):
            with use(binding), pytest.raises(HTTPException) as error:
                await endpoint()
            assert error.value.status_code == 503
        close(own)
        with pytest.raises(HTTPException) as error:
            await endpoint()
        assert error.value.status_code == 503
        with use(own[2]), pytest.raises(HTTPException) as error:
            await endpoint()
        assert error.value.status_code == 503
        replacement = installed(router, "a")
        try:
            with use(replacement[2]):
                assert (await endpoint()).body == b'{"profile":"a"}'
            with use(own[2]), pytest.raises(HTTPException) as error:
                await endpoint()
            assert error.value.status_code == 503
        finally:
            close(replacement)
    finally:
        close(foreign)


@pytest.mark.asyncio
async def test_startup_install_refuses_inflight_unbound_legacy():
    router = ScopedSessionsRoutes()
    entered, release = asyncio.Event(), asyncio.Event()

    async def legacy():
        entered.set()
        await release.wait()
        return {}

    endpoint = router.wrap("sessions_sidebar", legacy)
    request = asyncio.create_task(endpoint())
    await entered.wait()
    try:
        with pytest.raises(RuntimeError, match="before serving"):
            installed(router, "a")
    finally:
        release.set()
        assert await request == {}
    item = installed(router, "a")
    close(item)
