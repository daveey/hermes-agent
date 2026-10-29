"""Session teardown must not rediscover plugins for a profile it already loaded.

``PluginManager`` is already cached per resolved home, and ``discover_and_load``
is a no-op once that manager has run. A multiplexed reaper still paid for two
full scans on the first teardown of a secondary profile: ``on_session_end``
ran inside the session scope, then ``on_session_finalize`` ran after the scope
closed and discovered the launch profile. Two consecutive finalizations of one
profile must therefore enter ``discover_and_load`` at most once, and only for
that profile.
"""

import threading
from unittest.mock import MagicMock

from hermes_constants import hermes_home_key
from hermes_cli.plugins import PluginManager, _reset_plugin_managers_for_tests


def _session(profile_home: str, session_id: str) -> dict:
    agent = MagicMock()
    agent.session_id = session_id
    agent.model = "test-model"
    agent.platform = "tui"
    agent._session_messages = None
    return {
        "agent": agent,
        "history": [{"role": "user", "content": "x"}],
        "history_lock": threading.Lock(),
        "session_key": session_id,
        "profile_home": profile_home,
        "source": "tui",
    }


def test_two_finalizations_discover_each_profile_at_most_once(tmp_path, monkeypatch):
    """The inner scan runs at most once for the session profile, never for the launch home."""
    from tui_gateway import server

    profile = tmp_path / "profile-b"
    profile.mkdir()
    (profile / ".env").write_text("", encoding="utf-8")
    calls: list[str] = []
    real_discover = PluginManager.discover_and_load

    def _record_discover(self, force=False):
        calls.append(hermes_home_key(self.home_path))
        return real_discover(self, force=force)

    _reset_plugin_managers_for_tests()
    monkeypatch.setattr(PluginManager, "discover_and_load", _record_discover)
    # The guard under test is the per-manager ``_discovered`` flag, not YAML parsing.
    monkeypatch.setattr(PluginManager, "_discover_and_load_inner", lambda self: None)
    try:
        server._finalize_session(_session(str(profile), "sess-1"))
        server._finalize_session(_session(str(profile), "sess-2"))
    finally:
        _reset_plugin_managers_for_tests()

    assert calls == [hermes_home_key(profile)], calls
