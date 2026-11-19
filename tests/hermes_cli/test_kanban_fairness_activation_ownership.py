"""Real activation ownership across gateway/CLI dispatch ingress."""
import asyncio
from types import SimpleNamespace

import pytest

from hermes_cli import kanban_fairness_activation as activation
from hermes_cli import kanban_fairness_runtime as runtime
from hermes_cli.kanban_fairness_store import load
from hermes_cli.kanban_turn_urgency import read_owners


@pytest.fixture
def armed(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg = {"kanban": {"fairness_capture": {"enabled": True, "capture_id": "fixture",
                                           "directory": str(tmp_path / "capture")}}}
    monkeypatch.setattr("hermes_cli.config.load_config", lambda: cfg)
    activation.arm(cfg, home=tmp_path, board="default", source_gateway="gw", board_uid="board")
    session = activation.boot()
    assert session is not None
    try:
        yield session
    finally:
        session.close()
        session.activation_monitor.join(3)


def observe_after_nested_service(session, tmp_path):
    assert not session.stopping.is_set(), "borrowed service closed the gateway epoch"
    assert activation.start(_config(tmp_path), home=tmp_path) is session
    assert runtime.enabled(board="default")
    assert read_owners(board="default", now_s=150, path=tmp_path / "missing") == set()
    session.close()
    records = load(session.out)["records"]
    assert any(row["kind"] == "owner_generation" and row["payload"]["outcome"] == "missing"
               for row in records)


def _config(tmp_path):
    return {"kanban": {"fairness_capture": {"enabled": True, "capture_id": "fixture",
                                           "directory": str(tmp_path / "capture")}}}


def test_boot_then_nested_serving_preserves_outer_epoch(armed, tmp_path):
    with activation.serving():
        with activation.serving():
            assert runtime.enabled(board="default")
    observe_after_nested_service(armed, tmp_path)


def test_gateway_slash_dispatch_preserves_outer_epoch(armed, tmp_path, monkeypatch):
    from gateway.slash_commands import GatewaySlashCommandsMixin
    from hermes_cli import kanban_db_dispatch as dispatch

    seen = []

    def dispatch_work(conn, **kwargs):
        seen.append(runtime.enabled(board="default"))
        return dispatch.DispatchResult()

    monkeypatch.setattr(dispatch, "dispatch_once", dispatch_work)
    event = SimpleNamespace(text="/kanban dispatch --dry-run --json", source=None)
    output = asyncio.run(GatewaySlashCommandsMixin._handle_kanban_command(object(), event))
    assert seen == [True], output
    assert '"spawned"' in output, output
    observe_after_nested_service(armed, tmp_path)


def test_serving_owns_new_epoch_and_closes_it(tmp_path, monkeypatch):
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    cfg = _config(tmp_path)
    activation.arm(cfg, home=tmp_path, board="default", source_gateway="gw", board_uid="board")
    with activation.serving(cfg, home=tmp_path) as session:
        assert session is not None
        assert runtime.enabled(board="default")
        with activation.serving(cfg, home=tmp_path):
            pass
        assert runtime.enabled(board="default")
    assert session.stopping.is_set()
    session.activation_monitor.join(3)


def test_serving_retains_mocked_boot_signature(monkeypatch):
    closed = []
    session = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr(activation, "boot", lambda config=None, *, home=None: session)
    with activation.serving() as result:
        assert result is session
    assert closed == [True]
