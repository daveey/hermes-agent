"""Portable coverage of exec-time Darwin malloc policy and child isolation."""

import os
import sys
from pathlib import Path
from unittest.mock import Mock

import pytest


def _policy(monkeypatch):
    from hermes_cli import launch_malloc

    for name in (launch_malloc.ENV, launch_malloc.MARK, launch_malloc.OPT_OUT):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(sys, "platform", "darwin")  # os-marker: ok — pure environment policy; exec is stubbed
    return launch_malloc


def test_darwin_server_selection():
    from hermes_cli import launch_malloc as policy

    for argv in (["gateway", "run"], ["serve", "--port", "1"], ["dashboard"]):
        assert policy.launch_env(argv, {}, "darwin") == {policy.ENV: "1", policy.MARK: "1"}
    for argv in (["chat"], ["kanban", "list"], [], ["gateway", "status"]):
        assert policy.launch_env(argv, {}, "darwin") == {}


def test_other_platforms_unchanged():
    from hermes_cli import launch_malloc as policy

    for platform in ("linux", "win32"):
        assert policy.launch_env(["serve"], {}, platform) == {}


def test_operator_value_preserved(monkeypatch):
    policy = _policy(monkeypatch)
    execute = Mock()
    for value in ("0", "1"):
        monkeypatch.setenv(policy.ENV, value)
        assert policy.launch_env(["serve"], os.environ, "darwin") == {}
        policy.ensure(["serve"], execv=execute)
        assert os.environ[policy.ENV] == value
        assert policy.MARK not in os.environ
    execute.assert_not_called()


def test_opt_out(monkeypatch):
    policy = _policy(monkeypatch)
    for value in ("0", "false", "NO", "Off"):
        assert policy.launch_env(["serve"], {policy.OPT_OUT: value}, "darwin") == {}
    assert policy.launch_env(["serve"], {policy.OPT_OUT: "1"}, "darwin")


def test_mark_scrubs_child_environment(monkeypatch):
    policy = _policy(monkeypatch)
    monkeypatch.setenv(policy.MARK, "1")
    monkeypatch.setenv(policy.ENV, "1")
    execute = Mock()
    policy.ensure(["serve"], execv=execute)
    assert policy.MARK not in os.environ
    assert policy.ENV not in os.environ
    execute.assert_not_called()


def test_ensure_exec_once_and_withdraw_on_failure(monkeypatch):
    policy = _policy(monkeypatch)
    calls = []

    def execute(python, command):
        calls.append((python, command))
        assert os.environ[policy.ENV] == os.environ[policy.MARK] == "1"

    policy.ensure(["serve"], execv=execute)
    assert calls == [(sys.executable, sys.orig_argv)]
    policy.ensure(["serve"], execv=execute)
    assert len(calls) == 1
    assert policy.ENV not in os.environ
    assert policy.MARK not in os.environ

    def fail(python, command):
        execute(python, command)
        raise OSError("exec refused")

    policy.ensure(["serve"], execv=fail)
    assert len(calls) == 2
    assert policy.ENV not in os.environ
    assert policy.MARK not in os.environ


def test_exec_launch_carries_and_withdraws_policy(monkeypatch):
    from hermes_cli import venv_sync

    policy = _policy(monkeypatch)
    monkeypatch.setattr(venv_sync, "launch_handoff_env", lambda: {venv_sync.LAUNCH_HANDOFF_ENV: "test"})
    calls = []

    def execute(python, command):
        calls.append((python, command))
        assert os.environ[policy.ENV] == os.environ[policy.MARK] == "1"
        assert os.environ[venv_sync.LAUNCH_HANDOFF_ENV] == "test"
        raise OSError("exec refused")

    monkeypatch.setattr(os, "execv", execute)
    with pytest.raises(OSError, match="exec refused"):
        venv_sync.exec_launch(Path("/python"), ["/python", "hermes", "serve"], ["serve"])
    assert calls == [("/python", ["/python", "hermes", "serve"])]
    for name in (policy.ENV, policy.MARK, venv_sync.LAUNCH_HANDOFF_ENV):
        assert name not in os.environ
