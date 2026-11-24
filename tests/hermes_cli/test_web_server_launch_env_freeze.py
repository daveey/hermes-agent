"""The launch-env freeze is the LAST boot step, not the first.

Activation snapshots ``os.environ`` as the launch profile's credentials, and that snapshot is the
only source for launch keys with no ``.env`` to rebuild them from (systemd ``Environment=``,
``op run``, Compose). Taken at the TOP of ``start_server`` it missed everything the rest of boot
injected or rotated, for the process lifetime.
"""
import asyncio
from contextlib import nullcontext
from types import SimpleNamespace
import os

import pytest

from agent import secret_scope
from hermes_cli import web_server
from tui_gateway import launch_profile_policy

LATE_KEY = "LAUNCH_FREEZE_PROBE_TOKEN"


def test_boot_time_credential_injection_is_inside_the_frozen_launch_env(tmp_path, monkeypatch):
    monkeypatch.setattr(secret_scope, "_MULTIPLEX_ACTIVE", False)
    monkeypatch.setattr(launch_profile_policy, "_snapshot", None)
    monkeypatch.delenv(LATE_KEY, raising=False)
    # A two-profile host, without touching the live install's profiles/.
    monkeypatch.setattr(launch_profile_policy, "_servable_profile_homes",
                        lambda: {tmp_path / "a", tmp_path / "b"})

    # Stand-ins for the boot steps that follow the old (top-of-function) activation point. A
    # provider key injected by the auth gate / keepalive / a lifespan hook is the real case.
    def _inject(*_args, **_kwargs):
        os.environ[LATE_KEY] = "injected-during-boot"

    monkeypatch.setattr(web_server, "_configure_auth_gate", _inject)
    monkeypatch.setattr(web_server, "_build_uvicorn_server", lambda *a, **k: (object(), object()))
    monkeypatch.setattr(web_server, "_port_bind_conflict", lambda *a, **k: False)
    served: list[bool] = []
    monkeypatch.setattr(web_server, "_run_serve", lambda *a, **k: served.append(True))

    try:
        web_server.start_server(open_browser=False, headless=True)
    finally:
        os.environ.pop(LATE_KEY, None)

    assert served, "start_server never reached the serve step"
    assert secret_scope.is_multiplex_active()
    assert launch_profile_policy.capture_launch_env().get(LATE_KEY) == "injected-during-boot", (
        "the launch env was frozen before boot finished injecting credentials")


@pytest.mark.parametrize('failure', ['ready', 'loop'])
def test_exception_after_bind_drains_before_runner_teardown(monkeypatch, failure):
    calls = []
    async def startup():
        server.started = True
    async def loop():
        calls.append('loop')
        raise RuntimeError('loop failure')
    async def shutdown():
        calls.append('shutdown')
    server = SimpleNamespace(started=False, should_exit=False, capture_signals=nullcontext,
                             startup=startup, main_loop=loop, shutdown=shutdown)
    config = SimpleNamespace(loaded=True, lifespan_class=lambda config: object())
    def ready(*args, **kwargs):
        calls.append('ready')
        if failure == 'ready':
            raise RuntimeError('ready failure')
    monkeypatch.setattr(web_server, '_configure_auth_gate', lambda *a, **kw: None)
    monkeypatch.setattr(web_server, '_build_uvicorn_server', lambda *a, **kw: (config, server))
    monkeypatch.setattr(web_server, '_port_bind_conflict', lambda *a, **kw: False)
    monkeypatch.setattr(launch_profile_policy, 'activate_multi_profile_hosting_eagerly', lambda: None)
    monkeypatch.setattr(web_server, '_on_server_started', ready)
    monkeypatch.setattr(web_server, '_run_serve', lambda serve, *a: asyncio.run(serve()))
    with pytest.raises(RuntimeError, match=failure + ' failure'):
        web_server.start_server(open_browser=False, headless=False)
    assert calls[-1] == 'shutdown'
    assert calls.count('shutdown') == 1
