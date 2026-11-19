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


def test_disarm_does_not_require_enabled_config(tmp_path):
    from hermes_cli import kanban_fairness_activation as activation
    cfg = config(tmp_path)
    activation.arm(cfg, home=tmp_path, board="default", source_gateway="gw", board_uid="board")
    cfg["kanban"]["fairness_capture"]["enabled"] = False
    activation.disarm(cfg, home=tmp_path)
    assert not (tmp_path / "capture" / activation.NAME).exists()
