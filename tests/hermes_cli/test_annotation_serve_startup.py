"""Real serve composition admits native annotations before session captures."""
import asyncio
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from hermes_cli import fact_annotation_boot as boot
from hermes_cli import web_server
from tests.hermes_cli.test_serve_unloaded_sweeper import _quiet_boot


def test_explicit_on_real_serve_has_no_legacy_starter(monkeypatch):
    _quiet_boot(monkeypatch)
    monkeypatch.setenv('HERMES_FACTS_ANNOTATION', '1')
    calls = []
    runtime = SimpleNamespace(close=lambda: calls.append('close'))
    def prepare(*, on_created):
        calls.append('prepare')
        on_created(runtime)
        return runtime
    monkeypatch.setattr(boot, 'prepare', prepare)
    from hermes_cli import kanban_annotation_host as host
    from hermes_cli import kanban_annotation_prewarm as prewarm
    monkeypatch.setattr(host, 'start_holder', lambda role: calls.append('legacy'))
    monkeypatch.setattr(prewarm, 'start_annotation_prewarm', lambda: calls.append('prewarm'))
    with TestClient(web_server.app, raise_server_exceptions=False):
        assert web_server.app.state.annotation_facts is runtime
        assert calls == ['prepare']
    assert calls == ['prepare', 'close']
    assert web_server.app.state.annotation_facts is None


def test_off_real_serve_starts_legacy_exactly_once(monkeypatch):
    _quiet_boot(monkeypatch)
    monkeypatch.setenv('HERMES_FACTS_ANNOTATION', '0')
    calls = []
    monkeypatch.setattr(boot, 'prepare', lambda: pytest.fail('OFF admitted native'))
    from hermes_cli import kanban_annotation_host as host
    from hermes_cli import kanban_annotation_prewarm as prewarm
    monkeypatch.setattr(host, 'start_holder', lambda role: calls.append('legacy'))
    monkeypatch.setattr(prewarm, 'start_annotation_prewarm', lambda: calls.append('prewarm'))
    with TestClient(web_server.app, raise_server_exceptions=False):
        pass
    assert calls == ['prewarm', 'legacy']


def test_failed_native_startup_is_not_silent_legacy_fallback(monkeypatch):
    from hermes_cli.fact_annotation_lifespan import lifespan
    def fail(**kwargs):
        raise TimeoutError('not ready')
    monkeypatch.setattr(boot, 'prepare', fail)
    monkeypatch.setenv('HERMES_FACTS_ANNOTATION', '1')
    async def run():
        app = SimpleNamespace(state=SimpleNamespace())
        with pytest.raises(TimeoutError, match='not ready'):
            async with lifespan(app):
                pytest.fail('unready startup yielded')
        assert getattr(app.state, 'annotation_facts', None) is None
    asyncio.run(run())


def test_cleanup_failure_retains_custody_and_refuses_second_start(monkeypatch):
    from hermes_cli.fact_annotation_lifespan import lifespan
    calls = []
    def close():
        calls.append('close')
        raise TimeoutError('join pending')
    runtime = SimpleNamespace(close=close)
    def prepare(*, on_created):
        calls.append('prepare')
        on_created(runtime)
    monkeypatch.setattr(boot, 'prepare', prepare)
    monkeypatch.setenv('HERMES_FACTS_ANNOTATION', '1')
    async def run():
        app = SimpleNamespace(state=SimpleNamespace())
        with pytest.raises(TimeoutError, match='join pending'):
            async with lifespan(app):
                pass
        assert app.state.annotation_facts is runtime
        with pytest.raises(RuntimeError, match='custody retained'):
            async with lifespan(app):
                pytest.fail('replacement admitted')
        assert calls == ['prepare', 'close']
    asyncio.run(run())


def test_repeated_native_lifespan_and_unsupported_platform(monkeypatch):
    from hermes_cli import fact_annotation_lifespan as startup
    calls = []
    def prepare(*, on_created):
        calls.append('prepare')
        on_created(SimpleNamespace(close=lambda: calls.append('close')))
    monkeypatch.setattr(boot, 'prepare', prepare)
    monkeypatch.setenv('HERMES_FACTS_ANNOTATION', '1')
    async def run():
        app = SimpleNamespace(state=SimpleNamespace())
        for _ in range(2):
            async with startup.lifespan(app):
                assert app.state.annotation_facts is not None
        assert calls == ['prepare', 'close', 'prepare', 'close']
        monkeypatch.setattr(startup.sys, 'platform', 'darwin')  # os-marker: ok — gating only, no native emulation
        async with startup.lifespan(app):
            assert app.state.annotation_facts is None
        assert calls == ['prepare', 'close', 'prepare', 'close']
    asyncio.run(run())
