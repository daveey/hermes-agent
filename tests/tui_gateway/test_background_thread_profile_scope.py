"""Background threads started from a profile-scoped turn carry that profile's scope.

Under multiplex the HERMES_HOME override and secret scope are contextvars bound per turn; a bare
``threading.Thread`` / ``Timer`` starts with an EMPTY context and resolves the LAUNCH profile's config
and credentials (or fails closed on secrets). Two production spawn paths are exercised here through
their real entry points: the auto-title thread and the ws-orphan reap Timer → session teardown.
"""

import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from agent.secret_scope import (
    UnscopedSecretError, build_profile_secret_scope, get_secret, reset_secret_scope, set_multiplex_active,
    set_secret_scope,
)
from hermes_constants import get_hermes_home, reset_hermes_home_override, set_hermes_home_override


@pytest.fixture
def served_home(tmp_path, monkeypatch):
    a = tmp_path / ".hermes"
    b = a / "profiles" / "b"
    b.mkdir(parents=True)
    (a / ".env").write_text("TITLE_KEY=a-key\n", encoding="utf-8")
    (b / ".env").write_text("TITLE_KEY=b-key\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(a))
    monkeypatch.setenv("TITLE_KEY", "a-key")
    set_multiplex_active(True)
    try:
        yield a, b
    finally:
        set_multiplex_active(False)


def _observe_scope(seen: dict, done: threading.Event) -> None:
    seen["home"] = str(get_hermes_home())
    try:
        seen["key"] = get_secret("TITLE_KEY")
    except UnscopedSecretError as exc:
        seen["key"] = exc
    done.set()


def test_auto_title_thread_runs_in_the_turns_profile_scope(served_home, monkeypatch):
    """``maybe_auto_title`` fires ``auto_title_session`` on a thread; it must see the turn's profile."""
    import agent.title_generator as tg

    _a, b = served_home
    seen, done = {}, threading.Event()
    monkeypatch.setattr(tg, "auto_title_session", lambda *args, **kwargs: _observe_scope(seen, done))
    monkeypatch.setattr(tg, "apply_instant_title", lambda *args, **kwargs: None)
    db = MagicMock()
    db.get_session_title.return_value = None
    home_token = set_hermes_home_override(str(b))
    secret_token = set_secret_scope(build_profile_secret_scope(b))
    try:
        tg.maybe_auto_title(db, "sess-1", "hello there", [{"role": "user", "content": "hello there"}])
    finally:
        reset_secret_scope(secret_token)
        reset_hermes_home_override(home_token)
    assert done.wait(timeout=60), "auto-title thread never ran"
    assert seen == {"home": str(b), "key": "b-key"}


def test_ws_orphan_reap_tears_down_under_the_sessions_profile(served_home, monkeypatch):
    """The reap Timer (empty context) → ``_teardown_popped_session``: memory commit + ``agent.close`` run
    under ``session['profile_home']``, so the provider reads B's config/credentials, not the launch's."""
    from tui_gateway import server

    a, b = served_home
    seen_commit, seen_close = {}, {}
    done = threading.Event()
    agent = MagicMock()
    agent._session_messages = None
    agent.session_id = "sess-b"
    agent.commit_memory_session = lambda history: _observe_scope(seen_commit, threading.Event())
    agent.close = lambda: _observe_scope(seen_close, done)
    sid = "ws-b"
    session = {
        "agent": agent, "history": [{"role": "user", "content": "x"}], "history_lock": threading.Lock(),
        "session_key": "sess-b", "profile_home": str(b), "transport": server._detached_ws_transport,
        "last_active": 0.0,
    }
    # Real plugin discovery (scan + YAML-parse every installed plugin manifest, twice) is ~2.5s of CPU
    # inside the timed teardown and unrelated to the profile-scope contract; under CPU throttling it,
    # not the scope binding, blew the wait. Stub only the plugin dispatch leaf: the real Timer ->
    # _teardown_popped_session -> lifecycle.invoke_hook -> commit/close path still runs.
    from hermes_cli import lifecycle

    seen_hooks = []
    monkeypatch.setattr(
        lifecycle, "_plugin_hooks",
        lambda hook_name, **kwargs: seen_hooks.append((hook_name, str(get_hermes_home()))) or [])
    # Same for the reap's keep-alive gates (live delegations / wakeup schedules / background processes):
    # each opens state.db, whose writer preflight scans every host process's /proc fds (~0.9s CPU, scales
    # with host load). Reap eligibility has its own tests; here the session is simply idle.
    for gate in ("_session_has_active_delegations", "_session_owns_live_wakeup_schedule",
                 "_session_owns_running_process"):
        monkeypatch.setattr(server, gate, lambda *args, **kwargs: False)
    # Warm the one-time costs outside the wait so it times only the Timer -> teardown -> scope binding:
    # first import of tools.approval, and B's state.db generation in the refcounted registry (its writer
    # open also runs the /proc fd scan). Holding a ref makes the teardown's acquire a cache hit.
    import tools.approval
    from hermes_state_registry import acquire, release_or_close

    warm_db = acquire(Path(b) / "state.db")
    monkeypatch.setattr(server, "_WS_ORPHAN_REAP_GRACE_S", 0.01)
    monkeypatch.setattr(server, "_ws_session_is_orphaned", lambda s: s is session)
    with server._sessions_lock:
        server._sessions[sid] = session
    try:
        # Keep the registry lock until we capture the real Timer: even an immediate
        # callback cannot finish and remove its registration before we own its cleanup.
        with server._sessions_lock:
            server._schedule_ws_orphan_reap(sid)
            timer = server._pending_ws_reaps[sid]
        try:
            # Real lifecycle work on CPU-throttled runners can exceed 10s. This
            # event returns immediately on completion; it is not a timing assertion.
            assert done.wait(timeout=60), "reap timer never tore the session down"
        finally:
            server._cancel_ws_orphan_reap(sid)
            timer.join(timeout=60)
            assert not timer.is_alive(), "reap timer outlived its profile fixtures"
    finally:
        with server._sessions_lock:
            server._sessions.pop(sid, None)
        release_or_close(warm_db)
    assert seen_commit == {"home": str(b), "key": "b-key"}
    assert seen_close == {"home": str(b), "key": "b-key"}
    assert ("on_session_end", str(b)) in seen_hooks  # lifecycle hooks dispatched under B too
    assert Path(get_hermes_home()) == a  # the test thread's own context is untouched
