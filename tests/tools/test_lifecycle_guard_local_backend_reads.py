"""On a local backend, a path the guard cannot find on disk is final: no backend read.

Regression for self-heal defect lifecycleguard-fa6b2f3e49c2: a gateway chat on a Mac ran a large
tool script (``hermes-core-wt deliver ...``) whose sources name ~113 path-like words that are not
files (comment words, ``$HOME/...``, grep patterns). The terminal passed the backend reader even
though the terminal was local, so each missing path cost a ``head -c < path`` subprocess on the
same disk, and the 65th tripped "the scan budget was exhausted (remote reads at depth 2)".
"""

from __future__ import annotations

import json
import shlex
from pathlib import Path

import pytest

from tools.terminal_tool_guards import gateway_lifecycle_block


class _RecordingEnv:
    """A backend whose ``execute`` records every call and finds nothing (exit 1)."""

    def __init__(self, cwd: str) -> None:
        self.cwd = cwd
        self.calls: list[str] = []

    def execute(self, command: str, **_kwargs):
        self.calls.append(command)
        return {"returncode": 1, "output": ""}


def _script(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "big-tool"
    path.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    path.chmod(0o755)
    return path


def _missing_paths_script(tmp_path: Path, count: int = 100) -> Path:
    missing = tmp_path / "no-such-dir"
    body = "".join(f"{missing}/tool{i} --flag\n" for i in range(count))
    return _script(tmp_path, body)


def _block(tmp_path, monkeypatch, script: Path, env_type: str, env: _RecordingEnv):
    monkeypatch.setattr("tools.process_registry._is_supervised_gateway_process", lambda: True)
    return gateway_lifecycle_block(
        command=f"{shlex.quote(str(script))} deliver host abc123",
        env=env, env_type=env_type, cwd=str(tmp_path), workdir=str(tmp_path),
        session_key=f"local-backend-reads-{env_type}",
    )


def test_local_backend_never_reads_missing_paths_through_the_backend(tmp_path, monkeypatch):
    env = _RecordingEnv(str(tmp_path))
    script = _missing_paths_script(tmp_path)

    blocked = _block(tmp_path, monkeypatch, script, "local", env)

    assert blocked is None, json.loads(blocked)["error"]
    assert env.calls == []


@pytest.mark.parametrize("env_type", ["docker", "ssh"])
def test_remote_backend_keeps_the_remote_read_cap(tmp_path, monkeypatch, env_type):
    env = _RecordingEnv(str(tmp_path))
    script = _missing_paths_script(tmp_path)

    blocked = _block(tmp_path, monkeypatch, script, env_type, env)

    assert blocked is not None
    assert "remote reads" in json.loads(blocked)["error"]
    assert env.calls  # the backend was consulted for paths missing on this host


def test_local_script_that_restarts_the_gateway_is_still_blocked(tmp_path, monkeypatch):
    env = _RecordingEnv(str(tmp_path))
    missing = tmp_path / "no-such-dir"
    body = "".join(f"{missing}/tool{i}\n" for i in range(100)) + "hermes gateway restart\n"
    script = _script(tmp_path, body)

    blocked = _block(tmp_path, monkeypatch, script, "local", env)

    assert blocked is not None
    assert "hermes gateway restart" in json.loads(blocked)["error"]
    assert env.calls == []
