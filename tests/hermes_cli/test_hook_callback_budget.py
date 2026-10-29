"""Per-callback hook budgets and the host-deadline contract for bounded hook callbacks [daveey].

A turn-end checker (``pre_verify``) that makes bounded model calls can declare its own budget with the
``hermes_hook_timeout`` attribute instead of raising ``plugins.hook_callback_timeout`` for every hook.
While it runs, :func:`current_hook_call` exposes the host's deadline, and ``abandoned`` is set the moment
the host stops waiting, so a late callback can tell its result is discarded.
"""

import logging
import threading
import time

import pytest

from hermes_cli.plugins import (
    HOOK_CALLBACK_TIMEOUT_ATTR,
    PluginManager,
    current_hook_call,
    get_pre_verify_continue_message,
)
from hermes_cli.plugins_dispatch import hook_callback_timeout_for
from tests.waiting import wait_until


@pytest.fixture
def global_timeout(monkeypatch):
    def _set(value: float) -> None:
        monkeypatch.setattr("hermes_cli.plugins._resolve_hook_callback_timeout", lambda: value)
    return _set


def _sleeper(seconds: float, result):
    def cb(**_kwargs):
        time.sleep(seconds)  # health: allow HX013 -- a slow callback is the behaviour under test
        return result
    return cb


class TestCallbackBudget:
    def test_declared_budget_outlives_global_timeout(self, global_timeout):
        """RED before: a 0.3s callback under a 0.1s global bound is abandoned and skipped."""
        global_timeout(0.1)
        cb = _sleeper(0.3, {"action": "continue", "message": "m"})
        setattr(cb, HOOK_CALLBACK_TIMEOUT_ATTR, 2.0)
        mgr = PluginManager()
        mgr._hooks["pre_verify"] = [cb]
        assert mgr.invoke_hook("pre_verify", session_id="s1") == [{"action": "continue", "message": "m"}]

    def test_declared_budget_still_abandons_past_it(self, global_timeout):
        """The budget is a bound, not an exemption: past it the callback is skipped as before."""
        global_timeout(5.0)
        hold = threading.Event()

        def cb(**_kwargs):
            hold.wait(5.0)
            return "late"
        setattr(cb, HOOK_CALLBACK_TIMEOUT_ATTR, 0.1)
        mgr = PluginManager()
        mgr._hooks["pre_verify"] = [cb]
        t0 = time.monotonic()
        assert mgr.invoke_hook("pre_verify", session_id="s1") == []
        assert time.monotonic() - t0 < 2.0
        hold.set()

    @pytest.mark.parametrize("raw", [True, -1, 0, "90", None, float("nan")])
    def test_invalid_declared_budget_uses_global(self, raw):
        def cb(**_kwargs):
            return None
        if raw is not None:
            setattr(cb, HOOK_CALLBACK_TIMEOUT_ATTR, raw)
        assert hook_callback_timeout_for(cb, 30.0) == 30.0

    def test_declared_budget_is_clamped(self):
        from hermes_cli.plugins import _MAX_HOOK_CALLBACK_TIMEOUT_SECS

        def cb(**_kwargs):
            return None
        setattr(cb, HOOK_CALLBACK_TIMEOUT_ATTR, 10_000)
        assert hook_callback_timeout_for(cb, 30.0) == _MAX_HOOK_CALLBACK_TIMEOUT_SECS

    def test_declared_budget_does_not_enable_threading_when_disabled(self, global_timeout):
        """plugins.hook_callback_timeout <= 0 keeps every hook synchronous, declared budget or not."""
        global_timeout(0)
        seen = []

        def cb(**_kwargs):
            seen.append((threading.current_thread() is threading.main_thread(), current_hook_call()))
            time.sleep(0.2)  # health: allow HX013 -- outlives the declared budget on purpose
            return "done"
        setattr(cb, HOOK_CALLBACK_TIMEOUT_ATTR, 0.05)
        mgr = PluginManager()
        mgr._hooks["pre_verify"] = [cb]
        assert mgr.invoke_hook("pre_verify", session_id="s1") == ["done"]
        assert seen == [(True, None)]


class TestHookCallDeadline:
    def test_callback_sees_host_deadline(self, global_timeout):
        global_timeout(5.0)
        seen = {}

        def cb(**_kwargs):
            call = current_hook_call()
            seen["hook"], seen["timeout"] = call.hook_name, call.timeout
            seen["remaining"], seen["abandoned"] = call.remaining(), call.abandoned.is_set()
        setattr(cb, HOOK_CALLBACK_TIMEOUT_ATTR, 3.0)
        mgr = PluginManager()
        mgr._hooks["pre_verify"] = [cb]
        mgr.invoke_hook("pre_verify", session_id="s1")
        assert seen["hook"] == "pre_verify" and seen["timeout"] == 3.0
        assert 2.0 < seen["remaining"] <= 3.0
        assert seen["abandoned"] is False
        assert current_hook_call() is None  # nothing leaks to the caller thread

    def test_abandoned_flag_set_when_host_stops_waiting(self, global_timeout, caplog):
        """A late callback learns it was abandoned, and its finish is logged as discarded."""
        global_timeout(0.1)
        observed = {}
        finished = threading.Event()

        def cb(**_kwargs):
            call = current_hook_call()
            observed["abandoned"] = call.abandoned.wait(5.0)
            observed["remaining"] = call.remaining()
            finished.set()
            return {"action": "continue", "message": "late nudge"}
        mgr = PluginManager()
        mgr._hooks["pre_verify"] = [cb]
        with caplog.at_level(logging.WARNING, logger="hermes_cli.plugins"):
            assert mgr.invoke_hook("pre_verify", session_id="s1") == []
            assert finished.wait(5.0)
            deadline = time.monotonic() + 2.0
            while "result discarded" not in caplog.text and time.monotonic() < deadline:
                time.sleep(0.01)
        assert observed == {"abandoned": True, "remaining": 0.0}
        assert "timed out after 0.1s" in caplog.text
        assert "result discarded" in caplog.text

    def test_pre_verify_message_returned_within_budget(self, global_timeout):
        """End to end through the host helper the agent loop calls: a declared budget keeps a
        slower-than-global checker's continue message."""
        global_timeout(0.1)
        cb = _sleeper(0.3, {"action": "continue", "message": "check again"})
        setattr(cb, HOOK_CALLBACK_TIMEOUT_ATTR, 2.0)
        import hermes_cli.plugins as plugins_mod
        mgr = PluginManager()
        mgr._hooks["pre_verify"] = [cb]
        old = plugins_mod._plugin_manager
        plugins_mod._plugin_manager = mgr
        try:
            assert get_pre_verify_continue_message(session_id="s1", final_response="x") == "check again"
        finally:
            plugins_mod._plugin_manager = old


class TestSessionCallIdentity:
    @staticmethod
    def _run(mgr, session_id):
        t = threading.Thread(target=mgr.invoke_hook, args=("pre_verify",), kwargs={"session_id": session_id},
                             daemon=True)
        t.start()
        return t

    def test_other_sessions_turn_end_check_is_not_skipped(self, global_timeout):
        """RED before: session B's pre_verify was skipped 'while still running' because session A's ran."""
        global_timeout(5.0)
        hold, starts = threading.Event(), []

        def cb(session_id="", **_kwargs):
            starts.append(session_id)
            hold.wait(5.0)
        mgr = PluginManager()
        mgr._hooks["pre_verify"] = [cb]
        first = self._run(mgr, "a")
        wait_until(lambda: starts == ["a"], timeout=5.0)
        second = self._run(mgr, "b")
        try:
            second.join(0.5)  # a skipped call returns at once; a running one blocks on `hold`
            assert second.is_alive(), "session b's check was skipped while session a's ran"
            wait_until(lambda: len(starts) == 2, timeout=5.0)
        finally:
            hold.set()
            first.join(5.0)
            second.join(5.0)
        assert sorted(starts) == ["a", "b"]

    def test_same_session_still_deduplicated(self, global_timeout):
        global_timeout(5.0)
        hold, starts = threading.Event(), []

        def cb(session_id="", **_kwargs):
            starts.append(session_id)
            hold.wait(5.0)
        mgr = PluginManager()
        mgr._hooks["pre_verify"] = [cb]
        first = self._run(mgr, "a")
        wait_until(lambda: starts == ["a"], timeout=5.0)
        second = self._run(mgr, "a")
        second.join(5.0)  # skipped at once: it never waits on `hold`
        assert not second.is_alive()
        hold.set()
        first.join(5.0)
        assert starts == ["a"]
