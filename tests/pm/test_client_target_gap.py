"""A package with a gap on this host's target is refused in-process: no PM worker per call.

``selected_git_env`` asks ``pm.ensure("git")`` on every internal git call. On POSIX, git is a deliberate
gap (``Git.gaps``), so the worker could only answer "unavailable on <target>". It used to be spawned
anyway, a fresh interpreter and ~0.7-2 s per git call.
"""

import subprocess

import pytest

from pm import client
from pm.package import InstallError


@pytest.fixture
def no_worker(monkeypatch):
    def refuse(*args, **kwargs):
        raise AssertionError("a PM worker was requested")

    monkeypatch.setattr(client, "is_runtime", lambda: False)
    monkeypatch.setattr(client, "_request", refuse)
    monkeypatch.setattr(client, "_missing_or_refuse", lambda name: [name])  # git counts as missing


@pytest.mark.parametrize("target", ["darwin-arm64", "linux-x64"])
@pytest.mark.parametrize("explicit", [False, True])
def test_git_gap_is_refused_without_a_worker(monkeypatch, no_worker, target, explicit):
    monkeypatch.setattr("pm.store.current_target", lambda: target)

    with pytest.raises(InstallError, match=f"unavailable on {target}: POSIX uses system git by choice"):
        client.ensure("git", explicit=explicit)


def test_package_without_a_gap_still_goes_to_the_worker(monkeypatch):
    calls = []
    monkeypatch.setattr(client, "is_runtime", lambda: False)
    monkeypatch.setattr(client, "_missing_or_refuse", lambda name: [name])
    monkeypatch.setattr(client, "_request", lambda operation, arguments, **kw: calls.append((operation, arguments)))
    monkeypatch.setattr("pm.store.current_target", lambda: "win32-x64")  # Git for Windows: no gap
    monkeypatch.setattr("pm.install.env_for", lambda name, base_env=None: {"PATH": "/pm/git"})

    runner = client.ensure("git")

    assert calls == [("ensure", {"name": "git", "explicit": False})]
    assert runner.env == {"PATH": "/pm/git"}


def test_selected_git_env_falls_back_to_system_git_without_spawning(monkeypatch, no_worker):
    from hermes_cli._subprocess_compat import selected_git_env

    monkeypatch.setattr("pm.store.current_target", lambda: "darwin-arm64")

    def no_spawn(*args, **kwargs):
        raise AssertionError(f"spawned {args[:1]}")

    monkeypatch.setattr(subprocess, "Popen", no_spawn)
    base = {"PATH": "/usr/bin", "HOME": "/h"}

    assert selected_git_env(base) == base
