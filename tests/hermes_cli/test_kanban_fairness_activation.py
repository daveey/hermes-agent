"""Explicit operator arming is distinct from config and runtime startup."""
import json

import pytest

from hermes_cli import kanban_fairness_runtime as runtime
from hermes_cli.kanban_turn_urgency import read_owners
from hermes_cli.kanban_fairness_store import load


def config(tmp_path):
    return {"kanban": {"fairness_capture": {"enabled": True, "capture_id": "fixture",
                                           "directory": str(tmp_path / "capture")}}}


def test_disabled_and_unarmed_startup_do_not_activate(tmp_path, monkeypatch):
    from hermes_cli import kanban_fairness_activation as activation
    monkeypatch.setattr(runtime, "activate", lambda *a, **kw: pytest.fail("must stay inert"))
    assert activation.start({}, home=tmp_path) is None
    assert activation.start(config(tmp_path), home=tmp_path) is None
    assert not (tmp_path / "capture").exists()


def test_real_start_read_disarm_preserves_receipts(tmp_path, monkeypatch):
    from hermes_cli import kanban_fairness_activation as activation
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg = config(tmp_path)
    activation.arm(cfg, home=tmp_path, board="default", source_gateway="gw", board_uid="board")
    session = activation.start(cfg, home=tmp_path)
    try:
        assert session is not None
        assert activation.start(cfg, home=tmp_path) is session
        assert runtime.enabled(board="default")
        assert read_owners(board="default", now_s=150, path=tmp_path / "missing") == set()
        activation.disarm(cfg, home=tmp_path)
        session.activation_monitor.join(3)
        assert not session.activation_monitor.is_alive()
        assert not runtime.enabled(board="default")
        session.thread.join(2)
        capture = load(session.out)
        assert capture["records"][0]["payload"]["outcome"] == "missing"
        assert session.identity["clock_error_ns"] is None
        assert activation.start(cfg, home=tmp_path) is None
        assert session.out.exists()
    finally:
        session.close()


@pytest.mark.parametrize("field,value", [("board", "../bad"), ("source_gateway", ""),
                                         ("board_uid", ""), ("clock_error_ns", -1)])
def test_bad_identity_never_arms(tmp_path, field, value):
    from hermes_cli import kanban_fairness_activation as activation
    args = dict(board="default", source_gateway="gw", board_uid="board", clock_error_ns=None)
    args[field] = value
    with pytest.raises(ValueError):
        activation.arm(config(tmp_path), home=tmp_path, **args)
    assert not (tmp_path / "capture").exists()


def test_descriptor_scope_and_budget_must_match(tmp_path, monkeypatch):
    from hermes_cli import kanban_fairness_activation as activation
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg = config(tmp_path)
    activation.arm(cfg, home=tmp_path, board="default", source_gateway="gw", board_uid="board")
    descriptor = tmp_path / "capture" / activation.NAME
    data = json.loads(descriptor.read_bytes())
    data["capture_id"] = "other"
    descriptor.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        activation.start(cfg, home=tmp_path)
    assert not (tmp_path / "capture" / "runtime").exists()


@pytest.mark.parametrize("enabled", [False, True])
def test_real_daemon_binds_before_dispatch_and_closes(tmp_path, monkeypatch, enabled):
    import threading
    from hermes_cli import kanban_db_dispatch as dispatch
    from hermes_cli import kanban_fairness_activation as activation
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg = config(tmp_path)
    if enabled:
        activation.arm(cfg, home=tmp_path, board="default", source_gateway="gw", board_uid="board")
    else:
        cfg = {}
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    stop = threading.Event()
    seen = []
    def tick(conn, **kw):
        seen.append(runtime.enabled(board="default"))
        read_owners(board="default", now_s=150, path=tmp_path / "missing")
        stop.set()
    monkeypatch.setattr(dispatch, "dispatch_once", tick)
    dispatch.run_daemon(stop_event=stop, interval=0)
    assert seen == [enabled]
    assert not runtime.enabled(board="default")
    if enabled:
        epochs = list((tmp_path / "capture" / "runtime").iterdir())
        assert len(epochs) == 1
        assert load(epochs[0])["records"][0]["payload"]["outcome"] == "missing"
    else:
        assert not (tmp_path / "capture").exists()


def test_disarm_after_config_reset_uses_existing_directory(tmp_path):
    from hermes_cli import kanban_fairness_activation as activation
    cfg = config(tmp_path)
    activation.arm(cfg, home=tmp_path, board="default", source_gateway="gw", board_uid="board")
    root = tmp_path / "capture"
    original = (root / activation.NAME).read_bytes()
    activation.disarm({}, home=tmp_path, directory=root)
    assert not (root / activation.NAME).exists()
    assert (root / activation.DISARMED).read_bytes() == original
    activation.disarm({}, home=tmp_path, directory=root)


def test_explicit_disarm_refuses_other_profile(tmp_path):
    from hermes_cli import kanban_fairness_activation as activation
    activation.arm(config(tmp_path), home=tmp_path, board="default", source_gateway="gw", board_uid="board")
    with pytest.raises(ValueError):
        activation.disarm({}, home=tmp_path / "other", directory=tmp_path / "capture")
    assert (tmp_path / "capture" / activation.NAME).exists()


def test_disarm_does_not_require_enabled_config(tmp_path):
    from hermes_cli import kanban_fairness_activation as activation
    cfg = config(tmp_path)
    activation.arm(cfg, home=tmp_path, board="default", source_gateway="gw", board_uid="board")
    cfg["kanban"]["fairness_capture"]["enabled"] = False
    activation.disarm(cfg, home=tmp_path)
    assert not (tmp_path / "capture" / activation.NAME).exists()


@pytest.mark.parametrize("change", ["missing", "modified", "false_disarm"])
def test_authority_loss_cannot_publish_clean_tail(tmp_path, monkeypatch, change):
    from hermes_cli import kanban_fairness_activation as activation
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg = config(tmp_path)
    activation.arm(cfg, home=tmp_path, board="default", source_gateway="gw", board_uid="board")
    session = activation.start(cfg, home=tmp_path)
    try:
        read_owners(board="default", now_s=150, path=tmp_path / "missing")
        descriptor = tmp_path / "capture" / activation.NAME
        if change == "modified":
            descriptor.write_bytes(b"{}")
        else:
            descriptor.unlink()
            if change == "false_disarm":
                (descriptor.parent / activation.DISARMED).write_bytes(b"{}")
        session.activation_monitor.join(4)
        session.thread.join(2)
        assert not session.activation_monitor.is_alive()
        assert session.abandoned.is_set()
        assert not (session.out / "runtime-close.json").exists()
        assert load(session.out)["gaps"]
    finally:
        session.close()


def test_revocation_during_startup_never_returns_active_epoch(tmp_path, monkeypatch):
    from hermes_cli import kanban_fairness_activation as activation
    cfg = config(tmp_path)
    activation.arm(cfg, home=tmp_path, board="default", source_gateway="gw", board_uid="board")
    original = runtime.activate
    sessions = []
    def revoked(*args, **kwargs):
        activation.disarm(cfg, home=tmp_path)
        result = original(*args, **kwargs)
        sessions.append(result)
        return result
    monkeypatch.setattr(runtime, "activate", revoked)
    try:
        assert activation.start(cfg, home=tmp_path) is None
        assert sessions[0].stopping.is_set()
    finally:
        for session in sessions:
            session.close()


def test_real_cli_disarm_after_reset(tmp_path, monkeypatch):
    import os
    from pathlib import Path
    import subprocess
    import sys
    from hermes_cli import kanban_fairness_activation as activation
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    activation.arm(config(tmp_path), home=tmp_path, board="default", source_gateway="gw", board_uid="board")
    repo = Path(__file__).resolve().parents[2]
    result = subprocess.run(
        [sys.executable, str(repo / "scripts" / "kanban_fairness.py"), "runtime", "disarm",
         "--directory", str(tmp_path / "capture")],
        cwd=repo, env=dict(os.environ), capture_output=True, text=True, timeout=30, check=False)
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["runtime"] == "disarm"
    assert (tmp_path / "capture" / activation.DISARMED).is_file()
    assert not (tmp_path / "capture" / activation.NAME).exists()
