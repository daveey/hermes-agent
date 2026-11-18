"""process_manage wait has its own limit (TERMINAL_MAX_WAIT), kept under the executor deadline."""

import sys
import time
import types

import pytest

from tools import process_wait_limit as pwl
from tools.process_registry import ProcessRegistry


@pytest.fixture
def registry(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.delenv("TERMINAL_MAX_WAIT", raising=False)
    return ProcessRegistry()


@pytest.fixture
def no_executor(monkeypatch):
    monkeypatch.delitem(sys.modules, "agent.tool_executor", raising=False)


def _fake_executor(monkeypatch, deadline):
    mod = types.ModuleType("agent.tool_executor")
    if callable(deadline):
        mod._resolve_sequential_tool_timeout = deadline
    else:
        mod._resolve_sequential_tool_timeout = lambda: deadline
    monkeypatch.setitem(sys.modules, "agent.tool_executor", mod)


class TestMaxWaitSeconds:
    def test_default_is_400_and_ignores_terminal_timeout(self, monkeypatch, no_executor):
        monkeypatch.delenv("TERMINAL_MAX_WAIT", raising=False)
        monkeypatch.setenv("TERMINAL_TIMEOUT", "60")  # the hub's value; it used to clamp the wait
        assert pwl.max_wait_seconds() == 400

    def test_env_override(self, monkeypatch, no_executor):
        monkeypatch.setenv("TERMINAL_MAX_WAIT", "90")
        assert pwl.max_wait_seconds() == 90

    @pytest.mark.parametrize("raw", ["", "abc", "0", "-5"])
    def test_invalid_override_falls_back_to_default(self, monkeypatch, no_executor, raw):
        monkeypatch.setenv("TERMINAL_MAX_WAIT", raw)
        assert pwl.max_wait_seconds() == 400

    def test_stays_under_the_executor_deadline(self, monkeypatch):
        monkeypatch.setenv("TERMINAL_MAX_WAIT", "900")
        _fake_executor(monkeypatch, 420.0)
        assert pwl.max_wait_seconds() == 400  # 420 - 20 s margin

    def test_lower_configured_executor_deadline_lowers_the_wait(self, monkeypatch):
        monkeypatch.delenv("TERMINAL_MAX_WAIT", raising=False)
        _fake_executor(monkeypatch, 120.0)
        assert pwl.max_wait_seconds() == 100

    def test_tiny_executor_deadline_never_reaches_zero(self, monkeypatch):
        monkeypatch.delenv("TERMINAL_MAX_WAIT", raising=False)
        _fake_executor(monkeypatch, 5.0)
        assert pwl.max_wait_seconds() == 1

    @pytest.mark.parametrize("deadline", [None, 0, -1])
    def test_disabled_executor_deadline_leaves_the_limit(self, monkeypatch, deadline):
        monkeypatch.setenv("TERMINAL_MAX_WAIT", "900")
        _fake_executor(monkeypatch, deadline)
        assert pwl.max_wait_seconds() == 900

    def test_failing_executor_resolver_leaves_the_limit(self, monkeypatch):
        monkeypatch.delenv("TERMINAL_MAX_WAIT", raising=False)

        def boom():
            raise RuntimeError("config unreadable")

        _fake_executor(monkeypatch, boom)
        assert pwl.max_wait_seconds() == 400

    def test_raised_terminal_timeout_still_buys_a_longer_wait(self, monkeypatch, no_executor):
        """An install that set TERMINAL_TIMEOUT high to get long waits keeps them."""
        monkeypatch.delenv("TERMINAL_MAX_WAIT", raising=False)
        monkeypatch.setenv("TERMINAL_TIMEOUT", "1200")
        assert pwl.max_wait_seconds() == 1200
        monkeypatch.setenv("TERMINAL_MAX_WAIT", "300")  # the explicit limit wins either way
        assert pwl.max_wait_seconds() == 300

    def test_real_executor_clamps_the_wait(self, monkeypatch):
        """Through the real agent.tool_executor: its default deadline (420 s) holds a 900 s request
        to 400 s. Fails if the resolver this module reads is renamed or the defaults drift apart."""
        monkeypatch.setenv("TERMINAL_MAX_WAIT", "900")
        monkeypatch.delenv("HERMES_CONCURRENT_TOOL_TIMEOUT_S", raising=False)
        import agent.tool_executor as te

        monkeypatch.setitem(sys.modules, "agent.tool_executor", te)
        deadline = te._resolve_sequential_tool_timeout()
        assert deadline == te._DEFAULT_CONCURRENT_TOOL_TIMEOUT_S == 420.0
        assert pwl._executor_deadline_seconds() == deadline
        assert pwl.max_wait_seconds() == 400
        monkeypatch.delenv("TERMINAL_MAX_WAIT")
        assert pwl.max_wait_seconds() == pwl.DEFAULT_MAX_WAIT_SECONDS == 400


class TestRegistryWaitUsesTheLimit:
    def test_wait_outlasts_terminal_timeout(self, registry, monkeypatch, no_executor):
        """TERMINAL_TIMEOUT=1 used to cut every wait at 1 s; the process here exits after 2 s."""
        monkeypatch.setenv("TERMINAL_TIMEOUT", "1")
        session = registry.spawn_local("sleep 2", cwd="/tmp", task_id="t-waitlimit")
        r = registry.wait(session.id)
        assert r["status"] == "exited"
        assert "timeout_note" not in r

    def test_wait_returns_as_soon_as_the_process_exits(self, registry, no_executor):
        session = registry.spawn_local("sleep 1", cwd="/tmp", task_id="t-waitlimit")
        started = time.monotonic()
        r = registry.wait(session.id)  # default window 400 s
        assert r["status"] == "exited"
        assert time.monotonic() - started < 30

    def test_requested_timeout_above_the_limit_is_clamped(self, registry, monkeypatch, no_executor):
        monkeypatch.setenv("TERMINAL_MAX_WAIT", "1")
        session = registry.spawn_local("sleep 30", cwd="/tmp", task_id="t-waitlimit")
        try:
            started = time.monotonic()
            r = registry.wait(session.id, timeout=600)
            assert r["status"] == "timeout"
            assert time.monotonic() - started < 10
            assert "clamped to configured limit of 1s" in r["timeout_note"]
        finally:
            registry.kill_process(session.id)

    def test_default_window_is_the_limit(self, registry, monkeypatch, no_executor):
        monkeypatch.setenv("TERMINAL_MAX_WAIT", "1")
        session = registry.spawn_local("sleep 30", cwd="/tmp", task_id="t-waitlimit")
        try:
            r = registry.wait(session.id)
            assert r["status"] == "timeout"
            assert "Wait window of 1s elapsed" in r["timeout_note"]
        finally:
            registry.kill_process(session.id)


class TestForeignWaitUsesTheLimit:
    """process_manage wait on a process another gateway process supervises (foreign_wait)."""

    def _wait(self, monkeypatch, timeout):
        from tools import process_registry_foreign as pf

        monkeypatch.setattr(pf, "_settled", lambda registry, sid: None)
        monkeypatch.setattr(pf, "_running", lambda registry, entry: True)
        monkeypatch.setattr(pf, "_log_text", lambda entry: "")
        started = time.monotonic()
        result = pf.foreign_wait(object(), {"session_id": "proc_foreign", "command": "sleep 999"}, timeout)
        return result, time.monotonic() - started

    def test_default_window_is_the_wait_limit_not_terminal_timeout(self, monkeypatch, no_executor):
        monkeypatch.setenv("TERMINAL_MAX_WAIT", "1")
        monkeypatch.setenv("TERMINAL_TIMEOUT", "600")  # the old clamp: this call would block 600 s
        result, elapsed = self._wait(monkeypatch, None)
        assert result["status"] == "timeout" and result["process_running"] is True
        assert "Wait window of 1s elapsed" in result["timeout_note"]
        assert elapsed < 10

    def test_requested_timeout_is_clamped_to_the_limit(self, monkeypatch, no_executor):
        monkeypatch.setenv("TERMINAL_MAX_WAIT", "1")
        result, elapsed = self._wait(monkeypatch, 600)
        assert "Wait window of 1s elapsed" in result["timeout_note"]
        assert elapsed < 10


class TestDescriptions:
    def test_descriptions_no_longer_recommend_a_foreground_wait(self):
        from tools import terminal_tool as tt
        from tools.process_registry import PROCESS_SCHEMA

        assert "run the wait in the foreground" not in tt.TERMINAL_TOOL_DESCRIPTION
        assert 'process_manage(action="wait")' in tt.TERMINAL_TOOL_DESCRIPTION
        assert "instead of a terminal sleep" in PROCESS_SCHEMA["description"]
