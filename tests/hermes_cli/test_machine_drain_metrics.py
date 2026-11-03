"""Drain diagnostics preserve unknowns and old-Board load reporting."""
import json

import pytest

from agent import core_update_drain as drain
from hermes_cli import machine_drain_metrics as md
from hermes_cli import machine_metrics as mm
from hermes_cli.kanban_sync_client import HubError


@pytest.fixture
def hold(tmp_path, monkeypatch):
    path = tmp_path / "drain-hold"
    monkeypatch.setattr(drain, "hold_paths", lambda: [path])
    monkeypatch.setattr(drain, "_pid_alive", lambda pid: pid == 123)
    return path


def test_actual_producer_bound_and_dead_writer(hold):
    hold.write_text(json.dumps({"pid": 123, "started_at": 100, "max_s": 1200}))
    assert md.collect() == {"drain_active": 1, "drain_until_ms": 1300000}
    hold.write_text(json.dumps({"pid": 124, "started_at": 100, "max_s": 1200}))
    assert md.collect() == {"drain_active": 0}


@pytest.mark.parametrize("extra", [{}, {"max_s": None}, {"max_s": "1200"}, {"max_s": -1}])
def test_never_invents_end(hold, extra):
    hold.write_text(json.dumps({"pid": 123, "started_at": 100, **extra}))
    assert md.collect() == {"drain_active": 1}


def test_absent_and_unparseable(hold):
    assert md.collect() == {"drain_active": 0}
    hold.write_text("{")
    assert md.collect() == {"drain_active": 1}


def test_unreadable_is_unknown(hold, monkeypatch):
    monkeypatch.setattr(type(hold), "read_text", lambda *a, **k: (_ for _ in ()).throw(PermissionError()))
    assert md.collect() == {}


def test_stat_failure_is_unknown(hold, monkeypatch):
    hold.write_text(json.dumps({"pid": 123}))
    original = type(hold).stat
    def stat(path, *args, **kwargs):
        if path == hold:
            raise PermissionError("unreadable metadata")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(type(hold), "stat", stat)
    assert md.collect() == {}
    assert drain.read_hold(hold) is None  # scheduler compatibility


def test_reporter_falls_back_without_discarding_capacity():
    sent = []
    sample = {"cpu_load1": 1, "active_card_runs": 0, "drain_active": 1, "drain_until_ms": 1234}
    def send(value):
        sent.append(value)
        if "drain_active" in value:
            raise HubError(400, "unknown metric")
    assert mm.Reporter(send, collect_fn=lambda: sample).report_due()
    assert sent == [sample, {"cpu_load1": 1, "active_card_runs": 0}]


def test_no_retry_of_other_board_errors():
    sent = []
    def send(value):
        sent.append(value)
        raise HubError(500, "failure")
    assert not mm.Reporter(send, collect_fn=lambda: {"drain_active": 1}).report_due()
    assert len(sent) == 1
