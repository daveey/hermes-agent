"""Startup hooks must not import the agent until actual inference-side demand."""
from types import SimpleNamespace


def test_message_host_loads_gateway_on_delivery_not_publication(monkeypatch):
    from hermes_cli import web_gateway_lazy as lazy
    from hermes_cli import plugins
    calls, published = [], []
    manager = SimpleNamespace(set_tui_message_injector=lambda *a: None,
                              clear_tui_message_injector=lambda *a: calls.append('manager-clear'))
    gateway = SimpleNamespace(inject_tui_session_message=lambda **kw: calls.append(kw) or True)
    monkeypatch.setattr(plugins, 'get_plugin_manager', lambda: manager)
    monkeypatch.setattr(plugins, 'publish_tui_message_host', lambda *a: published.append(a))
    monkeypatch.setattr(plugins, 'clear_published_tui_message_host', lambda owner: calls.append(owner))
    monkeypatch.setattr(lazy, 'load_gateway', lambda: calls.append('load') or gateway)
    lazy.install_message_host()
    assert not calls
    owner, inject = published[0]
    assert inject(session_key='fixture', content='notice') is True
    assert calls == ['load', {'session_key': 'fixture', 'content': 'notice'}]
    lazy.clear_message_host()
    assert calls[-2:] == [owner, 'manager-clear']


def test_readopt_callback_loads_gateway_only_when_a_job_exits(monkeypatch):
    from hermes_cli import web_gateway_lazy as lazy
    calls = []
    registry = SimpleNamespace(on_unknown_exit=None)
    gateway = SimpleNamespace(_readopted_exit_deliver=lambda evt, **kw: calls.append((evt, kw)))
    monkeypatch.setattr(lazy, 'load_gateway', lambda: calls.append('load') or gateway)
    lazy.install_readopt_waker(registry)
    assert not calls
    registry.on_unknown_exit({'session_key': 'fixture'})
    assert calls == ['load', ({'session_key': 'fixture'}, {'registry': registry})]


def test_hosted_service_proxy_resolves_attributes_on_first_use(monkeypatch):
    from hermes_cli import web_gateway_lazy as lazy
    calls = []
    monkeypatch.setattr(lazy, 'load_gateway', lambda: calls.append('load') or SimpleNamespace(_methods={'x': 'handler'}))
    proxy = lazy.GatewayOnDemand()
    assert not calls
    assert proxy._methods == {'x': 'handler'}
    assert calls == ['load']
