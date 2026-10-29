"""Cron session finalization survives a state.db write that outlives the cleanup timeout.

Production failure (hermes-hub, x-reply-discovery): another process held the state.db WAL write lock for
50+ s, the first title write timed out, the bounded proxy disabled itself, and the fallback-title
expression (``get_next_title_in_lineage`` through the disabled proxy) raised out of
``_finalize_cron_session``. run_job then crashed with "session finalization disabled after prior cleanup
failure": no end_session, no agent teardown, an untitled session row left open.
"""

from __future__ import annotations

import threading
import time
from unittest.mock import MagicMock, patch

import cron.scheduler as sched
from cron.scheduler import _finalize_cron_session, run_job
import pytest


@pytest.fixture(autouse=True)
def owner_bound(monkeypatch):
    """Upstream runs an agent cron job only inside the canonical owner's execution (cdbe8c35adf).
    These tests stub the session DB, so bind that execution directly (``bind_execution`` is the call
    the owner's ``execute()`` makes) instead of going through the owner seam's real DB."""
    from gateway.session_cron import _execution, bind_execution
    tokens = []
    from types import SimpleNamespace

    def bind(job, db):
        # The owner's store is the one ``acquire`` hands back (here the stubbed DB).
        authority = SimpleNamespace(db=db)
        tokens.append(bind_execution(authority, f"cron_{job['id']}_1", job["id"], None))
    yield bind
    for token in reversed(tokens):
        _execution.reset(token)


_RUNTIME = {
    "api_key": "test-key",
    "base_url": "https://example.invalid/v1",
    "provider": "openrouter",
    "api_mode": "chat_completions",
}


class SlowTitleSessionDB:
    """A real-shaped SessionDB whose title write blocks past the cleanup timeout (lock held elsewhere)."""

    db_path = "state.db"

    def __init__(self, release: threading.Event):
        self.release = release
        self.title_entered = threading.Event()
        self.titles: list[tuple[str, str]] = []
        self.ended: list[tuple[str, str]] = []

    def get_compression_tip(self, _session_id):
        return None

    def set_session_title(self, session_id, title):
        self.title_entered.set()
        self.release.wait()
        self.titles.append((session_id, title))
        return True

    def get_next_title_in_lineage(self, base):
        return f"{base} #2"

    def session_lifecycle_statuses(self, ids):
        return {i: "complete" for i in ids}

    def update_session_cwd(self, *_args):
        return None

    def end_session(self, session_id, reason):
        self.ended.append((session_id, reason))

    def close(self):
        return None


class RecordingSessionDB:
    """The fresh connection the retry opens once the lock holder is gone."""

    db_path = "state.db"

    def __init__(self):
        self.titles: list[tuple[str, str]] = []
        self.ended: list[tuple[str, str]] = []
        self.closed = threading.Event()

    def get_compression_tip(self, _session_id):
        return None

    def set_session_title(self, session_id, title):
        self.titles.append((session_id, title))
        return True

    def get_next_title_in_lineage(self, base):
        return f"{base} #2"

    def session_lifecycle_statuses(self, ids):
        return {i: "complete" for i in ids}

    def update_session_cwd(self, *_args):
        return None

    def end_session(self, session_id, reason):
        self.ended.append((session_id, reason))

    def close(self):
        self.closed.set()


def test_finalize_survives_title_write_timeout_and_retries_on_fresh_connection():
    release = threading.Event()
    slow_db = SlowTitleSessionDB(release)
    fresh_db = RecordingSessionDB()
    agent = MagicMock()
    try:
        with patch("cron.scheduler._cron_cleanup_timeout_seconds", return_value=0.2), \
             patch("cron.scheduler._cron_finalize_retry_delay_seconds", return_value=0.0, create=True), \
             patch("cron.scheduler._open_fresh_cron_session_db", return_value=fresh_db, create=True):
            started = time.monotonic()
            # Must return normally: the disabled proxy's fail-fast error may not escape.
            _finalize_cron_session(slow_db, agent, "job-slow", "slow job", "cron_job-slow_1")
            assert time.monotonic() - started < 5.0
            assert slow_db.title_entered.is_set()
            assert fresh_db.closed.wait(timeout=5.0), "retry on a fresh connection never ran"
        assert fresh_db.titles and fresh_db.titles[0][0] == "cron_job-slow_1"
        assert fresh_db.titles[0][1].strip()
        assert fresh_db.ended == [("cron_job-slow_1", "cron_complete")]
        # The retry owns row finalization now; agent.close() must not re-end it on the busy DB.
        assert agent._end_session_on_close is False
    finally:
        release.set()


def test_run_job_survives_title_write_timeout_and_tears_agent_down(tmp_path, owner_bound):
    release = threading.Event()
    slow_db = SlowTitleSessionDB(release)
    fresh_db = RecordingSessionDB()
    job = {"id": "slow-title", "name": "slow title", "prompt": "hello"}
    owner_bound(job, slow_db)
    try:
        with patch("cron.scheduler._hermes_home", tmp_path), \
             patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
             patch("hermes_cli.env_loader.load_hermes_dotenv"), \
             patch("hermes_cli.env_loader.reset_secret_source_cache"), \
             patch("hermes_state_registry.acquire", return_value=slow_db), \
             patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=_RUNTIME), \
             patch("run_agent.AIAgent") as mock_agent_cls, \
             patch("cron.scheduler._cron_cleanup_timeout_seconds", return_value=0.2), \
             patch("cron.scheduler._cron_finalize_retry_delay_seconds", return_value=0.0, create=True), \
             patch("cron.scheduler._open_fresh_cron_session_db", return_value=fresh_db, create=True), \
             patch("cron.scheduler._teardown_cron_agent") as teardown:
            mock_agent = MagicMock()
            mock_agent.run_conversation.return_value = {"final_response": "ok"}
            mock_agent_cls.return_value = mock_agent

            success, _output, final_response, error = run_job(job)

            assert success is True
            assert final_response == "ok"
            assert error is None
            teardown.assert_called_once()
            assert teardown.call_args.args[0] is mock_agent
            assert fresh_db.closed.wait(timeout=5.0)
        assert fresh_db.ended and fresh_db.ended[0][1] == "cron_complete"
    finally:
        release.set()


def test_run_job_hands_agent_off_when_finalize_raises(tmp_path, owner_bound):
    """Even if finalization itself blows up, the deferred-teardown hand-off still happens."""
    job = {"id": "finalize-raises", "name": "finalize raises", "prompt": "hello"}
    finalize_db = RecordingSessionDB()
    owner_bound(job, finalize_db)
    deferred: list = []
    with patch("cron.scheduler._hermes_home", tmp_path), \
         patch("cron.scheduler_delivery._resolve_origin", return_value=None), \
         patch("hermes_cli.env_loader.load_hermes_dotenv"), \
         patch("hermes_cli.env_loader.reset_secret_source_cache"), \
         patch("hermes_state_registry.acquire", return_value=finalize_db), \
         patch("hermes_cli.runtime_provider.resolve_runtime_provider", return_value=_RUNTIME), \
         patch("run_agent.AIAgent") as mock_agent_cls, \
         patch("cron.scheduler._finalize_cron_session", side_effect=RuntimeError("boom")):
        mock_agent = MagicMock()
        mock_agent.run_conversation.return_value = {"final_response": "ok"}
        mock_agent_cls.return_value = mock_agent

        success, _output, final_response, error = run_job(job, defer_agent_teardown=deferred)

    assert success is True and error is None and final_response == "ok"
    assert deferred == [mock_agent]


def test_finalize_disabled_proxy_logs_one_warning_naming_job_and_session(caplog):
    release = threading.Event()
    slow_db = SlowTitleSessionDB(release)
    try:
        with patch("cron.scheduler._cron_cleanup_timeout_seconds", return_value=0.2), \
             patch("cron.scheduler._cron_finalize_retry_delay_seconds", return_value=0.0, create=True), \
             patch("cron.scheduler._open_fresh_cron_session_db", return_value=None, create=True), \
             caplog.at_level("WARNING", logger="cron.scheduler"):
            _finalize_cron_session(slow_db, None, "job-warn", "warn job", "cron_job-warn_1")
            time.sleep(0.3)
        warnings = [r for r in caplog.records
                    if r.levelname == "WARNING" and "job-warn" in r.getMessage()
                    and "cron_job-warn_1" in r.getMessage()]
        assert warnings, [r.getMessage() for r in caplog.records]
        assert "\n" not in warnings[0].getMessage()
    finally:
        release.set()
