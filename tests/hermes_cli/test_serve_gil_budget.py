"""Serve and dashboard share the gateway's interpreter fairness policy."""
import sys

import pytest

from hermes_cli import web_server


@pytest.mark.parametrize(('override', 'expected'), [(None, .001), ('0.5', .0005), ('0', .005)])
def test_serve_applies_switch_interval_before_runtime(monkeypatch, override, expected):
    if override is None:
        monkeypatch.delenv('HERMES_GATEWAY_SWITCH_INTERVAL_MS', raising=False)
    else:
        monkeypatch.setenv('HERMES_GATEWAY_SWITCH_INTERVAL_MS', override)
    previous = sys.getswitchinterval()
    observed = []
    monkeypatch.setattr(web_server, '_start_server_runtime', lambda **kw: observed.append(sys.getswitchinterval()))
    try:
        sys.setswitchinterval(.005)
        web_server.start_server(open_browser=False)
        assert observed == [pytest.approx(expected)]
    finally:
        sys.setswitchinterval(previous)
