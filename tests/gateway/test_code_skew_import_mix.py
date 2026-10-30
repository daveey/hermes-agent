"""An ImportError raised while the checkout has moved past the process's boot commit is named as a
code-skew import mix (agent-529f2244ac): one WARNING with both shas, on each path that catches tool
errors, so the coach signal shows the cause instead of a bare ``cannot import name``."""

from __future__ import annotations

import json
import logging

import pytest

from gateway import code_skew

BOOT = "a" * 40
DISK = "b" * 40


@pytest.fixture
def skewed(monkeypatch):
    monkeypatch.setattr(code_skew, "_boot_fingerprint", f"git:refs/heads/main:{BOOT}")
    monkeypatch.setattr(code_skew, "_fingerprint", lambda: f"git:refs/heads/main:{DISK}")
    monkeypatch.setattr(code_skew, "_import_mix_noted", set())


def _mix_lines(caplog):
    return [r for r in caplog.records
            if r.levelno == logging.WARNING and r.getMessage().startswith("code-skew import mix")]


def test_an_import_error_after_the_checkout_moved_is_named_once_with_both_shas(skewed, caplog):
    caplog.set_level(logging.WARNING, logger="gateway.code_skew")
    exc = ImportError("cannot import name 'spawn_owned' from 'hermes_cli.process_identity'")
    assert code_skew.note_import_error(exc, "code_kernel") is True
    assert code_skew.note_import_error(exc, "code_kernel") is True  # still a mix, logged once
    lines = _mix_lines(caplog)
    assert len(lines) == 1
    text = lines[0].getMessage()
    assert BOOT[:10] in text and DISK[:10] in text and "spawn_owned" in text


def test_no_note_when_the_checkout_did_not_move_or_the_error_is_not_an_import_error(
        monkeypatch, caplog):
    caplog.set_level(logging.WARNING, logger="gateway.code_skew")
    monkeypatch.setattr(code_skew, "_import_mix_noted", set())
    monkeypatch.setattr(code_skew, "_boot_fingerprint", f"git:refs/heads/main:{BOOT}")
    monkeypatch.setattr(code_skew, "_fingerprint", lambda: f"git:refs/heads/main:{BOOT}")
    assert code_skew.note_import_error(ImportError("x"), "tool") is False
    monkeypatch.setattr(code_skew, "_fingerprint", lambda: f"git:refs/heads/main:{DISK}")
    assert code_skew.note_import_error(ValueError("x"), "tool") is False
    assert _mix_lines(caplog) == []


def test_a_process_that_never_ran_the_gateway_record_uses_the_bootstrap_revision(monkeypatch, caplog):
    """CLI / kanban-worker processes never call ``record_boot_fingerprint``; the revision
    ``hermes_bootstrap`` read at process start still names the mix."""
    from hermes_cli import git_fingerprint

    caplog.set_level(logging.WARNING, logger="gateway.code_skew")
    monkeypatch.setenv("HERMES_BOOT_PINNED_IMPORTS", "1")  # pytest processes ignore it otherwise
    monkeypatch.setattr(code_skew, "_import_mix_noted", set())
    monkeypatch.setattr(code_skew, "_boot_fingerprint", None)
    monkeypatch.setattr(git_fingerprint, "_process_boot",
                        {str(code_skew._PROJECT_ROOT.resolve()): f"git:refs/heads/main:{BOOT}"})
    monkeypatch.setattr(code_skew, "_fingerprint", lambda: f"git:refs/heads/main:{DISK}")
    assert code_skew.note_import_error(ImportError("cannot import name 'x'"), "tool") is True
    assert len(_mix_lines(caplog)) == 1


def test_registry_dispatch_path_names_the_mix(skewed, caplog):
    from tools.registry import ToolRegistry

    caplog.set_level(logging.WARNING, logger="gateway.code_skew")
    reg = ToolRegistry()

    def handler(args, **_):
        raise ImportError("cannot import name 'spawn_owned' from 'hermes_cli.process_identity'")

    reg.register("mix_probe", "test", {"name": "mix_probe", "description": "probe",
                                       "parameters": {"type": "object", "properties": {}}}, handler)
    out = reg.dispatch("mix_probe", {})
    assert "spawn_owned" in (out if isinstance(out, str) else json.dumps(out))
    assert len(_mix_lines(caplog)) == 1


def test_execute_code_session_kernel_path_names_the_mix(skewed, caplog, monkeypatch):
    """The incident's path: ``code_kernel._run_cell`` catches the ImportError from ``_spawn``."""
    import sys

    from tools import code_kernel

    caplog.set_level(logging.WARNING, logger="gateway.code_skew")

    def boom(*_a, **_k):
        raise ImportError("cannot import name 'spawn_owned' from 'hermes_cli.process_identity'")

    monkeypatch.setattr(code_kernel, "_spawn", boom)
    out = code_kernel.execute_in_session_kernel(
        "print(1)", task_id="mix-probe", mode="project", child_python=sys.executable, child_cwd="",
        sandbox_tools=frozenset(), timeout=10, max_tool_calls=1, reset=True,
        is_interrupted=lambda: False)
    assert "spawn_owned" in out
    assert len(_mix_lines(caplog)) == 1
