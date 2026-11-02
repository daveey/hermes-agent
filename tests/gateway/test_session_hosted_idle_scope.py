"""Only fully prepared hosted services bypass owner scope on idle ticks."""
import asyncio
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from gateway import session_hosted_service as hosted


def test_initialized_service_skips_scope_but_keeps_readiness_and_restart_gates(monkeypatch):
    service = SimpleNamespace(_transport_installed=True, start=Mock())
    authority = SimpleNamespace(hosted_room_service=service)
    runner = SimpleNamespace(session_runtime_descriptor={'state': 'starting'}, _draining=False)
    monkeypatch.setattr(hosted, 'active_authority', lambda runner: authority)
    monkeypatch.setattr(hosted, 'serving_authorities', lambda runner: [authority])
    scope = Mock(side_effect=AssertionError('initialized service entered scope'))
    monkeypatch.setattr(hosted, 'owner_scope', scope)
    from gateway import hosted_rooms_gateway_import
    imports = Mock()
    monkeypatch.setattr(hosted_rooms_gateway_import, 'import_after_ready', imports)

    async def run():
        assert await hosted.ensure_hosted_service(runner) is service
        service.start.assert_not_called()
        imports.assert_not_called()
        runner.session_runtime_descriptor['state'] = 'ready'
        runner._draining = True
        await hosted.ensure_hosted_service(runner)
        service.start.assert_not_called()
        imports.assert_not_called()
        runner._draining = False
        await hosted.ensure_hosted_service(runner)
        service.start.assert_called_once_with()
        imports.assert_called_once_with([authority])
        # A previously stopped coordinator still gets its restart opportunity on every tick.
        await hosted.ensure_hosted_service(runner)
        assert service.start.call_count == imports.call_count == 2
    asyncio.run(run())
    scope.assert_not_called()


@pytest.mark.parametrize('existing', [False, True])
def test_incomplete_preparation_remains_in_owner_scope(tmp_path, monkeypatch, existing):
    events = []
    service = SimpleNamespace(_transport_installed=False, attest=object())
    authority = SimpleNamespace(hosted_room_service=service if existing else None,
                                db=SimpleNamespace(db_path=tmp_path / 'state.db'), epoch=7)
    runner = SimpleNamespace(session_control_server=object())
    in_scope = False

    @contextmanager
    def scope(owner):
        nonlocal in_scope
        assert owner is authority
        in_scope = True
        events.append('enter')
        try:
            yield
        finally:
            in_scope = False
            events.append('exit')

    def prepare(*args, **kwargs):
        assert in_scope
        events.append('import')

    def construct(*args):
        assert in_scope
        events.append('construct')
        return service

    def install(*args, **kwargs):
        assert in_scope
        assert args[:2] == (runner.session_control_server, authority)
        events.append('install')

    from gateway import hosted_rooms_gateway_import, session_hosted_transport
    monkeypatch.setattr(hosted, 'owner_scope', scope)
    monkeypatch.setattr(hosted_rooms_gateway_import, 'import_standalone_rooms', prepare)
    monkeypatch.setattr(hosted, 'CanonicalHostedRoomService', construct)
    monkeypatch.setattr(session_hosted_transport, 'install_hosted_transport', install)
    asyncio.run(hosted._ensure_hosted_service(runner, authority))
    assert events == (['enter', 'install', 'exit'] if existing else
                      ['enter', 'import', 'construct', 'install', 'exit'])
    assert authority.hosted_room_service is service
    assert service._transport_installed is True
