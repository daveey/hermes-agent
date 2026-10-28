"""The hermetic test env drops agent-session markers a worker/agent shell exports, and resets
session ContextVars between tests.

pytest launched from a Kanban worker terminal inherits HERMES_SINGLE_QUERY_SESSION=1 (the worker
is a ``hermes -q`` one-shot). Unscrubbed, ``delegate_task`` charged the one-shot spawn budget and
test_delegate_apiserver_background failed with a TypeError on hosts only, never on CI.

A multi-file run (``pytest a.py b.py``) shares one interpreter: a test that binds and clears
session vars (``clear_session_vars`` sets ``""``, which masks ``os.environ``) made later files'
``get_session_env`` ignore their monkeypatched HERMES_SESSION_ID / HERMES_CRON_SESSION
(test_kanban_provenance -> test_cronjob_tools, test_kanban_tools, test_completed_process_results).
"""

import os

from tests._fixtures.env_filter import _HERMES_BEHAVIORAL_VARS

_WORKER_SHELL_MARKERS = (
    "HERMES_SINGLE_QUERY_SESSION",
    "HERMES_UI_SESSION_ID",
    "HERMES_KANBAN_WORKER_TASK",
    "HERMES_DELEGATED_CHILD_OWNER",
    "HERMES_SUPERVISED_CHILD",
)


def test_worker_shell_markers_are_scrubbed_per_test():
    for name in _WORKER_SHELL_MARKERS:
        assert name in _HERMES_BEHAVIORAL_VARS
        assert name not in os.environ, f"{name} leaked from the host into a test"


def test_single_query_marker_off_in_tests():
    from agent.oneshot_footprint import is_single_query_session

    assert is_single_query_session() is False


def test_a_test_that_binds_and_clears_session_vars():
    """Leaves the ContextVars at ``""`` (masking env) and engages the process latch, like
    test_kanban_provenance does. The next test must not see either."""
    from gateway import session_context as sc

    sc.clear_session_vars(sc.set_session_vars(platform="discord", chat_id="chat", session_id="s1"))
    assert sc.get_session_env("HERMES_SESSION_ID", "fallback") == ""
    assert sc.session_context_engaged() is True


def test_next_test_reads_env_through_session_vars(monkeypatch):
    from gateway import session_context as sc

    assert sc.session_context_engaged() is False
    monkeypatch.setenv("HERMES_SESSION_ID", "s_env")
    monkeypatch.setenv("HERMES_CRON_SESSION", "1")
    assert sc.get_session_env("HERMES_SESSION_ID") == "s_env"
    assert sc.get_session_env("HERMES_CRON_SESSION") == "1"
