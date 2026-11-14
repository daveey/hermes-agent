"""Quiet-v3 R4-F F6: the dashboard PTY child holds a local-work reservation from before fork/exec until
``close()`` has proven the leader exited.

The REAL ``PtyBridge.spawn``/``close`` (the subclass the /api/pty handler uses) run against a real
``local_work.Registry``; only the ptyprocess child is faked.
"""
import pytest

from hermes_cli import local_work
from hermes_constants import hermes_home_key

pytestmark = pytest.mark.platforms("posix")


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    reg = local_work.Registry(homes=[hermes_home_key(tmp_path)])
    monkeypatch.setattr(local_work, "_registry", reg)
    return reg


def _raw(chat):
    """The platform bridge the custody subclass wraps (the bridge itself on a tree without custody)."""
    return getattr(chat, "_RawPtyBridge", chat.PtyBridge)


class FakeChild:
    def __init__(self, survives_close=False):
        self.alive = True
        self.survives_close = survives_close

    def isalive(self):
        return self.alive


@pytest.fixture
def bridge_env(monkeypatch):
    """Fake only the physical child: the raw bridge's spawn creates it, its close 'kills' it."""
    from hermes_cli import web_server_chat as chat

    seen = {"children": []}

    def raw_spawn(cls, *args, **kwargs):
        seen["active_at_spawn"] = local_work._registry.snapshot().active if local_work._registry else None
        if seen.get("spawn_error"):
            raise OSError("fork failed")
        bridge = cls.__new__(cls)
        bridge._proc = FakeChild(survives_close=seen.get("survives_close", False))
        seen["children"].append(bridge._proc)
        return bridge

    def raw_close(self):
        if not self._proc.survives_close:
            self._proc.alive = False

    monkeypatch.setattr(_raw(chat), "spawn", classmethod(raw_spawn))
    monkeypatch.setattr(_raw(chat), "close", raw_close)
    return chat, seen


def _clean(reg):
    snap = reg.snapshot()
    assert snap.unknowns == (), snap.unknowns
    assert snap.active == 0, dict(snap.per_kind)


def test_reserved_before_fork_and_released_at_verified_close(registry, bridge_env):
    chat, seen = bridge_env
    epoch0 = registry.snapshot().epoch
    bridge = chat.PtyBridge.spawn(["fake-tui"])
    assert seen["active_at_spawn"] == 1, "reserved before the child is created"
    assert registry.snapshot().epoch > epoch0
    assert registry.snapshot().active == 1, "a live PTY child holds the reservation"
    bridge.close()
    _clean(registry)
    bridge.close()  # idempotent
    _clean(registry)


def test_spawn_failure_releases(registry, bridge_env):
    chat, seen = bridge_env
    seen["spawn_error"] = True
    with pytest.raises(OSError):
        chat.PtyBridge.spawn(["fake-tui"])
    _clean(registry)


def test_child_that_survives_close_keeps_token_and_latches_unknown(registry, bridge_env):
    chat, seen = bridge_env
    seen["survives_close"] = True
    bridge = chat.PtyBridge.spawn(["fake-tui"])
    bridge.close()
    snap = registry.snapshot()
    assert snap.active == 1
    assert "pty-child-exit-unverified" in snap.unknowns


def test_pty_route_holds_reservation_while_child_runs_and_releases_after_close(registry, bridge_env, monkeypatch,
                                                                              _isolate_hermes_home):
    """/api/pty (legacy 1:1 path) through the real handler: EOF closes the bridge, which releases."""
    from urllib.parse import urlencode
    from starlette.testclient import TestClient
    from tests.waiting import wait_until
    import hermes_cli.web_server as ws

    chat, seen = bridge_env
    reads = []
    active_during_read = []

    def read(self, timeout):
        active_during_read.append(local_work._registry.snapshot().active)
        if not reads:
            reads.append(1)
            return b"ready"
        return None

    monkeypatch.setattr(_raw(chat), "read", read)
    monkeypatch.setattr(_raw(chat), "resize", lambda self, **kw: None, raising=False)
    monkeypatch.setattr(ws, "_DASHBOARD_EMBEDDED_CHAT_ENABLED", True)
    monkeypatch.setattr(chat, "_resolve_chat_argv", lambda **kw: (["fake-tui"], None, None))
    ws.app.state.pty_active_session_files = {}
    client = TestClient(ws.app)
    try:
        with client.websocket_connect(f"/api/pty?{urlencode({'token': ws._SESSION_TOKEN})}") as conn:
            assert conn.receive_bytes() == b"ready"
            # Keep the ASGI portal alive until EOF has reaped the child. Leaving
            # this context cancels the handler and can prevent close from even
            # reaching its executor; waiting after teardown cannot repair that.
            wait_until(lambda: registry.snapshot().active == 0, what="EOF closes bridge and releases")
    finally:
        client.close()
    assert seen["active_at_spawn"] == 1
    assert len(active_during_read) == 2, "the reader consumed data and then EOF"
    assert all(n == 1 for n in active_during_read)
    _clean(registry)


def test_no_registry_is_inert(bridge_env, monkeypatch):
    chat, _seen = bridge_env
    monkeypatch.setattr(local_work, "_registry", None)
    bridge = chat.PtyBridge.spawn(["fake-tui"])
    bridge.close()
