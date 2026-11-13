"""Quiet-v3 R4-F F2: kanban dispatcher worker spawn reserves local work BEFORE the child exists and
releases only when the dispatcher's reap proves that exact child exited (pid+handle keyed).

The REAL ``_default_spawn`` / ``reap_worker_zombies`` run against a real ``local_work.Registry``; only
the subprocess handle is faked.
"""
import subprocess

import pytest

from hermes_cli import local_work
from hermes_constants import hermes_home_key


@pytest.fixture
def registry(tmp_path, monkeypatch):
    root = tmp_path / ".hermes"
    (root / "profiles" / "w").mkdir(parents=True)
    root.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    (root / "profiles" / "w" / "config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    reg = local_work.Registry(homes=[hermes_home_key(root)])
    monkeypatch.setattr(local_work, "_registry", reg)
    from hermes_cli import kanban_db_dispatch as kbd
    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])
    if hasattr(kbd, "_custody"):
        monkeypatch.setattr(kbd._custody, "_custody", {})
    return reg


def _task(kb, tid="t_custody"):
    return kb.Task(
        id=tid, title="custody", body=None, assignee="w", status="running", priority=0, created_by="test",
        created_at=1, started_at=None, completed_at=None, workspace_kind="dir", workspace_path=None,
        claim_lock="lock", claim_expires=None, tenant=None, current_run_id=1,
    )


class FakeProc:
    def __init__(self, pid, returncode=None):
        self.pid = pid
        self.returncode = returncode

    def poll(self):
        return self.returncode


def _spawn(monkeypatch, tmp_path, proc, seen=None, registry=None):
    from hermes_cli import kanban_db as kb
    from hermes_cli import kanban_db_dispatch as kbd

    def popen(cmd, *args, **kwargs):
        if seen is not None:
            seen["active_at_popen"] = registry.snapshot().active
        if isinstance(proc, BaseException):
            raise proc
        return proc

    monkeypatch.setattr(subprocess, "Popen", popen)
    workspace = tmp_path / "ws"
    workspace.mkdir(exist_ok=True)
    return kbd._default_spawn(_task(kb), str(workspace))


def _clean(reg):
    snap = reg.snapshot()
    assert snap.unknowns == (), snap.unknowns
    assert snap.active == 0, dict(snap.per_kind)


def test_reserved_before_popen_and_held_until_reap_observes_exit(registry, monkeypatch, tmp_path):
    from hermes_cli import kanban_db_dispatch as kbd
    proc, seen = FakeProc(51001), {}
    epoch0 = registry.snapshot().epoch

    assert _spawn(monkeypatch, tmp_path, proc, seen, registry) == 51001
    assert seen["active_at_popen"] == 1, "token must exist before the child is created"
    assert registry.snapshot().epoch > epoch0

    assert kbd.reap_worker_zombies() == []  # child still running: custody persists
    assert registry.snapshot().active == 1

    proc.returncode = 0
    assert kbd.reap_worker_zombies() == [51001]
    _clean(registry)


def test_failed_spawn_releases_token(registry, monkeypatch, tmp_path):
    with pytest.raises(RuntimeError):
        _spawn(monkeypatch, tmp_path, FileNotFoundError("hermes"))
    _clean(registry)
    with pytest.raises(OSError):
        _spawn(monkeypatch, tmp_path, OSError("fork failed"))
    _clean(registry)


def test_pid_reuse_by_a_different_handle_cannot_release_custody(registry, monkeypatch, tmp_path):
    from hermes_cli import kanban_db_dispatch as kbd
    live = FakeProc(51002)
    _spawn(monkeypatch, tmp_path, live)
    assert registry.snapshot().active == 1

    impostor = FakeProc(51002, returncode=0)  # same pid, different (already exited) handle
    kbd._live_worker_procs[51002] = impostor
    kbd.reap_worker_zombies()
    assert registry.snapshot().active == 1, "a reused pid must not release the live child's token"

    live.returncode = 0
    kbd._live_worker_procs[51002] = live
    kbd.reap_worker_zombies()
    _clean(registry)


def test_handle_dropped_without_verified_exit_keeps_token_and_latches_unknown(registry, monkeypatch, tmp_path):
    from hermes_cli import kanban_db_dispatch as kbd

    class NoPoll:
        pid = 51003

    _spawn(monkeypatch, tmp_path, NoPoll())
    kbd.reap_worker_zombies()  # non-Popen handle is dropped: exit unprovable
    snap = registry.snapshot()
    assert snap.active == 1
    assert "kanban-worker-exit-unverified" in snap.unknowns


def test_unreadable_birth_after_popen_keeps_custody_and_latches_unknown(registry, monkeypatch, tmp_path):
    """The child exists once Popen returns: a raising birth fingerprint must not orphan the token
    (RED on 95089d00fb: the exception escaped before adopt, the token was never attached)."""
    from hermes_cli import kanban_db_dispatch as kbd

    def boom(pid):
        raise OSError("proc unreadable")

    monkeypatch.setattr(kbd, "_process_fingerprint", boom)
    proc = FakeProc(51004)
    assert _spawn(monkeypatch, tmp_path, proc) == 51004
    snap = registry.snapshot()
    assert snap.active == 1 and "kanban-worker-birth-unreadable" in snap.unknowns
    proc.returncode = 0
    assert kbd.reap_worker_zombies() == [51004]
    assert registry.snapshot().active == 0  # the verified exit still releases the attached token


def test_no_registry_is_inert(monkeypatch, tmp_path):
    from hermes_cli import kanban_db_dispatch as kbd
    monkeypatch.setattr(local_work, "_registry", None)
    root = tmp_path / ".hermes"
    (root / "profiles" / "w").mkdir(parents=True)
    root.joinpath("config.yaml").write_text("toolsets:\n  - kanban\n", encoding="utf-8")
    monkeypatch.setenv("HERMES_HOME", str(root))
    monkeypatch.setattr(kbd, "_resolve_hermes_argv", lambda: ["hermes"])
    proc = FakeProc(51004, returncode=0)
    assert _spawn(monkeypatch, tmp_path, proc) == 51004
    assert kbd.reap_worker_zombies() == [51004]
