"""The checkpoint records a background job's notification flags. [daveey]

``spawn_background_process`` spawns the job (which writes ``processes.json``) and only
then sets ``notify_on_complete`` / ``watch_patterns`` / ``completion_output_chars`` on the
session. Without a second write, the checkpoint kept ``notify_on_complete: false`` until
some unrelated spawn or exit rewrote it. A Desktop restart in that window re-adopted the
job as non-notifying, so its exit was pruned silently and the chat never woke (session
20261014_205123_3d7c36, 2026-10-15: an iOS release watcher lost across a 09:12 update).
"""

import json
import time
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from tools.process_registry import ProcessRegistry


@pytest.fixture()
def registry(tmp_path):
    path = tmp_path / "processes.json"
    reg = ProcessRegistry()
    with patch("tools.process_registry.CHECKPOINT_PATH", path), \
            patch("tools.process_registry.process_registry", reg):
        yield reg, path
    for s in list(reg._running.values()):
        reg.kill_process(s.id)


def _spawn(tmp_path, **overrides):
    from tools.terminal_tool_background import spawn_background_process
    kwargs = dict(
        command="sleep 30", env=SimpleNamespace(env={}), env_type="local", effective_task_id="default",
        task_id=None, session_key="20261015_091948_2e45bf", workdir=str(tmp_path), cwd=str(tmp_path),
        effective_pty=False, notify_on_complete=True, watch_patterns=None, approval_note=None,
        pty_disabled_reason=None)
    kwargs.update(overrides)
    return json.loads(spawn_background_process(**kwargs))


def _entry(path, sid):
    return next(e for e in json.loads(path.read_text()) if e["session_id"] == sid)


def test_notify_flag_reaches_checkpoint_at_spawn(registry, tmp_path):
    _, path = registry
    result = _spawn(tmp_path, completion_output_chars=4000)
    assert result.get("notify_on_complete") is True, result
    entry = _entry(path, result["session_id"])
    assert entry["notify_on_complete"] is True
    assert entry["completion_output_chars"] == 4000


def test_watch_patterns_reach_checkpoint_at_spawn(registry, tmp_path):
    _, path = registry
    result = _spawn(tmp_path, notify_on_complete=False, watch_patterns=["READY"])
    assert _entry(path, result["session_id"])["watch_patterns"] == ["READY"]


def test_plain_background_job_stays_non_notifying(registry, tmp_path):
    """Control: a job started without notify must not be checkpointed as notifying."""
    _, path = registry
    result = _spawn(tmp_path, notify_on_complete=False)
    assert _entry(path, result["session_id"])["notify_on_complete"] is False


def test_readopted_job_from_fresh_checkpoint_wakes_its_chat(registry, tmp_path, monkeypatch):
    """End to end: spawn, 'restart' (new registry reads the checkpoint), job exits → unknown-exit event."""
    _, path = registry
    monkeypatch.setattr("hermes_cli.process_identity.is_desktop_owned_backend", lambda argv=None: True)
    result = _spawn(tmp_path, command="sleep 1")
    entries = json.loads(path.read_text())
    for e in entries:
        e["writer_pid"] = 999999  # the spawning backend is gone
    path.write_text(json.dumps(entries))

    fresh = ProcessRegistry()
    events: list = []
    fresh.on_unknown_exit = events.append
    assert fresh.recover_from_checkpoint(owner="tui") == 1
    deadline = time.monotonic() + 10
    while not events and time.monotonic() < deadline:
        fresh.sweep_detached_once()
        time.sleep(0.2)
    assert [e["session_id"] for e in events] == [result["session_id"]]
    assert events[0]["session_key"] == "20261015_091948_2e45bf"


def test_watch_promotion_to_notify_is_checkpointed(registry, tmp_path, monkeypatch):
    """The lifetime cap promotes a watch job to notify_on_complete; the checkpoint must follow."""
    import tools.process_registry as pr
    reg, path = registry
    result = _spawn(tmp_path, notify_on_complete=False, watch_patterns=["HIT"])
    session = reg._running[result["session_id"]]
    monkeypatch.setattr(pr, "WATCH_MIN_INTERVAL_SECONDS", 0)
    for _ in range(pr.WATCH_LIFETIME_MAX_HITS):
        session._watch_cooldown_until = 0
        reg._check_watch_patterns(session, "HIT\n")
    assert session.notify_on_complete is True
    assert _entry(path, result["session_id"])["notify_on_complete"] is True
